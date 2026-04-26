"""FastAPI application. REST and WebSocket gateway for the ne-body SSA platform.

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

import numpy as np
from dotenv import load_dotenv

load_dotenv()  # load .env from repo root (no-op if file absent or vars already set)

# H-4: optional API key auth. Set NEBODY_API_KEY in .env to enable.
# If unset, all endpoints are unauthenticated (dev / local-demo default).
_API_KEY: str | None = os.environ.get("NEBODY_API_KEY") or None

# H-5: configurable CORS origins. Set NEBODY_ALLOWED_ORIGINS to a comma-separated
# list of allowed origins (e.g. "http://keep-0001.local:3000,http://localhost:3000").
# Falls back to ["*"] if unset so dev and existing deployments are unaffected.
_ALLOWED_ORIGINS: list[str] = (
    [o.strip() for o in os.environ.get("NEBODY_ALLOWED_ORIGINS", "").split(",") if o.strip()]
    or ["*"]
)

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

import backend.anomaly as anomaly
import backend.conjunction as conjunction
import backend.ingest as ingest
import backend.kalman as kalman
import backend.processing as processing
import backend.propagator as propagator
from backend.processing import (
    WS_TYPE_ANOMALY,
    WS_TYPE_RECALIBRATION,
    WS_TYPE_STATE_UPDATE,
    WS_TYPE_TRACK_UPDATE,
    _build_ws_message,
    _ensure_state_history_table,
)

logger = logging.getLogger(__name__)

# Maximum number of simultaneous WebSocket connections (resolved open question 2).
MAX_WS_CONNECTIONS: int = 20

# Valid WebSocket message types (F-043) — imported from processing.py.
_WS_TYPE_STATE_UPDATE: str = WS_TYPE_STATE_UPDATE
_WS_TYPE_ANOMALY: str = WS_TYPE_ANOMALY
_WS_TYPE_RECALIBRATION: str = WS_TYPE_RECALIBRATION
_WS_TYPE_TRACK_UPDATE: str = WS_TYPE_TRACK_UPDATE


# ---------------------------------------------------------------------------
# Phase 1: Database schema helpers (implementations live in processing.py)
# ---------------------------------------------------------------------------


# _ensure_state_history_table, _insert_state_history_row, and _build_ws_message
# are imported from backend.processing above. The names are re-bound here as
# module-level names so any existing call sites in this file and in tests that
# import these names from main continue to resolve correctly.
# TECH DEBT TD-013: state_history table retained for post-POC history endpoint.


def _ensure_conjunction_tables(db: sqlite3.Connection) -> None:
    """Create conjunction_events and conjunction_risks tables if they do not exist.

    Called during lifespan startup after _ensure_state_history_table.

    Args:
        db: Open SQLite connection.
    """
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS conjunction_events (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            anomalous_norad_id    INTEGER NOT NULL,
            screening_epoch_utc   TEXT    NOT NULL,
            horizon_s             INTEGER NOT NULL,
            threshold_km          REAL    NOT NULL,
            created_at            TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_conjunction_events_norad
        ON conjunction_events (anomalous_norad_id)
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS conjunction_risks (
            id                              INTEGER PRIMARY KEY AUTOINCREMENT,
            conjunction_event_id            INTEGER NOT NULL,
            risk_order                      INTEGER NOT NULL,
            norad_id                        INTEGER NOT NULL,
            min_distance_km                 REAL    NOT NULL,
            time_of_closest_approach_utc    TEXT    NOT NULL,
            via_norad_id                    INTEGER,
            FOREIGN KEY (conjunction_event_id) REFERENCES conjunction_events(id)
        )
        """
    )
    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_conjunction_risks_event
        ON conjunction_risks (conjunction_event_id)
        """
    )
    db.commit()


def _persist_conjunction_result(db: sqlite3.Connection, result: dict) -> int:
    """Insert a conjunction screening result into the SQLite persistence tables.

    Inserts one row into conjunction_events and one row per risk entry into
    conjunction_risks. Returns the conjunction_event_id for the inserted event row.

    Args:
        db: Open SQLite connection.
        result: Conjunction result dict as returned by conjunction.screen_conjunctions.

    Returns:
        Integer conjunction_event_id of the inserted conjunction_events row.
    """
    cursor = db.execute(
        """
        INSERT INTO conjunction_events
            (anomalous_norad_id, screening_epoch_utc, horizon_s, threshold_km)
        VALUES (?, ?, ?, ?)
        """,
        (
            result["anomalous_norad_id"],
            result["screening_epoch_utc"],
            result["horizon_s"],
            result["threshold_km"],
        ),
    )
    event_id: int = cursor.lastrowid  # type: ignore[assignment]

    for entry in result.get("first_order", []):
        db.execute(
            """
            INSERT INTO conjunction_risks
                (conjunction_event_id, risk_order, norad_id,
                 min_distance_km, time_of_closest_approach_utc, via_norad_id)
            VALUES (?, 1, ?, ?, ?, NULL)
            """,
            (
                event_id,
                entry["norad_id"],
                entry["min_distance_km"],
                entry["time_of_closest_approach_utc"],
            ),
        )

    for entry in result.get("second_order", []):
        db.execute(
            """
            INSERT INTO conjunction_risks
                (conjunction_event_id, risk_order, norad_id,
                 min_distance_km, time_of_closest_approach_utc, via_norad_id)
            VALUES (?, 2, ?, ?, ?, ?)
            """,
            (
                event_id,
                entry["norad_id"],
                entry["min_distance_km"],
                entry["time_of_closest_approach_utc"],
                entry.get("via_norad_id"),
            ),
        )

    db.commit()
    return event_id


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
                logger.warning("WebSocket broadcast failed for client, removing: %s", exc)
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


class _ApiKeyMiddleware(BaseHTTPMiddleware):
    """Enforce NEBODY_API_KEY on all HTTP endpoints except /config and CORS preflight.

    Accepts the key as a Bearer token (Authorization: Bearer <key>) or as a
    ?key=<key> query parameter. WebSocket auth is handled separately in the
    websocket_live endpoint handler (middleware does not intercept WS upgrades).

    If NEBODY_API_KEY is not set, all requests are allowed through unchanged.
    """

    _EXEMPT_PATHS: frozenset[str] = frozenset({"/config", "/docs", "/openapi.json", "/redoc"})

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        if _API_KEY is None or request.url.path in self._EXEMPT_PATHS:
            return await call_next(request)
        # Allow CORS preflight through so the CORSMiddleware can respond correctly.
        if request.method == "OPTIONS":
            return await call_next(request)
        # Bearer token
        auth: str | None = request.headers.get("Authorization")
        if auth and auth.startswith("Bearer ") and auth[7:] == _API_KEY:
            return await call_next(request)
        # ?key= query parameter (useful for browser-initiated fetches and WS polyfills)
        if request.query_params.get("key") == _API_KEY:
            return await call_next(request)
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)


# _build_ws_message is imported from backend.processing above.


# ---------------------------------------------------------------------------
# Phase 4: Background tasks
# ---------------------------------------------------------------------------


async def _run_conjunction_screening(app: FastAPI, screening_inputs: dict) -> None:
    """Async fire-and-forget task that runs conjunction screening off the event loop.

    Builds the other_objects list and catalog_name_map from current app state,
    calls conjunction.screen_conjunctions via run_in_executor (CPU-bound),
    persists the result to SQLite, and broadcasts the conjunction_risk WS message.

    The screening_inputs dict must contain:
        anomalous_norad_id (int): NORAD ID of the anomalous object.
        screening_epoch_utc (str): ISO-8601 UTC epoch string from the anomaly message.
        tle_line1 (str): TLE line 1 for the anomalous object.
        tle_line2 (str): TLE line 2 for the anomalous object.

    Errors are logged but do not crash this background task.

    Args:
        app: FastAPI application instance (for app.state access).
        screening_inputs: Dict with keys anomalous_norad_id, screening_epoch_utc,
            tle_line1, tle_line2.
    """
    try:
        anomalous_norad_id: int = int(screening_inputs["anomalous_norad_id"])
        screening_epoch_str: str = screening_inputs["screening_epoch_utc"]
        tle_line1: str = screening_inputs["tle_line1"]
        tle_line2: str = screening_inputs["tle_line2"]

        # Parse epoch string to UTC-aware datetime.
        screening_epoch_utc: datetime.datetime = datetime.datetime.strptime(
            screening_epoch_str, "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=datetime.timezone.utc)

        # Build other_objects list from filter_states: include every non-anomalous
        # object that has last_tle_line1 and last_tle_line2 set.
        filter_states: dict[int, dict] = app.state.filter_states
        other_objects: list[dict] = []
        for norad_id, fs in list(filter_states.items()):
            if norad_id == anomalous_norad_id:
                continue
            line1 = fs.get("last_tle_line1")
            line2 = fs.get("last_tle_line2")
            if line1 and line2:
                other_objects.append(
                    {
                        "norad_id": norad_id,
                        "tle_line1": line1,
                        "tle_line2": line2,
                    }
                )

        # Build catalog_name_map from catalog_entries.
        catalog_name_map: dict[int, str] = {
            int(e["norad_id"]): e.get("name", str(e["norad_id"])) for e in app.state.catalog_entries
        }

        logger.info(
            "_run_conjunction_screening: NORAD %d epoch=%s other_objects=%d",
            anomalous_norad_id,
            screening_epoch_str,
            len(other_objects),
        )

        # Run CPU-bound screening in thread pool to avoid blocking the event loop.
        loop = asyncio.get_event_loop()
        result: dict = await loop.run_in_executor(
            None,
            conjunction.screen_conjunctions,
            anomalous_norad_id,
            tle_line1,
            tle_line2,
            screening_epoch_utc,
            other_objects,
            catalog_name_map,
        )

        # Persist result to SQLite.
        _persist_conjunction_result(app.state.db, result)

        # Broadcast conjunction_risk message to all connected WebSocket clients.
        await ws_manager.broadcast(result)

        logger.info(
            "_run_conjunction_screening: complete for NORAD %d — first_order=%d second_order=%d",
            anomalous_norad_id,
            len(result.get("first_order", [])),
            len(result.get("second_order", [])),
        )

    except Exception as exc:  # noqa: BLE001
        logger.error(
            "_run_conjunction_screening: error for NORAD %s: %s",
            screening_inputs.get("anomalous_norad_id"),
            exc,
            exc_info=True,
        )


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
            logger.error("_ingest_loop_task encountered an error: %s — restarting in 60s", exc)
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
                await asyncio.to_thread(
                    _process_single_object,
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
                    logger.error("Error processing NORAD %d: %s", norad_id, exc, exc_info=True)
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
    tle_record: dict | None = ingest.get_latest_tle(db, norad_id)
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

    # Phase 2 (plan step 2): if an anomaly was detected, schedule conjunction screening.
    # Detect anomaly by checking the returned message list (no changes to processing.py).
    # tle_record is already in scope from line 290 above.
    anomaly_message: dict | None = next((m for m in messages if m.get("type") == WS_TYPE_ANOMALY), None)
    if anomaly_message is not None:
        screening_inputs: dict = {
            "anomalous_norad_id": norad_id,
            "screening_epoch_utc": anomaly_message["epoch_utc"],
            "tle_line1": tle_record["tle_line1"],
            "tle_line2": tle_record["tle_line2"],
        }
        asyncio.get_event_loop().create_task(_run_conjunction_screening(app, screening_inputs))


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
    catalog_config_path: str = os.environ.get("NBODY_CATALOG_CONFIG") or "data/catalog/catalog.json"

    logger.info("ne-body startup: db_path=%s catalog=%s", db_path, catalog_config_path)

    db: sqlite3.Connection = ingest.init_catalog_db(db_path)
    logger.info("Catalog DB initialized.")

    _ensure_state_history_table(db)
    logger.info("state_history table ready.")

    _ensure_conjunction_tables(db)
    logger.info("conjunction tables ready.")

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

    # Warm startup: process the latest TLE per object so filter_states is populated
    # before the first WebSocket client connects. Uses cold-start path only (one TLE
    # per object, no track generation) so startup completes in ~1-2s regardless of
    # how many historical TLEs are cached.
    warm_count: int = 0
    for entry in catalog_entries:
        _nid: int = int(entry["norad_id"])
        _latest_tle = ingest.get_latest_tle(db, _nid)
        if _latest_tle is None:
            continue
        try:
            msgs = processing.process_single_object(
                db=db,
                entry=entry,
                norad_id=_nid,
                filter_states=app.state.filter_states,
                tle_record=_latest_tle,
                generate_tracks=False,
            )
            if msgs:
                warm_count += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("warm startup: failed for NORAD %d: %s", _nid, exc)
    logger.info("Warm startup complete: %d objects initialized.", warm_count)

    # Restore persisted anomaly tracking into filter_states.
    # Warm startup runs process_single_object without _anomaly_row_id, so the
    # resolution branch can't fire if the anomaly already cleared.  Restoring
    # here means the NEXT ingest cycle will correctly call
    # record_recalibration_complete rather than leaving the alert active forever.
    persisted: dict[int, dict] = anomaly.load_active_anomalies(db)
    restored_count: int = 0
    for _nid, _state in persisted.items():
        if _nid in app.state.filter_states and "_anomaly_row_id" not in app.state.filter_states[_nid]:
            app.state.filter_states[_nid]["_anomaly_row_id"] = _state["anomaly_row_id"]
            app.state.filter_states[_nid]["_anomaly_detection_epoch_utc"] = _state["detection_epoch_utc"]
            restored_count += 1
    if restored_count:
        logger.info("Restored _anomaly_row_id for %d objects from DB.", restored_count)

    # Orphan migration: resolve active alerts that have no filter_active_anomaly
    # entry and are older than 2 hours.  These were orphaned by prior restarts
    # before this fix was deployed.
    db.execute(
        """
        UPDATE alerts
        SET status = 'resolved',
            resolution_epoch_utc = datetime('now'),
            recalibration_duration_s = NULL
        WHERE status = 'active'
          AND norad_id NOT IN (SELECT norad_id FROM filter_active_anomaly)
          AND detection_epoch_utc < datetime('now', '-2 hours')
        """
    )
    orphan_count: int = db.execute("SELECT changes()").fetchone()[0]
    db.commit()
    if orphan_count:
        logger.info("Orphan migration: resolved %d stale active alerts.", orphan_count)

    yield

    # --- SHUTDOWN ---
    # DEVIATION from plan docs/plans/2026-03-28-main.md step 6:
    # Plan says "Close app.state.db". Using local variable `db` here instead
    # because test helpers may replace app.state.db with a test connection after
    # lifespan startup. Closing the local `db` ensures we always close the
    # connection opened during startup, preventing thread-affinity errors.
    # Functionally equivalent in production (nothing replaces app.state.db at
    # runtime). Flagged for planner review.
    logger.info("ne-body shutdown: cancelling background tasks.")
    for task in app.state.background_tasks:
        task.cancel()
    await asyncio.gather(*app.state.background_tasks, return_exceptions=True)
    logger.info("Background tasks cancelled.")

    db.close()
    logger.info("Database connection closed. Shutdown complete.")


app = FastAPI(
    title="ne-body SSA Platform",
    description="Continuous Monitoring & Prediction Platform for Space Situational Awareness",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(_ApiKeyMiddleware)


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
            last_update_epoch_utc: str | None = epoch_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            confidence: float | None = float(state["confidence"])
            state_eci_km = state["state_eci_km"]
            cov_km2 = state["covariance_km2"]
            eci_km: list | None = state_eci_km[:3].tolist()
            eci_km_s: list | None = state_eci_km[3:].tolist()
            covariance_diagonal_km2: list | None = [
                float(cov_km2[0, 0]),
                float(cov_km2[1, 1]),
                float(cov_km2[2, 2]),
            ]
            nis: float | None = float(state["nis"])
            anomaly_flag: bool | None = bool(state["anomaly_flag"])
            innovation_eci_km: list | None = state["innovation_eci_km"].tolist()
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
    since_utc: str | None = None,
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


@app.get("/alerts/active")
async def get_active_alerts() -> list[dict]:
    """Return all currently unresolved anomaly alerts across the catalog.

    Used by the frontend on WebSocket connect/reconnect to seed the alert
    panel with any anomalies that fired while the client was disconnected.
    Returns alerts ordered newest-first, formatted as WebSocket anomaly
    message dicts so the frontend can call addAlert() directly.

    Returns:
        List of dicts with keys: type, norad_id, epoch_utc, anomaly_type,
        nis, innovation_eci_km, confidence, eci_km, eci_km_s,
        covariance_diagonal_km2.
    """
    db: sqlite3.Connection = app.state.db
    active = anomaly.get_active_anomalies(db)

    result: list[dict] = []
    for row in active:
        norad_id: int = row["norad_id"]
        # Build a WS-compatible anomaly message from the DB record.
        # Use filter state for position/confidence if available.
        fs: dict | None = app.state.filter_states.get(norad_id)
        if fs is not None:
            from backend.kalman import get_state

            state = get_state(fs)
            eci_km = state["state_eci_km"][:3].tolist()
            eci_km_s = state["state_eci_km"][3:].tolist()
            cov = state["covariance_km2"]
            cov_diag = [float(cov[0, 0]), float(cov[1, 1]), float(cov[2, 2])]
            confidence = float(state["confidence"])
        else:
            eci_km = [0.0, 0.0, 0.0]
            eci_km_s = [0.0, 0.0, 0.0]
            cov_diag = [1000.0, 1000.0, 1000.0]
            confidence = 0.0

        result.append(
            {
                "type": "anomaly",
                "norad_id": norad_id,
                "epoch_utc": row["detection_epoch_utc"],
                "anomaly_type": row["anomaly_type"],
                "nis": row["nis_value"],
                "innovation_eci_km": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                "confidence": confidence,
                "eci_km": eci_km,
                "eci_km_s": eci_km_s,
                "covariance_diagonal_km2": cov_diag,
            }
        )

    return result


class _DismissRequest(BaseModel):
    norad_id: int
    epoch_utc: str


@app.post("/alerts/dismiss")
async def dismiss_alert(body: _DismissRequest) -> dict:
    """Mark an alert as dismissed so it does not reappear on page reload.

    Looks up the alert by (norad_id, epoch_utc) and sets status='dismissed'.
    Dismissed alerts are excluded from GET /alerts/active responses.

    Args:
        body: JSON body with norad_id (int) and epoch_utc (ISO-8601 string).

    Returns:
        Dict with 'dismissed': true if updated, false if no matching alert found.
    """
    db: sqlite3.Connection = app.state.db
    updated: bool = anomaly.dismiss_alert(
        db,
        norad_id=body.norad_id,
        detection_epoch_utc=body.epoch_utc,
    )
    return {"dismissed": updated}


@app.get("/object/{norad_id}/conjunctions")
async def get_object_conjunctions(norad_id: int) -> list[dict]:
    """Return the last 5 conjunction screening results for a given NORAD ID.

    Queries conjunction_events joined with conjunction_risks, ordered by
    created_at DESC, limited to 5 events. Reconstructs each event as a dict
    matching the conjunction_risk WebSocket message schema.

    Args:
        norad_id: NORAD catalog ID (anomalous object).

    Returns:
        List of up to 5 conjunction result dicts (newest first).

    Raises:
        HTTPException 404 if norad_id is not in the catalog.
    """
    catalog_ids = {int(e["norad_id"]) for e in app.state.catalog_entries}
    if norad_id not in catalog_ids:
        raise HTTPException(
            status_code=404,
            detail=f"NORAD ID {norad_id} not found in catalog.",
        )

    db: sqlite3.Connection = app.state.db

    # Fetch the 5 most recent conjunction events for this norad_id.
    event_cursor = db.execute(
        """
        SELECT id, anomalous_norad_id, screening_epoch_utc, horizon_s, threshold_km
        FROM conjunction_events
        WHERE anomalous_norad_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT 5
        """,
        (norad_id,),
    )
    events = event_cursor.fetchall()

    # Build name map from catalog entries for risk entry name lookup.
    catalog_name_map: dict[int, str] = {
        int(e["norad_id"]): e.get("name", str(e["norad_id"])) for e in app.state.catalog_entries
    }

    results: list[dict] = []
    for event_row in events:
        event_id, anomalous_norad_id, screening_epoch_utc, horizon_s, threshold_km = event_row

        # Fetch associated risk rows.
        risk_cursor = db.execute(
            """
            SELECT risk_order, norad_id, min_distance_km,
                   time_of_closest_approach_utc, via_norad_id
            FROM conjunction_risks
            WHERE conjunction_event_id = ?
            ORDER BY risk_order ASC, min_distance_km ASC
            """,
            (event_id,),
        )
        risk_rows = risk_cursor.fetchall()

        first_order: list[dict] = []
        second_order: list[dict] = []
        for risk_row in risk_rows:
            risk_order, risk_norad_id, min_distance_km, tca_utc, via_norad_id = risk_row
            name = catalog_name_map.get(risk_norad_id, str(risk_norad_id))
            if risk_order == 1:
                first_order.append(
                    {
                        "norad_id": risk_norad_id,
                        "name": name,
                        "min_distance_km": float(min_distance_km),
                        "time_of_closest_approach_utc": tca_utc,
                    }
                )
            else:
                second_order.append(
                    {
                        "norad_id": risk_norad_id,
                        "name": name,
                        "min_distance_km": float(min_distance_km),
                        "via_norad_id": via_norad_id,
                        "time_of_closest_approach_utc": tca_utc,
                    }
                )

        results.append(
            {
                "type": "conjunction_risk",
                "anomalous_norad_id": anomalous_norad_id,
                "screening_epoch_utc": screening_epoch_utc,
                "horizon_s": horizon_s,
                "threshold_km": float(threshold_km),
                "first_order": first_order,
                "second_order": second_order,
            }
        )

    return results


@app.get("/object/{norad_id}/track")
async def get_object_track(
    norad_id: int,
    seconds_back: int = 1500,
    seconds_forward: int = 0,
    # DEVIATION from plan docs/plans/2026-03-29-history-tracks-cones.md step 2.1:
    # Plan decision 1 sets default step_s = 60 (not 30). Implemented as 60 here.
    # Tech debt entry TD-025 added for UI configurability (post-POC).
    step_s: int = 60,
    center_time: str | None = None,
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
    tle_record: dict | None = ingest.get_latest_tle(db, norad_id)
    if tle_record is None:
        raise HTTPException(
            status_code=404,
            detail=f"No cached TLE for NORAD ID {norad_id}.",
        )

    tle_line1: str = tle_record["tle_line1"]
    tle_line2: str = tle_record["tle_line2"]

    # Reference epoch defaults to now but accepts a caller-supplied center_time
    # so the frontend can align the track with the Cesium clock (which may run
    # faster than real time due to clock.multiplier > 1).
    if center_time is not None:
        try:
            reference_epoch_utc = datetime.datetime.fromisoformat(
                center_time.replace("Z", "+00:00")
            )
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid center_time format — use ISO 8601.")
    else:
        reference_epoch_utc = datetime.datetime.now(tz=datetime.timezone.utc)

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
                norad_id,
                t_s,
                exc,
            )
            continue

    # --- Forward track ---
    # Range: step_s, 2*step_s, ..., seconds_forward (inclusive if divisible).
    forward_track: list[dict] = []
    filter_state: dict | None = app.state.filter_states.get(norad_id)
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
                    norad_id,
                    t_s,
                    exc,
                )
                continue

            # Compute uncertainty radius at this forward time step.
            if has_filter:
                # Covariance growth: P_ii + Q_ii * (t / dt_nominal).
                # Linear approximation of unmodeled acceleration variance accumulation.
                # See plan docs/plans/2026-03-29-history-tracks-cones.md step 3.1.
                sigma2_grown = [float(cov_km2[i, i]) + float(q_matrix[i, i]) * (t_s / dt_nominal_s) for i in range(3)]
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

    # Precompute the latest TLE epoch per NORAD so we can gate track generation
    # to only the final TLE per object (avoids ~60 SGP4 calls per intermediate TLE).
    latest_tle_epoch_per_norad: dict[int, str] = {}
    for entry in catalog_entries:
        _nid = int(entry["norad_id"])
        _latest = ingest.get_latest_tle(db, _nid)
        if _latest:
            latest_tle_epoch_per_norad[_nid] = _latest["epoch_utc"]

    processed_count: int = 0
    last_broadcast_norad: set[int] = set()
    for entry, tle_record in all_tle_records:
        norad_id = int(entry["norad_id"])
        try:
            is_last = tle_record["epoch_utc"] == latest_tle_epoch_per_norad.get(norad_id)
            messages: list[dict] = processing.process_single_object(
                db=db,
                entry=entry,
                norad_id=norad_id,
                filter_states=filter_states,
                tle_record=tle_record,
                generate_tracks=is_last,
            )

            # Only broadcast the final message per object (the most recent state).
            # Intermediate updates converge P but don't need to hit the browser.
            for msg in messages:
                if is_last or msg.get("type") == WS_TYPE_ANOMALY:
                    await ws_manager.broadcast(msg)

            if messages:
                last_broadcast_norad.add(norad_id)

            # Phase 2 (plan step 3): schedule conjunction screening if an anomaly
            # was detected. Only trigger on the last TLE for each object to avoid
            # redundant screenings from intermediate replay TLEs.
            if is_last:
                admin_anomaly_msg: dict | None = next((m for m in messages if m.get("type") == WS_TYPE_ANOMALY), None)
                if admin_anomaly_msg is not None:
                    admin_screening_inputs: dict = {
                        "anomalous_norad_id": norad_id,
                        "screening_epoch_utc": admin_anomaly_msg["epoch_utc"],
                        "tle_line1": tle_record["tle_line1"],
                        "tle_line2": tle_record["tle_line2"],
                    }
                    asyncio.get_event_loop().create_task(_run_conjunction_screening(app, admin_screening_inputs))

        except Exception as exc:  # noqa: BLE001
            logger.error(
                "admin_trigger_process: error for NORAD %d @ %s: %s",
                norad_id,
                tle_record.get("epoch_utc"),
                exc,
                exc_info=True,
            )
            continue

    processed_count = len(last_broadcast_norad)

    logger.info("admin_trigger_process complete: processed=%d", processed_count)
    return {"processed": processed_count}


@app.post("/admin/trigger-ingest")
async def admin_trigger_ingest() -> dict:
    """Force-run one TLE ingest cycle for all catalog objects.

    Calls poll_once() immediately, bypassing the 30-minute schedule. Useful
    during development and demos to populate the TLE cache without waiting
    for the next scheduled poll.

    Returns:
        JSON with 'inserted' count of new TLE records written to the cache.
    """
    db: sqlite3.Connection = app.state.db
    catalog_entries: list[dict] = app.state.catalog_entries
    event_bus: asyncio.Queue = app.state.event_bus

    try:
        inserted: int = await ingest.poll_once(db, catalog_entries, event_bus=event_bus)
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f"Credential error: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("admin_trigger_ingest error: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    logger.info("admin_trigger_ingest complete: inserted=%d", inserted)
    return {"inserted": inserted}


@app.get("/events/history")
async def get_events_history(
    q: str | None = None,
    type: str | None = None,
    status: str | None = None,
    since_utc: str | None = None,
    until_utc: str | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    page: int = 1,
    page_size: int = 25,
) -> dict:
    """Return paginated, filtered anomaly event history across all tracked objects.

    Supports free-text search by NORAD ID or object name, filtering by anomaly
    type and status, time range filtering, multi-column sort, and pagination.

    Args:
        q: Free-text search. Matches NORAD ID (exact numeric) or object name
            (case-insensitive substring). No match returns empty results.
        type: Filter by anomaly_type. One of: maneuver, drag_anomaly,
            filter_divergence.
        status: Filter by alert status. One of: active, resolved, dismissed,
            recalibrating.
        since_utc: ISO-8601 UTC. Include only events with
            detection_epoch_utc >= this value.
        until_utc: ISO-8601 UTC. Include only events with
            detection_epoch_utc <= this value.
        sort_by: Column to sort by. One of: detection_epoch_utc, anomaly_type,
            status, nis_value, recalibration_duration_s.
            Default: detection_epoch_utc.
        sort_dir: Sort direction. asc or desc. Default: desc.
        page: 1-indexed page number. Default: 1.
        page_size: Results per page. Default: 25. Clamped to [1, 100].

    Returns:
        Dict with total (int), page (int), page_size (int), results (list).
        Each result includes: id, norad_id, name, object_class,
        detection_epoch_utc, anomaly_type, nis_value, status,
        resolution_epoch_utc, recalibration_duration_s.
    """
    # --- Allowlists to prevent SQL injection via sort parameters ---
    _SORT_BY_ALLOWLIST: set[str] = {
        "detection_epoch_utc",
        "anomaly_type",
        "status",
        "nis_value",
        "recalibration_duration_s",
    }
    _SORT_DIR_ALLOWLIST: set[str] = {"asc", "desc"}

    # --- Clamp page_size ---
    page_size = min(max(1, page_size), 100)
    page = max(1, page)

    # --- Validate and default sort parameters ---
    if sort_by not in _SORT_BY_ALLOWLIST:
        sort_by = "detection_epoch_utc"
    if sort_dir not in _SORT_DIR_ALLOWLIST:
        sort_dir = "desc"

    # --- Build catalog_name_map from app.state.catalog_entries ---
    catalog_name_map: dict[int, dict] = {}
    for entry in app.state.catalog_entries:
        norad_id_key: int = int(entry["norad_id"])
        catalog_name_map[norad_id_key] = {
            "name": entry.get("name", str(norad_id_key)),
            "object_class": entry.get("object_class", "unknown"),
        }

    # --- Resolve q param to a set of matching NORAD IDs ---
    matching_norad_ids: set[int] | None = None
    if q is not None and q.strip() != "":
        q_stripped: str = q.strip()
        matched: set[int] = set()

        # Exact numeric NORAD ID match.
        try:
            numeric_norad: int = int(q_stripped)
            if numeric_norad in catalog_name_map:
                matched.add(numeric_norad)
        except ValueError:
            pass

        # Case-insensitive name substring match.
        q_lower: str = q_stripped.lower()
        for nid, info in catalog_name_map.items():
            if q_lower in info["name"].lower():
                matched.add(nid)

        if not matched:
            # q provided but no catalog entries match — return empty immediately.
            return {
                "total": 0,
                "page": page,
                "page_size": page_size,
                "results": [],
            }

        matching_norad_ids = matched

    # --- Build parameterized WHERE clause ---
    where_clauses: list[str] = []
    params: list = []

    if matching_norad_ids is not None:
        # SQLite parameterized IN clause: generate one ? per ID.
        placeholders: str = ", ".join("?" for _ in matching_norad_ids)
        where_clauses.append(f"norad_id IN ({placeholders})")
        params.extend(sorted(matching_norad_ids))

    if type is not None:
        where_clauses.append("anomaly_type = ?")
        params.append(type)

    if status is not None:
        where_clauses.append("status = ?")
        params.append(status)

    if since_utc is not None:
        where_clauses.append("detection_epoch_utc >= ?")
        params.append(since_utc)

    if until_utc is not None:
        where_clauses.append("detection_epoch_utc <= ?")
        params.append(until_utc)

    where_sql: str = ""
    if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)

    db: sqlite3.Connection = app.state.db

    # --- COUNT query ---
    count_sql: str = f"SELECT COUNT(*) FROM alerts {where_sql}"
    count_cursor = db.execute(count_sql, params)
    total: int = count_cursor.fetchone()[0]

    # --- SELECT query with ORDER BY and LIMIT/OFFSET ---
    # sort_by and sort_dir are validated against allowlists above — safe to interpolate.
    offset: int = (page - 1) * page_size
    select_sql: str = (
        f"SELECT id, norad_id, anomaly_type, detection_epoch_utc, status, "
        f"nis_value, resolution_epoch_utc, recalibration_duration_s "
        f"FROM alerts "
        f"{where_sql} "
        f"ORDER BY {sort_by} {sort_dir} "
        f"LIMIT ? OFFSET ?"
    )
    select_params: list = params + [page_size, offset]
    rows = db.execute(select_sql, select_params).fetchall()

    # --- Join name and object_class from catalog_name_map ---
    results: list[dict] = []
    for row in rows:
        (
            row_id,
            row_norad_id,
            row_anomaly_type,
            row_detection_epoch_utc,
            row_status,
            row_nis_value,
            row_resolution_epoch_utc,
            row_recalibration_duration_s,
        ) = row
        catalog_info: dict = catalog_name_map.get(
            row_norad_id,
            {"name": str(row_norad_id), "object_class": "unknown"},
        )
        results.append(
            {
                "id": row_id,
                "norad_id": row_norad_id,
                "name": catalog_info["name"],
                "object_class": catalog_info["object_class"],
                "detection_epoch_utc": row_detection_epoch_utc,
                "anomaly_type": row_anomaly_type,
                "nis_value": row_nis_value,
                "status": row_status,
                "resolution_epoch_utc": row_resolution_epoch_utc,
                "recalibration_duration_s": row_recalibration_duration_s,
            }
        )

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "results": results,
    }


@app.post("/admin/reload-catalog")
async def admin_reload_catalog() -> dict:
    """Reload catalog.json from disk without restarting the server.

    Reads the catalog config file specified at startup and replaces
    app.state.catalog_entries. Useful after editing catalog.json during
    development or demo setup.

    Returns:
        JSON with 'count' of entries loaded and 'path' of the config file.
    """
    catalog_config_path: str = app.state.catalog_config_path
    try:
        catalog_entries: list[dict] = ingest.load_catalog_config(catalog_config_path)
    except Exception as exc:  # noqa: BLE001
        logger.error("admin_reload_catalog error: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    app.state.catalog_entries = catalog_entries
    logger.info("admin_reload_catalog: loaded %d entries from %s", len(catalog_entries), catalog_config_path)
    return {"count": len(catalog_entries), "path": catalog_config_path}


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
    # H-4: API key auth check before accepting. Close 1008 (Policy Violation) if key is
    # required but missing or wrong. Pass ?key=<value> as a query parameter on the WS URL.
    if _API_KEY is not None:
        ws_key: str | None = websocket.query_params.get("key")
        if ws_key != _API_KEY:
            await websocket.close(code=1008)
            logger.warning("WebSocket connection rejected: missing or invalid API key")
            return

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

        # NF-012 track burst: send track_update for every object with filter state
        # so the newly connected client gets animated tracks immediately.
        # Each generate_track_samples call runs in a thread pool (asyncio.to_thread)
        # to avoid blocking the event loop during the ~21s of SGP4 computation.
        for norad_id, filter_state in list(filter_states.items()):
            try:
                last_tle1: str | None = filter_state.get("last_tle_line1")
                last_tle2: str | None = filter_state.get("last_tle_line2")
                last_epoch: datetime.datetime | None = filter_state.get("last_epoch_utc")
                if last_tle1 and last_tle2 and last_epoch:
                    track_start = datetime.datetime.now(tz=datetime.timezone.utc)
                    track_samples = await asyncio.to_thread(
                        processing.generate_track_samples,
                        tle_line1=last_tle1,
                        tle_line2=last_tle2,
                        start_epoch_utc=track_start,
                    )
                    track_msg: dict = {
                        "type": _WS_TYPE_TRACK_UPDATE,
                        "norad_id": norad_id,
                        "epoch_utc": track_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "samples": track_samples,
                    }
                    await websocket.send_text(json.dumps(track_msg))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to send initial track_update for NORAD %d to new client: %s",
                    norad_id,
                    exc,
                )
                break  # Connection is dead — stop the burst immediately.

        # Keepalive receive loop — content is ignored.
        while True:
            await websocket.receive_text()

    except WebSocketDisconnect:
        pass
    finally:
        await ws_manager.disconnect(websocket)
