"""Tests for backend/main.py API endpoints and WebSocket gateway.

Covers F-040, F-041, F-042, F-043, F-044, NF-012.
"""
import asyncio
import datetime
import json
import sqlite3
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from fastapi.testclient import TestClient

import backend.kalman as kalman
from backend.main import (
    ConnectionManager,
    MAX_WS_CONNECTIONS,
    _WS_TYPE_ANOMALY,
    _WS_TYPE_RECALIBRATION,
    _WS_TYPE_STATE_UPDATE,
    _build_ws_message,
    _ensure_state_history_table,
    app,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _make_synthetic_filter_state(
    norad_id: int = 25544,
    epoch_utc: Optional[datetime.datetime] = None,
) -> dict:
    """Create a real filter state dict with synthetic initial state.

    Uses kalman.init_filter with a plausible ECI state vector so that
    get_state(), _build_ws_message(), etc. work correctly.
    """
    if epoch_utc is None:
        epoch_utc = datetime.datetime(2026, 3, 28, 19, 0, 0, tzinfo=datetime.timezone.utc)
    # ISS-like ECI position (km) and velocity (km/s), approximate.
    state_eci_km = np.array(
        [6378.0 + 400.0, 0.0, 0.0, 0.0, 7.67, 0.0], dtype=np.float64
    )
    fs = kalman.init_filter(
        state_eci_km=state_eci_km,
        epoch_utc=epoch_utc,
        process_noise_q=kalman.OBJECT_CLASS_Q[kalman.OBJECT_CLASS_ACTIVE],
    )
    return fs


def _make_synthetic_catalog_entries(norad_ids: list[int]) -> list[dict]:
    """Build minimal catalog entry dicts for testing."""
    return [
        {
            "norad_id": nid,
            "name": f"OBJECT-{nid}",
            "object_class": "active_satellite",
        }
        for nid in norad_ids
    ]


def _seed_alerts_table(db: sqlite3.Connection, norad_id: int, count: int) -> None:
    """Insert synthetic alert rows into the alerts table for testing.

    Inserts rows with epochs spaced 30 minutes apart.
    """
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            norad_id INTEGER NOT NULL,
            detection_epoch_utc TEXT NOT NULL,
            anomaly_type TEXT NOT NULL,
            nis_value REAL NOT NULL,
            resolution_epoch_utc TEXT,
            recalibration_duration_s REAL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    base_epoch = datetime.datetime(2026, 3, 28, 12, 0, 0, tzinfo=datetime.timezone.utc)
    for i in range(count):
        epoch = base_epoch + datetime.timedelta(minutes=30 * i)
        db.execute(
            """
            INSERT INTO alerts (norad_id, detection_epoch_utc, anomaly_type, nis_value, status)
            VALUES (?, ?, ?, ?, 'active')
            """,
            (norad_id, epoch.strftime("%Y-%m-%dT%H:%M:%SZ"), "filter_divergence", 15.0 + i),
        )
    db.commit()


def _setup_app_state_for_catalog(
    norad_ids: list[int],
    filter_states: Optional[dict] = None,
    db: Optional[sqlite3.Connection] = None,
) -> None:
    """Patch app.state for catalog/history endpoint tests.

    Must be called INSIDE a TestClient context manager so that the lifespan
    startup has already run. This helper overwrites only the fields that matter
    for the test (catalog_entries, filter_states, db). It intentionally leaves
    app.state.background_tasks untouched so the lifespan shutdown can cancel
    the real background tasks it started.

    SQLite connections are opened with check_same_thread=False because
    TestClient runs the ASGI app in a separate thread from the test, which
    would otherwise cause sqlite3.ProgrammingError on cross-thread connection
    access.

    The default in-memory db includes the tle_catalog table schema (needed by
    ingest.get_latest_tle() when the catalog endpoint falls back for objects
    without a filter state).
    """
    if db is None:
        db = sqlite3.connect(":memory:", check_same_thread=False)
        db.row_factory = sqlite3.Row
        # Create tle_catalog table so get_latest_tle() does not raise.
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS tle_catalog (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                norad_id INTEGER NOT NULL,
                epoch_utc TEXT NOT NULL,
                tle_line1 TEXT NOT NULL,
                tle_line2 TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'space_track',
                UNIQUE(norad_id, epoch_utc)
            )
            """
        )
        db.commit()
    app.state.db = db
    app.state.catalog_entries = _make_synthetic_catalog_entries(norad_ids)
    app.state.filter_states = filter_states if filter_states is not None else {}


# ---------------------------------------------------------------------------
# Phase 7 test 13: test_get_catalog_returns_list (F-040)
# ---------------------------------------------------------------------------


def test_get_catalog_returns_list() -> None:
    """GET /catalog returns a 200 JSON list with required keys."""
    norad_ids = [25544, 43013]
    fs_25544 = _make_synthetic_filter_state(norad_id=25544)
    fs_43013 = _make_synthetic_filter_state(norad_id=43013)
    filter_states = {25544: fs_25544, 43013: fs_43013}

    with TestClient(app, raise_server_exceptions=True) as client:
        # Set state inside the context so the lifespan startup does not override it.
        _setup_app_state_for_catalog(norad_ids, filter_states=filter_states)
        resp = client.get("/catalog")

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 2

    required_keys = {
        "norad_id", "name", "last_update_epoch_utc", "confidence",
        "eci_km", "eci_km_s", "covariance_diagonal_km2", "nis",
        "anomaly_flag", "innovation_eci_km",
    }
    for item in data:
        assert required_keys <= set(item.keys()), f"Missing keys in: {item}"
        assert isinstance(item["norad_id"], int)
        assert isinstance(item["name"], str)
        # confidence should be a float (filter is initialized)
        assert isinstance(item["confidence"], float)
        assert 0.0 <= item["confidence"] <= 1.0
        # Full state fields must be present and correctly typed.
        assert isinstance(item["eci_km"], list) and len(item["eci_km"]) == 3
        assert isinstance(item["eci_km_s"], list) and len(item["eci_km_s"]) == 3
        assert isinstance(item["covariance_diagonal_km2"], list) and len(item["covariance_diagonal_km2"]) == 3
        assert isinstance(item["nis"], float)
        assert isinstance(item["anomaly_flag"], bool)
        assert isinstance(item["innovation_eci_km"], list) and len(item["innovation_eci_km"]) == 6


# ---------------------------------------------------------------------------
# Phase 7 test 14: test_get_catalog_empty_filters (F-040 edge case)
# ---------------------------------------------------------------------------


def test_get_catalog_empty_filters() -> None:
    """GET /catalog with no initialized filters returns confidence=null."""
    norad_ids = [25544]

    with TestClient(app, raise_server_exceptions=True) as client:
        # Set state inside the context so the lifespan startup does not override it.
        _setup_app_state_for_catalog(norad_ids, filter_states={})
        resp = client.get("/catalog")

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 1
    item = data[0]
    assert item["norad_id"] == 25544
    # No filter initialized and no DB TLE -> confidence and state fields must be null
    assert item["confidence"] is None
    assert item["eci_km"] is None
    assert item["eci_km_s"] is None
    assert item["covariance_diagonal_km2"] is None
    assert item["nis"] is None
    assert item["anomaly_flag"] is None
    assert item["innovation_eci_km"] is None


# ---------------------------------------------------------------------------
# Phase 7 test 15: test_get_object_history_returns_list (F-041)
# ---------------------------------------------------------------------------


def test_get_object_history_returns_list() -> None:
    """GET /object/{norad_id}/history returns 200 with a list of alert records."""
    norad_id = 25544
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    _seed_alerts_table(db, norad_id=norad_id, count=5)

    with TestClient(app, raise_server_exceptions=True) as client:
        # Set state inside the context so the lifespan startup does not override it.
        _setup_app_state_for_catalog([norad_id], db=db)
        resp = client.get(f"/object/{norad_id}/history")

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 5

    required_keys = {"id", "epoch_utc", "anomaly_type", "nis", "status"}
    for record in data:
        assert required_keys <= set(record.keys()), f"Missing keys: {record}"


# ---------------------------------------------------------------------------
# Phase 7 test 16: test_get_object_history_404_unknown (F-041 error case)
# ---------------------------------------------------------------------------


def test_get_object_history_404_unknown() -> None:
    """GET /object/{norad_id}/history with unknown NORAD ID returns 404."""

    with TestClient(app, raise_server_exceptions=True) as client:
        # Set state inside the context so the lifespan startup does not override it.
        _setup_app_state_for_catalog([25544])
        resp = client.get("/object/99999/history")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Phase 7 test 17: test_get_object_history_since_filter (F-041)
# ---------------------------------------------------------------------------


def test_get_object_history_since_filter() -> None:
    """since_utc query parameter filters out earlier records."""
    norad_id = 25544
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    _seed_alerts_table(db, norad_id=norad_id, count=5)

    # Epochs are spaced 30 min apart starting at 2026-03-28T12:00:00Z:
    #   row 1: 12:00, row 2: 12:30, row 3: 13:00, row 4: 13:30, row 5: 14:00
    # Using since_utc = 12:30:00Z means "detection_epoch_utc > 12:30:00Z",
    # which returns rows 3, 4, 5 (13:00, 13:30, 14:00 = 3 rows).
    since_utc = "2026-03-28T12:30:00Z"  # After first 2 epochs (12:00, 12:30)

    with TestClient(app, raise_server_exceptions=True) as client:
        # Set state inside the context so the lifespan startup does not override it.
        _setup_app_state_for_catalog([norad_id], db=db)
        resp = client.get(f"/object/{norad_id}/history?since_utc={since_utc}")

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 3, f"Expected 3 records after filter, got {len(data)}"


# ---------------------------------------------------------------------------
# Phase 7 test 18: test_websocket_connects (F-042)
# ---------------------------------------------------------------------------


def test_websocket_connects() -> None:
    """WebSocket /ws/live accepts a connection without error."""
    with TestClient(app) as client:
        # Set state inside the context so the lifespan startup does not override it.
        _setup_app_state_for_catalog([25544])
        with client.websocket_connect("/ws/live") as ws:
            # Connection accepted; no error raised.
            pass


# ---------------------------------------------------------------------------
# Phase 7 test 19: test_websocket_receives_initial_state (NF-012)
# ---------------------------------------------------------------------------


def test_websocket_receives_initial_state() -> None:
    """On connect, client receives one state_update message per tracked object."""
    norad_ids = [25544, 43013]
    filter_states = {
        25544: _make_synthetic_filter_state(norad_id=25544),
        43013: _make_synthetic_filter_state(norad_id=43013),
    }

    with TestClient(app) as client:
        # Set state inside the context so the lifespan startup does not override it.
        _setup_app_state_for_catalog(norad_ids, filter_states=filter_states)
        with client.websocket_connect("/ws/live") as ws:
            received = []
            # Expect exactly 2 messages (one per tracked object).
            for _ in range(len(norad_ids)):
                raw = ws.receive_text()
                received.append(json.loads(raw))

    assert len(received) == 2
    received_norad_ids = {msg["norad_id"] for msg in received}
    assert received_norad_ids == {25544, 43013}
    for msg in received:
        assert msg["type"] == _WS_TYPE_STATE_UPDATE


# ---------------------------------------------------------------------------
# Phase 7 test 20: test_websocket_message_schema (F-043)
# ---------------------------------------------------------------------------


def test_websocket_message_schema() -> None:
    """Every initial state message conforms to the F-043 schema."""
    norad_id = 25544
    filter_states = {norad_id: _make_synthetic_filter_state(norad_id=norad_id)}

    with TestClient(app) as client:
        # Set state inside the context so the lifespan startup does not override it.
        _setup_app_state_for_catalog([norad_id], filter_states=filter_states)
        with client.websocket_connect("/ws/live") as ws:
            raw = ws.receive_text()
            msg = json.loads(raw)

    valid_types = {_WS_TYPE_STATE_UPDATE, _WS_TYPE_ANOMALY, _WS_TYPE_RECALIBRATION}
    assert msg["type"] in valid_types
    assert isinstance(msg["norad_id"], int)
    assert isinstance(msg["epoch_utc"], str)
    assert msg["epoch_utc"].endswith("Z"), "epoch_utc must end with Z"

    assert isinstance(msg["eci_km"], list) and len(msg["eci_km"]) == 3
    assert isinstance(msg["eci_km_s"], list) and len(msg["eci_km_s"]) == 3
    assert isinstance(msg["covariance_diagonal_km2"], list)
    assert len(msg["covariance_diagonal_km2"]) == 3

    assert isinstance(msg["nis"], (int, float))
    assert isinstance(msg["innovation_eci_km"], list) and len(msg["innovation_eci_km"]) == 6
    assert isinstance(msg["confidence"], (int, float))
    # anomaly_type must be str or None
    assert msg.get("anomaly_type") is None or isinstance(msg["anomaly_type"], str)


# ---------------------------------------------------------------------------
# Phase 7 test 21: test_build_ws_message_schema (F-043 unit test)
# ---------------------------------------------------------------------------


def test_build_ws_message_schema() -> None:
    """_build_ws_message returns correct schema with numpy arrays converted to lists."""
    fs = _make_synthetic_filter_state(norad_id=25544)

    for msg_type in (_WS_TYPE_STATE_UPDATE, _WS_TYPE_ANOMALY, _WS_TYPE_RECALIBRATION):
        msg = _build_ws_message(
            norad_id=25544,
            filter_state=fs,
            message_type=msg_type,
            anomaly_type="maneuver" if msg_type != _WS_TYPE_STATE_UPDATE else None,
        )

        assert msg["type"] == msg_type
        assert msg["norad_id"] == 25544
        assert msg["epoch_utc"].endswith("Z")

        # Numpy arrays must be converted to plain Python lists.
        assert isinstance(msg["eci_km"], list)
        assert isinstance(msg["eci_km_s"], list)
        assert isinstance(msg["covariance_diagonal_km2"], list)
        assert len(msg["eci_km"]) == 3
        assert len(msg["eci_km_s"]) == 3
        assert len(msg["covariance_diagonal_km2"]) == 3

        # All elements must be plain Python floats, not numpy scalars.
        for val in msg["eci_km"] + msg["eci_km_s"] + msg["covariance_diagonal_km2"]:
            assert isinstance(val, float), f"Expected float, got {type(val)}"

        assert isinstance(msg["nis"], float)
        assert isinstance(msg["innovation_eci_km"], list) and len(msg["innovation_eci_km"]) == 6
        for val in msg["innovation_eci_km"]:
            assert isinstance(val, float), f"Expected float in innovation_eci_km, got {type(val)}"
        assert isinstance(msg["confidence"], float)

        if msg_type == _WS_TYPE_STATE_UPDATE:
            assert msg["anomaly_type"] is None
        else:
            assert msg["anomaly_type"] == "maneuver"


# ---------------------------------------------------------------------------
# Phase 7 test 22: test_connection_manager_broadcast (F-044)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connection_manager_broadcast() -> None:
    """ConnectionManager broadcasts to all clients; removes broken connections."""
    manager = ConnectionManager()

    # Create two mock WebSocket objects that succeed.
    ws_good_1 = AsyncMock()
    ws_good_2 = AsyncMock()
    # Create one mock WebSocket that raises on send.
    ws_bad = AsyncMock()
    ws_bad.send_text.side_effect = Exception("connection broken")

    # Manually add to the internal set (bypassing accept for unit test).
    manager._connections.add(ws_good_1)
    manager._connections.add(ws_good_2)
    manager._connections.add(ws_bad)

    assert manager.active_count() == 3

    message = {"type": "state_update", "norad_id": 25544}
    await manager.broadcast(message)

    message_text = json.dumps(message)
    ws_good_1.send_text.assert_called_once_with(message_text)
    ws_good_2.send_text.assert_called_once_with(message_text)

    # Broken connection must be removed.
    assert manager.active_count() == 2
    assert ws_bad not in manager._connections


# ---------------------------------------------------------------------------
# Unit test: _ensure_state_history_table
# ---------------------------------------------------------------------------


def test_ensure_state_history_table_creates_table() -> None:
    """_ensure_state_history_table creates the table on an in-memory SQLite DB."""
    db = sqlite3.connect(":memory:")
    _ensure_state_history_table(db)

    cursor = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='state_history'"
    )
    row = cursor.fetchone()
    assert row is not None, "state_history table was not created"

    # Table should have the expected columns.
    cursor = db.execute("PRAGMA table_info(state_history)")
    columns = {row[1] for row in cursor.fetchall()}
    expected_columns = {
        "id", "norad_id", "epoch_utc",
        "x_km", "y_km", "z_km",
        "vx_km_s", "vy_km_s", "vz_km_s",
        "cov_x_km2", "cov_y_km2", "cov_z_km2",
        "nis", "confidence", "anomaly_type", "message_type",
    }
    assert expected_columns <= columns


def test_ensure_state_history_table_idempotent() -> None:
    """_ensure_state_history_table is idempotent (safe to call twice)."""
    db = sqlite3.connect(":memory:")
    _ensure_state_history_table(db)
    _ensure_state_history_table(db)  # Must not raise
