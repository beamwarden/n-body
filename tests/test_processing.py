"""Tests for backend/processing.py.

Covers the shared predict-update-anomaly-recalibrate pipeline.
"""

import datetime
import sqlite3
from unittest.mock import patch

import numpy as np

import backend.anomaly as anomaly
import backend.kalman as kalman
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
            source TEXT NOT NULL DEFAULT 'space_track',
            UNIQUE(norad_id, epoch_utc)
        )
        """
    )
    db.commit()
    _ensure_state_history_table(db)
    anomaly.ensure_alerts_table(db)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS filter_active_anomaly (
            norad_id       INTEGER PRIMARY KEY,
            anomaly_row_id INTEGER NOT NULL
        )
        """
    )
    db.commit()
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
    cursor = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='state_history'")
    assert cursor.fetchone() is not None


def test_ensure_state_history_table_idempotent() -> None:
    db = sqlite3.connect(":memory:")
    _ensure_state_history_table(db)
    _ensure_state_history_table(db)  # Must not raise


# ---------------------------------------------------------------------------
# Tests for _build_ws_message
# ---------------------------------------------------------------------------


def test_build_ws_message_schema() -> None:
    epoch_utc = datetime.datetime(2026, 3, 28, 19, 0, 0, tzinfo=datetime.UTC)
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
    epoch_utc = datetime.datetime(2026, 3, 28, 19, 0, 0, tzinfo=datetime.UTC)
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
        generate_tracks=False,
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
        generate_tracks=False,
        db=db,
        entry=entry,
        norad_id=25544,
        filter_states=filter_states,
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
        generate_tracks=False,
        db=db,
        entry=entry,
        norad_id=25544,
        filter_states=filter_states,
        tle_record=tle_record1,
    )
    assert 25544 in filter_states

    # Warm update
    messages = process_single_object(
        generate_tracks=False,
        db=db,
        entry=entry,
        norad_id=25544,
        filter_states=filter_states,
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
        generate_tracks=False,
        db=db,
        entry=entry,
        norad_id=25544,
        filter_states=filter_states,
        tle_record=tle_record,
    )

    # Same epoch again — should be skipped
    messages = process_single_object(
        generate_tracks=False,
        db=db,
        entry=entry,
        norad_id=25544,
        filter_states=filter_states,
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
        generate_tracks=False,
        db=db,
        entry=entry,
        norad_id=25544,
        filter_states=filter_states,
        tle_record=tle_record,
    )

    cursor = db.execute("SELECT norad_id, nis, confidence FROM state_history WHERE norad_id=25544")
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
        generate_tracks=False,
        db=db,
        entry=entry,
        norad_id=25544,
        filter_states=filter_states,
        tle_record=tle_record,
    )
    assert len(messages) == 1


# ---------------------------------------------------------------------------
# Tests for deferred recalibration (active satellite anomaly deferral)
# ---------------------------------------------------------------------------


def _inject_pending_anomaly_state(
    filter_state: dict,
    anomaly_row_id: int,
    anomaly_type: str,
    detection_epoch: datetime.datetime,
    timeout_hours: float = 2.0,
) -> None:
    """Directly set _pending_anomaly_check keys on a filter state for testing."""
    filter_state["_pending_anomaly_check"] = True
    filter_state["_pending_anomaly_row_id"] = anomaly_row_id
    filter_state["_pending_anomaly_type"] = anomaly_type
    filter_state["_pending_anomaly_nis"] = 300.0
    filter_state["_pending_anomaly_innovation"] = [10.0, 5.0, 3.0, 0.01, 0.01, 0.01]
    filter_state["_pending_anomaly_epoch_utc"] = detection_epoch
    filter_state["_pending_anomaly_timeout_utc"] = detection_epoch + datetime.timedelta(hours=timeout_hours)


def test_active_satellite_anomaly_defers_recalibration() -> None:
    """Cycle 1: anomaly on active satellite sets _pending_anomaly_check, no recal message."""
    db = _make_in_memory_db()
    entry = _make_catalog_entry(25544, object_class="active_satellite")
    filter_states: dict = {}

    # Cold start
    process_single_object(
        generate_tracks=False,
        db=db,
        entry=entry,
        norad_id=25544,
        filter_states=filter_states,
        tle_record=_make_tle_record(25544, "2026-03-28T12:00:00Z"),
    )

    # Force a high NIS on the next cycle by patching classify_anomaly.
    with patch("backend.processing.anomaly.classify_anomaly", return_value=anomaly.ANOMALY_DIVERGENCE):
        messages = process_single_object(
            generate_tracks=False,
            db=db,
            entry=entry,
            norad_id=25544,
            filter_states=filter_states,
            tle_record=_make_tle_record(25544, "2026-03-28T12:30:00Z"),
        )

    # Cycle 1: should emit provisional anomaly but NO recalibration message.
    assert len(messages) == 1
    assert messages[0]["type"] == WS_TYPE_ANOMALY
    assert messages[0]["anomaly_type"] == anomaly.ANOMALY_DIVERGENCE

    # Pending state must be set.
    fs = filter_states[25544]
    assert fs.get("_pending_anomaly_check") is True
    assert fs.get("_pending_anomaly_type") == anomaly.ANOMALY_DIVERGENCE


def test_active_satellite_two_consecutive_exceedances_classified_as_maneuver() -> None:
    """Cycle 2: two consecutive NIS exceedances -> anomaly upgraded to maneuver + recalibration."""
    db = _make_in_memory_db()
    entry = _make_catalog_entry(25544, object_class="active_satellite")
    filter_states: dict = {}
    detection_epoch = datetime.datetime(2026, 3, 28, 12, 30, 0, tzinfo=datetime.UTC)

    # Cold start
    process_single_object(
        generate_tracks=False,
        db=db,
        entry=entry,
        norad_id=25544,
        filter_states=filter_states,
        tle_record=_make_tle_record(25544, "2026-03-28T12:00:00Z"),
    )

    # Manually inject a provisional filter_divergence pending state.
    fs = filter_states[25544]
    row_id: int = anomaly.record_anomaly(
        db=db,
        norad_id=25544,
        detection_epoch_utc=detection_epoch,
        anomaly_type=anomaly.ANOMALY_DIVERGENCE,
        nis_value=247.2,
    )
    _inject_pending_anomaly_state(fs, row_id, anomaly.ANOMALY_DIVERGENCE, detection_epoch)

    # Cycle 2: classify_anomaly returns MANEUVER (2 consecutive exceedances).
    with patch("backend.processing.anomaly.classify_anomaly", return_value=anomaly.ANOMALY_MANEUVER):
        messages = process_single_object(
            generate_tracks=False,
            db=db,
            entry=entry,
            norad_id=25544,
            filter_states=filter_states,
            tle_record=_make_tle_record(25544, "2026-03-28T13:00:00Z"),
        )

    # Should emit anomaly (upgraded to maneuver) + recalibration.
    assert len(messages) == 2
    assert messages[0]["type"] == WS_TYPE_ANOMALY
    assert messages[0]["anomaly_type"] == anomaly.ANOMALY_MANEUVER
    assert messages[1]["type"] == WS_TYPE_RECALIBRATION

    # Pending state must be cleared.
    fs = filter_states[25544]
    assert "_pending_anomaly_check" not in fs

    # DB record should be updated to maneuver.
    cursor = db.execute("SELECT anomaly_type FROM alerts WHERE id=?", (row_id,))
    assert cursor.fetchone()[0] == anomaly.ANOMALY_MANEUVER


def test_non_active_satellite_recalibrates_immediately() -> None:
    """Debris object: anomaly triggers immediate recalibration, no deferral."""
    db = _make_in_memory_db()
    entry = _make_catalog_entry(25544, object_class="debris")
    filter_states: dict = {}

    # Cold start
    process_single_object(
        generate_tracks=False,
        db=db,
        entry=entry,
        norad_id=25544,
        filter_states=filter_states,
        tle_record=_make_tle_record(25544, "2026-03-28T12:00:00Z"),
    )

    with patch("backend.processing.anomaly.classify_anomaly", return_value=anomaly.ANOMALY_DIVERGENCE):
        messages = process_single_object(
            generate_tracks=False,
            db=db,
            entry=entry,
            norad_id=25544,
            filter_states=filter_states,
            tle_record=_make_tle_record(25544, "2026-03-28T12:30:00Z"),
        )

    # Debris: immediate recalibration — both anomaly AND recalibration messages.
    assert len(messages) == 2
    assert messages[0]["type"] == WS_TYPE_ANOMALY
    assert messages[1]["type"] == WS_TYPE_RECALIBRATION

    # No pending state.
    assert "_pending_anomaly_check" not in filter_states[25544]


def test_pending_anomaly_timeout_resolves_as_provisional_type() -> None:
    """If timeout elapses before cycle 2, resolve as provisional type and recalibrate."""
    db = _make_in_memory_db()
    entry = _make_catalog_entry(25544, object_class="active_satellite")
    filter_states: dict = {}
    detection_epoch = datetime.datetime(2026, 3, 28, 12, 30, 0, tzinfo=datetime.UTC)

    # Cold start
    process_single_object(
        generate_tracks=False,
        db=db,
        entry=entry,
        norad_id=25544,
        filter_states=filter_states,
        tle_record=_make_tle_record(25544, "2026-03-28T12:00:00Z"),
    )

    # Inject pending state with a timeout already in the past.
    fs = filter_states[25544]
    row_id = anomaly.record_anomaly(
        db=db,
        norad_id=25544,
        detection_epoch_utc=detection_epoch,
        anomaly_type=anomaly.ANOMALY_DIVERGENCE,
        nis_value=247.2,
    )
    _inject_pending_anomaly_state(fs, row_id, anomaly.ANOMALY_DIVERGENCE, detection_epoch, timeout_hours=0.0)
    # Force timeout: epoch far in the future relative to timeout.
    fs["_pending_anomaly_timeout_utc"] = detection_epoch  # already past

    # Cycle 2 epoch is after the timeout.
    with patch("backend.processing.anomaly.classify_anomaly", return_value=None):
        messages = process_single_object(
            generate_tracks=False,
            db=db,
            entry=entry,
            norad_id=25544,
            filter_states=filter_states,
            tle_record=_make_tle_record(25544, "2026-03-28T15:00:00Z"),
        )

    # Should resolve: anomaly + recalibration with the provisional type.
    assert len(messages) == 2
    assert messages[0]["type"] == WS_TYPE_ANOMALY
    assert messages[0]["anomaly_type"] == anomaly.ANOMALY_DIVERGENCE
    assert messages[1]["type"] == WS_TYPE_RECALIBRATION

    # Pending state cleared.
    assert "_pending_anomaly_check" not in filter_states[25544]


# ---------------------------------------------------------------------------
# Maneuver vs. filter_divergence distinction contract
#
# These tests document the exact behavioral difference between the two
# classifications from the perspective of an external observer (the operator
# watching the WebSocket stream and the alerts table).
#
# Maneuver (ANOMALY_MANEUVER):
#   - Requires active_satellite object class
#   - Requires >= 2 CONSECUTIVE NIS exceedances (MANEUVER_CONSECUTIVE_CYCLES)
#   - Recalibration uses inflation_factor = 20.0 (large covariance reset)
#   - DB record: anomaly_type = 'maneuver'
#   - WS: cycle-1 emits provisional 'filter_divergence'; cycle-2 corrects to 'maneuver'
#
# Filter divergence (ANOMALY_DIVERGENCE):
#   - Any object class
#   - Single NIS exceedance is sufficient
#   - Recalibration uses inflation_factor = 10.0
#   - DB record: anomaly_type = 'filter_divergence'
#   - WS: non-active satellites emit immediately; active satellites defer one cycle
#         but stay 'filter_divergence' if cycle-2 NIS is below threshold
# ---------------------------------------------------------------------------


def _setup_warm_filter(
    db: sqlite3.Connection,
    norad_id: int,
    object_class: str,
) -> dict:
    """Cold-start a filter and return filter_states dict ready for warm-path tests."""
    entry = _make_catalog_entry(norad_id, object_class=object_class)
    filter_states: dict = {}
    process_single_object(
        generate_tracks=False,
        db=db,
        entry=entry,
        norad_id=norad_id,
        filter_states=filter_states,
        tle_record=_make_tle_record(norad_id, "2026-03-28T12:00:00Z"),
    )
    return filter_states


def test_maneuver_requires_two_consecutive_exceedances() -> None:
    """MANEUVER classification requires exactly 2+ consecutive NIS exceedances.

    A single exceedance — no matter how large — must never produce 'maneuver'.
    Two consecutive exceedances on an active satellite must always produce 'maneuver'.
    """
    db = _make_in_memory_db()
    entry = _make_catalog_entry(25544, object_class="active_satellite")
    filter_states = _setup_warm_filter(db, 25544, "active_satellite")
    detection_epoch = datetime.datetime(2026, 3, 28, 12, 30, 0, tzinfo=datetime.UTC)

    # --- Single exceedance: must NOT produce maneuver ---
    with patch("backend.processing.anomaly.classify_anomaly", return_value=anomaly.ANOMALY_DIVERGENCE):
        msgs_cycle1 = process_single_object(
            generate_tracks=False,
            db=db,
            entry=entry,
            norad_id=25544,
            filter_states=filter_states,
            tle_record=_make_tle_record(25544, "2026-03-28T12:30:00Z"),
        )
    assert msgs_cycle1[0]["anomaly_type"] == anomaly.ANOMALY_DIVERGENCE, (
        "Single exceedance must never classify as maneuver on cycle 1"
    )
    assert filter_states[25544].get("_pending_anomaly_check") is True, (
        "Active satellite must defer recalibration after single exceedance"
    )

    # Inject the pending row ID so cycle 2 can resolve it.
    row_id = anomaly.record_anomaly(
        db=db,
        norad_id=25544,
        detection_epoch_utc=detection_epoch,
        anomaly_type=anomaly.ANOMALY_DIVERGENCE,
        nis_value=247.2,
    )
    filter_states[25544]["_pending_anomaly_row_id"] = row_id

    # --- Second consecutive exceedance: must produce maneuver ---
    with patch("backend.processing.anomaly.classify_anomaly", return_value=anomaly.ANOMALY_MANEUVER):
        msgs_cycle2 = process_single_object(
            generate_tracks=False,
            db=db,
            entry=entry,
            norad_id=25544,
            filter_states=filter_states,
            tle_record=_make_tle_record(25544, "2026-03-28T13:00:00Z"),
        )
    assert msgs_cycle2[0]["anomaly_type"] == anomaly.ANOMALY_MANEUVER, (
        "Two consecutive exceedances must produce maneuver"
    )


def test_single_exceedance_active_satellite_stays_divergence() -> None:
    """Active satellite: if cycle-2 NIS is normal, provisional type stays filter_divergence.

    This is the key distinction: a brief residual spike that self-corrects
    (e.g., a TLE update error, not a real maneuver) must not be misclassified
    as a maneuver just because it occurred on an active satellite.
    """
    db = _make_in_memory_db()
    entry = _make_catalog_entry(25544, object_class="active_satellite")
    filter_states = _setup_warm_filter(db, 25544, "active_satellite")
    detection_epoch = datetime.datetime(2026, 3, 28, 12, 30, 0, tzinfo=datetime.UTC)

    # Cycle 1: NIS exceedance — provisional filter_divergence, no recal yet.
    with patch("backend.processing.anomaly.classify_anomaly", return_value=anomaly.ANOMALY_DIVERGENCE):
        process_single_object(
            generate_tracks=False,
            db=db,
            entry=entry,
            norad_id=25544,
            filter_states=filter_states,
            tle_record=_make_tle_record(25544, "2026-03-28T12:30:00Z"),
        )

    row_id = anomaly.record_anomaly(
        db=db,
        norad_id=25544,
        detection_epoch_utc=detection_epoch,
        anomaly_type=anomaly.ANOMALY_DIVERGENCE,
        nis_value=247.2,
    )
    filter_states[25544]["_pending_anomaly_row_id"] = row_id

    # Cycle 2: NIS back to normal (classify_anomaly returns None — chain broken).
    with patch("backend.processing.anomaly.classify_anomaly", return_value=None):
        msgs_cycle2 = process_single_object(
            generate_tracks=False,
            db=db,
            entry=entry,
            norad_id=25544,
            filter_states=filter_states,
            tle_record=_make_tle_record(25544, "2026-03-28T13:00:00Z"),
        )

    # Must resolve as filter_divergence, NOT maneuver.
    assert msgs_cycle2[0]["anomaly_type"] == anomaly.ANOMALY_DIVERGENCE, (
        "Single exceedance with normal cycle-2 NIS must stay filter_divergence"
    )
    assert msgs_cycle2[0]["type"] == WS_TYPE_ANOMALY
    assert msgs_cycle2[1]["type"] == WS_TYPE_RECALIBRATION

    # DB record must remain filter_divergence.
    cursor = db.execute("SELECT anomaly_type FROM alerts WHERE id=?", (row_id,))
    assert cursor.fetchone()[0] == anomaly.ANOMALY_DIVERGENCE


def test_restart_no_duplicate_alerts() -> None:
    """Simulated backend restart must not produce duplicate alerts rows.

    Sequence:
      1. Cycle 1 — cold start (filter initialised, no anomaly).
      2. Cycle 2 — NIS exceedance on active satellite; provisional alert inserted.
      3. Simulate restart: clear filter_states (in-memory state lost).
      4. Re-run cycle 1 (same TLE/epoch as original cold start) — on cold start
         the epoch guard will not fire (filter_states is empty), so it re-inits.
         No anomaly is emitted on cold start, so no second INSERT attempt.
      5. Re-run cycle 2 with the anomaly-triggering TLE — record_anomaly is
         called again for the same (norad_id, detection_epoch_utc).
      6. Assert COUNT(*) == 1.
    """
    db = _make_in_memory_db()
    entry = _make_catalog_entry(25544, object_class="active_satellite")
    filter_states: dict = {}

    tle_record1 = _make_tle_record(25544, "2026-03-28T12:00:00Z")
    tle_record2 = _make_tle_record(25544, "2026-03-28T12:30:00Z")

    # Cycle 1 — cold start.
    process_single_object(
        generate_tracks=False,
        db=db,
        entry=entry,
        norad_id=25544,
        filter_states=filter_states,
        tle_record=tle_record1,
    )

    # Cycle 2 — force NIS exceedance so an alert row is written.
    with patch("backend.processing.anomaly.classify_anomaly", return_value=anomaly.ANOMALY_DIVERGENCE):
        process_single_object(
            generate_tracks=False,
            db=db,
            entry=entry,
            norad_id=25544,
            filter_states=filter_states,
            tle_record=tle_record2,
        )

    # Confirm exactly 1 alert row exists before simulated restart.
    cursor = db.execute("SELECT COUNT(*) FROM alerts WHERE norad_id=25544")
    assert cursor.fetchone()[0] == 1

    # Simulate restart: wipe in-memory filter state.
    filter_states.clear()

    # Re-run cycle 1 (cold start again).
    process_single_object(
        generate_tracks=False,
        db=db,
        entry=entry,
        norad_id=25544,
        filter_states=filter_states,
        tle_record=tle_record1,
    )

    # Re-run cycle 2 with the same anomaly-triggering TLE/epoch.
    with patch("backend.processing.anomaly.classify_anomaly", return_value=anomaly.ANOMALY_DIVERGENCE):
        process_single_object(
            generate_tracks=False,
            db=db,
            entry=entry,
            norad_id=25544,
            filter_states=filter_states,
            tle_record=tle_record2,
        )

    # Must still be exactly 1 alert row — INSERT OR IGNORE deduplicates.
    cursor = db.execute("SELECT COUNT(*) FROM alerts WHERE norad_id=25544")
    assert cursor.fetchone()[0] == 1, "Backend restart must not produce duplicate alerts rows for the same epoch"


def test_maneuver_uses_higher_inflation_factor_than_divergence() -> None:
    """Maneuver recalibration inflates covariance 2x more than filter_divergence.

    Maneuver: inflation_factor = 20.0  (larger uncertainty — maneuver destination unknown)
    Divergence: inflation_factor = 10.0 (smaller uncertainty — filter drifted, not jumped)

    This test verifies the correct inflation factor is applied to the filter
    covariance after each classification, not just that trigger_recalibration
    returns the right dict (which is already tested in test_anomaly.py).
    """
    import backend.kalman as kalman_mod

    def _covariance_trace_after_recal(anomaly_type: str) -> float:
        """Return trace of position covariance block after recalibration with given type."""
        epoch = datetime.datetime(2026, 3, 28, 12, 0, 0, tzinfo=datetime.UTC)
        state = np.array([6778.0, 0.0, 0.0, 0.0, 7.67, 0.0], dtype=np.float64)
        fs = kalman_mod.init_filter(
            state_eci_km=state,
            epoch_utc=epoch,
            process_noise_q=kalman_mod.OBJECT_CLASS_Q[kalman_mod.OBJECT_CLASS_ACTIVE],
        )
        params = anomaly.trigger_recalibration(
            norad_id=25544,
            anomaly_type=anomaly_type,
            epoch_utc=epoch,
        )
        fs_recal = kalman_mod.recalibrate(
            filter_state=fs,
            new_observation_eci_km=state,
            epoch_utc=epoch,
            inflation_factor=params["inflation_factor"],
        )
        cov = kalman_mod.get_state(fs_recal)["covariance_km2"]
        return float(cov[0, 0] + cov[1, 1] + cov[2, 2])  # position trace

    trace_maneuver = _covariance_trace_after_recal(anomaly.ANOMALY_MANEUVER)
    trace_divergence = _covariance_trace_after_recal(anomaly.ANOMALY_DIVERGENCE)

    assert trace_maneuver > trace_divergence, (
        f"Maneuver covariance trace ({trace_maneuver:.1f}) must exceed "
        f"divergence trace ({trace_divergence:.1f}) — maneuver uses 2x inflation"
    )
    # Maneuver inflation=20.0, divergence inflation=10.0 -> ratio should be ~2.0
    ratio = trace_maneuver / trace_divergence
    assert 1.8 <= ratio <= 2.2, f"Covariance trace ratio maneuver/divergence = {ratio:.3f}, expected ~2.0"
