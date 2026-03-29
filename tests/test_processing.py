"""Tests for backend/processing.py.

Covers the shared predict-update-anomaly-recalibrate pipeline.
"""
import datetime
import sqlite3
from typing import Optional
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

import backend.anomaly as anomaly
import backend.ingest as ingest
import backend.kalman as kalman
import backend.processing as processing
from backend.processing import (
    WS_TYPE_ANOMALY,
    WS_TYPE_RECALIBRATION,
    WS_TYPE_STATE_UPDATE,
    _build_ws_message,
    _ensure_state_history_table,
    process_single_object,
)


# ---------------------------------------------------------------------------
# Real ISS-class TLE (used for tests requiring actual SGP4 propagation)
# ---------------------------------------------------------------------------
_ISS_TLE_LINE1 = "1 25544U 98067A   26087.50000000  .00002182  00000-0  40768-4 0  9990"
_ISS_TLE_LINE2 = "2 25544  51.6431 117.2927 0006703  73.5764 286.6011 15.49559025498826"


def _make_in_memory_db() -> sqlite3.Connection:
    """Return an in-memory SQLite connection with the required tables."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    # Create TLE catalog table
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS tle_catalog (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            norad_id INTEGER NOT NULL,
            epoch_utc TEXT NOT NULL,
            tle_line1 TEXT NOT NULL,
            tle_line2 TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            UNIQUE(norad_id, epoch_utc)
        )
        """
    )
    db.commit()
    _ensure_state_history_table(db)
    anomaly.ensure_alerts_table(db)
    return db


def _make_catalog_entry(norad_id: int, object_class: str = "active_satellite") -> dict:
    return {
        "norad_id": norad_id,
        "name": f"OBJECT-{norad_id}",
        "object_class": object_class,
    }


def _make_tle_record(
    norad_id: int,
    epoch_utc_str: str,
    line1: str = _ISS_TLE_LINE1,
    line2: str = _ISS_TLE_LINE2,
) -> dict:
    return {
        "norad_id": norad_id,
        "epoch_utc": epoch_utc_str,
        "tle_line1": line1,
        "tle_line2": line2,
        "fetched_at": "2026-03-28T12:00:00Z",
    }


# ---------------------------------------------------------------------------
# Tests for _ensure_state_history_table
# ---------------------------------------------------------------------------

def test_ensure_state_history_table_creates_table() -> None:
    db = sqlite3.connect(":memory:")
    _ensure_state_history_table(db)
    cursor = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='state_history'"
    )
    assert cursor.fetchone() is not None


def test_ensure_state_history_table_idempotent() -> None:
    db = sqlite3.connect(":memory:")
    _ensure_state_history_table(db)
    _ensure_state_history_table(db)  # Must not raise


# ---------------------------------------------------------------------------
# Tests for _build_ws_message
# ---------------------------------------------------------------------------

def test_build_ws_message_schema() -> None:
    epoch_utc = datetime.datetime(2026, 3, 28, 19, 0, 0, tzinfo=datetime.timezone.utc)
    state_eci_km = np.array([6778.0, 0.0, 0.0, 0.0, 7.67, 0.0], dtype=np.float64)
    fs = kalman.init_filter(
        state_eci_km=state_eci_km,
        epoch_utc=epoch_utc,
        process_noise_q=kalman.OBJECT_CLASS_Q[kalman.OBJECT_CLASS_ACTIVE],
    )
    msg = _build_ws_message(25544, fs, WS_TYPE_STATE_UPDATE)
    assert msg["type"] == WS_TYPE_STATE_UPDATE
    assert msg["norad_id"] == 25544
    assert msg["epoch_utc"].endswith("Z")
    assert len(msg["eci_km"]) == 3
    assert len(msg["eci_km_s"]) == 3
    assert len(msg["covariance_diagonal_km2"]) == 3
    assert isinstance(msg["nis"], float)
    assert isinstance(msg["confidence"], float)
    assert msg["anomaly_type"] is None


def test_build_ws_message_anomaly_type_propagated() -> None:
    epoch_utc = datetime.datetime(2026, 3, 28, 19, 0, 0, tzinfo=datetime.timezone.utc)
    state_eci_km = np.array([6778.0, 0.0, 0.0, 0.0, 7.67, 0.0], dtype=np.float64)
    fs = kalman.init_filter(state_eci_km=state_eci_km, epoch_utc=epoch_utc)
    msg = _build_ws_message(25544, fs, WS_TYPE_ANOMALY, anomaly_type="maneuver")
    assert msg["type"] == WS_TYPE_ANOMALY
    assert msg["anomaly_type"] == "maneuver"


# ---------------------------------------------------------------------------
# Tests for process_single_object
# ---------------------------------------------------------------------------

def test_process_single_object_cold_start() -> None:
    """Cold start: filter not yet initialized; should return [state_update]."""
    db = _make_in_memory_db()
    entry = _make_catalog_entry(25544)
    filter_states: dict = {}
    tle_record = _make_tle_record(25544, "2026-03-28T12:00:00Z")

    messages = process_single_object(
        db=db,
        entry=entry,
        norad_id=25544,
        filter_states=filter_states,
        tle_record=tle_record,
    )

    assert len(messages) == 1
    assert messages[0]["type"] == WS_TYPE_STATE_UPDATE
    assert messages[0]["norad_id"] == 25544
    # Filter state should now be initialized
    assert 25544 in filter_states
    # One state_history row should have been written
    cursor = db.execute("SELECT COUNT(*) FROM state_history WHERE norad_id=25544")
    assert cursor.fetchone()[0] == 1


def test_process_single_object_cold_start_initializes_filter() -> None:
    """Cold start must call init_filter and populate filter_states."""
    db = _make_in_memory_db()
    entry = _make_catalog_entry(25544)
    filter_states: dict = {}
    tle_record = _make_tle_record(25544, "2026-03-28T12:00:00Z")

    process_single_object(
        db=db, entry=entry, norad_id=25544, filter_states=filter_states,
        tle_record=tle_record,
    )

    assert 25544 in filter_states
    fs = filter_states[25544]
    assert "filter" in fs
    assert fs["last_epoch_utc"].tzinfo is not None


def test_process_single_object_warm_path() -> None:
    """Warm path: filter already initialized; should run predict+update."""
    db = _make_in_memory_db()
    entry = _make_catalog_entry(25544)
    filter_states: dict = {}
    tle_record1 = _make_tle_record(25544, "2026-03-28T12:00:00Z")
    tle_record2 = _make_tle_record(25544, "2026-03-28T12:30:00Z")

    # Cold start
    process_single_object(
        db=db, entry=entry, norad_id=25544, filter_states=filter_states,
        tle_record=tle_record1,
    )
    assert 25544 in filter_states

    # Warm update
    messages = process_single_object(
        db=db, entry=entry, norad_id=25544, filter_states=filter_states,
        tle_record=tle_record2,
    )

    # Should return at least one message (state_update or anomaly path)
    assert len(messages) >= 1
    assert messages[0]["type"] in {WS_TYPE_STATE_UPDATE, WS_TYPE_ANOMALY}
    # Should have 2 state_history rows now
    cursor = db.execute("SELECT COUNT(*) FROM state_history WHERE norad_id=25544")
    assert cursor.fetchone()[0] == 2


def test_process_single_object_duplicate_epoch_skipped() -> None:
    """Duplicate or out-of-order TLE epoch should return empty list."""
    db = _make_in_memory_db()
    entry = _make_catalog_entry(25544)
    filter_states: dict = {}
    tle_record = _make_tle_record(25544, "2026-03-28T12:00:00Z")

    # Cold start
    process_single_object(
        db=db, entry=entry, norad_id=25544, filter_states=filter_states,
        tle_record=tle_record,
    )

    # Same epoch again — should be skipped
    messages = process_single_object(
        db=db, entry=entry, norad_id=25544, filter_states=filter_states,
        tle_record=tle_record,
    )
    assert messages == []


def test_process_single_object_state_history_written() -> None:
    """State history row should be written on every successful processing step."""
    db = _make_in_memory_db()
    entry = _make_catalog_entry(25544)
    filter_states: dict = {}
    tle_record = _make_tle_record(25544, "2026-03-28T12:00:00Z")

    process_single_object(
        db=db, entry=entry, norad_id=25544, filter_states=filter_states,
        tle_record=tle_record,
    )

    cursor = db.execute(
        "SELECT norad_id, nis, confidence FROM state_history WHERE norad_id=25544"
    )
    row = cursor.fetchone()
    assert row is not None
    assert row[0] == 25544
    assert isinstance(row[1], float)
    assert isinstance(row[2], float)


def test_process_single_object_debris_class() -> None:
    """Debris object class should use the debris Q matrix without error."""
    db = _make_in_memory_db()
    entry = _make_catalog_entry(25544, object_class="debris")
    filter_states: dict = {}
    tle_record = _make_tle_record(25544, "2026-03-28T12:00:00Z")

    messages = process_single_object(
        db=db, entry=entry, norad_id=25544, filter_states=filter_states,
        tle_record=tle_record,
    )
    assert len(messages) == 1
