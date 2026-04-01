# System Architecture Reference: n-body SSA Platform
Version: 0.1.0
Status: Draft
Last updated: 2026-04-01

---

## 1. Overview

The n-body platform is a browser-based, closed-loop Space Situational Awareness (SSA) system that replaces static long-horizon orbital prediction with a continuous observe-propagate-validate-recalibrate cycle. The architectural problem it solves is the absence of a feedback loop in conventional SSA tooling: when a new TLE arrives that is inconsistent with the prior propagated trajectory, standard tools provide no automated mechanism to detect the inconsistency, classify its cause, or update the state estimate without analyst intervention. n-body closes this loop by maintaining a per-object Unscented Kalman Filter (UKF) whose state is updated on every incoming TLE, whose NIS statistic is tested against a chi-squared threshold on every update, and whose anomaly output drives immediate recalibration and conjunction screening — all within the same processing cycle that received the new data. The mathematical basis for the UKF, the NIS test, and the anomaly classifier is documented in `docs/reference/algorithmic-foundation.md`; this document describes the software architecture that realizes that algorithm at system scale.

---

## 2. System Context Diagram

```
  Space-Track.org
  (TLE catalog API)
        |
        | HTTPS, authenticated session
        | every 30 minutes (POLL_INTERVAL_S = 1800)
        |
        v
  +-----------+
  |  ingest.py |  validate checksum, INSERT OR IGNORE into tle_catalog
  +-----------+
        |
        | catalog_update event (asyncio.Queue, maxsize=10)
        |
        v
  +----------------+
  | processing.py  |  predict -> update -> anomaly -> recalibrate (per object)
  | (processing    |  sequential iteration over catalog entries
  |  loop task)    |  ~30 s per full catalog pass (100 objects, SGP4 + astropy)
  +----------------+
        |           \
        |            \-- conjunction.py (fire-and-forget, run_in_executor)
        |                on every anomaly detection
        v
  +------------+
  |  SQLite DB |  tables: tle_catalog, state_history, alerts,
  |  (WAL mode)|          conjunction_events, conjunction_risks
  +------------+
        |
        | app.state.filter_states (in-memory dict, keyed by NORAD ID)
        |
        v
  +------------+
  |  main.py   |  FastAPI application, REST + WebSocket gateway
  | (API server)|
  +------------+
        |
        | WebSocket /ws/live   (up to MAX_WS_CONNECTIONS = 20)
        | JSON text frames
        |
        v
  +-------------------+
  |  Browser          |
  |  main.js          |  WebSocket client, message router
  |  globe.js         |  CesiumJS 3D orbital view
  |  residuals.js     |  D3.js NIS / innovation timeline
  |  alerts.js        |  alert panel, conjunction risk enrichment
  +-------------------+

  REST endpoints (HTTP, same FastAPI server):
    GET /config                       -> Cesium Ion token
    GET /catalog                      -> current state of all objects
    GET /object/{id}/history          -> anomaly time series
    GET /object/{id}/anomalies        -> detailed anomaly records
    GET /object/{id}/conjunctions     -> recent conjunction screenings
    GET /object/{id}/track            -> back- and forward-propagated track
    GET /alerts/active                -> unresolved alerts (reconnect seed)
    POST /admin/trigger-process       -> force one processing cycle
```

---

## 3. Component Inventory

| Module | File | Responsibility | Primary Inputs | Primary Outputs |
|--------|------|----------------|----------------|-----------------|
| Ingest | `backend/ingest.py` | Sole interface to Space-Track.org. Authenticates, fetches TLEs, validates checksums, caches to SQLite, emits events. | SPACETRACK_USER / SPACETRACK_PASS env vars; catalog.json NORAD IDs | Rows in `tle_catalog`; `catalog_update` events on asyncio queue |
| Propagator | `backend/propagator.py` | Stateless SGP4 wrapper. Converts TLE + epoch to ECI J2000 state vector via TEME→GCRS rotation. | TLE line 1 / line 2 (str), epoch (UTC-aware datetime) | `(position_eci_km, velocity_eci_km_s)` numpy arrays |
| Kalman | `backend/kalman.py` | Per-object UKF lifecycle: init, predict, update, recalibrate, NIS computation, confidence scoring. | Filter state dict, ECI observation vector, TLE strings | Updated filter state dict; NIS scalar; innovation vector |
| Anomaly | `backend/anomaly.py` | NIS-based anomaly classification (maneuver / drag / divergence). Writes to `alerts` table. Triggers recalibration parameters. | NIS history list, innovation vector, object_class flag | Anomaly type string or None; `alerts` DB rows |
| Conjunction | `backend/conjunction.py` | Post-anomaly conjunction screening. Propagates anomalous object and all catalog peers over 90-minute horizon; identifies first- and second-order close approaches. | Anomalous object TLE, other-objects TLE list, screening epoch | Conjunction result dict (first_order, second_order lists) |
| Processing | `backend/processing.py` | Shared predict-update-anomaly-recalibrate pipeline for one catalog object. Imported by both main.py and replay.py. | DB connection, catalog entry, filter_states dict, TLE record | List of WebSocket message dicts (0–3 per cycle) |
| API Server | `backend/main.py` | FastAPI application. Lifespan management, background task orchestration, REST endpoints, WebSocket endpoint, ConnectionManager. | asyncio event bus; app.state.filter_states; SQLite DB | JSON REST responses; WebSocket JSON frames |
| Globe | `frontend/src/globe.js` | CesiumJS entity management. ECI→ECEF conversion, satellite billboards, uncertainty ellipsoids, conjunction risk color overlay. | WebSocket state_update / anomaly / conjunction_risk messages | Cesium.Entity mutations on viewer |
| Residuals | `frontend/src/residuals.js` | D3.js NIS timeline and innovation charts. Incremental append on each state_update for the selected object. | WebSocket state_update messages for selected NORAD ID | SVG chart updates |
| Alerts | `frontend/src/alerts.js` | Alert panel. Adds, updates, and resolves alert cards. Enriches with conjunction data. | WebSocket anomaly / recalibration / conjunction_risk messages | DOM alert panel mutations |
| App Entry | `frontend/src/main.js` | WebSocket connection, reconnect backoff, message routing, catalog seeding, object info panel, track requests. | WebSocket frames from /ws/live; GET /catalog; GET /object/* | Calls to globe, residuals, alerts modules |

---

## 4. Data Flow: One Observation Cycle

This section traces a single TLE publication through the full system, from HTTP fetch to WebSocket broadcast, identifying the data format at each boundary.

### Step 1: Space-Track fetch (ingest.py)

`ingest.run_ingest_loop()` wakes every 1800 seconds. It authenticates via HTTPS POST to `https://www.space-track.org/ajaxauth/login` using credentials from `SPACETRACK_USER` / `SPACETRACK_PASS`. For each NORAD ID in the catalog, it fetches the most recent TLE via:

```
GET /basicspacedata/query/class/gp/NORAD_CAT_ID/{ids}/orderby/EPOCH desc/limit/1/format/tle
```

The raw response is two 69-character lines of text. `ingest.validate_tle()` verifies the modulo-10 checksum on both lines (columns 0–67, stored digit at column 68). Invalid TLEs are logged and discarded.

### Step 2: Cache (ingest.py → tle_catalog)

Validated TLEs are written to the `tle_catalog` SQLite table via `INSERT OR IGNORE` on the unique key `(norad_id, epoch_utc)`. The epoch string is ISO-8601 UTC (`YYYY-MM-DDTHH:MM:SSZ`), parsed from the TLE line 1 epoch field. On return, `cache_tles()` emits a `catalog_update` event dict onto the asyncio queue:

```python
{"type": "catalog_update", "count": N, "timestamp_utc": "2026-03-28T19:00:00Z"}
```

### Step 3: Processing loop wakes (main.py → processing.py)

`_processing_loop_task()` dequeues the `catalog_update` event. It iterates `app.state.catalog_entries` sequentially. For each object, `_process_single_object()` calls `ingest.get_latest_tle()` to retrieve the most recent TLE dict from SQLite:

```python
{"norad_id": 25544, "epoch_utc": "2026-03-28T19:00:00Z",
 "tle_line1": "1 25544U ...", "tle_line2": "2 25544 ...", "fetched_at": "..."}
```

### Step 4: SGP4 propagation (propagator.py)

`propagator.tle_to_state_vector_eci_km()` calls `Satrec.twoline2rv()` (sgp4 2.x, WGS72 gravity model) and propagates to the TLE epoch. SGP4 returns position and velocity in TEME (True Equator Mean Equinox). `_teme_to_eci_j2000()` applies the astropy TEME→GCRS transform:

```python
teme_coord = TEME(position.with_differentials(velocity), obstime=Time(epoch_utc, scale="utc"))
gcrs_coord = teme_coord.transform_to(GCRS(obstime=obstime))
```

The output is a 6-element numpy array `[x_km, y_km, z_km, vx_km_s, vy_km_s, vz_km_s]` in ECI J2000 (GCRS). All values carry SI units per convention: km for position, km/s for velocity.

**Simulation fidelity boundary:** This state vector is used as a synthetic observation. In the POC, Space-Track TLE publications serve as both the propagation seed and the ground-truth observation. No real sensor data enters the pipeline at this boundary.

### Step 5: UKF predict (kalman.py)

For a warm filter (NORAD ID in `filter_states`), `kalman.predict()` propagates the prior filter state forward to the new TLE epoch. The process model `fx` is SGP4 applied to the *previous* TLE (stored in `filter_state["last_tle_line1/2"]`), not the new TLE. This separation is essential: using the new TLE as the process model would make the predicted state identical to the observation, yielding zero innovation.

Because SGP4 is a deterministic trajectory model (not a force-model ODE), all 13 UKF sigma points map to the same SGP4-propagated state. Covariance growth during predict is therefore dominated entirely by the process noise matrix Q, not by sigma-point divergence through dynamics. This is a documented POC simplification; see `docs/reference/algorithmic-foundation.md` and the POST-002/POST-003 tech debt entries.

Q is selected per object class at filter initialization:

| Object class | Q position diagonal (km²) | Q velocity diagonal ((km/s)²) |
|---|---|---|
| active_satellite | 0.25 | 25e-4 |
| debris | 1.00 | 1e-4 |
| rocket_body | 0.5625 | 4e-4 |

### Step 6: UKF update (kalman.py)

`kalman.update()` incorporates the new-TLE ECI observation. The measurement function `hx` is the identity (full-state observation; no partial observability in POC). The measurement noise matrix R is diagonal:

```
R = diag([900.0, 900.0, 900.0, 2e-3, 2e-3, 2e-3])  # km² and (km/s)²
```

The 900 km² position variance (30 km 1-sigma) is calibrated against observed ISS TLE-to-TLE prediction error. After `ukf.update()`, the innovation vector `y = z_observed − z_predicted` and innovation covariance S are used to compute NIS:

```
NIS = yᵀ S⁻¹ y
```

NIS is appended to `filter_state["nis_history"]` (capped at 20 entries). A confidence score is computed from the current NIS and recent history. The chi-squared threshold for 6 DOF at p=0.05 is 12.592 (`CHI2_THRESHOLD_6DOF`).

### Step 7: Anomaly classification (anomaly.py)

`anomaly.classify_anomaly()` applies three rules in priority order:

1. **Maneuver:** NIS exceeds threshold for `MANEUVER_CONSECUTIVE_CYCLES` (= 2) consecutive cycles AND `object_class == "active_satellite"`. Triggers covariance inflation factor 20.0.
2. **Drag anomaly:** Single NIS exceedance with along-track velocity residual dominating cross-track by 3:1 and cross-track residual < 1 km. Triggers inflation factor 10.0.
3. **Filter divergence:** Any remaining NIS exceedance. Triggers inflation factor 10.0.

For active satellites, a two-cycle deferred confirmation protocol is used: the first NIS exceedance is stored as `_pending_anomaly_*` keys in the filter state. On the second cycle, the classifier runs again on the updated NIS history. If confirmed, the provisional anomaly type may be upgraded (e.g., `filter_divergence` → `maneuver`). The DB row written in cycle 1 is retroactively corrected via `UPDATE state_history`.

### Step 8: DB write and WebSocket broadcast

Detected anomalies are written to the `alerts` table by `anomaly.record_anomaly()`. All state updates (normal and anomaly) are written to the `state_history` table by `_insert_state_history_row()`. `_build_ws_message()` constructs the JSON-serializable message dict, converting numpy arrays to Python lists. The `ConnectionManager.broadcast()` sends the JSON string to all active WebSocket connections.

### Step 9: Conjunction screening (conjunction.py, async)

If the processing cycle produced an anomaly message, `_run_conjunction_screening()` is scheduled as a fire-and-forget asyncio task. The CPU-bound screening work runs in `loop.run_in_executor(None, ...)` to avoid blocking the event loop. Screening propagates the anomalous object and all catalog peers (those with cached TLEs) at 60-second steps over a 5400-second horizon (90 minutes, one LEO orbital period at ISS altitude). First-order risks are catalog objects with minimum separation < 5 km. Second-order risks are catalog objects within 10 km of any first-order object. The result is persisted to `conjunction_events` and `conjunction_risks` and broadcast as a `conjunction_risk` WebSocket message.

### Step 10: Frontend rendering

`main.js.routeMessage()` dispatches the incoming message by type:
- `state_update`: updates satellite position on globe, uncertainty ellipsoid, D3 chart (if selected object), and `latestStateMap`.
- `anomaly`: highlights object on globe, adds alert card to panel, adds anomaly marker to D3 chart.
- `recalibration`: updates position and ellipsoid, transitions alert card to "recalibrating" state.
- `conjunction_risk`: applies risk color overlay on globe (first-order: red, second-order: yellow), enriches alert card.

---

## 5. Backend Architecture

### 5.1 FastAPI application structure

`backend/main.py` defines a single FastAPI application instance with:
- **Lifespan context manager** (`@contextlib.asynccontextmanager async def lifespan`): handles startup (DB init, catalog load, background task launch) and shutdown (task cancellation, DB close). Uses the modern lifespan pattern rather than the deprecated `@app.on_event` decorators.
- **CORS middleware**: allows cross-origin requests from `http://localhost:3000` and `http://127.0.0.1:3000` (frontend dev server).
- **Application state** (`app.state`): holds `db` (sqlite3.Connection), `db_path`, `catalog_config_path`, `catalog_entries` (list[dict]), `filter_states` (dict[int, dict]), `event_bus` (asyncio.Queue), `background_tasks` (list[asyncio.Task]).
- **No router decomposition**: all endpoints are registered directly on the `app` object. Router factoring is post-POC.

### 5.2 Background tasks

Two asyncio tasks are created at startup and cancelled at shutdown:

**`_ingest_loop_task`**: wraps `ingest.run_ingest_loop()` in a retry loop. Transient exceptions trigger a 60-second backoff before restarting (NF-010). `asyncio.CancelledError` exits cleanly.

**`_processing_loop_task`**: consumes `catalog_update` events from the asyncio queue. For each event, iterates all catalog entries sequentially and calls `_process_single_object()`. Per-object exceptions are caught and logged without killing the loop. A `POST /admin/trigger-process` endpoint provides a synchronous force-run that processes all cached TLEs in epoch order — used by `scripts/seed_maneuver.py` for demo injection.

### 5.3 Processing cycle timing

The nominal cycle interval is 1800 seconds (the Space-Track poll interval). A full catalog pass over 100 objects requires approximately 100 SGP4 propagations plus 100 astropy TEME→GCRS transforms (one per new TLE) plus 100 UKF predict/update cycles. At measured latency of approximately 90–180 ms per object (dominated by astropy coordinate transform overhead), a 100-object catalog processes in approximately 9–18 seconds. This is well within the 1800-second inter-cycle window. No per-object parallelism is implemented in the POC; a `POST-POC` comment in `_processing_loop_task` documents `ThreadPoolExecutor` as the intended upgrade path.

### 5.4 Filter state management

`app.state.filter_states` is an in-memory Python dict keyed by integer NORAD ID. Each value is a filter state dict containing:
- `filter`: `filterpy.kalman.UnscentedKalmanFilter` instance
- `last_epoch_utc`: UTC-aware `datetime.datetime`
- `state_eci_km`: 6-element numpy array (current mean state estimate, ECI J2000 km and km/s)
- `covariance_km2`: 6×6 numpy array (current covariance, km² and (km/s)²)
- `q_matrix`, `r_matrix`: process and measurement noise matrices
- `nis`: float (most recent NIS value)
- `nis_history`: list[float] (last 20 NIS values)
- `innovation_eci_km`: 6-element numpy array (most recent innovation vector)
- `confidence`: float in [0, 1]
- `last_tle_line1`, `last_tle_line2`: TLE strings used for the most recent filter update (used as the process model TLE in the next predict step)
- `_pending_anomaly_*` keys: transient state for the two-cycle maneuver confirmation protocol

Filter state is not persisted to disk. On process restart, all filter states reinitialize from the most recent cached TLE on the next processing cycle. The `tle_catalog` table uses `INSERT OR IGNORE` on `(norad_id, epoch_utc)`, so cached TLEs survive restarts.

### 5.5 Database schema

The SQLite database file is specified by `NBODY_DB_PATH` (default: `data/catalog/tle_cache.db`). WAL journal mode is enabled at initialization. Five tables are used:

**`tle_catalog`**
```
id          INTEGER PRIMARY KEY AUTOINCREMENT
norad_id    INTEGER NOT NULL
epoch_utc   TEXT    NOT NULL          -- ISO-8601 UTC: 'YYYY-MM-DDTHH:MM:SSZ'
tle_line1   TEXT    NOT NULL
tle_line2   TEXT    NOT NULL
fetched_at  TEXT    NOT NULL
UNIQUE(norad_id, epoch_utc)           -- prevents duplicate inserts on repeated polls
```
Index: `(norad_id, epoch_utc)` via unique constraint.

**`state_history`**
```
id                INTEGER PRIMARY KEY AUTOINCREMENT
norad_id          INTEGER NOT NULL
epoch_utc         TEXT    NOT NULL
x_km, y_km, z_km REAL    NOT NULL    -- ECI J2000 position, km
vx_km_s, vy_km_s, vz_km_s  REAL NOT NULL  -- ECI J2000 velocity, km/s
cov_x_km2, cov_y_km2, cov_z_km2  REAL NOT NULL  -- P diagonal, km²
nis               REAL    NOT NULL
confidence        REAL    NOT NULL
anomaly_type      TEXT                -- NULL for normal updates
message_type      TEXT    NOT NULL    -- 'state_update' | 'anomaly' | 'recalibration'
```
Index: `(norad_id, epoch_utc)`. Written on every processing cycle. Serves the `GET /object/{id}/history` endpoint and provides a complete audit trail for post-analysis.

**`alerts`**
```
id                       INTEGER PRIMARY KEY AUTOINCREMENT
norad_id                 INTEGER NOT NULL
detection_epoch_utc      TEXT    NOT NULL
anomaly_type             TEXT    NOT NULL   -- 'maneuver' | 'drag_anomaly' | 'filter_divergence'
nis_value                REAL    NOT NULL
resolution_epoch_utc     TEXT               -- NULL until recalibration completes
recalibration_duration_s REAL               -- seconds from detection to resolution
status                   TEXT    NOT NULL DEFAULT 'active'   -- 'active' | 'recalibrating' | 'resolved'
created_at               TEXT    NOT NULL DEFAULT (datetime('now'))
```
Indexes: `(norad_id, status)`, unique `(norad_id, detection_epoch_utc)`. A migration at table creation removes any pre-existing duplicate rows from prior schema versions.

**`conjunction_events`**
```
id                   INTEGER PRIMARY KEY AUTOINCREMENT
anomalous_norad_id   INTEGER NOT NULL
screening_epoch_utc  TEXT    NOT NULL
horizon_s            INTEGER NOT NULL    -- 5400 (90 minutes)
threshold_km         REAL    NOT NULL    -- 5.0 (first-order) or 10.0 (second-order)
created_at           TEXT    NOT NULL DEFAULT (datetime('now'))
```
Index: `(anomalous_norad_id)`.

**`conjunction_risks`**
```
id                             INTEGER PRIMARY KEY AUTOINCREMENT
conjunction_event_id           INTEGER NOT NULL    -- FK → conjunction_events.id
risk_order                     INTEGER NOT NULL    -- 1 (first-order) or 2 (second-order)
norad_id                       INTEGER NOT NULL    -- object at risk
min_distance_km                REAL    NOT NULL
time_of_closest_approach_utc   TEXT    NOT NULL
via_norad_id                   INTEGER             -- NULL for first-order; first-order NORAD for second-order
```
Index: `(conjunction_event_id)`.

### 5.6 Coordinate frames

Frame discipline is enforced by module boundary:

| Module | Input frame | Internal frame | Output frame |
|--------|-------------|----------------|--------------|
| `ingest.py` | N/A (TLE text) | N/A | TLE strings |
| `propagator.py` | TLE (mean elements) | TEME (SGP4 native) | ECI J2000 (GCRS) |
| `kalman.py` | ECI J2000 (GCRS) | ECI J2000 (GCRS) | ECI J2000 (GCRS) |
| `anomaly.py` | ECI J2000 (GCRS) | ECI J2000 (GCRS) | Scalar / string |
| `conjunction.py` | TLE strings | ECI J2000 (GCRS) | Scalar distances |
| `main.py` REST | N/A | ECI J2000 (GCRS) | ECI J2000 (GCRS) JSON |
| `globe.js` | ECI J2000 (GCRS) | ECEF (computed) | Cesium.Cartesian3 (meters) |

The TEME→GCRS transform in `propagator.py._teme_to_eci_j2000()` uses `astropy>=6.0` (required dependency). GCRS is used as a J2000 equivalent per TD-003: the GCRS and FK5-based J2000 frames differ by at most ~20 milliarcseconds, translating to sub-meter position differences for LEO objects — negligible for POC filter accuracy. A frame-tie rotation matrix correcting for this difference is a post-POC task.

The ECI→ECEF conversion in `globe.js` uses a simplified GMST rotation (Vallado IAU-1982 formula). This applies only to visualization; it does not affect filter state. All filter computation remains in ECI J2000. ECEF and geodetic coordinates are never used outside `globe.js`.

---

## 6. Frontend Architecture

### 6.1 Single-page structure and CDN dependencies

The frontend is a single HTML file (`frontend/index.html`) that imports ES2022 modules from `frontend/src/`. There is no build step. All third-party libraries are loaded from CDN:

- **CesiumJS 1.114** — 3D globe, entity API, `Cesium.Viewer`, `Cesium.Cartesian3`
- **D3.js v7** — SVG-based residual timeline and NIS history charts

The Cesium Ion access token is not hardcoded. `main.js` fetches `GET /config` on startup to retrieve `cesium_ion_token` from the backend (which reads `CESIUM_ION_TOKEN` from the environment) and passes it to `initGlobe()`. This resolves TD-018.

### 6.2 WebSocket message routing

`main.js.connectWebSocket()` establishes a WebSocket connection to `ws://{host}/ws/live` with exponential backoff reconnection (initial delay 1 s, maximum 30 s). On `onopen`, the client fetches the full catalog from `GET /catalog` to seed the globe and charts, then fetches `GET /alerts/active` to seed the alert panel with any anomalies that fired during disconnection.

`routeMessage(message)` dispatches by `message.type`:

| Message type | Globe action | Chart action | Alert panel action |
|---|---|---|---|
| `state_update` | Update billboard position and color; update ellipsoid | Append NIS data point (selected object only) | Resolve recalibrating alerts if `anomaly_type === null` |
| `anomaly` | `highlightAnomaly()` (magenta billboard) | Add anomaly marker at detection epoch | `addAlert()` with anomaly card |
| `recalibration` | Update billboard position; update ellipsoid | Append NIS data point | Transition alert to "recalibrating" |
| `conjunction_risk` | Apply risk color overlay (red/yellow) | No action | `updateAlertConjunctions()` enriches alert card |

### 6.3 Globe entity management

`globe.js` uses the CesiumJS Entity API directly rather than CZML DataSource (deviation from the original architecture specification; documented as TD-024). At POC scale (up to 100 objects), the entity API produces identical visual output with simpler code and no CZML serialization overhead. CZML DataSource is the recommended upgrade for production to enable server-side timeline control.

Two `Map` instances track live entities:
- `entityMap`: `Map<norad_id, Cesium.Entity>` — satellite billboard entities
- `ellipsoidMap`: `Map<norad_id, Cesium.Entity>` — uncertainty ellipsoid entities

Entity positions are updated via `entity.position = new Cesium.ConstantPositionProperty(cartesian3)`. The ECI→ECEF conversion applies the GMST rotation at the observation epoch, producing an instantaneous ECEF position. This is correct for the real-time display use case (current position); historical track rendering uses per-point epoch GMST rotation via the same `eciToEcefCartesian3()` function.

Confidence color mapping (F-051): confidence > 0.85 → `Cesium.Color.LIME`; 0.60–0.85 → `Cesium.Color.ORANGE`; < 0.60 → `Cesium.Color.RED`. Conjunction risk overrides: first-order → `Cesium.Color.RED`; second-order → `Cesium.Color.YELLOW`. Risk overrides are cleared on the next `state_update` for the anomalous object (auto-clear logic in `routeMessage`).

### 6.4 D3 chart update pattern

The residuals chart (`residuals.js`) maintains an in-memory time-series array per NORAD ID. On each `appendResidualDataPoint()` call, the new point is appended, the array is trimmed to the most recent 200 points, and the SVG is re-rendered with a D3 linear scale update. Anomaly markers are added as vertical lines at the detection epoch via `addAnomalyMarker()`. The chart only renders data for the currently selected NORAD ID; switching selection via `selectObject()` replaces the series without fetching from the server.

---

## 7. Key Interfaces

### 7.1 REST endpoints

| Method | Path | Purpose | Key response fields |
|--------|------|---------|---------------------|
| GET | `/config` | Frontend configuration | `cesium_ion_token: str` |
| GET | `/catalog` | Full catalog with current state | Array of: `norad_id, name, object_class, last_update_epoch_utc, confidence, eci_km[3], eci_km_s[3], covariance_diagonal_km2[3], nis, anomaly_flag, innovation_eci_km[6]` |
| GET | `/object/{norad_id}/history` | Alert history (last 100) | Array of: `id, epoch_utc, anomaly_type, nis, status` |
| GET | `/object/{norad_id}/anomalies` | Detailed anomaly records (last 20) | Array of: `id, norad_id, detection_epoch_utc, anomaly_type, nis_value, resolution_epoch_utc, recalibration_duration_s, status` |
| GET | `/object/{norad_id}/conjunctions` | Recent conjunction screenings (last 5) | Array of conjunction_risk message dicts |
| GET | `/object/{norad_id}/track` | SGP4 track points (back and forward) | `norad_id, reference_epoch_utc, step_s, backward_track[{epoch_utc, eci_km}], forward_track[{epoch_utc, eci_km, uncertainty_radius_km}]` |
| GET | `/alerts/active` | Unresolved anomaly alerts | Array of anomaly message dicts (WS-compatible schema) |
| POST | `/admin/trigger-process` | Force one processing cycle | `{"processed": N}` |
| WebSocket | `/ws/live` | Real-time streaming | See Section 7.2 |

All ECI state vectors in REST responses are in ECI J2000 (GCRS), km and km/s. Covariance values are km². Timestamps are ISO-8601 UTC strings ending in `Z`. Error responses use standard FastAPI HTTPException (JSON `{"detail": "..."}` body).

### 7.2 WebSocket message types

All messages are JSON text frames. The schema is defined in `main.py` module docstring and `processing.py._build_ws_message()`.

**Core state message (applies to `state_update`, `anomaly`, `recalibration`):**

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"state_update"` \| `"anomaly"` \| `"recalibration"` |
| `norad_id` | int | NORAD catalog number |
| `epoch_utc` | string | ISO-8601 UTC epoch (`YYYY-MM-DDTHH:MM:SSZ`) |
| `eci_km` | float[3] | Position `[x, y, z]` in ECI J2000, km |
| `eci_km_s` | float[3] | Velocity `[vx, vy, vz]` in ECI J2000, km/s |
| `covariance_diagonal_km2` | float[3] | Diagonal of P: `[P00, P11, P22]` in km² |
| `nis` | float | Normalized Innovation Squared |
| `innovation_eci_km` | float[6] | Innovation vector `[dx, dy, dz, dvx, dvy, dvz]`, km and km/s |
| `confidence` | float | Confidence score in [0, 1] |
| `anomaly_type` | string \| null | `"maneuver"` \| `"drag_anomaly"` \| `"filter_divergence"` \| `null` |

**Conjunction risk message (`conjunction_risk`):**

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"conjunction_risk"` |
| `anomalous_norad_id` | int | NORAD ID of the anomalous triggering object |
| `screening_epoch_utc` | string | ISO-8601 UTC screening start epoch |
| `horizon_s` | int | Screening horizon in seconds (5400) |
| `threshold_km` | float | Screening threshold in km |
| `first_order` | array | Objects within 5 km: `[{norad_id, name, min_distance_km, time_of_closest_approach_utc}]` |
| `second_order` | array | Objects within 10 km of first-order: same fields plus `via_norad_id` |

**Message triggers and consumers:**

| Type | Trigger | Consumer |
|------|---------|----------|
| `state_update` | Each normal UKF update cycle | `globe.updateSatellitePosition`, `globe.updateUncertaintyEllipsoid`, `residuals.appendResidualDataPoint` |
| `anomaly` | NIS exceeds chi-squared threshold (after two-cycle confirmation for active satellites) | `globe.highlightAnomaly`, `alerts.addAlert`, `residuals.addAnomalyMarker` |
| `recalibration` | Filter re-initialization after anomaly classification | `globe.updateSatellitePosition`, `globe.updateUncertaintyEllipsoid`, `alerts.updateAlertStatus` |
| `conjunction_risk` | Conjunction screening completes post-anomaly | `globe.applyConjunctionRisk`, `alerts.updateAlertConjunctions` |

---

## 8. Deployment Topology

### 8.1 POC local deployment

The POC runs as two processes on a single developer workstation:

```
Process 1: uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
           (FastAPI + ingest loop + processing loop)

Process 2: python -m http.server 3000 (or equivalent static file server)
           (serves frontend/index.html and frontend/src/*.js)
```

The SQLite database file is written to `data/catalog/tle_cache.db` (default; overridden by `NBODY_DB_PATH`). The catalog configuration file is `data/catalog/catalog.json` (overridden by `NBODY_CATALOG_CONFIG`). Both files are local to the repository.

Required environment variables at startup:
```
SPACETRACK_USER   — Space-Track.org account email
SPACETRACK_PASS   — Space-Track.org account password
CESIUM_ION_TOKEN  — Cesium Ion access token for globe imagery
```

For offline demo operation, the 72-hour TLE cache must be pre-loaded before disconnecting from the network:
```bash
python scripts/replay.py --hours 72
```

### 8.2 Production deployment requirements

A production deployment serving multiple concurrent operators would require the following architectural changes not implemented in the POC:

- **Process separation:** The ingest loop and processing loop should run as independent services (or at minimum independent processes) with a message broker (e.g., Redis) replacing the in-process asyncio queue. This eliminates the single point of failure and allows horizontal scaling of the processing tier.
- **Persistent filter state:** Filter state dicts must be serialized to persistent storage (database or object store) so that process restarts do not require a cold-start convergence period. The `state_history` table schema already captures the necessary state.
- **Authentication and authorization:** The WebSocket endpoint (`/ws/live`) and the admin endpoint (`POST /admin/trigger-process`) have no authentication. Production deployments require at minimum bearer token authentication on all endpoints and role-based access control separating operator, analyst, and administrator roles as defined in `docs/reference/conops.md`.
- **HTTP 429 handling:** `ingest.py` does not implement retry logic for Space-Track HTTP 429 (rate limit exceeded) responses. Production ingest requires exponential backoff with jitter.
- **TLS:** All connections must use TLS in production. The current configuration assumes localhost HTTP.
- **Database:** SQLite is appropriate for a single-node POC. Multi-node deployments require a client-server database (e.g., PostgreSQL) with appropriate connection pooling.

---

## 9. Known Architectural Constraints

The following constraints are acknowledged characteristics of the POC implementation, not defects. They are documented to enable accurate technical evaluation and to define the scope of the production engineering effort.

**Single-process asyncio, no per-object parallelism.** The processing loop iterates catalog objects sequentially on the main asyncio event loop. Blocking numpy and SQLite operations are performed synchronously. At 100 objects with ~150 ms per object, a full catalog pass completes in approximately 15 seconds — acceptable within the 1800-second interval. For catalogs beyond ~500 objects, this approach would cause processing lag, and `ThreadPoolExecutor`-based parallelism would be required.

**In-memory filter state lost on restart.** `app.state.filter_states` is not persisted. On uvicorn restart, all filters reinitialize cold from the most recent cached TLE. Convergence to a well-calibrated state estimate requires 2–5 observation cycles (60–150 minutes at nominal poll rates). The `state_history` table contains the information needed to reconstruct filter state on restart; this reconstruction is not yet implemented.

**SGP4 sigma point collapse.** Because SGP4 is used as the UKF process model and all 13 sigma points receive the same deterministic SGP4 trajectory output, the covariance forecast during the predict step is driven entirely by the additive Q matrix. The sigma-point spread through the process model, which is the UKF's primary advantage over the EKF for nonlinear systems, is not exercised. Post-POC replacement with a numerical integrator (Runge-Kutta + J2/J4 perturbations) as the process model is documented as POST-003.

**Measurement noise R is hand-calibrated.** The DEFAULT_R position variance of 900 km² (30 km 1-sigma) is calibrated against observed ISS TLE-to-TLE prediction error over a 30-minute interval. This value is applied uniformly to all object classes. Adaptive noise estimation (adjusting R based on observed innovation statistics) is documented as POST-002.

**Drag anomaly classifier uses ECI velocity residual as along-track proxy.** The proper decomposition requires the object's actual velocity vector to define the RSW (Radial-Along-Track-Cross-Track) frame. The current heuristic uses the velocity residual direction as a surrogate, which is an ECI simplification that produces incorrect results when the residual direction is not aligned with the orbital track. Post-POC RSW frame decomposition is documented as a tech debt item.

**No WebSocket authentication.** The `/ws/live` endpoint accepts any connection up to the `MAX_WS_CONNECTIONS = 20` cap. No bearer token or session credential is required. Acceptable for local demo; not suitable for any networked deployment.

**Cesium Ion imagery requires network access.** The CesiumJS globe uses Cesium Ion default imagery, which requires an active internet connection and a valid `CESIUM_ION_TOKEN`. For fully offline demo operation, a `SingleTileImageryProvider` with a locally cached texture must be configured.

**ITAR compliance scope.** Space-Track.org data is publicly releasable under the user's acknowledgment of export control terms at account registration. All data provenance in this POC derives from that source. No classified or CUI data is ingested. The `ingest.py` module is the sole point of external data acquisition; no other module initiates network requests to external systems.
