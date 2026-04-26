# Production Hardening — COA1 (Adopted)
**Date:** 2026-04-25  
**Status:** COA2 rejected. COA1 adopted. Proceeding to implementation.

---

## Strategic Context

This plan must be read against the NASA SBIR Phase I submission (EXPAND.3.S26B, dated 2026-04-24). ne-body is cited as a **TRL-3 demonstrated asset** — the algorithmic foundation for the Beamrider autonomous health management architecture. The proposal makes the following specific claims:

- NIS/UKF detects unknown faults (ISS reboost, Starlink CA maneuvers) without pre-specified fault models
- Phase I will generalize ne-body's orbital filter architecture to six spacecraft subsystem classes (ADCS, EPS, TCS, COMMS, OBC, Radiation)
- ne-body is the technical basis for KPP-02 (Pd ≥ 0.75 on unknown fault classes) and KPP-05 (TTD ≤ 120 s on NIS-only detectable faults)

**Hardening consequence:** Any reviewer who runs ne-body and observes stale "active" alerts, background KF updates that silently fail, or HTTP 429 crashes will directly undermine the TRL-3 claim. Production hardening is not housekeeping — it is proposal support. The priority order below reflects this.

**Architecture consequence:** ne-body will eventually generalize from orbital state estimation to multi-subsystem health monitoring. The filter abstractions (`kalman.py`, `processing.py`) and the anomaly/recalibration pipeline must remain clean and extensible. No shortcuts that couple the orbital domain into the filter core.

---

## Background

Two issues surfaced during the keep-0001 deployment that expose a deeper question about ne-body's architecture going forward:

1. **Alert orphaning** — `_anomaly_row_id` and `_pending_anomaly_*` keys live only in the in-memory `filter_states` dict. Server restarts drop them, leaving `active` alerts in the DB that can never self-resolve. The 03/31 and 04/09 STARLINK-1990 divergences are orphaned this way.

2. **SQLite thread-safety** — `sqlite3.connect()` defaults to `check_same_thread=True`. The `_processing_loop_task` passes the main-thread connection into `asyncio.to_thread`, which fails at runtime. Background KF updates never execute on keep-0001.

Both fixes are small and independent. However, the right production path depends on the architectural decision below.

---

## Production hardening debt (priority order)

| # | Issue | File | Priority | SBIR Impact |
|---|-------|------|----------|-------------|
| H-2 | SQLite thread-safety — background KF updates silently failing | `ingest.py:142` | **Immediate** | Direct: continuous monitoring claim is false without it |
| H-1 | Alert orphaning on restart — stale "active" rows | `processing.py`, `anomaly.py` | **Immediate** | Direct: NIS recalibration claim looks broken in any demo |
| H-3 | Space-Track HTTP 429 no backoff — ingest crashes on rate limit | `ingest.py` | Sprint 2 | Indirect: unreliable data source undermines detection demo |
| H-4 | No auth on WebSocket / REST endpoints | `main.py` | Sprint 2 | Indirect: required before any external review access |
| H-5 | CORS `allow_origins=["*"]` | `main.py` | Sprint 3 | Low |
| H-6 | UKF process noise Q hand-tuned | `kalman.py` | Post-Phase I | Adaptive Q is a Phase I deliverable candidate |

---

## COA1 — Harden ne-body as a standalone service

### Summary
Keep ne-body as an independent FastAPI service. Harden it in-place. Beamwarden continues to consume it via the existing `src/nebody/` proxy.

### Fix sequence

**Phase 1 — Immediate (unblock keep-0001):**

- **H-2: SQLite thread-safety**
  - `ingest.py:142`: add `check_same_thread=False` to `sqlite3.connect()`
  - One-line fix, zero risk

- **H-1: Alert persistence across restarts**
  - Add `current_anomaly_row_id INTEGER` column to a new `filter_state` table keyed by `norad_id`
  - Write `_anomaly_row_id` to this table whenever `record_anomaly()` fires
  - Clear the row on `record_recalibration_complete()`
  - On warm startup (lifespan), read `filter_state` table back into in-memory `filter_states` dict
  - Affected files: `ingest.py` (schema), `anomaly.py` (write/clear), `main.py` (warm startup read)
  - **Also needed:** one-time migration to resolve existing orphaned rows — query alerts WHERE status='active' AND detection_epoch_utc < (now - 24h) and mark them resolved or dismissed

**Phase 2 — Hardening:**

- **H-3: Space-Track 429 backoff**
  - `ingest.py`: catch 429 in `fetch_tles()`, parse `Retry-After` header, sleep with exponential fallback
  - Cap at 4 retries, then emit warning and skip cycle

- **H-4: WebSocket / REST auth**
  - Add an `API_KEY` env var; require it as a Bearer token or `?key=` param on all non-`/config` endpoints
  - WebSocket: check on upgrade, close 1008 (policy violation) if missing
  - Acceptable for demo; full RBAC is post-POC

**Phase 3 — Structural (pre-production):**

- Migrate SQLite → PostgreSQL (removes thread-safety class of bugs, enables multi-worker uvicorn)
- Containerize ne-body (add `Dockerfile`, `docker-compose.yml` alongside beamwarden)
- Move from `python -m http.server` to nginx for the frontend

### COA1 trade-offs

| Pro | Con |
|-----|-----|
| Minimal migration risk | Two separate repos to maintain |
| ne-body domain stays clean (orbital mechanics ≠ fleet management) | Deploy coordination between ne-body and beamwarden versions |
| asyncio event loop and heavy numpy/sgp4/astropy fit FastAPI better than Django | Separate auth systems |
| Existing beamwarden proxy already expresses the right boundary | |
| Phase 1 fixes are a single afternoon | |

---

## COA2 — Fold ne-body into beamwarden

### Summary
Migrate ne-body's SSA engine into beamwarden as a Django app. Retire the standalone service.

### What already exists
Beamwarden already contains `src/nebody/` — a read-only reverse proxy that forwards five endpoints to the standalone ne-body API. This is **not** the SSA engine; it is a thin HTTP client. COA2 means replacing the standalone engine with code that lives inside Django.

### What this would require

**Step 1 — Port the data layer**
- Translate ne-body's SQLite schema (tle_cache, filter_state, state_history, alerts, conjunction_events, conjunction_risks) into Django models with migrations
- PostgreSQL replaces SQLite (net positive)

**Step 2 — Port the compute engine**
- `propagator.py`, `kalman.py`, `anomaly.py`, `conjunction.py`, `processing.py` become a `ssa/` Django app (or remain standalone Python modules imported by it)
- Heavy dependencies (sgp4, filterpy, numpy, scipy, astropy) join beamwarden's `pyproject.toml`

**Step 3 — Replace the async loops**
- `ingest.py`'s `run_ingest_loop` becomes a Celery beat task (beamwarden would need Celery + Redis/broker added to `docker-compose`)
- `processing.py`'s processing loop becomes another Celery task triggered by ingest completion
- OR: run as a Django management command on a cron

**Step 4 — Port the WebSocket**
- `/ws/live` becomes a Django Channels consumer (beamwarden would need `channels` + `daphne` or Channels layers backed by Redis)

**Step 5 — Port the frontend**
- CesiumJS globe, residual charts, alert panel become Django templates
- Or: serve the static frontend separately and point it at beamwarden's API

**Step 6 — Replace the existing proxy**
- `src/nebody/client.py` and `views.py` are deleted; views call Django ORM directly

### COA2 trade-offs

| Pro | Con |
|-----|-----|
| One codebase, one database, one deploy unit | Massive migration effort (6–8 weeks conservatively) |
| Shared auth and RBAC from beamwarden | Beamwarden is Django (sync-first); ne-body is asyncio-first — requires Celery + Channels |
| Single Docker Compose stack on keep-0001 | Adds sgp4, filterpy, astropy (~500 MB of heavy science deps) to beamwarden's image |
| PostgreSQL-native from day one | Beamwarden is fleet management; SSA is an orthogonal domain — conceptual pollution |
| | beamwarden's `src/nebody/` proxy already encodes the right separation of concerns — COA2 discards a design decision already made |
| | Demo stability risk: any beamwarden change can break the SSA pipeline |

### Why COA2 is probably wrong
The existing proxy module is the tell: it draws an explicit service boundary between "what beamwarden knows about" (fleet, devices, telemetry) and "what ne-body does" (orbital mechanics, Kalman filtering, SSA alerts). That boundary is correct. The orbital engine's runtime shape — event loop, blocking numpy calls offloaded to threads, 30-minute poll cycle, WebSocket broadcast — maps cleanly to FastAPI but fights Django and Channels. The only tangible gain of COA2 is unified auth and a single docker-compose; both are achievable in COA1 at far lower cost.

---

## Decision

**COA1 adopted. COA2 rejected.**

The SBIR proposal clarifies the architecture permanently: ne-body is the algorithmic engine (TRL 3, orbital domain); Beamwarden is the ground segment control plane (TRL 4–5, fleet domain). They are co-equal, complementary assets cited together as the technical foundation for Phase I. Folding one into the other would dissolve a boundary the proposal explicitly markets.

ne-body's path is: orbital state estimation (now) → generalized multi-subsystem health monitoring (Phase I–II). That generalization must happen inside ne-body's architecture, not inside Django. Beamwarden's path is: terrestrial fleet management (now) → spacecraft ground segment extensions (Phase II). The integration point remains the proxy API boundary already built in `src/nebody/`.

---

## Immediate next steps (COA1 Phase 1)

1. `ingest.py:142` — add `check_same_thread=False` (H-2)
2. Add `filter_state` persistence table schema to `ingest.py` `init_catalog_db()`
3. Update `anomaly.py`: `record_anomaly()` writes `norad_id → row_id`; `record_recalibration_complete()` clears it
4. Update `main.py` warm startup: read `filter_state` table into `filter_states` dict
5. Write and run one-time script to resolve orphaned alerts older than 24 h
6. Sync fix to keep-0001 and restart service

Affected files: `backend/ingest.py`, `backend/anomaly.py`, `backend/main.py`  
Estimated effort: 4–6 hours  
Risk: Low — additive DB column + warm startup read, no changes to Kalman or propagator paths
