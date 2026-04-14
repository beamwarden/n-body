# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# ne-body (Near Earth body) — Space Situational Awareness Platform

**ne-body** (Near Earth body) is a browser-based Continuous Monitoring & Prediction Platform for Space Situational Awareness (SSA). The core concept is a closed-loop system that replaces static long-horizon orbital predictions with a continuous observe → propagate → validate → recalibrate cycle, circumventing the mathematical limitations (Lyapunov instability) of traditional propagation.

The current deliverable is a **proof-of-concept** targeting DoD/Space Force and NASA audiences. It uses public TLE data from Space-Track.org as both propagation input and ground-truth observation, runs a Kalman filter loop to close residuals, and visualizes divergence, anomaly detection, and recalibration in the browser.

---

## Agent roles

This project uses two specialized sub-agents. Always clarify which agent should handle a task before beginning.

### Planner agent (`@planner`)
- Reads requirements and architecture docs before proposing anything
- Produces a written implementation plan saved to `docs/plans/YYYY-MM-DD-<feature>.md`
- Plans include: affected files, data flow changes, risk level, test strategy
- Does NOT write implementation code — outputs plans only
- Uses extended thinking / plan mode for complex design decisions
- Flags any requirements conflicts or ambiguities before the implementer begins

### Implementer agent (`@implementer`)
- Receives a written plan from `@planner` — never starts without one
- Follows the plan exactly; notes deviations as comments and flags them
- Does not refactor surrounding code unless the plan explicitly calls for it
- After each file edit, runs the appropriate validator (see Validation commands)
- Writes or updates tests alongside implementation

**Workflow rule:** Planner → human review → Implementer. No implementer work without an approved plan.

---

## Tech stack

### Backend (Python 3.11+)
- `fastapi` + `uvicorn` — API server and WebSocket endpoint
- `sgp4` — SGP4/SDP4 propagation (Vallado implementation)
- `filterpy` — Kalman filter (UKF preferred; EKF fallback)
- `numpy`, `scipy` — numerical computation
- `httpx` — async Space-Track.org polling
- `sqlite3` (stdlib) — lightweight state storage for POC

### Frontend (vanilla JS + CDN)
- **CesiumJS** — 3D orbital visualization, CZML satellite trajectories
- **D3.js** — residual timeline, uncertainty envelope charts
- No build step for POC — CDN imports, single-page HTML

### Data source
- **Space-Track.org** — free TLE catalog (requires free account, ITAR-aware)
- Polling interval: every 30 minutes (per Space-Track rate limits)
- Up to 100 curated objects for POC (20-50 originally, expanded for demo richness — Starlink batch, ISS, debris clouds, rocket bodies, CubeSats)

---

## Running the system

**Required environment variables:**
```
SPACETRACK_USER=<email>
SPACETRACK_PASS=<password>
CESIUM_ION_TOKEN=<token>
```

**Startup sequence:**
```bash
# 1. Start backend
cd backend
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# 2. Serve frontend (any static server)
cd frontend
python -m http.server 3000

# 3. For demo: pre-load TLE cache (72-hour window)
python scripts/replay.py --hours 72

# 4. For demo: inject a maneuver event
python scripts/seed_maneuver.py --object 25544 --delta-v 5
```

---

## Validation commands

Run these after every backend file change:

```bash
# Syntax check
python -m py_compile backend/<file>.py

# Type check
mypy backend/ --ignore-missing-imports

# Unit tests
pytest tests/ -v

# Run only the mathematically critical tests
pytest tests/test_kalman.py tests/test_propagator.py -v

# Integration: start backend and verify WebSocket connects
uvicorn backend.main:app --reload &
python scripts/test_ws_connect.py
```

Run these after every frontend file change:

```bash
# Lint (if eslint configured)
npx eslint frontend/src/

# Manual: open index.html in browser and check browser console for errors
```

---

## Critical constraints

- **ITAR awareness:** Space-Track.org data is publicly releasable but the account requires acknowledgment of export control terms. Do not integrate any classified or restricted data sources in this POC. Log all data provenance.
- **Rate limits:** Space-Track.org limits requests. Never poll more than once per 30 minutes. Cache TLE responses locally. The `ingest.py` module owns all Space-Track calls — no other module should call the API directly.
- **No real sensor data:** The POC uses TLE updates as synthetic observations. The `ingest.py` → `kalman.py` boundary must be clearly documented so reviewers understand the simulation fidelity level.
- **Demo stability:** The demo must run offline after initial TLE pull. Always cache at least 72 hours of TLE snapshots before a presentation.
- **Coordinate frames:** All internal state vectors use ECI (Earth-Centered Inertial) J2000. Conversions to ECEF or geodetic happen only at the API boundary. Never mix frames silently.
- **Units:** SI throughout. Kilometers and km/s for orbital state. Seconds for time. UTC for all timestamps. No ambiguous unit variables — suffix names: `_km`, `_s`, `_rad`.

---

## Code conventions

- Python: PEP 8, type hints required on all function signatures, docstrings on all public functions
- JavaScript: ES2022 modules, `const`/`let` only, no `var`
- Commit messages: `type(scope): description` — e.g. `feat(kalman): add UKF state initialization`
- Branch naming: `feature/<short-name>`, `fix/<short-name>`, `demo/<short-name>`
- No commented-out code committed. Use git stash or branches.

---

## Domain glossary

| Term | Definition |
|------|------------|
| TLE | Two-Line Element set — compact format encoding orbital state |
| SGP4 | Simplified General Perturbations 4 — standard low-Earth propagator |
| ECI | Earth-Centered Inertial frame — inertial reference frame for orbital mechanics |
| UKF | Unscented Kalman Filter — nonlinear state estimator used for orbit determination |
| Residual | Difference between predicted and observed state |
| NIS | Normalized Innovation Squared — scalar metric for filter consistency |
| Conjunction | Close approach event between two space objects |
| Maneuver | Deliberate orbit change by an active satellite |
| Divergence | When residuals grow beyond the filter's predicted uncertainty — triggers anomaly alert |
| Recalibration | Resetting or updating filter state when divergence is detected |

---

## Demo script (for presentations)

1. **Normal tracking** — show 3D view with 5–10 objects, residual chart flat, confidence high
2. **Inject maneuver** — run `scripts/seed_maneuver.py --object ISS-analog` — residual spikes
3. **Anomaly fires** — alert panel highlights object, uncertainty ellipsoid grows on globe
4. **Recalibration** — filter updates, residual returns to baseline within 2–3 observation cycles
5. **Contrast** — show what a static SGP4-only prediction looks like for the same event (no detection)

The contrast in step 5 is the funding moment. Make sure the static baseline is rendered clearly.

---

## Known issues / tech debt (POC scope)

- UKF process noise matrix `Q` is hand-tuned; adaptive noise estimation is post-POC
- Space-Track polling does not yet handle HTTP 429 backoff gracefully (retry logic TODO)
- CesiumJS Ion token is hardcoded in `globe.js` — must be moved to env var before any public deployment
- No authentication on the WebSocket endpoint — acceptable for local demo, not for production

---

## Useful references

- [Space-Track.org API docs](https://www.space-track.org/documentation)
- [sgp4 Python library](https://pypi.org/project/sgp4/)
- [FilterPy docs](https://filterpy.readthedocs.io/)
- [CesiumJS CZML guide](https://github.com/CesiumGS/czml-writer/wiki/CZML-Guide)
- [Vallado: Fundamentals of Astrodynamics and Applications](https://celestrak.org/software/vallado-sw.php)
