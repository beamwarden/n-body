"""Anomaly detection and classification. Interprets Kalman filter residuals as
operationally meaningful events.

Anomaly events are written to an 'alerts' table in SQLite and pushed to
connected WebSocket clients immediately via main.py.
"""
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
    raise NotImplementedError("not implemented")


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
    raise NotImplementedError("not implemented")


def trigger_recalibration(
    norad_id: int,
    anomaly_type: str,
    epoch_utc: datetime.datetime,
) -> dict:
    """Create a recalibration event record to be acted on by the filter.

    Args:
        norad_id: NORAD catalog ID.
        anomaly_type: One of ANOMALY_MANEUVER, ANOMALY_DRAG, ANOMALY_DIVERGENCE.
        epoch_utc: UTC epoch of anomaly detection. Must be UTC-aware.

    Returns:
        Dict with recalibration parameters: norad_id, anomaly_type, epoch_utc,
        inflation_factor, status ('pending').
    """
    raise NotImplementedError("not implemented")


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
        detection_epoch_utc: UTC epoch of detection. Must be UTC-aware.
        anomaly_type: Classification string.
        nis_value: NIS value at detection.

    Returns:
        Row ID of the inserted record.
    """
    raise NotImplementedError("not implemented")


def record_recalibration_complete(
    db: sqlite3.Connection,
    anomaly_row_id: int,
    resolution_epoch_utc: datetime.datetime,
) -> None:
    """Update an anomaly record with recalibration completion time.

    Args:
        db: Open SQLite connection.
        anomaly_row_id: Row ID from record_anomaly.
        resolution_epoch_utc: UTC epoch when NIS returned to normal range. Must be UTC-aware.
    """
    raise NotImplementedError("not implemented")


def get_active_anomalies(db: sqlite3.Connection) -> list[dict]:
    """Retrieve all unresolved anomaly records.

    Args:
        db: Open SQLite connection.

    Returns:
        List of anomaly dicts with: norad_id, detection_epoch_utc,
        anomaly_type, nis_value, status.
    """
    raise NotImplementedError("not implemented")
