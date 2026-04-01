# ne-body — Space Situational Awareness Platform

**ne-body** is a browser-based Continuous Monitoring & Prediction Platform for Space Situational Awareness (SSA). It replaces static long-horizon orbital predictions with a closed-loop **observe → propagate → validate → recalibrate** cycle, directly addressing the Lyapunov instability that limits traditional propagation accuracy over time.

The current deliverable is a funded proof-of-concept targeting DoD/Space Force and NASA audiences.

---

## How it works

```
Space-Track.org ──► ingest.py (fetch / validate / cache TLEs)
                         │
                         ▼
                   propagator.py  (SGP4 → ECI state vector)
                         │
                         ▼
                     kalman.py    (UKF predict / update)
                         │
                         ▼
                    anomaly.py    (NIS divergence detection)
                         │
                         ▼
                     main.py      (REST + WebSocket API)
                         │
                         ▼
              Browser  (CesiumJS globe · D3 residuals · alert panel)
```

TLE updates from Space-Track.org serve as both propagation seeds and synthetic ground-truth observations. The Kalman filter continuously closes residuals; when residuals exceed the predicted uncertainty envelope, an anomaly alert fires and the filter recalibrates.

---

## Tech stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11+, FastAPI, uvicorn |
| Propagation | sgp4 (Vallado SGP4/SDP4) |
| State estimation | filterpy (UKF preferred, EKF fallback) |
| Numerics | numpy, scipy |
| Data polling | httpx (async), sqlite3 (cache) |
| Frontend | Vanilla JS (ES2022), no build step |
| Visualization | CesiumJS (3D globe, CZML), D3.js (charts) |
| Data source | Space-Track.org TLE catalog |

---

## Project structure

```
ne-body/
├── backend/
│   ├── main.py          # FastAPI app — REST endpoints + WebSocket /ws/live
│   ├── ingest.py        # Sole interface to Space-Track.org
│   ├── propagator.py    # SGP4 propagation, TLE → ECI state vector
│   ├── kalman.py        # UKF state estimation per tracked object
│   ├── anomaly.py       # NIS-based anomaly detection and recalibration
│   └── requirements.txt
├── frontend/
│   ├── index.html       # Single-page app shell (CDN imports)
│   └── src/
│       ├── main.js      # App entry point, WebSocket client
│       ├── globe.js     # CesiumJS 3D orbital view
│       ├── residuals.js # D3 residual timeline + uncertainty envelope
│       └── alerts.js    # Anomaly alert panel
├── scripts/
│   ├── replay.py        # Historical TLE replay for offline demos
│   └── seed_maneuver.py # Synthetic maneuver injection
├── data/catalog/        # Cached TLE snapshots (gitignored except .gitkeep)
├── tests/               # pytest suite mirroring backend modules
└── docs/
    ├── requirements.md
    ├── architecture.md
    └── plans/           # Planner-produced implementation plans
```

---

## Quickstart

### Prerequisites

- Python 3.11+
- A free [Space-Track.org](https://www.space-track.org) account
- A [Cesium Ion](https://ion.cesium.com) token

### Environment variables

```bash
export SPACETRACK_USER=<your-email>
export SPACETRACK_PASS=<your-password>
export CESIUM_ION_TOKEN=<your-token>
```

### Start the backend

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### Serve the frontend

```bash
cd frontend
python -m http.server 3000
# Open http://localhost:3000
```

### Pre-load TLE cache for demo (offline use)

```bash
python scripts/replay.py --hours 72
```

### Inject a synthetic maneuver event

```bash
python scripts/seed_maneuver.py --object 25544 --delta-v 0.5
```

---

## Running tests

```bash
# Full suite
pytest tests/ -v

# Mathematically critical tests only
pytest tests/test_kalman.py tests/test_propagator.py -v

# Type check
mypy backend/ --ignore-missing-imports
```

---

## Demo script

1. **Normal tracking** — 3D globe with 5–10 objects, residual chart flat, confidence high
2. **Inject maneuver** — `scripts/seed_maneuver.py --object ISS-analog` — residual spikes
3. **Anomaly fires** — alert panel highlights object, uncertainty ellipsoid grows on globe
4. **Recalibration** — filter updates, residual returns to baseline within 2–3 cycles
5. **Contrast** — static SGP4-only prediction for the same event shows no detection

Step 5 is the funding moment — the static baseline must be rendered clearly alongside the live filter output.

---

## Development workflow

This project uses two Claude Code sub-agents with a strict handoff protocol:

**`@planner`** (claude-opus-4-6) — reads requirements and architecture docs, produces a written plan to `docs/plans/YYYY-MM-DD-<feature>.md`. Does not write implementation code.

**`@implementer`** (claude-sonnet-4-6) — receives an approved plan, executes it exactly, validates after every file edit, writes tests alongside implementation.

**Rule:** Planner → human review → Implementer. No implementation work starts without an approved plan document.

---

## Critical constraints

- **ITAR:** Space-Track.org data is publicly releasable but requires export control acknowledgment. No classified or restricted sources in this POC. All data provenance is logged.
- **Rate limits:** Never poll Space-Track more than once per 30 minutes. `ingest.py` is the only module permitted to call the API.
- **Coordinate frames:** All internal state vectors are ECI J2000. Conversions to ECEF or geodetic happen only at the API boundary.
- **Units:** SI throughout — `_km`, `_km_s`, `_s`, `_rad` suffixes on all unit-bearing variable names.
- **No real sensor data:** TLE updates are used as synthetic observations. The `ingest.py` → `kalman.py` boundary documents simulation fidelity for reviewers.
- **Demo stability:** System must run fully offline after initial TLE pull. Cache at least 72 hours before any presentation.

---

## Known issues (POC scope)

- UKF process noise matrix `Q` is hand-tuned — adaptive noise estimation is post-POC
- Space-Track HTTP 429 backoff not yet implemented — retry logic TODO
- CesiumJS Ion token is hardcoded in `globe.js` — must move to env var before any public deployment
- No authentication on the WebSocket endpoint — acceptable for local demo only

---

## Glossary

| Term | Definition |
|------|-----------|
| TLE | Two-Line Element set — compact format encoding orbital state |
| SGP4 | Simplified General Perturbations 4 — standard low-Earth propagator |
| ECI | Earth-Centered Inertial frame — inertial reference frame for orbital mechanics |
| UKF | Unscented Kalman Filter — nonlinear state estimator used for orbit determination |
| Residual | Difference between predicted and observed state |
| NIS | Normalized Innovation Squared — scalar filter consistency metric |
| Divergence | Residuals exceeding predicted uncertainty — triggers anomaly alert |
| Recalibration | Resetting or updating filter state after divergence |
| Conjunction | Close approach event between two space objects |
| Maneuver | Deliberate orbit change by an active satellite |

---

## References

- [Space-Track.org API docs](https://www.space-track.org/documentation)
- [sgp4 Python library](https://pypi.org/project/sgp4/)
- [FilterPy docs](https://filterpy.readthedocs.io/)
- [CesiumJS CZML guide](https://github.com/CesiumGS/czml-writer/wiki/CZML-Guide)
- [Vallado: Fundamentals of Astrodynamics and Applications](https://celestrak.org/software/vallado-sw.php)
