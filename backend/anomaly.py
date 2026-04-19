"""Anomaly detection and classification. Interprets Kalman filter residuals as
operationally meaningful events.

Anomaly events are written to an 'alerts' table in SQLite and pushed to
connected WebSocket clients immediately via main.py.

NOTE: This module is a pure data layer. It does NOT import or reference any
WebSocket, FastAPI, or HTTP types. All WebSocket message construction is the
responsibility of main.py.

Coordinate frame: All innovation vectors consumed by this module are expected
in ECI J2000, as produced by kalman.py.

Units: km for position residuals, km/s for velocity residuals, seconds for
duration values.
"""
import datetime
import sqlite3

import numpy as np
from numpy.typing import NDArray

# Import the chi-squared threshold constant from kalman.py so there is a
# single definition. anomaly.py deliberately does not duplicate this value.
from backend.kalman import CHI2_THRESHOLD_6DOF

# Anomaly type constants
ANOMALY_MANEUVER: str = "maneuver"
ANOMALY_DRAG: str = "drag_anomaly"
ANOMALY_DIVERGENCE: str = "filter_divergence"

# F-032: Maneuver classification requires NIS elevation on at least this many
# consecutive update cycles for an active satellite. Resolved 2026-03-28:
# use the more conservative threshold of 2 (not 3 as mentioned in the
# architecture doc section 3.4 — architecture.md predates this resolution).
MANEUVER_CONSECUTIVE_CYCLES: int = 2


def ensure_alerts_table(db: sqlite3.Connection) -> None:
    """Create the alerts table and index if they do not already exist.

    This function must be called at application startup (from main.py) before
    any calls to record_anomaly, record_recalibration_complete, or
    get_active_anomalies.

    The anomaly.py module receives already-open sqlite3.Connection objects; it
    does not open database connections itself. The database path is determined
    by the caller (expected to be os.environ.get("NBODY_DB_PATH",
    "data/catalog/tle_cache.db") in main.py).

    Args:
        db: Open SQLite connection.
    """
    cursor = db.cursor()
    cursor.execute(
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
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_alerts_norad_status
        ON alerts (norad_id, status)
        """
    )
    # Migration: remove duplicate (norad_id, detection_epoch_utc) rows that may
    # exist from prior runs before the unique index was added.  Keep the row
    # with the highest id so that existing foreign references remain valid.
    cursor.execute(
        """
        DELETE FROM alerts WHERE id NOT IN (
            SELECT MAX(id) FROM alerts GROUP BY norad_id, detection_epoch_utc
        )
        """
    )
    cursor.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_norad_epoch_unique
        ON alerts (norad_id, detection_epoch_utc)
        """
    )
    db.commit()


def evaluate_nis(
    nis: float,
    threshold: float = CHI2_THRESHOLD_6DOF,
) -> bool:
    """Check if NIS exceeds the chi-squared critical value.

    Args:
        nis: Current NIS value from Kalman filter update. Must be non-negative.
        threshold: Chi-squared critical value (default: 6 DOF, p=0.05,
            value 12.592 from kalman.CHI2_THRESHOLD_6DOF).

    Returns:
        True if NIS strictly exceeds threshold (anomaly detected), False otherwise.

    Raises:
        ValueError: If nis is negative.
    """
    if nis < 0:
        raise ValueError(f"nis must be non-negative, got {nis}")
    return nis > threshold


def _count_consecutive_tail_exceedances(
    nis_history: list[float],
    threshold: float,
) -> int:
    """Count how many consecutive values at the tail of nis_history exceed threshold.

    Traverses from the end of the list backward, stopping at the first value
    that does not exceed threshold.

    Examples:
        [3.0, 15.0, 14.0] with threshold=12.592 -> 2 (last two exceed)
        [15.0, 3.0, 14.0] with threshold=12.592 -> 1 (only last value exceeds)
        [] -> 0

    Args:
        nis_history: NIS value list (most recent last).
        threshold: The exceedance threshold (strictly greater than).

    Returns:
        Count of consecutive tail exceedances.
    """
    count = 0
    for value in reversed(nis_history):
        if value > threshold:
            count += 1
        else:
            break
    return count


def classify_anomaly(
    norad_id: int,
    nis_history: list[float],
    innovation_eci_km: list[float],
    is_active_satellite: bool,
    threshold: float = CHI2_THRESHOLD_6DOF,
) -> str | None:
    """Classify the type of detected anomaly based on NIS pattern and innovation.

    Classification rules (per F-031, F-032), applied in priority order — first
    match wins:
    1. If the most recent NIS does not exceed threshold: return None.
    2. maneuver: NIS elevated for >= MANEUVER_CONSECUTIVE_CYCLES consecutive
       cycles AND object is active satellite (is_active_satellite=True).
    3. drag_anomaly: systematic along-track residual growth without cross-track
       signature (heuristic using ECI velocity residual as along-track proxy).
    4. filter_divergence: catch-all for any remaining NIS threshold exceedance.

    NOTE: The drag anomaly heuristic uses the velocity residual direction
    (innovation_eci_km[3:6]) as a proxy for the along-track direction. This is
    an ECI simplification — a proper along-track/cross-track decomposition
    requires the object's actual velocity vector to define the RSW frame.
    # TECH DEBT (post-POC): replace with proper RSW frame decomposition.

    Args:
        norad_id: NORAD catalog ID (used for logging context; not mutated).
        nis_history: Recent NIS values (most recent last). May be empty.
        innovation_eci_km: Most recent 6-element innovation vector [dx,dy,dz,dvx,dvy,dvz]
            in ECI km and km/s.
        is_active_satellite: Whether object is classified as active in the catalog.
            The caller (main.py) is responsible for this lookup; anomaly.py does
            NOT query the catalog.
        threshold: NIS threshold for anomaly classification.

    Returns:
        One of ANOMALY_MANEUVER, ANOMALY_DRAG, ANOMALY_DIVERGENCE, or None.
    """
    # Step a: No anomaly if history is empty or most recent NIS is below threshold.
    if not nis_history:
        return None
    if not evaluate_nis(nis_history[-1], threshold):
        return None

    # Step b: Maneuver check (F-032).
    # Requires >= MANEUVER_CONSECUTIVE_CYCLES consecutive NIS exceedances AND
    # the object must be an active satellite.
    consecutive_count = _count_consecutive_tail_exceedances(nis_history, threshold)
    if consecutive_count >= MANEUVER_CONSECUTIVE_CYCLES and is_active_satellite:
        return ANOMALY_MANEUVER

    # Step c: Drag anomaly check.
    # Heuristic: use the velocity residual direction as a proxy for the along-track
    # direction. Compute position residual projection onto that direction.
    # If along-track component dominates cross-track by >= 3:1 and cross-track
    # is small (< 1 km), classify as drag anomaly.
    #
    # TECH DEBT (post-POC): replace with proper RSW frame decomposition using
    # the object's actual velocity vector (not the velocity residual) to define
    # the along-track unit vector.
    innovation_arr: NDArray[np.float64] = np.asarray(
        innovation_eci_km, dtype=np.float64
    )
    pos_residual_km: NDArray[np.float64] = innovation_arr[0:3]
    vel_direction: NDArray[np.float64] = innovation_arr[3:6]
    vel_norm: float = float(np.linalg.norm(vel_direction))

    if vel_norm >= 1e-10:
        vel_unit: NDArray[np.float64] = vel_direction / vel_norm
        along_track_km: float = abs(float(np.dot(pos_residual_km, vel_unit)))
        cross_track_km: float = float(
            np.linalg.norm(pos_residual_km - np.dot(pos_residual_km, vel_unit) * vel_unit)
        )
        if along_track_km > 3.0 * cross_track_km and cross_track_km < 1.0:
            return ANOMALY_DRAG

    # Step d: Fallback — filter divergence.
    return ANOMALY_DIVERGENCE


def trigger_recalibration(
    norad_id: int,
    anomaly_type: str,
    epoch_utc: datetime.datetime,
) -> dict:
    """Create a recalibration parameter dict to be acted on by the filter.

    The returned dict is designed to be passed directly to
    kalman.recalibrate(filter_state, observation_eci_km, epoch_utc,
    inflation_factor=params["inflation_factor"]) by the caller.

    Inflation factors encode physical intuition:
    - maneuver (20.0): large inflation because the orbit has physically changed.
    - drag_anomaly (10.0): moderate inflation for atmospheric model mismatch.
    - filter_divergence (10.0): default inflation for unclassified divergence.

    Args:
        norad_id: NORAD catalog ID.
        anomaly_type: Must be one of ANOMALY_MANEUVER, ANOMALY_DRAG,
            ANOMALY_DIVERGENCE.
        epoch_utc: UTC epoch of anomaly detection. Must be UTC-aware.

    Returns:
        Dict with keys: norad_id, anomaly_type, epoch_utc, inflation_factor,
        status ('pending').

    Raises:
        ValueError: If epoch_utc is timezone-naive or anomaly_type is unknown.
    """
    if epoch_utc.tzinfo is None:
        raise ValueError("epoch_utc must be UTC-aware")

    valid_types = {ANOMALY_MANEUVER, ANOMALY_DRAG, ANOMALY_DIVERGENCE}
    if anomaly_type not in valid_types:
        raise ValueError(
            f"anomaly_type must be one of {valid_types}, got {anomaly_type!r}"
        )

    if anomaly_type == ANOMALY_MANEUVER:
        inflation_factor: float = 20.0
    elif anomaly_type == ANOMALY_DRAG:
        inflation_factor = 10.0
    else:  # ANOMALY_DIVERGENCE
        inflation_factor = 10.0

    return {
        "norad_id": norad_id,
        "anomaly_type": anomaly_type,
        "epoch_utc": epoch_utc,
        "inflation_factor": inflation_factor,
        "status": "pending",
    }


def record_anomaly(
    db: sqlite3.Connection,
    norad_id: int,
    detection_epoch_utc: datetime.datetime,
    anomaly_type: str,
    nis_value: float,
) -> int:
    """Write an anomaly event to the SQLite alerts table.

    The detection_epoch_utc is stored as an ISO-8601 string with timezone
    offset (e.g., '2026-03-28T19:00:00+00:00') so it can be round-tripped
    via datetime.fromisoformat() without losing timezone information.

    Args:
        db: Open SQLite connection.
        norad_id: NORAD catalog ID.
        detection_epoch_utc: UTC epoch of detection. Must be UTC-aware.
        anomaly_type: Classification string.
        nis_value: NIS value at detection.

    Returns:
        Row ID of the inserted record (cursor.lastrowid).

    Raises:
        ValueError: If detection_epoch_utc is timezone-naive.
    """
    if detection_epoch_utc.tzinfo is None:
        raise ValueError("detection_epoch_utc must be UTC-aware")

    cursor = db.cursor()
    cursor.execute(
        """
        INSERT OR IGNORE INTO alerts (norad_id, detection_epoch_utc, anomaly_type, nis_value, status)
        VALUES (?, ?, ?, ?, 'active')
        """,
        (
            norad_id,
            detection_epoch_utc.isoformat(),
            anomaly_type,
            nis_value,
        ),
    )
    db.commit()
    # rowcount is 0 when INSERT OR IGNORE skips the insert because a unique
    # constraint conflict was detected.  In that case, fetch and return the id
    # of the existing row so callers can track it correctly.
    if cursor.rowcount > 0:
        row_id: int = cursor.lastrowid  # type: ignore[assignment]
    else:
        existing = cursor.execute(
            "SELECT id FROM alerts WHERE norad_id = ? AND detection_epoch_utc = ?",
            (norad_id, detection_epoch_utc.isoformat()),
        ).fetchone()
        row_id = existing[0]
    return row_id


def record_recalibration_complete(
    db: sqlite3.Connection,
    anomaly_row_id: int,
    resolution_epoch_utc: datetime.datetime,
) -> None:
    """Update an anomaly record with recalibration completion time.

    Computes recalibration_duration_s as the difference between
    resolution_epoch_utc and the stored detection_epoch_utc, fulfilling F-034.

    Args:
        db: Open SQLite connection.
        anomaly_row_id: Row ID from record_anomaly.
        resolution_epoch_utc: UTC epoch when NIS returned to normal range.
            Must be UTC-aware.

    Raises:
        ValueError: If resolution_epoch_utc is timezone-naive or if no anomaly
            record with anomaly_row_id exists.
    """
    if resolution_epoch_utc.tzinfo is None:
        raise ValueError("resolution_epoch_utc must be UTC-aware")

    cursor = db.cursor()
    cursor.execute(
        "SELECT detection_epoch_utc FROM alerts WHERE id = ?",
        (anomaly_row_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise ValueError(f"No anomaly record with id {anomaly_row_id}")

    # Parse ISO-8601 string back to UTC-aware datetime.
    # datetime.fromisoformat() in Python 3.11+ handles the '+00:00' suffix.
    detection_epoch_utc: datetime.datetime = datetime.datetime.fromisoformat(row[0])

    recalibration_duration_s: float = (
        resolution_epoch_utc - detection_epoch_utc
    ).total_seconds()

    cursor.execute(
        """
        UPDATE alerts SET
            resolution_epoch_utc = ?,
            recalibration_duration_s = ?,
            status = 'resolved'
        WHERE id = ?
        """,
        (
            resolution_epoch_utc.isoformat(),
            recalibration_duration_s,
            anomaly_row_id,
        ),
    )
    db.commit()


def update_anomaly_type(
    db: sqlite3.Connection,
    anomaly_row_id: int,
    new_anomaly_type: str,
) -> None:
    """Update the anomaly_type column for an existing alerts row.

    Used by processing.py to retroactively change a provisional
    'filter_divergence' record to 'maneuver' (or another type) once the
    deferred classification is confirmed on the second exceedance cycle.

    Args:
        db: Open SQLite connection.
        anomaly_row_id: Row ID returned by record_anomaly.
        new_anomaly_type: New anomaly type string. Must be one of the
            ANOMALY_* constants.

    Raises:
        ValueError: If no anomaly record with anomaly_row_id exists, or if
            new_anomaly_type is not a recognised constant.
    """
    valid_types = {ANOMALY_MANEUVER, ANOMALY_DRAG, ANOMALY_DIVERGENCE}
    if new_anomaly_type not in valid_types:
        raise ValueError(
            f"new_anomaly_type must be one of {valid_types}, got {new_anomaly_type!r}"
        )

    cursor = db.cursor()
    cursor.execute(
        "SELECT id FROM alerts WHERE id = ?",
        (anomaly_row_id,),
    )
    if cursor.fetchone() is None:
        raise ValueError(f"No anomaly record with id {anomaly_row_id}")

    cursor.execute(
        "UPDATE alerts SET anomaly_type = ? WHERE id = ?",
        (new_anomaly_type, anomaly_row_id),
    )
    db.commit()


def get_active_anomalies(db: sqlite3.Connection) -> list[dict]:
    """Retrieve all unresolved anomaly records.

    Returns anomalies with status != 'resolved'. The detection_epoch_utc
    field is parsed back to a UTC-aware datetime object in each returned dict.

    Args:
        db: Open SQLite connection.

    Returns:
        List of dicts, each with keys: id, norad_id, detection_epoch_utc
        (datetime), anomaly_type, nis_value, status.
    """
    cursor = db.cursor()
    cursor.execute(
        """
        SELECT id, norad_id, detection_epoch_utc, anomaly_type, nis_value, status
        FROM alerts
        WHERE status NOT IN ('resolved', 'dismissed')
        ORDER BY id ASC
        """
    )
    results: list[dict] = []
    for row in cursor.fetchall():
        row_id, norad_id, detection_epoch_str, anomaly_type, nis_value, status = row
        detection_epoch_utc = datetime.datetime.fromisoformat(detection_epoch_str)
        results.append(
            {
                "id": row_id,
                "norad_id": norad_id,
                "detection_epoch_utc": detection_epoch_utc,
                "anomaly_type": anomaly_type,
                "nis_value": nis_value,
                "status": status,
            }
        )
    return results


def dismiss_alert(
    db: sqlite3.Connection,
    norad_id: int,
    detection_epoch_utc: str,
) -> bool:
    """Mark an alert as dismissed so it is excluded from future active queries.

    Looks up the alert by (norad_id, detection_epoch_utc) and sets its status
    to 'dismissed'. Dismissed alerts are never re-shown on page reload.

    Args:
        db: Open SQLite connection.
        norad_id: NORAD catalog ID of the alerted object.
        detection_epoch_utc: ISO-8601 UTC string matching detection_epoch_utc
            in the alerts table (e.g. '2026-04-15T12:34:56Z').

    Returns:
        True if a row was updated, False if no matching alert was found.
    """
    cursor = db.execute(
        """
        UPDATE alerts
        SET status = 'dismissed'
        WHERE norad_id = ? AND detection_epoch_utc = ?
          AND status NOT IN ('resolved', 'dismissed')
        """,
        (norad_id, detection_epoch_utc),
    )
    db.commit()
    return cursor.rowcount > 0
