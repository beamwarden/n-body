"""FastAPI application. REST and WebSocket gateway for the n-body SSA platform.

Exposes:
  GET /catalog                      — list tracked objects with state summaries
  GET /object/{norad_id}/history    — time-series state and NIS for one object
  WebSocket /ws/live                — streaming state updates and anomaly alerts

WebSocket message format (architecture Section 3.5):
  {
    "type": "state_update" | "anomaly" | "recalibration",
    "norad_id": 25544,
    "epoch_utc": "2026-03-28T19:00:00Z",
    "eci_km": [x, y, z],
    "eci_km_s": [vx, vy, vz],
    "covariance_diagonal_km2": [sigma_x2, sigma_y2, sigma_z2],
    "nis": 2.3,
    "confidence": 0.94,
    "anomaly_type": null | "maneuver" | "drag_anomaly" | "filter_divergence"
  }
"""
# DEVIATION from plan docs/plans/2026-03-28-initial-scaffold.md step 11:
# Plan noted that @app.on_event("startup"/"shutdown") decorators are deprecated
# in favor of the lifespan context manager pattern. Per the plan's own mitigation
# note, the lifespan pattern is used here for the scaffold stubs. The bodies
# still raise NotImplementedError as specified. Flagged for planner awareness.
import asyncio
import contextlib
import datetime
import json
import logging
import os
import sqlite3
from typing import Optional

import numpy as np
from dotenv import load_dotenv

load_dotenv()  # load .env from repo root (no-op if file absent or vars already set)

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

import backend.anomaly as anomaly
import backend.ingest as ingest
import backend.kalman as kalman
import backend.propagator as propagator
import backend.processing as processing
from backend.processing import (
    _build_ws_message,
    _ensure_state_history_table,
    _insert_state_history_row,
    WS_TYPE_STATE_UPDATE,
    WS_TYPE_ANOMALY,
    WS_TYPE_RECALIBRATION,
)

logger = logging.getLogger(__name__)

# Maximum number of simultaneous WebSocket connections (resolved open question 2).
MAX_WS_CONNECTIONS: int = 20

# Valid WebSocket message types (F-043) — imported from processing.py.
_WS_TYPE_STATE_UPDATE: str = WS_TYPE_STATE_UPDATE
_WS_TYPE_ANOMALY: str = WS_TYPE_ANOMALY
_WS_TYPE_RECALIBRATION: str = WS_TYPE_RECALIBRATION


# ---------------------------------------------------------------------------
# Phase 1: Database schema helpers (implementations live in processing.py)
# ---------------------------------------------------------------------------


# _ensure_state_history_table, _insert_state_history_row, and _build_ws_message
# are imported from backend.processing above. The names are re-bound here as
# module-level names so any existing call sites in this file and in tests that
# import these names from main continue to resolve correctly.
# TECH DEBT TD-013: state_history table retained for post-POC history endpoint.


# ---------------------------------------------------------------------------
# Phase 2: WebSocket connection manager
# ---------------------------------------------------------------------------


class ConnectionManager:
    """Manages active WebSocket connections for the /ws/live endpoint.

    Supports up to MAX_WS_CONNECTIONS simultaneous connections (F-044).
    Broadcast failures on one client do not block others.
    """

    def __init__(self) -> None:
        """Initialize an empty connection set."""
        self._connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        """Accept a WebSocket connection and add it to the active set.

        Args:
            websocket: Incoming WebSocket connection.
        """
        await websocket.accept()
        self._connections.add(websocket)
        logger.info(
            "WebSocket client connected. Active connections: %d",
            len(self._connections),
        )

    async def disconnect(self, websocket: WebSocket) -> None:
        """Remove a WebSocket from the active set.

        Args:
            websocket: WebSocket to remove.
        """
        self._connections.discard(websocket)
        logger.info(
            "WebSocket client disconnected. Active connections: %d",
            len(self._connections),
        )

    async def broadcast(self, message: dict) -> None:
        """Send a JSON message to all active WebSocket connections.

        Per-client exceptions are caught and logged. Failed connections are
        removed from the active set so they do not block future broadcasts.

        Args:
            message: Dict to serialize as JSON and send.
        """
        message_text = json.dumps(message)
        failed: list[WebSocket] = []
        for ws in list(self._connections):
            try:
                await ws.send_text(message_text)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "WebSocket broadcast failed for client, removing: %s", exc
                )
                failed.append(ws)
        for ws in failed:
            self._connections.discard(ws)

    def active_count(self) -> int:
        """Return the number of currently active WebSocket connections.

        Returns:
            Integer count of active connections.
        """
        return len(self._connections)


# Module-level connection manager instance (accessed by endpoint and background task).
ws_manager = ConnectionManager()


# _build_ws_message is imported from backend.processing above.


# ---------------------------------------------------------------------------
# Phase 4: Background tasks
# ---------------------------------------------------------------------------


async def _ingest_loop_task(app: FastAPI) -> None:
    """Background asyncio task that runs the TLE ingestion loop.

    Wraps ingest.run_ingest_loop() in a retry loop so transient errors do not
    kill the background task. On CancelledError, exits cleanly. On any other
    exception, logs and waits 60 seconds before restarting (NF-010).

    Args:
        app: FastAPI application instance (for accessing app.state).
    """
    db_path: str = app.state.db_path
    catalog_config_path: str = app.state.catalog_config_path
    event_bus: asyncio.Queue = app.state.event_bus

    while True:
        try:
            await ingest.run_ingest_loop(
                db_path=db_path,
                catalog_config_path=catalog_config_path,
                event_bus=event_bus,
            )
        except asyncio.CancelledError:
            logger.info("_ingest_loop_task cancelled, shutting down cleanly.")
            return
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "_ingest_loop_task encountered an error: %s — restarting in 60s", exc
            )
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                logger.info("_ingest_loop_task cancelled during backoff sleep.")
                return


async def _processing_loop_task(app: FastAPI) -> None:
    """Background asyncio task that processes catalog_update events.

    Consumes events from the event bus. For each catalog_update event, iterates
    all catalog objects and runs the predict -> update -> anomaly -> broadcast
    pipeline for each.

    Coordinate frame: all state vectors passed between modules are ECI J2000 km.
    This is enforced by propagator.tle_to_state_vector_eci_km() which converts
    SGP4's TEME output before returning.

    Args:
        app: FastAPI application instance (for accessing app.state).
    """
    # POST-POC: parallelize UKF updates with ThreadPoolExecutor for large catalogs
    while True:
        try:
            event: dict = await app.state.event_bus.get()
        except asyncio.CancelledError:
            logger.info("_processing_loop_task cancelled, shutting down cleanly.")
            return

        if event.get("type") != "catalog_update":
            logger.debug("_processing_loop_task ignoring unknown event type: %s", event.get("type"))
            app.state.event_bus.task_done()
            continue

        logger.debug(
            "Processing catalog_update event: count=%s timestamp=%s",
            event.get("count"),
            event.get("timestamp_utc"),
        )

        db: sqlite3.Connection = app.state.db
        catalog_entries: list[dict] = app.state.catalog_entries
        filter_states: dict[int, dict] = app.state.filter_states

        for entry in catalog_entries:
            norad_id: int = int(entry["norad_id"])
            try:
                _process_single_object(
                    app=app,
                    db=db,
                    entry=entry,
                    norad_id=norad_id,
                    filter_states=filter_states,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                # Per plan step 8: per-object exception must not crash the loop.
                # Log at DEBUG for NotImplementedError (expected during development)
                # to avoid spam; ERROR for all other exceptions.
                if isinstance(exc, NotImplementedError):
                    logger.debug(
                        "NotImplementedError for NORAD %d (stub not yet implemented): %s",
                        norad_id,
                        exc,
                    )
                else:
                    logger.error(
                        "Error processing NORAD %d: %s", norad_id, exc, exc_info=True
                    )
                continue

        app.state.event_bus.task_done()


def _process_single_object(
    app: FastAPI,
    db: sqlite3.Connection,
    entry: dict,
    norad_id: int,
    filter_states: dict[int, dict],
) -> None:
    """Thin wrapper: look up latest TLE and delegate to processing.process_single_object.

    After processing, broadcasts each returned WebSocket message dict via ws_manager.
    Writes to state_history and alerts tables are handled by processing.process_single_object.

    This function is synchronous (called from the async processing loop).
    All blocking DB and numpy operations are acceptable for POC single-threaded
    asyncio, per the plan's concurrency model.

    Args:
        app: FastAPI application instance.
        db: Open SQLite connection (WAL mode, shared with REST endpoints).
        entry: Catalog entry dict with keys norad_id, name, object_class.
        norad_id: NORAD catalog ID (int, already extracted from entry).
        filter_states: Mutable dict of filter state dicts keyed by norad_id.
    """
    tle_record: Optional[dict] = ingest.get_latest_tle(db, norad_id)
    if tle_record is None:
        logger.warning("No cached TLE for NORAD %d — skipping", norad_id)
        return

    messages: list[dict] = processing.process_single_object(
        db=db,
        entry=entry,
        norad_id=norad_id,
        filter_states=filter_states,
        tle_record=tle_record,
    )

    for msg in messages:
        # Schedule broadcast as a fire-and-forget coroutine from sync context.
        asyncio.get_event_loop().create_task(ws_manager.broadcast(msg))


# ---------------------------------------------------------------------------
# Phase 3: Lifespan (startup and shutdown)
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown lifecycle.

    Startup: Initialize database, load catalog config, start ingest polling loop.
    Shutdown: Clean up database connections and cancel background tasks.
    """
    # --- STARTUP ---
    db_path: str = os.environ.get("NBODY_DB_PATH") or "data/catalog/tle_cache.db"
    catalog_config_path: str = (
        os.environ.get("NBODY_CATALOG_CONFIG") or "data/catalog/catalog.json"
    )

    logger.info("n-body startup: db_path=%s catalog=%s", db_path, catalog_config_path)

    db: sqlite3.Connection = ingest.init_catalog_db(db_path)
    logger.info("Catalog DB initialized.")

    _ensure_state_history_table(db)
    logger.info("state_history table ready.")

    try:
        anomaly.ensure_alerts_table(db)
        logger.info("alerts table ready.")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not ensure alerts table (anomaly.py may not yet be implemented): %s",
            exc,
        )

    try:
        catalog_entries: list[dict] = ingest.load_catalog_config(catalog_config_path)
        logger.info("Loaded %d catalog entries.", len(catalog_entries))
    except (FileNotFoundError, ValueError) as exc:
        logger.error(
            "Failed to load catalog config from %s: %s — starting with empty catalog.",
            catalog_config_path,
            exc,
        )
        catalog_entries = []

    event_bus: asyncio.Queue = asyncio.Queue(maxsize=10)

    # Store all application-scoped state on app.state.
    app.state.db = db
    app.state.db_path = db_path
    app.state.catalog_config_path = catalog_config_path
    app.state.catalog_entries = catalog_entries
    app.state.filter_states = {}
    app.state.event_bus = event_bus

    background_tasks: list[asyncio.Task] = [
        asyncio.create_task(_ingest_loop_task(app), name="ingest_loop"),
        asyncio.create_task(_processing_loop_task(app), name="processing_loop"),
    ]
    app.state.background_tasks = background_tasks
    logger.info("Background tasks started: ingest_loop, processing_loop.")

    yield

    # --- SHUTDOWN ---
    # DEVIATION from plan docs/plans/2026-03-28-main.md step 6:
    # Plan says "Close app.state.db". Using local variable `db` here instead
    # because test helpers may replace app.state.db with a test connection after
    # lifespan startup. Closing the local `db` ensures we always close the
    # connection opened during startup, preventing thread-affinity errors.
    # Functionally equivalent in production (nothing replaces app.state.db at
    # runtime). Flagged for planner review.
    logger.info("n-body shutdown: cancelling background tasks.")
    for task in app.state.background_tasks:
        task.cancel()
    await asyncio.gather(*app.state.background_tasks, return_exceptions=True)
    logger.info("Background tasks cancelled.")

    db.close()
    logger.info("Database connection closed. Shutdown complete.")


app = FastAPI(
    title="n-body SSA Platform",
    description="Continuous Monitoring & Prediction Platform for Space Situational Awareness",
    version="0.1.0",
    lifespan=lifespan,
)

# Allow the frontend dev server (port 3000) to call the backend (port 8000).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Phase 5: REST endpoints
# ---------------------------------------------------------------------------


@app.get("/config")
async def get_config() -> dict:
    """Return non-secret frontend configuration.

    Resolves TD-018: the CesiumJS Ion token is read from the CESIUM_ION_TOKEN
    environment variable and served to the frontend at runtime so it is never
    committed to source.

    Returns:
        Dict with key ``cesium_ion_token``. Empty string if env var is unset
        (CesiumJS will display a grey globe — visible configuration error).
    """
    cesium_ion_token: str = os.environ.get("CESIUM_ION_TOKEN", "")
    return {"cesium_ion_token": cesium_ion_token}


@app.get("/catalog")
async def get_catalog() -> list[dict]:
    """Return the list of tracked objects with current state summary and full state vectors.

    Response includes: norad_id, name, last_update_epoch_utc, confidence score,
    and full current state (eci_km, eci_km_s, covariance_diagonal_km2, nis,
    anomaly_flag, innovation_eci_km) for each object.

    F-040: Returns all catalog entries regardless of filter initialization state.
    Objects without a filter state (no TLE received yet) return confidence=null
    and null state vector fields.

    # NF-012: full state returned so reconnecting WebSocket clients can seed the globe
    """
    result: list[dict] = []
    filter_states: dict[int, dict] = app.state.filter_states
    db: sqlite3.Connection = app.state.db

    for entry in app.state.catalog_entries:
        norad_id: int = int(entry["norad_id"])
        name: str = entry.get("name", "")

        fs = filter_states.get(norad_id)
        if fs is not None:
            state = kalman.get_state(fs)
            epoch_dt: datetime.datetime = state["last_epoch_utc"]
            last_update_epoch_utc: Optional[str] = epoch_dt.strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            confidence: Optional[float] = float(state["confidence"])
            state_eci_km = state["state_eci_km"]
            cov_km2 = state["covariance_km2"]
            eci_km: Optional[list] = state_eci_km[:3].tolist()
            eci_km_s: Optional[list] = state_eci_km[3:].tolist()
            covariance_diagonal_km2: Optional[list] = [
                float(cov_km2[0, 0]),
                float(cov_km2[1, 1]),
                float(cov_km2[2, 2]),
            ]
            nis: Optional[float] = float(state["nis"])
            anomaly_flag: Optional[bool] = bool(state["anomaly_flag"])
            innovation_eci_km: Optional[list] = state["innovation_eci_km"].tolist()
        else:
            # No filter yet — try the cached TLE for last epoch.
            tle_record = ingest.get_latest_tle(db, norad_id)
            if tle_record is not None:
                last_update_epoch_utc = tle_record["epoch_utc"]
            else:
                last_update_epoch_utc = None
            confidence = None
            eci_km = None
            eci_km_s = None
            covariance_diagonal_km2 = None
            nis = None
            anomaly_flag = None
            innovation_eci_km = None

        result.append(
            {
                "norad_id": norad_id,
                "name": name,
                "object_class": entry.get("object_class", "unknown"),
                "last_update_epoch_utc": last_update_epoch_utc,
                "confidence": confidence,
                "eci_km": eci_km,
                "eci_km_s": eci_km_s,
                "covariance_diagonal_km2": covariance_diagonal_km2,
                "nis": nis,
                "anomaly_flag": anomaly_flag,
                "innovation_eci_km": innovation_eci_km,
            }
        )

    return result


@app.get("/object/{norad_id}/history")
async def get_object_history(
    norad_id: int,
    since_utc: Optional[str] = None,
) -> list[dict]:
    """Return time-series of state updates and NIS values for one object.

    # TECH DEBT TD-013: full state history table deferred to post-POC.
    # This endpoint returns last 100 records from the alerts table only.
    # The state_history table exists but is not served here.

    Args:
        norad_id: NORAD catalog ID.
        since_utc: Optional ISO-8601 UTC timestamp to filter history.

    Returns:
        List of alert records with: epoch_utc, anomaly_type, nis, confidence.

    Raises:
        HTTPException 404 if norad_id is not in the catalog.
    """
    # Validate norad_id is in catalog.
    catalog_ids = {int(e["norad_id"]) for e in app.state.catalog_entries}
    if norad_id not in catalog_ids:
        raise HTTPException(
            status_code=404,
            detail=f"NORAD ID {norad_id} not found in catalog.",
        )

    db: sqlite3.Connection = app.state.db

    if since_utc is not None:
        cursor = db.execute(
            """
            SELECT id, norad_id, detection_epoch_utc, anomaly_type, nis_value, status
            FROM alerts
            WHERE norad_id = ? AND detection_epoch_utc > ?
            ORDER BY detection_epoch_utc ASC
            LIMIT 100
            """,
            (norad_id, since_utc),
        )
    else:
        cursor = db.execute(
            """
            SELECT id, norad_id, detection_epoch_utc, anomaly_type, nis_value, status
            FROM alerts
            WHERE norad_id = ?
            ORDER BY detection_epoch_utc ASC
            LIMIT 100
            """,
            (norad_id,),
        )

    rows = cursor.fetchall()
    result: list[dict] = []
    for row in rows:
        row_id, row_norad_id, epoch_str, anomaly_type, nis_value, status = row
        result.append(
            {
                "id": row_id,
                "epoch_utc": epoch_str,
                "anomaly_type": anomaly_type,
                "nis": nis_value,
                "status": status,
            }
        )

    return result


@app.get("/object/{norad_id}/anomalies")
async def get_object_anomalies(norad_id: int) -> list[dict]:
    """Return anomaly history for one tracked object.

    Returns the 20 most recent anomaly events for the given NORAD ID, ordered
    newest-first. Includes resolution epoch and recalibration duration so the
    frontend can display resolved/unresolved status with timing.

    This endpoint returns a superset of what GET /object/{norad_id}/history
    returns: it includes resolution_epoch_utc and recalibration_duration_s,
    which the history endpoint omits to preserve its existing contract.

    Args:
        norad_id: NORAD catalog ID.

    Returns:
        List of anomaly event dicts with keys: id, norad_id,
        detection_epoch_utc, anomaly_type, nis_value,
        resolution_epoch_utc (nullable), recalibration_duration_s (nullable),
        status.

    Raises:
        HTTPException 404 if norad_id is not in the catalog.
    """
    # Validate norad_id is in catalog (same pattern as get_object_history).
    catalog_ids = {int(e["norad_id"]) for e in app.state.catalog_entries}
    if norad_id not in catalog_ids:
        raise HTTPException(
            status_code=404,
            detail=f"NORAD ID {norad_id} not found in catalog.",
        )

    db: sqlite3.Connection = app.state.db

    cursor = db.execute(
        """
        SELECT
            id,
            norad_id,
            detection_epoch_utc,
            anomaly_type,
            nis_value,
            resolution_epoch_utc,
            recalibration_duration_s,
            status
        FROM alerts
        WHERE norad_id = ?
        ORDER BY detection_epoch_utc DESC
        LIMIT 20
        """,
        (norad_id,),
    )

    rows = cursor.fetchall()
    result: list[dict] = []
    for row in rows:
        (
            row_id,
            row_norad_id,
            detection_epoch_utc,
            anomaly_type,
            nis_value,
            resolution_epoch_utc,
            recalibration_duration_s,
            status,
        ) = row
        result.append(
            {
                "id": row_id,
                "norad_id": row_norad_id,
                "detection_epoch_utc": detection_epoch_utc,
                "anomaly_type": anomaly_type,
                "nis_value": nis_value,
                "resolution_epoch_utc": resolution_epoch_utc,
                "recalibration_duration_s": recalibration_duration_s,
                "status": status,
            }
        )

    return result


@app.get("/object/{norad_id}/track")
async def get_object_track(
    norad_id: int,
    seconds_back: int = 1500,
    seconds_forward: int = 0,
    # DEVIATION from plan docs/plans/2026-03-29-history-tracks-cones.md step 2.1:
    # Plan decision 1 sets default step_s = 60 (not 30). Implemented as 60 here.
    # Tech debt entry TD-025 added for UI configurability (post-POC).
    step_s: int = 60,
) -> dict:
    """Return historical and predictive track points for one tracked object.

    Back-propagates and forward-propagates the latest cached TLE using SGP4
    to generate track points. Each point is in ECI J2000 km. The frontend
    converts to ECEF Cartesian3 using eciToEcefCartesian3() with the per-point
    epoch for correct GMST rotation.

    Forward track points include uncertainty_radius_km derived from filter
    covariance growth. If no filter state exists for the object, a default
    linear growth model is used (1 km + 0.5 km per 300s).

    Coordinate frame: ECI J2000 (GCRS). Same frame as all internal state vectors.
    Conversion to ECEF happens only in the frontend (globe.js), consistent with
    architecture section 3.2.

    Performance note: 100 SGP4 propagations + astropy TEME->GCRS transforms
    may take 500ms-1s. This is acceptable for user-initiated click actions.
    See plan docs/plans/2026-03-29-history-tracks-cones.md section 2.1 for
    discussion.

    Args:
        norad_id: NORAD catalog ID.
        seconds_back: Seconds into the past to back-propagate (default 1500).
        seconds_forward: Seconds into the future to forward-propagate (default 0).
        step_s: Time step between track points in seconds (default 60).

    Returns:
        Dict with: norad_id, reference_epoch_utc, step_s,
        backward_track (list of {epoch_utc, eci_km}),
        forward_track (list of {epoch_utc, eci_km, uncertainty_radius_km}).

    Raises:
        HTTPException 404 if norad_id is not in catalog or no TLE is cached.
    """
    # Validate norad_id is in catalog.
    catalog_ids = {int(e["norad_id"]) for e in app.state.catalog_entries}
    if norad_id not in catalog_ids:
        raise HTTPException(
            status_code=404,
            detail=f"NORAD ID {norad_id} not found in catalog.",
        )

    db: sqlite3.Connection = app.state.db

    # Retrieve latest cached TLE. Returns 404 if none.
    tle_record: Optional[dict] = ingest.get_latest_tle(db, norad_id)
    if tle_record is None:
        raise HTTPException(
            status_code=404,
            detail=f"No cached TLE for NORAD ID {norad_id}.",
        )

    tle_line1: str = tle_record["tle_line1"]
    tle_line2: str = tle_record["tle_line2"]

    # Determine reference epoch: prefer filter last_epoch_utc; fall back to TLE epoch.
    filter_states: dict[int, dict] = app.state.filter_states
    filter_state: Optional[dict] = filter_states.get(norad_id)

    if filter_state is not None:
        reference_epoch_utc: datetime.datetime = filter_state["last_epoch_utc"]
    else:
        reference_epoch_utc = propagator.tle_epoch_utc(tle_line1)

    # --- Backward track ---
    # Range: from -seconds_back up to (not including) 0, step step_s, then t=0.
    backward_offsets = list(range(-seconds_back, 0, step_s)) + [0]
    backward_track: list[dict] = []
    for t_s in backward_offsets:
        point_epoch = reference_epoch_utc + datetime.timedelta(seconds=t_s)
        try:
            position_eci_km, _ = propagator.propagate_tle(tle_line1, tle_line2, point_epoch)
            backward_track.append(
                {
                    "epoch_utc": point_epoch.isoformat(),
                    "eci_km": position_eci_km.tolist(),
                }
            )
        except ValueError as exc:
            logger.warning(
                "Track back-propagation failed for NORAD %d at t=%+d s: %s",
                norad_id, t_s, exc,
            )
            continue

    # --- Forward track ---
    # Range: step_s, 2*step_s, ..., seconds_forward (inclusive if divisible).
    forward_track: list[dict] = []
    if seconds_forward > 0:
        # Nominal update interval (seconds) used as the Q calibration reference.
        # Q was tuned assuming a 30-minute (1800s) update cycle.
        dt_nominal_s: float = 1800.0

        # Extract covariance and process noise from filter state (if available).
        if filter_state is not None:
            cov_km2: np.ndarray = filter_state["covariance_km2"]
            q_matrix: np.ndarray = filter_state["q_matrix"]
            has_filter: bool = True
        else:
            has_filter = False

        forward_offsets = range(step_s, seconds_forward + step_s, step_s)
        for t_s in forward_offsets:
            if t_s > seconds_forward:
                break
            point_epoch = reference_epoch_utc + datetime.timedelta(seconds=t_s)
            try:
                position_eci_km, _ = propagator.propagate_tle(tle_line1, tle_line2, point_epoch)
            except ValueError as exc:
                logger.warning(
                    "Track forward-propagation failed for NORAD %d at t=+%d s: %s",
                    norad_id, t_s, exc,
                )
                continue

            # Compute uncertainty radius at this forward time step.
            if has_filter:
                # Covariance growth: P_ii + Q_ii * (t / dt_nominal).
                # Linear approximation of unmodeled acceleration variance accumulation.
                # See plan docs/plans/2026-03-29-history-tracks-cones.md step 3.1.
                sigma2_grown = [
                    float(cov_km2[i, i]) + float(q_matrix[i, i]) * (t_s / dt_nominal_s)
                    for i in range(3)
                ]
                # 3-sigma radius from maximum position axis variance.
                # Clamped: minimum 1 km (visibility), maximum 500 km (prevent artifacts).
                radius_km: float = float(3.0 * np.sqrt(max(sigma2_grown)))
                radius_km = float(np.clip(radius_km, 1.0, 500.0))
            else:
                # Default growth: 1 km base + 0.5 km per 300 s (no filter state).
                radius_km = float(np.clip(1.0 + 0.5 * (t_s / 300.0), 1.0, 500.0))

            forward_track.append(
                {
                    "epoch_utc": point_epoch.isoformat(),
                    "eci_km": position_eci_km.tolist(),
                    "uncertainty_radius_km": radius_km,
                }
            )

    return {
        "norad_id": norad_id,
        "reference_epoch_utc": reference_epoch_utc.isoformat(),
        "step_s": step_s,
        "backward_track": backward_track,
        "forward_track": forward_track,
    }


# ---------------------------------------------------------------------------
# Admin endpoint: manual processing trigger (NF-023, seed_maneuver.py support)
# ---------------------------------------------------------------------------


@app.post("/admin/trigger-process")
async def admin_trigger_process() -> dict:
    """Force-run one processing cycle for all catalog objects.

    Reads the latest cached TLE for each object from the DB and runs the
    predict->update->anomaly->broadcast pipeline via processing.process_single_object.
    Returns the count of objects that were successfully processed (had a TLE and
    produced at least one WS message).

    Designed to be called by scripts/seed_maneuver.py after inserting a synthetic TLE
    so that the anomaly appears in the browser within 10 seconds (NF-023).

    # TECH DEBT TD-023: resolved — scripts implemented per plan docs/plans/2026-03-28-scripts.md

    Returns:
        Dict with key 'processed' (int) indicating objects with at least one message.
    """
    db: sqlite3.Connection = app.state.db
    catalog_entries: list[dict] = app.state.catalog_entries
    filter_states: dict[int, dict] = app.state.filter_states

    # Process ALL cached TLEs in chronological order so the filter covariance P
    # converges before the most-recent (possibly maneuver-injected) TLE is processed.
    # Collect all TLEs across all objects, sort by epoch, then process sequentially.
    all_tle_records: list[tuple[dict, dict]] = []  # (entry, tle_record)
    for entry in catalog_entries:
        norad_id: int = int(entry["norad_id"])
        tle_records = ingest.get_cached_tles(db, norad_id)
        for tle_record in tle_records:
            all_tle_records.append((entry, tle_record))

    # Sort globally by epoch so inter-object ordering is deterministic.
    all_tle_records.sort(key=lambda x: x[1]["epoch_utc"])

    processed_count: int = 0
    last_broadcast_norad: set[int] = set()
    for entry, tle_record in all_tle_records:
        norad_id = int(entry["norad_id"])
        try:
            messages: list[dict] = processing.process_single_object(
                db=db,
                entry=entry,
                norad_id=norad_id,
                filter_states=filter_states,
                tle_record=tle_record,
            )

            # Only broadcast the final message per object (the most recent state).
            # Intermediate updates converge P but don't need to hit the browser.
            is_last = (tle_record == ingest.get_latest_tle(db, norad_id))
            for msg in messages:
                if is_last or msg.get("type") == WS_TYPE_ANOMALY:
                    await ws_manager.broadcast(msg)

            if messages:
                last_broadcast_norad.add(norad_id)

        except Exception as exc:  # noqa: BLE001
            logger.error(
                "admin_trigger_process: error for NORAD %d @ %s: %s",
                norad_id, tle_record.get("epoch_utc"), exc, exc_info=True,
            )
            continue

    processed_count = len(last_broadcast_norad)

    logger.info("admin_trigger_process complete: processed=%d", processed_count)
    return {"processed": processed_count}


# ---------------------------------------------------------------------------
# Phase 6: WebSocket endpoint
# ---------------------------------------------------------------------------


@app.websocket("/ws/live")
async def websocket_live(websocket: WebSocket) -> None:
    """WebSocket endpoint for real-time state updates and anomaly alerts.

    On connect, enforces MAX_WS_CONNECTIONS cap; returns 503 if exceeded.
    Immediately sends the current state of all tracked objects (NF-012).
    Loops receiving keepalive messages; removes connection on disconnect.

    Message format conforms to architecture document Section 3.5:
    {type, norad_id, epoch_utc, eci_km, eci_km_s,
     covariance_diagonal_km2, nis, confidence, anomaly_type}
    """
    # Enforce connection cap before accepting (returns 503 to client).
    if ws_manager.active_count() >= MAX_WS_CONNECTIONS:
        await websocket.close(code=1013)  # 1013: Try Again Later
        logger.warning(
            "WebSocket connection refused: MAX_WS_CONNECTIONS (%d) reached.",
            MAX_WS_CONNECTIONS,
        )
        return

    await ws_manager.connect(websocket)
    try:
        # NF-012: Send current state of all objects immediately on connect.
        filter_states: dict[int, dict] = app.state.filter_states
        for norad_id, filter_state in list(filter_states.items()):
            try:
                msg = _build_ws_message(
                    norad_id=norad_id,
                    filter_state=filter_state,
                    message_type=_WS_TYPE_STATE_UPDATE,
                    anomaly_type=None,
                )
                await websocket.send_text(json.dumps(msg))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to send initial state for NORAD %d to new client: %s",
                    norad_id,
                    exc,
                )

        # Keepalive receive loop — content is ignored.
        while True:
            await websocket.receive_text()

    except WebSocketDisconnect:
        pass
    finally:
        await ws_manager.disconnect(websocket)
