# System Architecture Reference: ne-body (Near Earth body) SSA Platform
Version: 0.2.0
Status: Draft
Last updated: 2026-04-12

---

## 1. Overview

The ne-body (Near Earth body) platform is a browser-based, closed-loop Space Situational Awareness (SSA) system that replaces static long-horizon orbital prediction with a continuous observe-propagate-validate-recalibrate cycle. The architectural problem it solves is the absence of a feedback loop in conventional SSA tooling: when a new Two-Line Element set (TLE) arrives that is inconsistent with the prior propagated trajectory, standard tools provide no automated mechanism to detect the inconsistency, classify its cause, or update the state estimate without analyst intervention. ne-body closes this loop by maintaining a per-object Unscented Kalman Filter (UKF) whose state is updated on every incoming TLE, whose Normalized Innovation Squared (NIS) statistic is tested against a chi-squared threshold on every update, and whose anomaly output drives immediate recalibration and conjunction screening — all within the same processing cycle that received the new data. The mathematical basis for the UKF, the NIS test, and the anomaly classifier is documented in `docs/reference/algorithmic-foundation.md`; this document describes the software architecture that realizes that algorithm at system scale.

Version 0.2.0 of this document reflects the following architectural additions since 0.1.0: a supplemental N2YO TLE ingest path, the rescoped 75-object Very Low Earth Orbit (VLEO) catalog, a frontend 28-day TLE staleness filter, a real-time telemetry dashboard layout, an obtrusive audio and visual anomaly alerting subsystem, and fragmentation-event monitoring. The core closed-loop algorithm is unchanged.

---

## 2. System Context Diagram

```
  Space-Track.org                        N2YO.com
  (primary TLE source)                   (supplemental fallback)
        |                                       |
        | HTTPS, authenticated session          | HTTPS, api key in query
        | every 30 minutes                      | per-object, max 50/cycle
        | (POLL_INTERVAL_S = 1800)              | (N2YO_MAX_REQUESTS_PER_CYCLE)
        |                                       |
        +------------------+--------------------+
                           |
                           v
                  +----------------+
                  |   ingest.py    |  validate checksum; INSERT OR IGNORE
                  | (sole external |  tle_catalog (norad_id, epoch_utc,
                  |  network seam) |  tle_line1, tle_line2, fetched_at, source)
                  +----------------+
                           |
                           | catalog_update event (asyncio.Queue, maxsize=10)
                           |
                           v
                  +----------------+
                  | processing.py  |  predict -> update -> anomaly -> recalibrate
                  | (processing    |  sequential iteration over catalog entries
                  |  loop task)    |  ~10 s per full catalog pass (75 objects)
                  +----------------+
                           |           \
                           |            \-- conjunction.py (fire-and-forget,
                           |                run_in_executor) on every anomaly
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
                  |  Browser          |  real-time telemetry dashboard
                  |  main.js          |  WebSocket client, 28-day freshness filter,
                  |                   |  tracked-object counter, event-driven chart
                  |                   |  visibility, audio + visual alerting
                  |  globe.js         |  CesiumJS 3D orbital view
                  |  residuals.js     |  D3.js NIS / innovation timeline
                  |  alerts.js        |  alert panel, conjunction risk enrichment
                  |  alertsound.js    |  Web Audio API 3-beep alarm
                  |  alertflash.js    |  fullscreen red flash overlay
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
| Ingest | `backend/ingest.py` | Sole interface to all external TLE sources (Space-Track primary, N2YO supplemental). Authenticates, fetches TLEs, validates checksums, caches to SQLite with per-row source tag, emits events. | `SPACETRACK_USER` / `SPACETRACK_PASS` / `N2YO_API_KEY` env vars; catalog.json NORAD IDs | Rows in `tle_catalog` (tagged `space_track` or `n2yo`); `catalog_update` events on asyncio queue |
| Propagator | `backend/propagator.py` | Stateless SGP4 wrapper. Converts TLE + epoch to Earth-Centered Inertial (ECI) J2000 state vector via True Equator Mean Equinox (TEME) → Geocentric Celestial Reference System (GCRS) rotation. | TLE line 1 / line 2 (str), epoch (Coordinated Universal Time (UTC)-aware datetime) | `(position_eci_km, velocity_eci_km_s)` numpy arrays |
| Kalman | `backend/kalman.py` | Per-object UKF lifecycle: init, predict, update, recalibrate, NIS computation, confidence scoring. | Filter state dict, ECI observation vector, TLE strings | Updated filter state dict; NIS scalar; innovation vector |
| Anomaly | `backend/anomaly.py` | NIS-based anomaly classification (maneuver / drag / divergence). Writes to `alerts` table. Triggers recalibration parameters. | NIS history list, innovation vector, object_class flag | Anomaly type string or None; `alerts` DB rows |
| Conjunction | `backend/conjunction.py` | Post-anomaly conjunction screening. Propagates anomalous object and all catalog peers over 90-minute horizon; identifies first- and second-order close approaches. | Anomalous object TLE, other-objects TLE list, screening epoch | Conjunction result dict (first_order, second_order lists) |
| Processing | `backend/processing.py` | Shared predict-update-anomaly-recalibrate pipeline for one catalog object. Imported by both main.py and replay.py. | DB connection, catalog entry, filter_states dict, TLE record | List of WebSocket message dicts (0–3 per cycle) |
| API Server | `backend/main.py` | FastAPI application. Lifespan management, background task orchestration, REST endpoints, WebSocket endpoint, ConnectionManager. | asyncio event bus; app.state.filter_states; SQLite DB | JSON REST responses; WebSocket JSON frames |
| Globe | `frontend/src/globe.js` | CesiumJS entity management. ECI→Earth-Centered Earth-Fixed (ECEF) conversion, satellite billboards, uncertainty ellipsoids, conjunction risk color overlay, stale-entity removal. | WebSocket state_update / anomaly / conjunction_risk messages | Cesium.Entity mutations on viewer |
| Residuals | `frontend/src/residuals.js` | D3.js NIS timeline and innovation charts. Incremental append on each state_update for the selected object. Exports `resizeChart()` for post-transition redraws. | WebSocket state_update messages for selected NORAD ID | Scalable Vector Graphics (SVG) chart updates |
| Alerts | `frontend/src/alerts.js` | Alert panel. Adds, updates, and resolves alert cards. Enriches with conjunction data. | WebSocket anomaly / recalibration / conjunction_risk messages | Document Object Model (DOM) alert panel mutations |
| Alert Sound | `frontend/src/alertsound.js` | Web Audio API three-beep rising alarm (660 Hz / 880 Hz / 1100 Hz). Debounced (2-second cooldown). Mute toggle. | `triggerAlertSound()` calls from `main.js` anomaly handler | Audio output |
| Alert Flash | `frontend/src/alertflash.js` | Fullscreen red flash overlay bearing object name and anomaly type. Auto-fades after 3 seconds. | `triggerAlertFlash(objectName, anomalyType)` calls | DOM overlay mutations |
| App Entry | `frontend/src/main.js` | WebSocket connection, reconnect backoff, message routing, catalog seeding, 28-day freshness filter, tracked-object counter, event-driven chart visibility controller, object info panel, camera fly-to, track requests. | WebSocket frames from /ws/live; GET /catalog; GET /object/* | Calls to globe, residuals, alerts, alertsound, alertflash modules |

---

## 4. Data Flow: One Observation Cycle

This section traces a single TLE publication through the full system, from HTTP fetch to WebSocket broadcast, identifying the data format at each boundary.

### Step 1: Space-Track fetch (ingest.py)

`ingest.run_ingest_loop()` wakes every 1800 seconds. It authenticates via HTTPS POST to `https://www.space-track.org/ajaxauth/login` using credentials from `SPACETRACK_USER` / `SPACETRACK_PASS`. For the full catalog NORAD ID list, it fetches the most recent TLE per object via:

```
GET /basicspacedata/query/class/gp/NORAD_CAT_ID/{ids}/orderby/EPOCH desc/limit/1/format/tle
```

The raw response is pairs of 69-character lines of text. `ingest.validate_tle()` verifies the modulo-10 checksum on both lines (columns 0–67, stored digit at column 68). Invalid TLEs are logged and discarded.

### Step 2: N2YO supplemental fallback (ingest.py)

After the Space-Track fetch and cache step, `ingest.poll_once()` computes a list of catalog objects for which Space-Track returned no TLE at all, or whose most recent cached TLE has an epoch older than `N2YO_STALE_THRESHOLD_S = 7 * 86400` seconds (7 days). The list is capped at `N2YO_MAX_REQUESTS_PER_CYCLE = 50` objects per cycle and ordered oldest-first to prioritize the most stale entries. The fallback runs only if `N2YO_API_KEY` is present in the process environment; if unset, the block is skipped and an informational message is logged once per process.

For each selected NORAD ID, `fetch_tle_n2yo()` issues:

```
GET https://api.n2yo.com/rest/v1/satellite/tle/{norad_id}&apiKey=<redacted>
```

The N2YO URL uses `&apiKey=` as the first query separator; this is preserved exactly. The API key value is redacted in all audit log entries. The response body is a JSON object of the form `{"info": {"satid": <int>, ...}, "tle": "<line1>\r\n<line2>"}`. The `tle` field is split on `\r\n` or `\n`, validated via the same checksum routine used for Space-Track, cross-checked against the requested NORAD ID, and returned as a dict matching the Space-Track fetch shape. Any failure (HTTP non-2xx, malformed body, checksum failure, satid mismatch) yields `None` — N2YO failures never raise out of `fetch_tle_n2yo`, honoring NF-010. Between calls, `poll_once` awaits 100 ms to pace under the N2YO free-tier rate limit (1,000 requests/hour account-wide).

### Step 3: Cache with source tagging (ingest.py → tle_catalog)

Validated TLEs from either source are written via `cache_tles(db, tles, fetched_at_utc, source=<'space_track'|'n2yo'>)`. The SQLite `tle_catalog` table now carries a `source` column (default `'space_track'` for backward compatibility; migrated in place via `ALTER TABLE ADD COLUMN` on first startup against an older database). The unique key `(norad_id, epoch_utc)` still prevents duplicate inserts.

A single `catalog_update` event is emitted per cycle covering both sources:

```python
{"type": "catalog_update", "count": N_space_track + N_n2yo, "timestamp_utc": "2026-04-12T19:00:00Z"}
```

The event shape is unchanged; downstream consumers (`_processing_loop_task`, `kalman.py`, the broadcast path) read `get_latest_tle()` which returns the newest-epoch TLE regardless of source. The `source` column is queryable but not used by any existing downstream module in the POC.

### Step 4: Processing loop wakes (main.py → processing.py)

`_processing_loop_task()` dequeues the `catalog_update` event. It iterates `app.state.catalog_entries` sequentially. For each object, `_process_single_object()` calls `ingest.get_latest_tle()` to retrieve the most recent TLE dict from SQLite (now including the `source` field).

### Step 5: SGP4 propagation (propagator.py)

`propagator.tle_to_state_vector_eci_km()` calls `Satrec.twoline2rv()` (sgp4 2.x, WGS72 gravity model) and propagates to the TLE epoch. SGP4 returns position and velocity in TEME. `_teme_to_eci_j2000()` applies the astropy TEME→GCRS transform:

```python
teme_coord = TEME(position.with_differentials(velocity), obstime=Time(epoch_utc, scale="utc"))
gcrs_coord = teme_coord.transform_to(GCRS(obstime=obstime))
```

The output is a 6-element numpy array `[x_km, y_km, z_km, vx_km_s, vy_km_s, vz_km_s]` in ECI J2000 (GCRS). All values carry SI units per convention: km for position, km/s for velocity.

**Simulation fidelity boundary:** This state vector is used as a synthetic observation. In the POC, TLE publications (from either Space-Track or N2YO) serve as both the propagation seed and the ground-truth observation. No real sensor data enters the pipeline at this boundary. N2YO is a republisher of public US Space Surveillance Network data; in practice, N2YO-sourced TLEs share the same underlying observation origin as Space-Track for the majority of catalog objects.

### Step 6: UKF predict (kalman.py)

For a warm filter (NORAD ID in `filter_states`), `kalman.predict()` propagates the prior filter state forward to the new TLE epoch. The process model `fx` is SGP4 applied to the *previous* TLE (stored in `filter_state["last_tle_line1/2"]`), not the new TLE. This separation is essential: using the new TLE as the process model would make the predicted state identical to the observation, yielding zero innovation.

Because SGP4 is a deterministic trajectory model (not a force-model ODE), all 13 UKF sigma points map to the same SGP4-propagated state. Covariance growth during predict is therefore dominated entirely by the process noise matrix Q, not by sigma-point divergence through dynamics. This is a documented POC simplification; see `docs/reference/algorithmic-foundation.md` and the POST-002/POST-003 tech debt entries.

Q is selected per object class at filter initialization:

| Object class | Q position diagonal (km²) | Q velocity diagonal ((km/s)²) |
|---|---|---|
| active_satellite | 0.25 | 25e-4 |
| debris | 1.00 | 1e-4 |
| rocket_body | 0.5625 | 4e-4 |

### Step 7: UKF update (kalman.py)

`kalman.update()` incorporates the new-TLE ECI observation. The measurement function `hx` is the identity (full-state observation; no partial observability in POC). The measurement noise matrix R is diagonal:

```
R = diag([900.0, 900.0, 900.0, 2e-3, 2e-3, 2e-3])  # km² and (km/s)²
```

The 900 km² position variance (30 km 1-sigma) is calibrated against observed ISS TLE-to-TLE prediction error. After `ukf.update()`, the innovation vector `y = z_observed − z_predicted` and innovation covariance S are used to compute NIS:

```
NIS = yᵀ S⁻¹ y
```

NIS is appended to `filter_state["nis_history"]` (capped at 20 entries). A confidence score is computed from the current NIS and recent history. The chi-squared threshold for 6 DOF at p=0.05 is 12.592 (`CHI2_THRESHOLD_6DOF`).

R is applied uniformly regardless of the TLE's `source` tag. Per-source R tuning (for example, if empirical study demonstrated different accuracy classes between Space-Track and N2YO-sourced TLEs for a subset of objects) is a named post-POC activity; the `source` column in `tle_catalog` enables that analysis but the filter does not currently act on it.

### Step 8: Anomaly classification (anomaly.py)

`anomaly.classify_anomaly()` applies three rules in priority order:

1. **Maneuver:** NIS exceeds threshold for `MANEUVER_CONSECUTIVE_CYCLES` (= 2) consecutive cycles AND `object_class == "active_satellite"`. Triggers covariance inflation factor 20.0.
2. **Drag anomaly:** Single NIS exceedance with along-track velocity residual dominating cross-track by 3:1 and cross-track residual < 1 km. Triggers inflation factor 10.0.
3. **Filter divergence:** Any remaining NIS exceedance. Triggers inflation factor 10.0.

For active satellites, a two-cycle deferred confirmation protocol is used: the first NIS exceedance is stored as `_pending_anomaly_*` keys in the filter state. On the second cycle, the classifier runs again on the updated NIS history. If confirmed, the provisional anomaly type may be upgraded (e.g., `filter_divergence` → `maneuver`). The DB row written in cycle 1 is retroactively corrected via `UPDATE state_history`.

### Step 9: DB write and WebSocket broadcast

Detected anomalies are written to the `alerts` table by `anomaly.record_anomaly()`. All state updates (normal and anomaly) are written to the `state_history` table by `_insert_state_history_row()`. `_build_ws_message()` constructs the JSON-serializable message dict, converting numpy arrays to Python lists. The `ConnectionManager.broadcast()` sends the JSON string to all active WebSocket connections.

### Step 10: Conjunction screening (conjunction.py, async)

If the processing cycle produced an anomaly message, `_run_conjunction_screening()` is scheduled as a fire-and-forget asyncio task. The CPU-bound screening work runs in `loop.run_in_executor(None, ...)` to avoid blocking the event loop. Screening propagates the anomalous object and all catalog peers (those with cached TLEs) at 60-second steps over a 5400-second horizon (90 minutes, one LEO orbital period at ISS altitude). First-order risks are catalog objects with minimum separation < 5 km. Second-order risks are catalog objects within 10 km of any first-order object. The result is persisted to `conjunction_events` and `conjunction_risks` and broadcast as a `conjunction_risk` WebSocket message.

### Step 11: Frontend rendering (28-day staleness filter, dashboard, alerts)

`main.js.routeMessage()` dispatches the incoming message by type. Before any rendering action, `state_update` and `recalibration` messages are tested against the 28-day TLE staleness filter:

```
MAX_TLE_AGE_MS = 28 * 24 * 60 * 60 * 1000   // 28 days
_isFreshEpoch(epoch_utc) === (Date.now() - Date.parse(epoch_utc)) <= MAX_TLE_AGE_MS
```

If the message epoch is older than 28 days, the frontend:

1. Deletes the object from `latestStateMap`.
2. Calls `removeSatelliteEntity(viewer, norad_id)` to remove the billboard, label, and uncertainty ellipsoid from the Cesium viewer.
3. Updates the tracked-object counter in the header via `_updateTrackedCount()`.
4. Returns without further rendering.

The 28-day window exists to suppress equatorial clustering artifacts: when a TLE epoch is far in the past, SGP4 propagation to "now" can drift by hundreds of kilometers along the equator, producing a visually misleading pile-up of stale objects near the Prime Meridian crossing. Removing such entities is correct because the backend no longer has a trustworthy state estimate for them; the next fresh TLE arrival will restore the entity automatically.

For fresh messages, dispatch proceeds by type:

- `state_update`: updates satellite position on globe, uncertainty ellipsoid, D3 chart (if selected object), `latestStateMap`, and the tracked-object counter. If `anomaly_type === null`, any recalibrating alerts for that object are resolved.
- `anomaly`: highlights object on globe, adds alert card to panel, adds anomaly marker to D3 chart. Additionally, `triggerAlertSound()` fires the Web Audio three-beep alarm (unless muted), `triggerAlertFlash()` displays the fullscreen red overlay with object name and anomaly type, and if the anomalous object is the currently selected object, the residual chart panel expands via `_setChartVisible(true)`.
- `recalibration`: re-applies the staleness filter, updates position and ellipsoid, transitions alert card to "recalibrating" state.
- `conjunction_risk`: applies risk color overlay on globe (first-order: red, second-order: yellow), enriches alert card, refreshes the object info panel if the selected object is involved.

The two obtrusive alerting paths (`triggerAlertSound`, `triggerAlertFlash`) fire only from the live `routeMessage` anomaly branch, never from the reconnection seed path (`GET /alerts/active`), so historical alerts reloaded after a WebSocket drop do not retrigger audio or visual alarms.

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

**`_ingest_loop_task`**: wraps `ingest.run_ingest_loop()` in a retry loop. Transient exceptions trigger a 60-second backoff before restarting (NF-010). `asyncio.CancelledError` exits cleanly. Inside `run_ingest_loop`, each cycle invokes `poll_once`, which performs the Space-Track primary fetch and the N2YO supplemental fallback sequentially.

**`_processing_loop_task`**: consumes `catalog_update` events from the asyncio queue. For each event, iterates all catalog entries sequentially and calls `_process_single_object()`. Per-object exceptions are caught and logged without killing the loop. A `POST /admin/trigger-process` endpoint provides a synchronous force-run that processes all cached TLEs in epoch order — used by `scripts/seed_maneuver.py` for demo injection.

### 5.3 Processing cycle timing

The nominal cycle interval is 1800 seconds (the Space-Track poll interval). A full catalog pass over the 75-object VLEO catalog requires approximately 75 SGP4 propagations plus 75 astropy TEME→GCRS transforms (one per new TLE) plus 75 UKF predict/update cycles. At measured latency of approximately 90–180 ms per object (dominated by astropy coordinate transform overhead), a 75-object catalog processes in approximately 7–14 seconds. This is well within the 1800-second inter-cycle window. No per-object parallelism is implemented in the POC; a `POST-POC` comment in `_processing_loop_task` documents `ThreadPoolExecutor` as the intended upgrade path.

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
source      TEXT    NOT NULL DEFAULT 'space_track'   -- 'space_track' | 'n2yo'
UNIQUE(norad_id, epoch_utc)           -- prevents duplicate inserts on repeated polls
```
Index: `(norad_id, epoch_utc)` via unique constraint. The `source` column is added via an idempotent `ALTER TABLE ADD COLUMN` migration at `init_catalog_db` on databases created prior to version 0.2.0; rows predating the migration are backfilled with `'space_track'`, which matches their actual provenance.

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

The ECI→ECEF conversion in `globe.js` uses a simplified Greenwich Mean Sidereal Time (GMST) rotation (Vallado IAU-1982 formula). This applies only to visualization; it does not affect filter state. All filter computation remains in ECI J2000. ECEF and geodetic coordinates are never used outside `globe.js`.

### 5.7 Tracked catalog (VLEO scope)

The tracked catalog is defined in `data/catalog/catalog.json` and loaded into `app.state.catalog_entries` at startup. It contains **75 verified objects**, all with SGP4-derived altitudes at or below **600 km** (Very Low Earth Orbit). Altitude verification is recorded in `data/catalog/altitude_verification_report.txt`.

Object composition:

| Category | Example objects |
|----------|-----------------|
| Crewed platforms | ISS (ZARYA) NORAD 25544; CSS (TIANHE) NORAD 48274 |
| Legacy active satellite | HST NORAD 20580 |
| SpaceX Starlink (VLEO subset) | STARLINK-24, -25, -26, -1095, -1306, -1571, -1706, -1800, -1965, -1990, … |
| SpaceX Starlink (fragmentation monitoring) | STARLINK-34343 NORAD 64157 (fragmented 2026-03-29; see section 5.8) |
| Commercial imaging (SAR / electro-optical) | BlackSky GLOBAL-1 through GLOBAL-5; CAPELLA-2 (SEQUOIA), CAPELLA-5 through CAPELLA-8; UMBRA-04, -05, -06; ICEYE-X1, -X6, -X7, -X9, -X11, -X14, and additional ICEYE platforms |
| Radio-frequency geolocation | HawkEye 360 HAWK-A (NORAD 43765), HAWK-B (43794), HAWK-C (43799), HAWK-8A (59443), HAWK-8B (59445), HAWK-8C (59449) |
| Planet smallsats | FLOCK constellation entries (VLEO subset) |
| Swarm smallsats | SpaceBEE entries (VLEO subset) |
| Launch vehicle upper stages | CZ-5B and Falcon 9 rocket bodies (`object_class == "rocket_body"`) |
| Fragmentation debris (monitored field) | Cosmos 1408 debris cloud members (`object_class == "debris"`) |

**Rationale for VLEO scope.** Very Low Earth Orbit concentrates the highest-activity population for SSA: the ISS and CSS crewed platforms, the densest commercial smallsat deployments, the Starlink operational shell at the low end, the majority of active commercial radar-imaging assets, and the fragment populations most relevant to operational conjunction risk. The ≤600 km altitude bound simplifies drag-regime analysis (all objects experience meaningful atmospheric interaction) and produces a visually dense globe view appropriate for operator-facing demonstration. Objects at geostationary or medium Earth orbits are out of catalog scope for this POC.

**HawkEye 360 nomenclature correction.** Earlier versions of the catalog contained entries under the legacy labels `HAWKEYE PATHFINDER` and `HAWKEYE CLUSTER`. These were corrected in April 2026 to use the canonical Space-Track names (`HAWK-A`, `HAWK-B`, `HAWK-C`, `HAWK-8A`, `HAWK-8B`, `HAWK-8C`) to ensure deterministic cross-referencing against Space-Track queries. Downstream modules reference the objects by NORAD ID, not by name; the correction is name-only and did not affect filter state or alert history.

### 5.8 Fragmentation event monitoring

The catalog includes **STARLINK-34343 (NORAD 64157)** as a monitored object. STARLINK-34343 fragmented on **2026-03-29** at an approximate altitude of 560 km. At the time of catalog update (2026-04-12), no fragment NORAD IDs had been publicly assigned by the 18th Space Defense Squadron. Given the ~560 km initial altitude, the fragment population is expected to deorbit within weeks to months under atmospheric drag; adding individual fragment NORAD IDs to the catalog is deferred until public assignments are available.

The parent object is tagged in the catalog with the suffix `(FRAGMENTED)` in its name field and carries the `active_satellite` object class (unchanged from its pre-event classification; no `fragmented` object class exists in the POC schema). For the parent object, Space-Track may no longer publish new TLEs, in which case the 28-day staleness filter on the frontend will remove it from the globe approximately 28 days after the last TLE epoch. This behavior is intentional and does not require special handling: the system's staleness path is the correct response to an object for which no fresh observation is available.

Monitoring of this object is retained in the catalog specifically as a demonstration that the system surfaces fragmentation-related gaps in observation coverage rather than silently dropping the object. When fragment NORAD IDs are published, they can be added to `catalog.json` without code changes.

---

## 6. Frontend Architecture

### 6.1 Single-page structure and CDN dependencies

The frontend is a single HTML file (`frontend/index.html`) that imports ES2022 modules from `frontend/src/`. There is no build step. All third-party libraries are loaded from CDN:

- **CesiumJS 1.114** — 3D globe, entity API, `Cesium.Viewer`, `Cesium.Cartesian3`
- **D3.js v7** — SVG-based residual timeline and NIS history charts

The Cesium Ion access token is not hardcoded. `main.js` fetches `GET /config` on startup to retrieve `cesium_ion_token` from the backend (which reads `CESIUM_ION_TOKEN` from the environment) and passes it to `initGlobe()`. This resolves TD-018.

### 6.2 Real-time telemetry dashboard layout

The layout is an operator-facing telemetry dashboard, not a demo toy. Header, globe, and side panel are arranged for continuous passive monitoring:

```
+--------------------------------------------------------------------+
| ne-body SSA Platform — Continuous Monitoring & Prediction          |
|                                          [LIVE] [ 75 TRACKED] [MUTE]|
+--------------------------------------------------------------------+
|                                               |                    |
|                                               |   ALERT PANEL      |
|                                               |   (fills full      |
|                                               |    side-panel      |
|                                               |    height when     |
|          CESIUM GLOBE                         |    charts hidden)  |
|          (fills remaining viewport)           |                    |
|                                               |                    |
|   [Object Info Panel overlay, upper-left]     |                    |
|                                               |--------------------|
|                                               |   RESIDUALS / NIS  |
|                                               |   (slides in when  |
|                                               |    selected object |
|                                               |    has anomaly)    |
+--------------------------------------------------------------------+
```

**Header elements** (left-to-right layout excluding the title):
- **WebSocket status indicator** (`#ws-status`): `LIVE` (green) when `socket.onopen` fires; `RECONNECTING` (amber) while `_scheduleReconnect` is pending. Updated by `connectWebSocket()`.
- **Tracked-object counter** (`#tracked-count`): shows `N TRACKED` where N = `latestStateMap.size`. Updated on every `state_update` message, on every staleness-driven removal, and at `_seedFromCatalog()` completion. The value corresponds to the number of catalog objects for which a fresh-epoch (within the 28-day window) state has been received since app start.
- **Mute toggle**: toggles `setAlertSoundMuted(bool)`; when active, suppresses the audio alarm branch only. Visual flash is unaffected by mute state.

**Side panel** (width 380 px):
- **Alert panel** (`#alert-panel`): scrollable feed of alert cards. When the residual chart panel is collapsed, the alert panel fills the full side-panel height via `flex: 1; min-height: 200px`.
- **Residual / NIS chart panel** (`#residual-chart`): collapsed by default (`max-height: 0; opacity: 0`). Expands to `max-height: 400px` via CSS transition (300 ms) under the conditions in Section 6.3.

**Globe area**: fills all horizontal space not consumed by the side panel. The object info panel overlay in the upper-left shows details for the currently selected object without occluding the globe.

### 6.3 Event-driven chart visibility

The residual / NIS chart panel is hidden by default and shown only when the operator selects an object that has an anomaly of interest. Visibility is controlled by `_setChartVisible(visible)` in `main.js`:

| Event | Chart visibility action |
|-------|-------------------------|
| Globe click on object with active anomaly | `_setChartVisible(true)` |
| Globe click on object with no anomaly | `_setChartVisible(false)` |
| Alert card click (any status: active, recalibrating, resolved) | `_setChartVisible(true)` |
| Incoming `anomaly` message for the currently selected object | `_setChartVisible(true)` |
| Currently selected object's recalibration alert resolves to `resolved` | `_setChartVisible(false)` |
| Selection cleared | `_setChartVisible(false)` |
| App startup | Collapsed; no object auto-selected |

After the CSS transition completes, `transitionend` (filtered to `propertyName === 'max-height'`) calls the newly exported `residuals.resizeChart(chartState)` to force a D3 redraw at the correct dimensions. Without this step, D3 would render at the initial collapsed-height dimensions.

### 6.4 Object selection and camera fly-to

Object selection is unified across two entry points in `main.js`:

1. **Globe click** (CesiumJS screen-space event handler in `globe.js`): resolves the picked entity to a NORAD ID, calls `selectObject(chartState, noradId)`, displays the object info panel via `_showObjectInfoPanel(noradId)`, sets chart visibility via `_setChartVisible(_hasAnyAnomaly(noradId))`, and calls `flyToObject(viewer, noradId)` which issues a `viewer.camera.flyTo` to the object's current ECEF position with a smooth transition.
2. **Alert card click** (handler in `alerts.js` invoked via the callback passed into `addAlert()`): same sequence — selects the object, shows info panel, sets chart visible unconditionally, fetches and draws the back-and-forward propagated track, and flies the camera to the object.

Alert-driven selection is the primary operator workflow during an anomaly event: an audio alarm and visual flash fire, the operator clicks the alert card, and the globe camera flies to the affected object with charts and track already rendered.

### 6.5 28-day TLE staleness filter (frontend)

As described in Section 4 Step 11, `main.js` enforces a 28-day freshness window on all WebSocket messages before rendering. The rule is applied in `routeMessage()` for `state_update` and `recalibration` types. Stale entities are removed from the `entityMap`, `ellipsoidMap`, and `latestStateMap`, and the tracked-object counter decrements. If a subsequent fresh TLE arrives for the same object, normal rendering resumes and the entity is recreated on the next `updateSatellitePosition` call.

The filter is purely a frontend concern. The backend does not enforce staleness; all cached TLEs remain in `tle_catalog`, all historical alerts remain queryable, and `GET /catalog` returns all objects. The staleness filter is a visualization-layer quality control against stale-epoch propagation artifacts; it is not a data-retention policy.

### 6.6 Audio and visual anomaly alerting

Two subsystems deliver obtrusive operator alerts on anomaly detection. Both fire from the `anomaly` branch in `routeMessage()`, and only from that branch (they do not fire on reconnect-seeded alerts).

**Audio alarm (`frontend/src/alertsound.js`)** — Web Audio API implementation:
- Creates an `AudioContext` in suspended state at app init; resumes on first user interaction (click) to comply with browser autoplay policy.
- `triggerAlertSound()` plays three square-wave beeps at **660 Hz, 880 Hz, 1100 Hz** — a rising tone sequence — through an `OscillatorNode` connected to a `GainNode`. Each beep has a short attack / release envelope to avoid clipping.
- Debounced with a 2-second cooldown: repeated anomaly messages within 2 seconds of the last alarm produce no additional sound. The cooldown is distinct from the mute state.
- `setAlertSoundMuted(bool)` toggles a module-level mute flag. When muted, `triggerAlertSound()` is a no-op. The mute toggle is bound to the header mute button.
- Gracefully degrades if `AudioContext` is unavailable in the host browser; a warning is logged once and the visual flash continues to fire.

**Visual flash (`frontend/src/alertflash.js`)** — fullscreen overlay implementation:
- `triggerAlertFlash(objectName, anomalyType)` creates a fixed-position div covering the full viewport with a red translucent background and a central text block displaying the object name (from `nameMap`) and the anomaly type (`maneuver` / `drag_anomaly` / `filter_divergence` / `unknown`).
- The overlay animates from opacity 0 to peak and fades back to 0 over approximately 3 seconds via CSS transition, then is removed from the DOM.
- If a subsequent `triggerAlertFlash` call arrives while an overlay is still visible, the existing overlay is reset rather than stacked.
- The flash is not suppressed by the audio mute toggle; visual alerting is always on.

Both subsystems are intentionally obtrusive: anomaly events are operationally significant and must not be missable by an operator who has looked away from the globe. The 2-second audio cooldown and the single-overlay flash semantics prevent alarm fatigue during a burst of anomalies within the same processing cycle.

### 6.7 Globe entity management

`globe.js` uses the CesiumJS Entity API directly rather than CZML DataSource (deviation from the original architecture specification; documented as TD-024). At POC scale (up to 75 objects), the entity API produces identical visual output with simpler code and no CZML serialization overhead. CZML DataSource is the recommended upgrade for production to enable server-side timeline control.

Two `Map` instances track live entities:
- `entityMap`: `Map<norad_id, Cesium.Entity>` — satellite billboard entities
- `ellipsoidMap`: `Map<norad_id, Cesium.Entity>` — uncertainty ellipsoid entities

Entity positions are updated via `entity.position = new Cesium.ConstantPositionProperty(cartesian3)`. The ECI→ECEF conversion applies the GMST rotation at the observation epoch, producing an instantaneous ECEF position. This is correct for the real-time display use case (current position); historical track rendering uses per-point epoch GMST rotation via the same `eciToEcefCartesian3()` function. `removeSatelliteEntity(viewer, norad_id)` removes both the billboard and the uncertainty ellipsoid from the viewer and from the two tracking maps; this is called from the 28-day staleness filter path and on explicit cleanup.

Confidence color mapping (F-051): confidence > 0.85 → `Cesium.Color.LIME`; 0.60–0.85 → `Cesium.Color.ORANGE`; < 0.60 → `Cesium.Color.RED`. Conjunction risk overrides: first-order → `Cesium.Color.RED`; second-order → `Cesium.Color.YELLOW`. Risk overrides are cleared on the next `state_update` for the anomalous object (auto-clear logic in `routeMessage`).

### 6.8 D3 chart update pattern

The residuals chart (`residuals.js`) maintains an in-memory time-series array per NORAD ID. On each `appendResidualDataPoint()` call, the new point is appended, the array is trimmed to the most recent 200 points, and the SVG is re-rendered with a D3 linear scale update. Anomaly markers are added as vertical lines at the detection epoch via `addAnomalyMarker()`. The chart only renders data for the currently selected NORAD ID; switching selection via `selectObject()` replaces the series without fetching from the server. The exported `resizeChart(chartState)` helper forces a full redraw at the current SVG container dimensions and is invoked by `main.js` after the chart panel expands from its collapsed state.

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
| `state_update` | Each normal UKF update cycle | `globe.updateSatellitePosition`, `globe.updateUncertaintyEllipsoid`, `residuals.appendResidualDataPoint`, `main._updateTrackedCount`, 28-day staleness filter |
| `anomaly` | NIS exceeds chi-squared threshold (after two-cycle confirmation for active satellites) | `globe.highlightAnomaly`, `alerts.addAlert`, `residuals.addAnomalyMarker`, `alertsound.triggerAlertSound`, `alertflash.triggerAlertFlash`, chart-panel auto-expand if selected |
| `recalibration` | Filter re-initialization after anomaly classification | `globe.updateSatellitePosition`, `globe.updateUncertaintyEllipsoid`, `alerts.updateAlertStatus`, 28-day staleness filter |
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
SPACETRACK_USER   — Space-Track.org account email (required)
SPACETRACK_PASS   — Space-Track.org account password (required)
CESIUM_ION_TOKEN  — Cesium Ion access token for globe imagery (required)
N2YO_API_KEY      — N2YO API key for the supplemental TLE fallback (optional;
                    fallback is skipped silently when unset)
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
- **HTTP 429 handling:** `ingest.py` does not implement retry logic for Space-Track HTTP 429 (rate limit exceeded) responses. Production ingest requires exponential backoff with jitter. The N2YO path handles HTTP 429 as a normal `None`-return per-object failure and does not retry.
- **TLS:** All connections must use TLS in production. The current configuration assumes localhost HTTP.
- **Database:** SQLite is appropriate for a single-node POC. Multi-node deployments require a client-server database (e.g., PostgreSQL) with appropriate connection pooling.

---

## 9. Known Architectural Constraints

The following constraints are acknowledged characteristics of the POC implementation, not defects. They are documented to enable accurate technical evaluation and to define the scope of the production engineering effort.

**Single-process asyncio, no per-object parallelism.** The processing loop iterates catalog objects sequentially on the main asyncio event loop. Blocking numpy and SQLite operations are performed synchronously. At 75 objects with ~150 ms per object, a full catalog pass completes in approximately 11 seconds — acceptable within the 1800-second interval. For catalogs beyond ~500 objects, this approach would cause processing lag, and `ThreadPoolExecutor`-based parallelism would be required.

**In-memory filter state lost on restart.** `app.state.filter_states` is not persisted. On uvicorn restart, all filters reinitialize cold from the most recent cached TLE. Convergence to a well-calibrated state estimate requires 2–5 observation cycles (60–150 minutes at nominal poll rates). The `state_history` table contains the information needed to reconstruct filter state on restart; this reconstruction is not yet implemented.

**SGP4 sigma point collapse.** Because SGP4 is used as the UKF process model and all 13 sigma points receive the same deterministic SGP4 trajectory output, the covariance forecast during the predict step is driven entirely by the additive Q matrix. The sigma-point spread through the process model, which is the UKF's primary advantage over the EKF for nonlinear systems, is not exercised. Post-POC replacement with a numerical integrator (Runge-Kutta + J2/J4 perturbations) as the process model is documented as POST-003.

**Measurement noise R is hand-calibrated and source-agnostic.** The `DEFAULT_R` position variance of 900 km² (30 km 1-sigma) is calibrated against observed ISS TLE-to-TLE prediction error over a 30-minute interval. This value is applied uniformly to all object classes and to TLEs from both Space-Track and N2YO, even though the `source` column is present and queryable. Adaptive noise estimation (adjusting R based on observed innovation statistics) and per-source R tuning are documented as POST-002.

**Drag anomaly classifier uses ECI velocity residual as along-track proxy.** The proper decomposition requires the object's actual velocity vector to define the RSW (Radial-Along-Track-Cross-Track) frame. The current heuristic uses the velocity residual direction as a surrogate, which is an ECI simplification that produces incorrect results when the residual direction is not aligned with the orbital track. Post-POC RSW frame decomposition is documented as a tech debt item.

**No WebSocket authentication.** The `/ws/live` endpoint accepts any connection up to the `MAX_WS_CONNECTIONS = 20` cap. No bearer token or session credential is required. Acceptable for local demo; not suitable for any networked deployment.

**Cesium Ion imagery requires network access.** The CesiumJS globe uses Cesium Ion default imagery, which requires an active internet connection and a valid `CESIUM_ION_TOKEN`. For fully offline demo operation, a `SingleTileImageryProvider` with a locally cached texture must be configured.

**28-day staleness filter is frontend-only.** The 28-day freshness window is enforced in `frontend/src/main.js` and nowhere else. The backend's `GET /catalog` endpoint returns all objects regardless of TLE age; the backend continues to process stale TLEs through the UKF until a fresh observation arrives. A frontend-reached operator who bypasses `main.js._isFreshEpoch()` (for example by reading `GET /catalog` directly) will see stale objects. This is acceptable for the POC because the dashboard is the sole operator interface.

**N2YO fallback is best-effort and non-authoritative.** N2YO is consulted only after the Space-Track fetch completes and only for objects with missing or > 7-day-old Space-Track TLEs, capped at 50 per cycle. N2YO failures (HTTP error, rate limit, malformed body, satid mismatch) produce `None` per-object returns that are silently skipped. The system cannot distinguish between an object with no fresh TLE available anywhere versus one with a transient N2YO failure; in either case the most recent cached TLE remains in use until a fresher one arrives from either source.

**ITAR compliance scope.** Space-Track.org and N2YO data are both publicly releasable under their respective terms of service. The Space-Track account is registered under ITAR-awareness terms at account creation. All data provenance in this POC derives from these two sources, tagged in the `tle_catalog.source` column. No classified or Controlled Unclassified Information (CUI) data is ingested. The `ingest.py` module is the sole point of external data acquisition; no other module initiates network requests to external systems.
