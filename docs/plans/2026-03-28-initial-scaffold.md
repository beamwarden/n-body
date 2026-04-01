# Implementation Plan: Initial Project Scaffold
Date: 2026-03-28
Status: Approved

## Summary

Create the complete directory structure and stub files for the ne-body SSA platform as defined in CLAUDE.md and docs/architecture.md. Every backend Python module gets stub functions with full type annotations and docstrings; every frontend JS module gets exported function shells with JSDoc; dependency manifests are created. All function bodies raise NotImplementedError (Python) or throw Error (JS). This scaffold establishes the contract surface that all subsequent implementation plans build against.

## Requirements addressed

This plan does not implement any functional requirement to completion, but it establishes the file structure and public API surface required by all of the following:

- **F-001 through F-006** — ingest.py stubs
- **F-010 through F-013** — propagator.py stubs
- **F-020 through F-025** — kalman.py stubs
- **F-030 through F-035** — anomaly.py stubs
- **F-040 through F-044** — main.py stubs (API endpoints)
- **F-050 through F-056** — frontend JS module stubs
- **F-060 through F-063** — scripts stubs
- **NF-030** — type annotations and docstrings on all public functions (satisfied by scaffold convention)
- **NF-040** — no credentials in source (enforced by using os.environ in stubs)
- **C-003** — frontend has no build step; package.json is CDN-only
- **C-004** — Python 3.11+ enforced in requirements.txt

## Files affected

All files below are **new creations** -- nothing in the repository exists yet beyond CLAUDE.md, README.md, docs/architecture.md, docs/requirements.md, and the .claude/agents/ directory.

### Backend
- `backend/__init__.py` — empty package marker
- `backend/main.py` — FastAPI app, REST endpoints, WebSocket endpoint
- `backend/ingest.py` — Space-Track.org TLE polling, validation, caching
- `backend/propagator.py` — SGP4 propagation, TLE to ECI state vector
- `backend/kalman.py` — UKF state estimation per object
- `backend/anomaly.py` — NIS-based anomaly detection and classification
- `backend/requirements.txt` — Python dependencies

### Frontend
- `frontend/index.html` — single-page app shell loading CesiumJS and D3 from CDN
- `frontend/src/main.js` — app entry point, WebSocket client
- `frontend/src/globe.js` — CesiumJS 3D orbital view
- `frontend/src/residuals.js` — D3 residual timeline chart
- `frontend/src/alerts.js` — anomaly alert panel
- `frontend/package.json` — metadata only (no build step)

### Scripts
- `scripts/replay.py` — historical TLE replay for demos
- `scripts/seed_maneuver.py` — synthetic maneuver injection

### Data
- `data/catalog/.gitkeep` — placeholder for cached TLE snapshots

### Tests
- `tests/__init__.py` — empty package marker
- `tests/test_propagator.py` — propagator test stubs
- `tests/test_kalman.py` — kalman test stubs
- `tests/test_anomaly.py` — anomaly detection test stubs
- `tests/test_ingest.py` — ingest test stubs
- `tests/test_main.py` — API endpoint test stubs

## Data flow changes

No data flow changes -- this is the initial creation. The data flow defined in architecture.md Section 4 is the target. This scaffold establishes the module boundaries and function signatures that implement that flow:

```
Space-Track.org --> ingest.py (fetch_tles, validate_tle, cache_tles)
                       |
                       v
                  propagator.py (propagate_tle)
                       |
                       v
                   kalman.py (init_filter, predict, update, get_state)
                       |
                       v
                  anomaly.py (evaluate_nis, classify_anomaly, trigger_recalibration)
                       |
                       v
                   main.py (GET /catalog, GET /object/{norad_id}/history, WS /ws/live)
                       |
                       v
                  Browser (globe.js, residuals.js, alerts.js)
```

## Implementation steps

### Phase 1: Directory structure and empty markers

1. **Create backend package directory** (`backend/__init__.py`)
   - Action: Create `backend/` directory with empty `__init__.py`
   - Why: Establishes Python package for imports
   - Dependencies: none
   - Risk: Low

2. **Create frontend directory structure** (`frontend/src/`)
   - Action: Create `frontend/` and `frontend/src/` directories
   - Why: Houses all frontend assets
   - Dependencies: none
   - Risk: Low

3. **Create scripts directory** (`scripts/`)
   - Action: Create `scripts/` directory
   - Why: Houses demo and utility scripts
   - Dependencies: none
   - Risk: Low

4. **Create data directory with gitkeep** (`data/catalog/.gitkeep`)
   - Action: Create `data/catalog/` directory with empty `.gitkeep` file
   - Why: Ensures the TLE cache directory exists in git without committing data files
   - Dependencies: none
   - Risk: Low

5. **Create tests package directory** (`tests/__init__.py`)
   - Action: Create `tests/` directory with empty `__init__.py`
   - Why: Establishes test package for pytest discovery
   - Dependencies: none
   - Risk: Low

### Phase 2: Backend dependency manifest

6. **Create backend/requirements.txt** (`backend/requirements.txt`)
   - Action: Create file with the following pinned dependencies (use compatible-release pins):
     ```
     fastapi>=0.104.0
     uvicorn[standard]>=0.24.0
     sgp4>=2.22
     filterpy>=1.4.5
     numpy>=1.26.0
     scipy>=1.11.0
     httpx>=0.25.0
     mypy>=1.7.0
     pytest>=7.4.0
     pytest-asyncio>=0.23.0
     ```
   - Why: Matches tech stack in CLAUDE.md. sqlite3 is stdlib, not listed. Dev tools (mypy, pytest) included for validation workflow.
   - Dependencies: none
   - Risk: Low

### Phase 3: Backend module stubs

7. **Create backend/ingest.py** (`backend/ingest.py`)
   - Action: Create module with the following stub functions. All functions must have type hints and docstrings. Bodies must be `raise NotImplementedError("not implemented")`. Module-level docstring must state: "Sole interface to Space-Track.org. No other module may call the Space-Track API."
   - Stub functions:
     ```python
     import datetime
     import sqlite3
     from typing import Optional

     POLL_INTERVAL_S: int = 1800  # 30 minutes, per F-002

     async def authenticate() -> str:
         """Authenticate with Space-Track.org using credentials from environment variables.

         Reads SPACETRACK_USER and SPACETRACK_PASS from os.environ.
         Returns a session cookie string for subsequent requests.

         Raises:
             EnvironmentError: If credentials are not set.
             httpx.HTTPStatusError: If authentication fails.
         """

     async def fetch_tles(norad_ids: list[int], session_cookie: str) -> list[dict]:
         """Fetch current TLEs for the given NORAD IDs from Space-Track.org.

         Args:
             norad_ids: List of NORAD catalog IDs to retrieve.
             session_cookie: Valid session cookie from authenticate().

         Returns:
             List of dicts with keys: norad_id, epoch_utc, tle_line1, tle_line2.
         """

     def validate_tle(tle_line1: str, tle_line2: str) -> bool:
         """Validate TLE checksum integrity for both lines.

         Args:
             tle_line1: First line of the TLE.
             tle_line2: Second line of the TLE.

         Returns:
             True if both lines pass checksum validation, False otherwise.
         """

     def cache_tles(db: sqlite3.Connection, tles: list[dict], fetched_at_utc: datetime.datetime) -> int:
         """Write validated TLEs to the local SQLite catalog table.

         Table schema: (norad_id INTEGER, epoch_utc TEXT, tle_line1 TEXT, tle_line2 TEXT, fetched_at TEXT)

         Args:
             db: Open SQLite connection.
             tles: List of validated TLE dicts from fetch_tles().
             fetched_at_utc: UTC timestamp of the fetch operation.

         Returns:
             Number of rows inserted.
         """

     def get_cached_tles(db: sqlite3.Connection, norad_id: int, since_utc: Optional[datetime.datetime] = None) -> list[dict]:
         """Retrieve cached TLEs for a given NORAD ID from local storage.

         Args:
             db: Open SQLite connection.
             norad_id: NORAD catalog ID.
             since_utc: If provided, only return TLEs with epoch after this time.

         Returns:
             List of TLE dicts ordered by epoch_utc ascending.
         """

     def get_latest_tle(db: sqlite3.Connection, norad_id: int) -> Optional[dict]:
         """Retrieve the most recent cached TLE for a given NORAD ID.

         Args:
             db: Open SQLite connection.
             norad_id: NORAD catalog ID.

         Returns:
             TLE dict or None if no cached data exists.
         """

     def init_catalog_db(db_path: str) -> sqlite3.Connection:
         """Initialize the SQLite database and create the catalog table if it does not exist.

         Args:
             db_path: File path for the SQLite database.

         Returns:
             Open SQLite connection.
         """

     def load_catalog_config(config_path: str) -> list[int]:
         """Load the list of NORAD IDs to track from a configuration file.

         Args:
             config_path: Path to a JSON or text file listing NORAD IDs.

         Returns:
             List of NORAD catalog IDs.
         """
     ```
   - Why: Satisfies F-001 through F-006 interface. ingest.py is the sole Space-Track interface per architecture Section 3.1.
   - Dependencies: Phase 1 step 1
   - Risk: Low

8. **Create backend/propagator.py** (`backend/propagator.py`)
   - Action: Create module with stub functions. Module-level docstring must state: "Stateless SGP4 propagation engine. All outputs in ECI J2000, units km and km/s."
   - Stub functions:
     ```python
     import datetime
     import numpy as np
     from numpy.typing import NDArray

     def propagate_tle(
         tle_line1: str,
         tle_line2: str,
         epoch_utc: datetime.datetime,
     ) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
         """Propagate a TLE to the given epoch using SGP4.

         Args:
             tle_line1: First line of the TLE set.
             tle_line2: Second line of the TLE set.
             epoch_utc: Target UTC epoch for propagation.

         Returns:
             Tuple of (position_eci_km, velocity_eci_km_s) where each is a
             3-element numpy array. Position in km, velocity in km/s,
             both in ECI J2000 frame.

         Raises:
             ValueError: If TLE is malformed or propagation fails.
         """

     def tle_to_state_vector_eci_km(
         tle_line1: str,
         tle_line2: str,
         epoch_utc: datetime.datetime,
     ) -> NDArray[np.float64]:
         """Convert a TLE to a full 6-element state vector at the given epoch.

         Args:
             tle_line1: First line of the TLE set.
             tle_line2: Second line of the TLE set.
             epoch_utc: Target UTC epoch.

         Returns:
             6-element numpy array [x_km, y_km, z_km, vx_km_s, vy_km_s, vz_km_s]
             in ECI J2000 frame.
         """

     def tle_epoch_utc(tle_line1: str) -> datetime.datetime:
         """Extract the epoch from TLE line 1 as a UTC datetime.

         Args:
             tle_line1: First line of the TLE set.

         Returns:
             UTC-aware datetime representing the TLE epoch.
         """

     def eci_to_geodetic(
         position_eci_km: NDArray[np.float64],
         epoch_utc: datetime.datetime,
     ) -> tuple[float, float, float]:
         """Convert ECI position to geodetic coordinates (for API boundary only).

         NOTE: This function exists solely for the API response layer.
         Internal computations must remain in ECI J2000.

         Args:
             position_eci_km: 3-element ECI position vector in km.
             epoch_utc: UTC epoch (needed for Earth rotation angle).

         Returns:
             Tuple of (latitude_rad, longitude_rad, altitude_km).
         """
     ```
   - Why: Satisfies F-010 through F-013. Propagator is stateless per architecture Section 3.2. ECI J2000 output enforced. Unit suffixes on all variable names.
   - Dependencies: Phase 1 step 1
   - Risk: Low

9. **Create backend/kalman.py** (`backend/kalman.py`)
   - Action: Create module with stub functions. Module-level docstring must state: "UKF state estimation engine. One filter instance per tracked object. State vector: [x, y, z, vx, vy, vz] in ECI J2000 km and km/s."
   - Stub functions:
     ```python
     import datetime
     import numpy as np
     from numpy.typing import NDArray
     from typing import Optional

     # Chi-squared critical value for 6 DOF at p=0.05 (per F-030)
     CHI2_THRESHOLD_6DOF: float = 12.592

     def init_filter(
         state_eci_km: NDArray[np.float64],
         epoch_utc: datetime.datetime,
         process_noise_q: Optional[NDArray[np.float64]] = None,
         measurement_noise_r: Optional[NDArray[np.float64]] = None,
     ) -> dict:
         """Initialize a UKF instance for a single tracked object.

         Args:
             state_eci_km: 6-element initial state [x,y,z,vx,vy,vz] in ECI km and km/s.
             epoch_utc: UTC epoch of the initial state.
             process_noise_q: 6x6 process noise covariance matrix. If None, use default.
             measurement_noise_r: 6x6 measurement noise covariance matrix. If None, use default.

         Returns:
             Dict containing the filter object, last epoch, and metadata.
             Keys: 'filter', 'last_epoch_utc', 'norad_id', 'covariance_km2'.
         """

     def predict(
         filter_state: dict,
         target_epoch_utc: datetime.datetime,
         tle_line1: str,
         tle_line2: str,
     ) -> NDArray[np.float64]:
         """Run the UKF predict step: propagate state to target epoch.

         Uses SGP4 via propagator.py as the process model.

         Args:
             filter_state: Filter dict from init_filter or previous update.
             target_epoch_utc: UTC epoch to propagate to.
             tle_line1: Current TLE line 1 (for SGP4 propagation).
             tle_line2: Current TLE line 2 (for SGP4 propagation).

         Returns:
             6-element predicted state vector in ECI km and km/s.
         """

     def update(
         filter_state: dict,
         observation_eci_km: NDArray[np.float64],
         epoch_utc: datetime.datetime,
     ) -> dict:
         """Run the UKF update step: incorporate a new observation.

         Args:
             filter_state: Filter dict after predict step.
             observation_eci_km: 6-element observed state [x,y,z,vx,vy,vz] in ECI km and km/s.
             epoch_utc: UTC epoch of the observation.

         Returns:
             Updated filter state dict with new keys:
             'innovation_eci_km': residual vector,
             'nis': Normalized Innovation Squared scalar,
             'confidence': float 0-1.
         """

     def compute_nis(
         innovation_eci_km: NDArray[np.float64],
         innovation_covariance_km2: NDArray[np.float64],
     ) -> float:
         """Compute the Normalized Innovation Squared (NIS) statistic.

         NIS = y^T * S^{-1} * y where y is the innovation and S is
         the innovation covariance.

         Args:
             innovation_eci_km: 6-element innovation (residual) vector.
             innovation_covariance_km2: 6x6 innovation covariance matrix.

         Returns:
             NIS scalar value.
         """

     def get_state(filter_state: dict) -> dict:
         """Extract the current state estimate and metadata from the filter.

         Args:
             filter_state: Filter dict.

         Returns:
             Dict with keys: 'state_eci_km' (6-element array),
             'covariance_km2' (6x6 matrix), 'last_epoch_utc' (datetime),
             'confidence' (float 0-1).
         """

     def recalibrate(
         filter_state: dict,
         new_observation_eci_km: NDArray[np.float64],
         epoch_utc: datetime.datetime,
         inflation_factor: float = 10.0,
     ) -> dict:
         """Re-initialize the filter from a new observation with inflated covariance.

         Called when anomaly detection determines the filter has diverged.

         Args:
             filter_state: Current (diverged) filter dict.
             new_observation_eci_km: 6-element state from new TLE.
             epoch_utc: UTC epoch of the new observation.
             inflation_factor: Multiply default covariance by this factor.

         Returns:
             Fresh filter state dict with inflated initial uncertainty.
         """

     def compute_confidence(nis: float, nis_history: list[float]) -> float:
         """Compute a 0-1 confidence score from NIS value and recent history.

         Args:
             nis: Current NIS value.
             nis_history: List of recent NIS values (last N updates).

         Returns:
             Float between 0.0 (no confidence) and 1.0 (full confidence).
         """
     ```
   - Why: Satisfies F-020 through F-025. UKF per architecture Section 3.3. State vector in ECI J2000 with unit suffixes. NIS threshold from chi-squared table for 6 DOF at p=0.05 per F-030.
   - Dependencies: Phase 1 step 1
   - Risk: Low

10. **Create backend/anomaly.py** (`backend/anomaly.py`)
    - Action: Create module with stub functions. Module-level docstring must state: "Anomaly detection and classification. Interprets Kalman filter residuals as operationally meaningful events."
    - Stub functions:
      ```python
      import datetime
      import sqlite3
      from typing import Optional

      # Anomaly type constants
      ANOMALY_MANEUVER: str = "maneuver"
      ANOMALY_DRAG: str = "drag_anomaly"
      ANOMALY_DIVERGENCE: str = "filter_divergence"

      def evaluate_nis(
          nis: float,
          threshold: float = 12.592,
      ) -> bool:
          """Check if NIS exceeds the chi-squared critical value.

          Args:
              nis: Current NIS value from Kalman filter update.
              threshold: Chi-squared critical value (default: 6 DOF, p=0.05).

          Returns:
              True if NIS exceeds threshold (anomaly detected), False otherwise.
          """

      def classify_anomaly(
          norad_id: int,
          nis_history: list[float],
          innovation_eci_km: list[float],
          is_active_satellite: bool,
          threshold: float = 12.592,
      ) -> Optional[str]:
          """Classify the type of detected anomaly based on NIS pattern and innovation.

          Classification rules (per F-031, F-032):
          - maneuver: NIS elevated for >= 2 consecutive cycles AND object is active satellite
          - drag_anomaly: systematic along-track residual growth without cross-track signature
          - filter_divergence: catch-all for unclassified NIS threshold exceedances

          Args:
              norad_id: NORAD catalog ID.
              nis_history: Recent NIS values (most recent last).
              innovation_eci_km: Most recent 6-element innovation vector.
              is_active_satellite: Whether object is classified as active.
              threshold: NIS threshold for anomaly.

          Returns:
              Anomaly type string or None if no anomaly.
          """

      def trigger_recalibration(
          norad_id: int,
          anomaly_type: str,
          epoch_utc: datetime.datetime,
      ) -> dict:
          """Create a recalibration event record to be acted on by the filter.

          Args:
              norad_id: NORAD catalog ID.
              anomaly_type: One of ANOMALY_MANEUVER, ANOMALY_DRAG, ANOMALY_DIVERGENCE.
              epoch_utc: UTC epoch of anomaly detection.

          Returns:
              Dict with recalibration parameters: norad_id, anomaly_type, epoch_utc,
              inflation_factor, status ('pending').
          """

      def record_anomaly(
          db: sqlite3.Connection,
          norad_id: int,
          detection_epoch_utc: datetime.datetime,
          anomaly_type: str,
          nis_value: float,
      ) -> int:
          """Write an anomaly event to the SQLite alerts table.

          Args:
              db: Open SQLite connection.
              norad_id: NORAD catalog ID.
              detection_epoch_utc: UTC epoch of detection.
              anomaly_type: Classification string.
              nis_value: NIS value at detection.

          Returns:
              Row ID of the inserted record.
          """

      def record_recalibration_complete(
          db: sqlite3.Connection,
          anomaly_row_id: int,
          resolution_epoch_utc: datetime.datetime,
      ) -> None:
          """Update an anomaly record with recalibration completion time.

          Args:
              db: Open SQLite connection.
              anomaly_row_id: Row ID from record_anomaly.
              resolution_epoch_utc: UTC epoch when NIS returned to normal range.
          """

      def get_active_anomalies(db: sqlite3.Connection) -> list[dict]:
          """Retrieve all unresolved anomaly records.

          Args:
              db: Open SQLite connection.

          Returns:
              List of anomaly dicts with: norad_id, detection_epoch_utc,
              anomaly_type, nis_value, status.
          """
      ```
    - Why: Satisfies F-030 through F-035. Classification types per architecture Section 3.4.
    - Dependencies: Phase 1 step 1
    - Risk: Low

11. **Create backend/main.py** (`backend/main.py`)
    - Action: Create FastAPI application with stub endpoints. Module-level docstring must state: "FastAPI application. REST and WebSocket gateway for the ne-body SSA platform."
    - Stub contents:
      ```python
      import datetime
      from typing import Optional
      from fastapi import FastAPI, WebSocket, WebSocketDisconnect

      app = FastAPI(
          title="ne-body SSA Platform",
          description="Continuous Monitoring & Prediction Platform for Space Situational Awareness",
          version="0.1.0",
      )

      @app.get("/catalog")
      async def get_catalog() -> list[dict]:
          """Return the list of tracked objects with current state summary.

          Response includes: norad_id, name, last_update_epoch_utc,
          confidence score for each object.
          """

      @app.get("/object/{norad_id}/history")
      async def get_object_history(
          norad_id: int,
          since_utc: Optional[str] = None,
      ) -> list[dict]:
          """Return time-series of state updates and NIS values for one object.

          Args:
              norad_id: NORAD catalog ID.
              since_utc: Optional ISO-8601 UTC timestamp to filter history.

          Returns:
              List of state update records with: epoch_utc, eci_km,
              eci_km_s, nis, confidence.
          """

      @app.websocket("/ws/live")
      async def websocket_live(websocket: WebSocket) -> None:
          """WebSocket endpoint for real-time state updates and anomaly alerts.

          Message format conforms to architecture document Section 3.5:
          {type, norad_id, epoch_utc, eci_km, eci_km_s,
           covariance_diagonal_km2, nis, confidence, anomaly_type}
          """

      @app.on_event("startup")
      async def startup_event() -> None:
          """Initialize database, load catalog config, start ingest polling loop."""

      @app.on_event("shutdown")
      async def shutdown_event() -> None:
          """Clean up database connections and cancel background tasks."""
      ```
    - Why: Satisfies F-040 through F-044. Endpoint paths match architecture Section 3.5.
    - Dependencies: Phase 1 step 1
    - Risk: Low
    - **Note on deprecation:** FastAPI's `@app.on_event("startup")` and `@app.on_event("shutdown")` are deprecated in favor of lifespan context managers in newer FastAPI versions. The implementer should use the lifespan pattern if the installed FastAPI version supports it. For the scaffold, the on_event decorators are acceptable stubs since the bodies are NotImplementedError anyway.

### Phase 4: Frontend stubs

12. **Create frontend/index.html** (`frontend/index.html`)
    - Action: Create a minimal HTML5 page that:
      - Loads CesiumJS from CDN (`https://cesium.com/downloads/cesiumjs/releases/1.114/Build/Cesium/Cesium.js` and CSS)
      - Loads D3.js v7 from CDN (`https://d3js.org/d3.v7.min.js`)
      - Loads the four JS modules as ES2022 modules: `src/main.js`, `src/globe.js`, `src/residuals.js`, `src/alerts.js`
      - Contains placeholder `<div>` elements: `#cesium-container`, `#residual-chart`, `#alert-panel`
      - Reads CESIUM_ION_TOKEN from nowhere (placeholder comment noting it must come from configuration, not hardcoded)
    - Why: Satisfies C-003 (no build step, CDN only). Provides the DOM structure the JS modules attach to.
    - Dependencies: Phase 1 step 2
    - Risk: Low

13. **Create frontend/src/main.js** (`frontend/src/main.js`)
    - Action: Create ES2022 module with JSDoc and exported function stubs:
      ```javascript
      /**
       * @module main
       * @description Application entry point. Establishes WebSocket connection
       * to the backend and routes incoming messages to globe, residuals, and
       * alerts modules.
       */

      /**
       * Initialize the application: connect WebSocket, set up modules.
       * @returns {void}
       */
      export function initApp() {
          throw new Error('not implemented');
      }

      /**
       * Connect to the backend WebSocket endpoint.
       * @param {string} url - WebSocket URL (e.g., 'ws://localhost:8000/ws/live')
       * @returns {WebSocket} The WebSocket instance.
       */
      export function connectWebSocket(url) {
          throw new Error('not implemented');
      }

      /**
       * Route an incoming WebSocket message to the appropriate handler.
       * @param {Object} message - Parsed JSON message from backend.
       * @returns {void}
       */
      export function routeMessage(message) {
          throw new Error('not implemented');
      }

      /**
       * Fetch the current catalog from the REST endpoint.
       * @param {string} baseUrl - Backend base URL.
       * @returns {Promise<Array<Object>>} List of tracked objects.
       */
      export async function fetchCatalog(baseUrl) {
          throw new Error('not implemented');
      }
      ```
    - Why: Entry point for browser app per architecture Section 3.6.
    - Dependencies: Phase 1 step 2
    - Risk: Low

14. **Create frontend/src/globe.js** (`frontend/src/globe.js`)
    - Action: Create ES2022 module with JSDoc and exported function stubs:
      ```javascript
      /**
       * @module globe
       * @description CesiumJS 3D orbital view. Renders Earth globe with satellite
       * positions, ground tracks, uncertainty ellipsoids, and anomaly highlights.
       * All position data received in ECI J2000 km; conversion to Cesium's
       * Cartesian3 (ECEF meters) happens in this module.
       */

      /**
       * Initialize the CesiumJS viewer in the given container.
       * @param {string} containerId - DOM element ID for the Cesium viewer.
       * @param {string} ionToken - Cesium Ion access token.
       * @returns {Object} Cesium.Viewer instance.
       */
      export function initGlobe(containerId, ionToken) {
          throw new Error('not implemented');
      }

      /**
       * Update or add a satellite entity on the globe.
       * @param {Object} viewer - Cesium.Viewer instance.
       * @param {Object} stateUpdate - State update message from backend.
       * @returns {void}
       */
      export function updateSatellitePosition(viewer, stateUpdate) {
          throw new Error('not implemented');
      }

      /**
       * Render or update the uncertainty ellipsoid for a tracked object.
       * @param {Object} viewer - Cesium.Viewer instance.
       * @param {number} noradId - NORAD catalog ID.
       * @param {Array<number>} covarianceDiagonalKm2 - [sigma_x^2, sigma_y^2, sigma_z^2].
       * @param {Array<number>} positionEciKm - [x, y, z] in ECI km.
       * @returns {void}
       */
      export function updateUncertaintyEllipsoid(viewer, noradId, covarianceDiagonalKm2, positionEciKm) {
          throw new Error('not implemented');
      }

      /**
       * Highlight an object due to anomaly detection.
       * @param {Object} viewer - Cesium.Viewer instance.
       * @param {number} noradId - NORAD catalog ID.
       * @param {string} anomalyType - Type of anomaly.
       * @returns {void}
       */
      export function highlightAnomaly(viewer, noradId, anomalyType) {
          throw new Error('not implemented');
      }

      /**
       * Get the color for a confidence level.
       * Green > 0.85, amber 0.60-0.85, red < 0.60.
       * @param {number} confidence - Confidence score 0-1.
       * @returns {Object} Cesium.Color instance.
       */
      export function confidenceColor(confidence) {
          throw new Error('not implemented');
      }

      /**
       * Handle click selection of an object on the globe.
       * @param {Object} viewer - Cesium.Viewer instance.
       * @param {function} onSelect - Callback receiving the selected NORAD ID.
       * @returns {void}
       */
      export function setupSelectionHandler(viewer, onSelect) {
          throw new Error('not implemented');
      }
      ```
    - Why: Satisfies F-050 through F-053, F-056. Color coding thresholds from F-051.
    - Dependencies: Phase 1 step 2
    - Risk: Low

15. **Create frontend/src/residuals.js** (`frontend/src/residuals.js`)
    - Action: Create ES2022 module with JSDoc and exported function stubs:
      ```javascript
      /**
       * @module residuals
       * @description D3.js residual timeline charts. Renders per-object
       * residual magnitude, NIS score, and confidence time series.
       */

      /**
       * Initialize the residual chart in the given container.
       * @param {string} containerId - DOM element ID for the chart.
       * @returns {Object} Chart state object for subsequent updates.
       */
      export function initResidualChart(containerId) {
          throw new Error('not implemented');
      }

      /**
       * Append a new data point to the residual chart.
       * @param {Object} chartState - Chart state from initResidualChart.
       * @param {Object} stateUpdate - State update message from backend.
       * @returns {void}
       */
      export function appendResidualDataPoint(chartState, stateUpdate) {
          throw new Error('not implemented');
      }

      /**
       * Switch the chart to display data for a different object.
       * @param {Object} chartState - Chart state from initResidualChart.
       * @param {number} noradId - NORAD catalog ID to display.
       * @returns {void}
       */
      export function selectObject(chartState, noradId) {
          throw new Error('not implemented');
      }

      /**
       * Render the +/- 2-sigma expected noise band on the chart.
       * @param {Object} chartState - Chart state from initResidualChart.
       * @param {number} sigma2Km - 2-sigma threshold in km.
       * @returns {void}
       */
      export function renderNoiseBand(chartState, sigma2Km) {
          throw new Error('not implemented');
      }

      /**
       * Render the NIS threshold line on the NIS sub-chart.
       * @param {Object} chartState - Chart state from initResidualChart.
       * @param {number} threshold - Chi-squared critical value.
       * @returns {void}
       */
      export function renderNisThreshold(chartState, threshold) {
          throw new Error('not implemented');
      }
      ```
    - Why: Satisfies F-054, F-056. Cross-filter on click handled via selectObject.
    - Dependencies: Phase 1 step 2
    - Risk: Low

16. **Create frontend/src/alerts.js** (`frontend/src/alerts.js`)
    - Action: Create ES2022 module with JSDoc and exported function stubs:
      ```javascript
      /**
       * @module alerts
       * @description Anomaly alert panel. Receives anomaly events via WebSocket
       * and renders a scrolling feed of alerts with status tracking.
       */

      /**
       * Initialize the alert panel in the given container.
       * @param {string} containerId - DOM element ID for the alert panel.
       * @returns {Object} Panel state object for subsequent updates.
       */
      export function initAlertPanel(containerId) {
          throw new Error('not implemented');
      }

      /**
       * Add a new anomaly alert to the panel.
       * @param {Object} panelState - Panel state from initAlertPanel.
       * @param {Object} anomalyEvent - Anomaly message from backend WebSocket.
       * @returns {void}
       */
      export function addAlert(panelState, anomalyEvent) {
          throw new Error('not implemented');
      }

      /**
       * Update the status of an existing alert (e.g., recalibrating -> resolved).
       * @param {Object} panelState - Panel state from initAlertPanel.
       * @param {number} noradId - NORAD catalog ID.
       * @param {string} newStatus - New status: 'active' | 'recalibrating' | 'resolved'.
       * @param {string|null} resolutionTime - ISO-8601 UTC time of resolution, or null.
       * @returns {void}
       */
      export function updateAlertStatus(panelState, noradId, newStatus, resolutionTime) {
          throw new Error('not implemented');
      }

      /**
       * Clear all resolved alerts from the panel.
       * @param {Object} panelState - Panel state from initAlertPanel.
       * @returns {void}
       */
      export function clearResolved(panelState) {
          throw new Error('not implemented');
      }
      ```
    - Why: Satisfies F-055. Status values from architecture Section 3.6.3.
    - Dependencies: Phase 1 step 2
    - Risk: Low

### Phase 5: Frontend dependency manifest

17. **Create frontend/package.json** (`frontend/package.json`)
    - Action: Create a minimal package.json. Since C-003 requires no build step and CDN-only dependencies, this file serves as project metadata only. No `dependencies` or `devDependencies` that require npm install for runtime.
      ```json
      {
        "name": "ne-body-frontend",
        "version": "0.1.0",
        "description": "Browser frontend for the ne-body SSA platform. All runtime dependencies loaded from CDN.",
        "private": true,
        "scripts": {
          "serve": "python3 -m http.server 3000",
          "lint": "npx eslint src/"
        },
        "devDependencies": {
          "eslint": "^8.56.0"
        },
        "type": "module"
      }
      ```
    - Why: Satisfies C-003. Only eslint as optional dev dependency for linting.
    - Dependencies: Phase 1 step 2
    - Risk: Low

### Phase 6: Script stubs

18. **Create scripts/replay.py** (`scripts/replay.py`)
    - Action: Create script with argparse stub and main function:
      ```python
      """Historical TLE replay script for demos.

      Replays cached TLE data through the observe-predict-validate loop
      to simulate real-time operation without live Space-Track connectivity.

      Usage:
          python scripts/replay.py --hours 72
      """
      import argparse
      import datetime

      def replay_tles(hours: int, db_path: str, backend_url: str) -> None:
          """Replay cached TLEs over the specified time window.

          Args:
              hours: Number of hours of history to replay.
              db_path: Path to SQLite database with cached TLEs.
              backend_url: Backend API base URL.
          """
          raise NotImplementedError("not implemented")

      def main() -> None:
          """Parse arguments and run replay."""
          parser = argparse.ArgumentParser(description="Replay historical TLEs for demo")
          parser.add_argument("--hours", type=int, default=72, help="Hours of history to replay")
          parser.add_argument("--db", type=str, default="data/catalog/tle_cache.db", help="SQLite DB path")
          parser.add_argument("--backend", type=str, default="http://localhost:8000", help="Backend URL")
          args = parser.parse_args()
          replay_tles(args.hours, args.db, args.backend)

      if __name__ == "__main__":
          main()
      ```
    - Why: Satisfies F-060, F-063.
    - Dependencies: Phase 1 step 3
    - Risk: Low

19. **Create scripts/seed_maneuver.py** (`scripts/seed_maneuver.py`)
    - Action: Create script with argparse stub:
      ```python
      """Synthetic maneuver injection script for demos.

      Introduces a delta-V event into a selected object's cached TLE sequence
      to trigger anomaly detection through the Kalman filter pipeline.

      Usage:
          python scripts/seed_maneuver.py --object 25544 --delta-v 0.5
      """
      import argparse
      import datetime

      def inject_maneuver(
          norad_id: int,
          delta_v_m_s: float,
          direction: str,
          epoch_offset_s: float,
          db_path: str,
      ) -> None:
          """Inject a synthetic maneuver into the cached TLE sequence.

          Args:
              norad_id: NORAD catalog ID of the target object.
              delta_v_m_s: Delta-V magnitude in m/s.
              direction: One of 'along-track', 'cross-track', 'radial'.
              epoch_offset_s: Seconds from current time for the maneuver epoch.
              db_path: Path to SQLite database.
          """
          raise NotImplementedError("not implemented")

      def main() -> None:
          """Parse arguments and inject maneuver."""
          parser = argparse.ArgumentParser(description="Inject synthetic maneuver for demo")
          parser.add_argument("--object", type=int, required=True, help="NORAD ID")
          parser.add_argument("--delta-v", type=float, default=0.5, help="Delta-V in m/s")
          parser.add_argument("--direction", type=str, default="along-track",
                              choices=["along-track", "cross-track", "radial"],
                              help="Maneuver direction")
          parser.add_argument("--epoch-offset", type=float, default=0.0,
                              help="Seconds offset from now for maneuver epoch")
          parser.add_argument("--db", type=str, default="data/catalog/tle_cache.db",
                              help="SQLite DB path")
          args = parser.parse_args()
          inject_maneuver(args.object, args.delta_v, args.direction, args.epoch_offset, args.db)

      if __name__ == "__main__":
          main()
      ```
    - Why: Satisfies F-061, F-062, F-063.
    - Dependencies: Phase 1 step 3
    - Risk: Low

### Phase 7: Test stubs

20. **Create tests/test_propagator.py** (`tests/test_propagator.py`)
    - Action: Create pytest test stubs:
      ```python
      """Tests for backend/propagator.py."""
      import datetime
      import numpy as np
      import pytest

      def test_propagate_tle_returns_correct_shape() -> None:
          """propagate_tle returns two 3-element arrays."""
          pytest.skip("not implemented")

      def test_propagate_tle_rejects_malformed_tle() -> None:
          """propagate_tle raises ValueError on bad TLE."""
          pytest.skip("not implemented")

      def test_tle_to_state_vector_returns_6_elements() -> None:
          """tle_to_state_vector_eci_km returns a 6-element array."""
          pytest.skip("not implemented")

      def test_tle_epoch_utc_is_utc_aware() -> None:
          """tle_epoch_utc returns a timezone-aware UTC datetime."""
          pytest.skip("not implemented")

      def test_eci_to_geodetic_returns_lat_lon_alt() -> None:
          """eci_to_geodetic returns (lat_rad, lon_rad, alt_km)."""
          pytest.skip("not implemented")

      def test_propagation_output_is_eci_j2000() -> None:
          """Verify output frame is ECI J2000 by comparing with known values."""
          pytest.skip("not implemented")
      ```
    - Why: Satisfies NF-031 (70% coverage target on propagator).
    - Dependencies: Phase 1 step 5
    - Risk: Low

21. **Create tests/test_kalman.py** (`tests/test_kalman.py`)
    - Action: Create pytest test stubs:
      ```python
      """Tests for backend/kalman.py."""
      import datetime
      import numpy as np
      import pytest

      def test_init_filter_returns_valid_state() -> None:
          """init_filter returns a dict with required keys."""
          pytest.skip("not implemented")

      def test_predict_advances_epoch() -> None:
          """predict step moves the filter epoch forward."""
          pytest.skip("not implemented")

      def test_update_incorporates_observation() -> None:
          """update step modifies state based on observation."""
          pytest.skip("not implemented")

      def test_compute_nis_positive_definite() -> None:
          """NIS is always non-negative."""
          pytest.skip("not implemented")

      def test_nis_within_threshold_for_consistent_filter() -> None:
          """NIS stays below chi-squared threshold when filter is consistent."""
          pytest.skip("not implemented")

      def test_recalibrate_inflates_covariance() -> None:
          """recalibrate produces larger covariance than the prior state."""
          pytest.skip("not implemented")

      def test_confidence_decreases_with_high_nis() -> None:
          """compute_confidence returns lower score for higher NIS."""
          pytest.skip("not implemented")

      def test_state_vector_units_km() -> None:
          """Verify state vector is in km and km/s, not meters."""
          pytest.skip("not implemented")
      ```
    - Why: Satisfies NF-031 (70% coverage target on kalman).
    - Dependencies: Phase 1 step 5
    - Risk: Low

22. **Create tests/test_anomaly.py** (`tests/test_anomaly.py`)
    - Action: Create pytest test stubs:
      ```python
      """Tests for backend/anomaly.py."""
      import datetime
      import pytest

      def test_evaluate_nis_detects_threshold_exceedance() -> None:
          """evaluate_nis returns True when NIS > threshold."""
          pytest.skip("not implemented")

      def test_evaluate_nis_passes_normal_values() -> None:
          """evaluate_nis returns False when NIS <= threshold."""
          pytest.skip("not implemented")

      def test_classify_maneuver_requires_consecutive_elevated_nis() -> None:
          """Maneuver requires >= 2 consecutive NIS exceedances on active satellite."""
          pytest.skip("not implemented")

      def test_classify_divergence_for_inactive_object() -> None:
          """Non-active object with elevated NIS classifies as filter_divergence."""
          pytest.skip("not implemented")

      def test_record_anomaly_writes_to_db() -> None:
          """record_anomaly inserts a row into the alerts table."""
          pytest.skip("not implemented")

      def test_record_recalibration_complete_updates_duration() -> None:
          """record_recalibration_complete sets resolution time on the record."""
          pytest.skip("not implemented")
      ```
    - Why: Tests the anomaly detection logic per F-030 through F-035.
    - Dependencies: Phase 1 step 5
    - Risk: Low

23. **Create tests/test_ingest.py** (`tests/test_ingest.py`)
    - Action: Create pytest test stubs:
      ```python
      """Tests for backend/ingest.py."""
      import pytest

      def test_validate_tle_accepts_valid_tle() -> None:
          """validate_tle returns True for a correctly checksummed TLE."""
          pytest.skip("not implemented")

      def test_validate_tle_rejects_bad_checksum() -> None:
          """validate_tle returns False for a corrupted TLE."""
          pytest.skip("not implemented")

      def test_cache_tles_inserts_rows() -> None:
          """cache_tles writes the expected number of rows."""
          pytest.skip("not implemented")

      def test_get_cached_tles_respects_since_filter() -> None:
          """get_cached_tles only returns TLEs after since_utc."""
          pytest.skip("not implemented")

      def test_get_latest_tle_returns_most_recent() -> None:
          """get_latest_tle returns the TLE with the newest epoch."""
          pytest.skip("not implemented")

      def test_load_catalog_config_returns_norad_ids() -> None:
          """load_catalog_config parses a config file into a list of ints."""
          pytest.skip("not implemented")

      def test_init_catalog_db_creates_table() -> None:
          """init_catalog_db creates the catalog table if it does not exist."""
          pytest.skip("not implemented")
      ```
    - Why: Tests the ingest boundary per F-001 through F-006.
    - Dependencies: Phase 1 step 5
    - Risk: Low

24. **Create tests/test_main.py** (`tests/test_main.py`)
    - Action: Create pytest test stubs:
      ```python
      """Tests for backend/main.py API endpoints."""
      import pytest

      def test_get_catalog_returns_list() -> None:
          """GET /catalog returns a JSON list."""
          pytest.skip("not implemented")

      def test_get_object_history_returns_list() -> None:
          """GET /object/{norad_id}/history returns a JSON list."""
          pytest.skip("not implemented")

      def test_websocket_connects() -> None:
          """WebSocket /ws/live accepts a connection."""
          pytest.skip("not implemented")

      def test_websocket_receives_state_update() -> None:
          """Connected WebSocket receives state_update messages."""
          pytest.skip("not implemented")
      ```
    - Why: Tests the API surface per F-040 through F-044.
    - Dependencies: Phase 1 step 5
    - Risk: Low

### Phase 8: Data directory setup

25. **Create data/catalog/.gitkeep** (`data/catalog/.gitkeep`)
    - Action: Create empty file
    - Why: Preserves directory in git for TLE cache storage
    - Dependencies: none
    - Risk: Low

## Test strategy

- **Unit tests:** All test files in Phase 7 contain stub tests with `pytest.skip("not implemented")`. When the implementer builds out each module, the corresponding test stubs will be replaced with real assertions. The stubs document what must be tested.
- **Validation after scaffold:** The implementer should verify the following immediately after creating all files:
  1. `python -m py_compile backend/ingest.py` (and all other .py files) -- must pass syntax check
  2. `pytest tests/ -v` -- all tests must be collected and skipped (not errored)
  3. `frontend/index.html` opens in a browser without console errors (scripts will throw on call, but import should succeed)
- **Integration test:** Not applicable at scaffold stage. The scaffold establishes structure only.

## Risks and mitigations

- **Risk:** Stub function signatures may not perfectly match what the implementer needs once full logic is written. -- Mitigation: Signatures are derived directly from architecture.md and requirements.md. The implementer may add private helper functions but must preserve the public API defined here. Any signature changes require a plan amendment.

- **Risk:** CDN URLs for CesiumJS and D3 may become stale. -- Mitigation: Use stable release URLs with pinned versions. The implementer should verify CDN availability at implementation time.

- **Risk:** FastAPI on_event decorators are deprecated. -- Mitigation: Noted in Phase 3 step 11. The implementer should use the lifespan pattern when implementing the startup/shutdown logic.

- **Risk:** The `filterpy` library may have compatibility issues with newer numpy versions. -- Mitigation: The requirements.txt uses `>=` pins. The implementer should verify compatibility during the first real implementation phase and pin exact versions if needed.

## Open questions

**All resolved 2026-03-28:**

1. **Catalog configuration format:** ~~Human decision needed.~~ **RESOLVED:** JSON format with `{norad_id, name, object_class}` per object entry. `load_catalog_config` shall return `list[dict]` (not `list[int]`) — implementer must update stub signature accordingly.

2. **SQLite database location:** ~~Human decision needed.~~ **RESOLVED:** `NBODY_DB_PATH` environment variable with default fallback to `data/catalog/tle_cache.db`.

3. **CesiumJS version pinning:** ~~Human decision needed.~~ **RESOLVED:** Pin to 1.114 as used in scaffold. Do not upgrade without explicit approval.
