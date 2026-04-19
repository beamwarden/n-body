# ne-body (Near Earth body) — Space Situational Awareness Platform

**ne-body** is a browser-based continuous monitoring platform for Space Situational Awareness (SSA). It replaces static long-horizon orbital predictions with a closed-loop **observe → propagate → validate → recalibrate** cycle, directly addressing the Lyapunov instability that limits traditional propagation accuracy.

The current deliverable is a proof-of-concept (TRL 3–4) targeting DoD/Space Force and NASA audiences. It tracks **75 curated Very Low Earth Orbit (VLEO) objects** using publicly available TLE data as synthetic observations, runs a per-object Unscented Kalman Filter to close residuals, and surfaces anomaly detections to a real-time operator dashboard within a single observation cycle (~30 minutes).

---

## How it works

```
Space-Track.org ──► ingest.py ──► tle_catalog (SQLite)
   N2YO.com ───►        │
                         ▼
                   processing.py
                    ┌────┴────┐
                    ▼         ▼
             propagator.py  kalman.py  ──►  anomaly.py
             (SGP4+TEME→ECI) (UKF)         (NIS detector)
                         │
                         ▼
                      main.py  (FastAPI REST + WebSocket)
                         │
                         ▼
              Browser dashboard
        CesiumJS globe · D3 residuals · alert panel
```

TLE updates from Space-Track.org (primary) and N2YO.com (supplemental fallback) serve as both propagation seeds and synthetic observations. The UKF continuously closes residuals; when the Normalized Innovation Squared (NIS) exceeds the chi-squared threshold, an anomaly fires, the filter recalibrates, and conjunction screening runs immediately against the full catalog.

---

## Features

- **Real-time 3D globe** — CesiumJS satellite positions, animated orbital tracks, uncertainty ellipsoids, and live anomaly highlighting
- **Per-object UKF** — continuous recursive state estimation with SGP4 process model and TEME→GCRS frame rotation via astropy
- **Anomaly detection** — NIS chi-squared test (6 DOF, p=0.05) classifies events as maneuver, drag anomaly, or filter divergence within one TLE update cycle
- **Anomaly-triggered conjunction screening** — immediate re-screening against the full 75-object catalog on every anomaly detection
- **Obtrusive alerting** — fullscreen visual flash + three-beep audio tone on every live anomaly message
- **Collapsible alert panel** — persistent anomaly history with status tracking (active / recalibrating / resolved / dismissed)
- **Event History page** — sortable, filterable table of all historical events with date-range search and pagination
- **Dual TLE sources** — Space-Track.org primary, N2YO.com supplemental fallback with per-row provenance tagging
- **Warm startup** — all filter states initialized from the latest cached TLE on server boot; no manual processing step needed after restart

---

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11+, FastAPI, uvicorn |
| Propagation | sgp4 (Vallado SGP4/SDP4), astropy (TEME→GCRS) |
| State estimation | filterpy (UKF) |
| Numerics | numpy, scipy |
| Data polling | httpx (async), sqlite3 WAL |
| Frontend | Vanilla JS ES2022, no build step |
| Visualization | CesiumJS 1.114 (3D globe), D3.js v7 (charts) |
| Audio alerting | Web Audio API |
| Data sources | Space-Track.org, N2YO.com |

---

## Project structure

```
ne-body/
├── backend/
│   ├── main.py           # FastAPI app — REST + WebSocket /ws/live, warm startup
│   ├── ingest.py         # Sole interface to Space-Track.org and N2YO.com
│   ├── propagator.py     # SGP4 propagation, TEME→GCRS, TLE → ECI state vector
│   ├── kalman.py         # Per-object UKF (init, predict, update, NIS)
│   ├── anomaly.py        # NIS anomaly classification and filter recalibration
│   ├── processing.py     # Orchestrates propagate → filter → classify per object
│   ├── conjunction.py    # Conjunction screening (spherical miss-distance)
│   └── requirements.txt
├── frontend/
│   ├── index.html        # Live dashboard (CDN imports, cache-busted)
│   ├── history.html      # Event history page
│   ├── favicon.svg       # Error ellipsoid icon
│   └── src/
│       ├── main.js       # App entry point, WebSocket client, catalog seeding
│       ├── globe.js      # CesiumJS 3D view, uncertainty ellipsoids, tracks
│       ├── alerts.js     # Collapsible alert panel
│       ├── alertflash.js # Fullscreen visual anomaly flash
│       ├── alertsound.js # Web Audio three-beep tone
│       ├── residuals.js  # D3 NIS timeline and residual charts
│       └── history.js    # Event history table, filters, pagination
├── data/
│   └── catalog/
│       ├── catalog.json  # 75-object VLEO tracked catalog
│       └── altitude_verification_report.txt
├── scripts/
│   ├── replay.py               # Historical TLE replay for offline demos
│   ├── seed_maneuver.py        # Synthetic maneuver injection
│   ├── seed_conjunction.py     # Synthetic conjunction injection
│   ├── demo.py                 # End-to-end demo sequence
│   └── verify_catalog_ids.py   # NORAD ID / name validation against Space-Track
├── tests/                      # pytest suite — 94 unit, 128 integration
├── docs/
│   ├── reference/
│   │   ├── whitepaper.md             # DARPA BAA format technical whitepaper
│   │   ├── system-architecture.md    # Authoritative architecture (v0.2.0)
│   │   ├── algorithmic-foundation.md # UKF, NIS, classifier math
│   │   ├── conops.md                 # Operational concept and demo scenario
│   │   └── api-spec.md               # REST and WebSocket schemas
│   └── plans/                        # Planner-produced implementation plans
├── .github/workflows/
│   ├── ci-develop.yml   # Unit tests gate for PRs to develop
│   └── ci-main.yml      # Unit + build + integration gate for PRs to main
├── Makefile
└── pytest.ini
```

---

## Quickstart

### Prerequisites

- Python 3.11+
- A free [Space-Track.org](https://www.space-track.org) account
- A [Cesium Ion](https://ion.cesium.com) token

### Environment setup

Copy `.env.example` and fill in your credentials:

```bash
cp .env.example .env
# edit .env with your values
```

```
SPACETRACK_USER=<your-email>
SPACETRACK_PASS=<your-password>
CESIUM_ION_TOKEN=<your-token>
N2YO_API_KEY=<optional — enables N2YO supplemental TLE fallback>
```

### Start the backend

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8001 --reload
```

### Serve the frontend

```bash
cd frontend
python -m http.server 8080
# Open http://localhost:8080
```

Or start both with one command:

```bash
make dev
```

### Pre-load TLE cache (required before first use or demo)

```bash
make replay          # pulls 72 hours of TLEs — run once before presenting
```

After this, the backend warm-startup will initialize all filter states automatically on the next restart. No manual `make process` needed.

---

## Make targets

```bash
make dev             # start backend (8001) + frontend (8080) in parallel
make backend         # backend only
make frontend        # frontend only
make replay          # pull 72-hour TLE cache from Space-Track
make ingest          # trigger one ingest cycle on running backend
make process         # trigger one processing cycle on running backend
make reload-catalog  # hot-reload catalog.json without restart
make verify          # check catalog object altitudes against TLE cache
make verify-ids      # validate catalog NORAD IDs against Space-Track satcat
make test            # run full test suite
make demo            # run end-to-end demo sequence
make kill-backend    # kill anything holding port 8001
```

---

## Running tests

```bash
# Full suite (222 tests)
pytest tests/ -v

# Unit tests only — no external dependencies (94 tests)
pytest -m unit -v

# Integration tests only — requires backend modules (128 tests)
pytest -m integration -v

# Type check
mypy backend/ --ignore-missing-imports

# Syntax check
python -m py_compile backend/*.py
```

---

## CI / branch protection

| Branch gate | Triggers on | Jobs |
|---|---|---|
| `ci-develop.yml` | PR → `develop` | Unit tests |
| `ci-main.yml` | PR → `main` | Unit tests → Build (py_compile + mypy) → Integration tests |

Integration tests use `SPACETRACK_USER`, `SPACETRACK_PASS`, and `CESIUM_ION_TOKEN` from repository secrets.

---

## Demo script

1. **Normal tracking** — globe shows 75 VLEO objects with animated orbital motion, alert panel empty, residual charts flat
2. **Inject maneuver** — `python scripts/seed_maneuver.py --object 25544 --delta-v 0.5` — NIS spikes on ISS
3. **Anomaly fires** — audio alarm + fullscreen red flash, alert card appears, object turns red on globe, uncertainty ellipsoid expands
4. **Conjunction screening** — alert card enriches with conjunction risk data from the immediate re-screen
5. **Recalibration** — filter reinitializes, residual returns to baseline within 2–3 observation cycles
6. **Contrast** — static SGP4-only prediction for the same event shows no detection, no alert

Step 6 is the funding moment — the static baseline must be rendered clearly alongside the live filter output.

---

## Tracked catalog

75 VLEO objects at or below 600 km altitude, including:

- **Crewed platforms** — ISS (NORAD 25544), CSS/Tianhe (NORAD 48274)
- **Starlink subset** — dense station-keeping constellation at VLEO altitudes
- **Commercial imaging** — BlackSky, Capella, ICEYE, UMBRA
- **RF geolocation** — HawkEye 360 formations
- **Fragmentation monitoring** — Cosmos 1408 debris, STARLINK-34343 (fragmented 2026-03-29)
- **Rocket bodies** — CZ-5B, Falcon 9 upper stages

See [`data/catalog/catalog.json`](data/catalog/catalog.json) and [`data/catalog/altitude_verification_report.txt`](data/catalog/altitude_verification_report.txt) for the full list.

---

## Critical constraints

- **ITAR** — Space-Track.org data is publicly releasable but requires export control acknowledgment at registration. No classified or CUI sources in this POC. All data provenance is logged.
- **Rate limits** — Space-Track limits to one poll per 30 minutes. `ingest.py` is the only module permitted to call the API.
- **Coordinate frames** — all internal state vectors are ECI J2000 (km, km/s). Conversions to ECEF or geodetic occur only at the API/rendering boundary.
- **Units** — SI throughout. Variable names carry unit suffixes: `_km`, `_km_s`, `_s`, `_rad`.
- **Simulation boundary** — TLE updates are used as synthetic observations, not raw sensor measurements. The `ingest.py → kalman.py` boundary is documented so reviewers understand the simulation fidelity level.
- **Demo stability** — the system must run fully offline after the initial TLE pull. Run `make replay` before any presentation.

---

## Known limitations (POC scope)

- UKF sigma-point collapse under SGP4 — all 13 sigma points propagate identically because SGP4 is a deterministic analytical model, not an ODE integrator. Production fix: numerical integrator (RK45 + force models).
- Process noise `Q` is hand-tuned per object class — adaptive estimation is post-POC.
- R matrix calibrated against ISS TLE error only — not validated for debris or rocket bodies.
- Conjunction screening uses spherical miss-distance thresholds, not the operational RSW pizza-box.
- No authentication on REST or WebSocket endpoints — acceptable for local demo, not for networked deployment.
- Filter state is in-memory only — restarting the backend causes a 2–4 cycle cold-start convergence period (warm startup initializes filter state; full covariance is re-derived live).

See [`docs/reference/whitepaper.md`](docs/reference/whitepaper.md) for the full limitations and production roadmap.

---

## Documentation

| Document | Description |
|---|---|
| [`docs/reference/whitepaper.md`](docs/reference/whitepaper.md) | DARPA BAA format technical whitepaper with reviewer Q&A |
| [`docs/reference/system-architecture.md`](docs/reference/system-architecture.md) | Authoritative architecture reference (v0.2.0) |
| [`docs/reference/algorithmic-foundation.md`](docs/reference/algorithmic-foundation.md) | UKF, NIS, and anomaly classifier math |
| [`docs/reference/conops.md`](docs/reference/conops.md) | Operational concept and demo scenario |
| [`docs/reference/api-spec.md`](docs/reference/api-spec.md) | REST and WebSocket message schemas |

---

## Glossary

| Term | Definition |
|---|---|
| TLE | Two-Line Element set — compact format encoding orbital state |
| SGP4 | Simplified General Perturbations 4 — standard low-Earth propagator |
| ECI | Earth-Centered Inertial frame — inertial reference frame for orbital mechanics |
| TEME | True Equator Mean Equinox — SGP4 native output frame, rotated to ECI via astropy |
| UKF | Unscented Kalman Filter — nonlinear state estimator used for orbit determination |
| NIS | Normalized Innovation Squared — scalar filter consistency metric |
| Residual | Difference between predicted and observed state |
| Divergence | Residuals exceeding predicted uncertainty — triggers anomaly alert |
| Recalibration | Reinitializing filter state after divergence detection |
| Conjunction | Close approach event between two space objects |
| Maneuver | Deliberate orbit change by an active satellite |
| VLEO | Very Low Earth Orbit — altitudes at or below ~600 km |

---

## References

- [Space-Track.org API docs](https://www.space-track.org/documentation)
- [sgp4 Python library](https://pypi.org/project/sgp4/)
- [FilterPy docs](https://filterpy.readthedocs.io/)
- [CesiumJS CZML guide](https://github.com/CesiumGS/czml-writer/wiki/CZML-Guide)
- [Vallado: Fundamentals of Astrodynamics and Applications](https://celestrak.org/software/vallado-sw.php)
- [Kessler & Cour-Palais (1978) — Collision frequency of artificial satellites](https://doi.org/10.1029/JA083iA06p02637)
