"""Tests for backend/anomaly.py."""
import datetime
import sqlite3

import numpy as np
import pytest

from backend.anomaly import (
    ANOMALY_DIVERGENCE,
    ANOMALY_DRAG,
    ANOMALY_MANEUVER,
    MANEUVER_CONSECUTIVE_CYCLES,
    _count_consecutive_tail_exceedances,
    classify_anomaly,
    ensure_alerts_table,
    evaluate_nis,
    get_active_anomalies,
    record_anomaly,
    record_recalibration_complete,
    trigger_recalibration,
)
from backend.kalman import CHI2_THRESHOLD_6DOF

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_THRESHOLD = CHI2_THRESHOLD_6DOF  # 12.592


@pytest.fixture
def db() -> sqlite3.Connection:
    """In-memory SQLite database with the alerts table already created."""
    conn = sqlite3.connect(":memory:")
    ensure_alerts_table(conn)
    yield conn
    conn.close()


@pytest.fixture
def sample_innovation_eci_km() -> "np.ndarray":
    """A generic 6-element innovation vector in ECI km / km/s.

    The position residual is NOT aligned with the velocity residual direction,
    so this vector does NOT trigger the drag anomaly heuristic. Specifically:
    - vel_direction = [0.001, 0.001, 0.001], vel_unit ≈ [0.577, 0.577, 0.577]
    - pos_residual = [1.0, 0.5, -0.7]
    - along_track_km ≈ 0.462, cross_track_km ≈ 1.236 (ratio ≈ 0.37 < 3.0)
    - drag heuristic condition (along > 3*cross and cross < 1.0) is False.

    # DEVIATION from plan docs/plans/2026-03-28-anomaly.md step 9 (fixture):
    # Plan specified [1.0, 1.0, 1.0, 0.001, 0.001, 0.001] and described it as
    # "does NOT trigger the drag anomaly heuristic". That claim is incorrect:
    # when pos_residual = [1,1,1] and vel_direction = [0.001,0.001,0.001], the
    # velocity unit vector is [0.577,0.577,0.577], making pos_residual perfectly
    # along-track (cross_track_km = 0), so the heuristic fires and returns
    # drag_anomaly. Changed to [1.0, 0.5, -0.7, 0.001, 0.001, 0.001] which has
    # significant cross-track component and does not trigger the heuristic.
    # Flagged for planner review.
    """
    return np.array([1.0, 0.5, -0.7, 0.001, 0.001, 0.001], dtype=np.float64)


@pytest.fixture
def utc_epoch() -> datetime.datetime:
    """A representative UTC-aware detection epoch."""
    return datetime.datetime(2026, 3, 28, 19, 0, 0, tzinfo=datetime.timezone.utc)


# ---------------------------------------------------------------------------
# evaluate_nis tests
# ---------------------------------------------------------------------------


def test_evaluate_nis_detects_threshold_exceedance() -> None:
    """evaluate_nis returns True when NIS > threshold."""
    assert evaluate_nis(15.0, _THRESHOLD) is True


def test_evaluate_nis_passes_normal_values() -> None:
    """evaluate_nis returns False when NIS < threshold."""
    assert evaluate_nis(5.0, _THRESHOLD) is False


def test_evaluate_nis_at_exact_threshold() -> None:
    """evaluate_nis returns False at exactly the threshold (not strictly greater)."""
    assert evaluate_nis(_THRESHOLD, _THRESHOLD) is False


def test_evaluate_nis_rejects_negative() -> None:
    """evaluate_nis raises ValueError for negative NIS."""
    with pytest.raises(ValueError):
        evaluate_nis(-1.0, _THRESHOLD)


# ---------------------------------------------------------------------------
# _count_consecutive_tail_exceedances tests
# ---------------------------------------------------------------------------


def test_count_tail_exceedances_two_at_end() -> None:
    """Last two values exceed threshold, first does not."""
    result = _count_consecutive_tail_exceedances([3.0, 15.0, 14.0], _THRESHOLD)
    assert result == 2


def test_count_tail_exceedances_only_last() -> None:
    """Only the final value exceeds threshold."""
    result = _count_consecutive_tail_exceedances([15.0, 3.0, 14.0], _THRESHOLD)
    assert result == 1


def test_count_tail_exceedances_empty_list() -> None:
    """Empty history returns 0."""
    assert _count_consecutive_tail_exceedances([], _THRESHOLD) == 0


def test_count_tail_exceedances_none_exceed() -> None:
    """No values exceed threshold."""
    assert _count_consecutive_tail_exceedances([2.0, 3.0, 4.0], _THRESHOLD) == 0


def test_count_tail_exceedances_all_exceed() -> None:
    """All values exceed threshold."""
    result = _count_consecutive_tail_exceedances([15.0, 16.0, 17.0], _THRESHOLD)
    assert result == 3


# ---------------------------------------------------------------------------
# classify_anomaly tests
# ---------------------------------------------------------------------------


def test_classify_maneuver_requires_consecutive_elevated_nis(
    sample_innovation_eci_km: "np.ndarray",
) -> None:
    """Maneuver requires >= 2 consecutive NIS exceedances on active satellite."""
    nis_history = [3.0, 15.0, 14.0]  # last two exceed threshold
    result = classify_anomaly(
        norad_id=25544,
        nis_history=nis_history,
        innovation_eci_km=sample_innovation_eci_km,
        is_active_satellite=True,
        threshold=_THRESHOLD,
    )
    assert result == ANOMALY_MANEUVER


def test_classify_maneuver_requires_active_satellite(
    sample_innovation_eci_km: "np.ndarray",
) -> None:
    """3 consecutive exceedances but is_active_satellite=False -> filter_divergence."""
    nis_history = [15.0, 16.0, 17.0]
    result = classify_anomaly(
        norad_id=99999,
        nis_history=nis_history,
        innovation_eci_km=sample_innovation_eci_km,
        is_active_satellite=False,
        threshold=_THRESHOLD,
    )
    assert result == ANOMALY_DIVERGENCE


def test_classify_divergence_for_inactive_object(
    sample_innovation_eci_km: "np.ndarray",
) -> None:
    """Single NIS exceedance on inactive object -> filter_divergence."""
    nis_history = [3.0, 15.0]
    result = classify_anomaly(
        norad_id=12345,
        nis_history=nis_history,
        innovation_eci_km=sample_innovation_eci_km,
        is_active_satellite=False,
        threshold=_THRESHOLD,
    )
    assert result == ANOMALY_DIVERGENCE


def test_classify_divergence_single_exceedance_active(
    sample_innovation_eci_km: "np.ndarray",
) -> None:
    """Single NIS exceedance on active satellite (only 1 consecutive, not 2) ->
    filter_divergence, NOT maneuver (F-032 requires >= 2 consecutive)."""
    nis_history = [3.0, 15.0]  # only last one exceeds
    result = classify_anomaly(
        norad_id=25544,
        nis_history=nis_history,
        innovation_eci_km=sample_innovation_eci_km,
        is_active_satellite=True,
        threshold=_THRESHOLD,
    )
    assert result == ANOMALY_DIVERGENCE


def test_classify_drag_anomaly() -> None:
    """Dominant along-track residual and small cross-track -> drag_anomaly.

    Construct an innovation where the velocity residual points roughly in the
    +X direction (along-track proxy). The position residual has a large
    component along +X (along-track) and very small Y/Z components (cross-track).
    Single NIS exceedance, so maneuver check will not fire.
    """
    # vel_direction = [1, 0, 0] -> vel_unit = [1, 0, 0]
    # pos_residual = [5, 0.1, 0.1] ->
    #   along_track = |dot([5, 0.1, 0.1], [1,0,0])| = 5.0
    #   cross_track = norm([5,0.1,0.1] - 5*[1,0,0]) = norm([0, 0.1, 0.1]) ≈ 0.141
    #   5.0 > 3 * 0.141 ✓  and  0.141 < 1.0 ✓
    innovation = np.array([5.0, 0.1, 0.1, 1.0, 0.0, 0.0], dtype=np.float64)
    nis_history = [3.0, 15.0]  # single exceedance (consecutive_count=1 < 2)
    result = classify_anomaly(
        norad_id=11111,
        nis_history=nis_history,
        innovation_eci_km=innovation,
        is_active_satellite=False,
        threshold=_THRESHOLD,
    )
    assert result == ANOMALY_DRAG


def test_classify_returns_none_when_below_threshold(
    sample_innovation_eci_km: "np.ndarray",
) -> None:
    """All NIS values below threshold -> None."""
    nis_history = [2.0, 3.0, 4.0]
    result = classify_anomaly(
        norad_id=25544,
        nis_history=nis_history,
        innovation_eci_km=sample_innovation_eci_km,
        is_active_satellite=True,
        threshold=_THRESHOLD,
    )
    assert result is None


def test_classify_empty_history(
    sample_innovation_eci_km: "np.ndarray",
) -> None:
    """Empty nis_history -> None (no anomaly possible)."""
    result = classify_anomaly(
        norad_id=25544,
        nis_history=[],
        innovation_eci_km=sample_innovation_eci_km,
        is_active_satellite=True,
        threshold=_THRESHOLD,
    )
    assert result is None


# ---------------------------------------------------------------------------
# trigger_recalibration tests
# ---------------------------------------------------------------------------


def test_trigger_recalibration_returns_correct_dict(utc_epoch: datetime.datetime) -> None:
    """Returned dict has all expected keys with correct values."""
    result = trigger_recalibration(
        norad_id=25544,
        anomaly_type=ANOMALY_DIVERGENCE,
        epoch_utc=utc_epoch,
    )
    assert result["norad_id"] == 25544
    assert result["anomaly_type"] == ANOMALY_DIVERGENCE
    assert result["epoch_utc"] == utc_epoch
    assert result["inflation_factor"] == 10.0
    assert result["status"] == "pending"


def test_trigger_recalibration_maneuver_inflation(utc_epoch: datetime.datetime) -> None:
    """Maneuver anomaly type produces inflation_factor of 20.0."""
    result = trigger_recalibration(
        norad_id=25544,
        anomaly_type=ANOMALY_MANEUVER,
        epoch_utc=utc_epoch,
    )
    assert result["inflation_factor"] == 20.0


def test_trigger_recalibration_drag_inflation(utc_epoch: datetime.datetime) -> None:
    """Drag anomaly type produces inflation_factor of 10.0."""
    result = trigger_recalibration(
        norad_id=25544,
        anomaly_type=ANOMALY_DRAG,
        epoch_utc=utc_epoch,
    )
    assert result["inflation_factor"] == 10.0


def test_trigger_recalibration_rejects_naive_datetime() -> None:
    """Timezone-naive epoch_utc raises ValueError."""
    naive_epoch = datetime.datetime(2026, 3, 28, 19, 0, 0)  # no tzinfo
    with pytest.raises(ValueError, match="UTC-aware"):
        trigger_recalibration(
            norad_id=25544,
            anomaly_type=ANOMALY_MANEUVER,
            epoch_utc=naive_epoch,
        )


def test_trigger_recalibration_rejects_unknown_type(
    utc_epoch: datetime.datetime,
) -> None:
    """Unknown anomaly_type raises ValueError."""
    with pytest.raises(ValueError):
        trigger_recalibration(
            norad_id=25544,
            anomaly_type="unknown_type",
            epoch_utc=utc_epoch,
        )


# ---------------------------------------------------------------------------
# Database tests
# ---------------------------------------------------------------------------


def test_record_anomaly_writes_to_db(
    db: sqlite3.Connection, utc_epoch: datetime.datetime
) -> None:
    """record_anomaly inserts a row into the alerts table and returns its id."""
    row_id = record_anomaly(
        db=db,
        norad_id=25544,
        detection_epoch_utc=utc_epoch,
        anomaly_type=ANOMALY_MANEUVER,
        nis_value=18.3,
    )
    assert isinstance(row_id, int)
    assert row_id >= 1

    cursor = db.cursor()
    cursor.execute("SELECT norad_id, anomaly_type, nis_value, status FROM alerts WHERE id = ?", (row_id,))
    row = cursor.fetchone()
    assert row is not None
    assert row[0] == 25544
    assert row[1] == ANOMALY_MANEUVER
    assert row[2] == pytest.approx(18.3)
    assert row[3] == "active"


def test_record_anomaly_rejects_naive_datetime(db: sqlite3.Connection) -> None:
    """record_anomaly raises ValueError for timezone-naive epoch."""
    naive_epoch = datetime.datetime(2026, 3, 28, 19, 0, 0)
    with pytest.raises(ValueError, match="UTC-aware"):
        record_anomaly(
            db=db,
            norad_id=25544,
            detection_epoch_utc=naive_epoch,
            anomaly_type=ANOMALY_MANEUVER,
            nis_value=18.3,
        )


def test_record_recalibration_complete_updates_duration(
    db: sqlite3.Connection, utc_epoch: datetime.datetime
) -> None:
    """record_recalibration_complete stores duration and sets status to resolved."""
    row_id = record_anomaly(
        db=db,
        norad_id=25544,
        detection_epoch_utc=utc_epoch,
        anomaly_type=ANOMALY_MANEUVER,
        nis_value=18.3,
    )

    resolution_epoch = utc_epoch + datetime.timedelta(seconds=120)
    record_recalibration_complete(
        db=db,
        anomaly_row_id=row_id,
        resolution_epoch_utc=resolution_epoch,
    )

    cursor = db.cursor()
    cursor.execute(
        "SELECT recalibration_duration_s, status FROM alerts WHERE id = ?", (row_id,)
    )
    row = cursor.fetchone()
    assert row is not None
    assert row[0] == pytest.approx(120.0)
    assert row[1] == "resolved"


def test_record_recalibration_complete_rejects_naive_datetime(
    db: sqlite3.Connection, utc_epoch: datetime.datetime
) -> None:
    """record_recalibration_complete raises ValueError for timezone-naive epoch."""
    row_id = record_anomaly(
        db=db,
        norad_id=25544,
        detection_epoch_utc=utc_epoch,
        anomaly_type=ANOMALY_MANEUVER,
        nis_value=18.3,
    )
    naive_resolution = datetime.datetime(2026, 3, 28, 19, 2, 0)
    with pytest.raises(ValueError, match="UTC-aware"):
        record_recalibration_complete(
            db=db,
            anomaly_row_id=row_id,
            resolution_epoch_utc=naive_resolution,
        )


def test_record_recalibration_complete_missing_id(
    db: sqlite3.Connection, utc_epoch: datetime.datetime
) -> None:
    """record_recalibration_complete raises ValueError for nonexistent row id."""
    with pytest.raises(ValueError, match="No anomaly record with id 9999"):
        record_recalibration_complete(
            db=db,
            anomaly_row_id=9999,
            resolution_epoch_utc=utc_epoch,
        )


def test_get_active_anomalies_excludes_resolved(
    db: sqlite3.Connection, utc_epoch: datetime.datetime
) -> None:
    """get_active_anomalies returns only unresolved anomalies."""
    # Insert two anomalies
    row_id_1 = record_anomaly(
        db=db,
        norad_id=25544,
        detection_epoch_utc=utc_epoch,
        anomaly_type=ANOMALY_MANEUVER,
        nis_value=18.3,
    )
    record_anomaly(
        db=db,
        norad_id=99999,
        detection_epoch_utc=utc_epoch,
        anomaly_type=ANOMALY_DIVERGENCE,
        nis_value=14.1,
    )

    # Resolve the first one
    resolution_epoch = utc_epoch + datetime.timedelta(seconds=300)
    record_recalibration_complete(
        db=db,
        anomaly_row_id=row_id_1,
        resolution_epoch_utc=resolution_epoch,
    )

    active = get_active_anomalies(db)
    assert len(active) == 1
    assert active[0]["norad_id"] == 99999
    assert active[0]["status"] == "active"


def test_get_active_anomalies_parses_datetime(
    db: sqlite3.Connection, utc_epoch: datetime.datetime
) -> None:
    """get_active_anomalies returns detection_epoch_utc as a datetime object."""
    record_anomaly(
        db=db,
        norad_id=25544,
        detection_epoch_utc=utc_epoch,
        anomaly_type=ANOMALY_MANEUVER,
        nis_value=18.3,
    )
    active = get_active_anomalies(db)
    assert len(active) == 1
    epoch = active[0]["detection_epoch_utc"]
    assert isinstance(epoch, datetime.datetime)
    # Round-trip must preserve UTC timezone
    assert epoch.tzinfo is not None


def test_ensure_alerts_table_idempotent(db: sqlite3.Connection) -> None:
    """Calling ensure_alerts_table twice on the same db does not raise."""
    ensure_alerts_table(db)  # db fixture already called it once; call again
    # If no exception, the CREATE TABLE IF NOT EXISTS worked correctly.


def test_datetime_round_trip_preserves_timezone(
    db: sqlite3.Connection,
) -> None:
    """A UTC-aware datetime stored and retrieved retains its timezone offset."""
    epoch = datetime.datetime(2026, 3, 28, 12, 34, 56, tzinfo=datetime.timezone.utc)
    row_id = record_anomaly(
        db=db,
        norad_id=12345,
        detection_epoch_utc=epoch,
        anomaly_type=ANOMALY_DIVERGENCE,
        nis_value=13.0,
    )
    active = get_active_anomalies(db)
    stored_epoch = active[0]["detection_epoch_utc"]
    # Both should be the same instant; verify they compare equal
    assert stored_epoch == epoch
    assert stored_epoch.tzinfo is not None
