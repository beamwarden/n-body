"""Tests for GET /object/{norad_id}/anomalies endpoint.

Covers plan docs/plans/2026-03-29-history-tracks-cones.md Step 1.1 test cases:
- Empty alerts table returns [].
- Three inserted records return in descending epoch order, limited to 20.
- Resolved anomaly includes resolution_epoch_utc and recalibration_duration_s.
- Invalid NORAD ID returns 404.
- Endpoint performs no writes (read-only).
"""
import datetime
import sqlite3

import pytest
from fastapi.testclient import TestClient

import backend.anomaly as anomaly
from backend.main import app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_in_memory_db() -> sqlite3.Connection:
    """Create an in-memory SQLite database with the alerts table."""
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    # Ensure tle_catalog table exists (required by lifespan startup helpers).
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
    # state_history table is also required.
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS state_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            norad_id INTEGER NOT NULL,
            epoch_utc TEXT NOT NULL,
            nis REAL,
            confidence REAL,
            anomaly_flag INTEGER,
            inserted_at TEXT
        )
        """
    )
    anomaly.ensure_alerts_table(db)
    db.commit()
    return db


def _setup_app_state(norad_ids: list[int], db: sqlite3.Connection) -> None:
    """Configure app.state with the given catalog and DB for tests."""
    app.state.catalog_entries = [
        {"norad_id": nid, "name": f"OBJ-{nid}", "object_class": "active_satellite"}
        for nid in norad_ids
    ]
    app.state.filter_states = {}
    app.state.db = db


def _insert_alert(
    db: sqlite3.Connection,
    norad_id: int,
    detection_epoch_utc: datetime.datetime,
    anomaly_type: str = "maneuver",
    nis_value: float = 18.0,
    resolution_epoch_utc: datetime.datetime | None = None,
    status: str = "active",
) -> int:
    """Insert a single alert row and return its row ID."""
    cursor = db.execute(
        """
        INSERT INTO alerts
            (norad_id, detection_epoch_utc, anomaly_type, nis_value, status)
        VALUES (?, ?, ?, ?, ?)
        """,
        (norad_id, detection_epoch_utc.isoformat(), anomaly_type, nis_value, status),
    )
    row_id: int = cursor.lastrowid  # type: ignore[assignment]
    db.commit()
    if resolution_epoch_utc is not None:
        anomaly.record_recalibration_complete(db, row_id, resolution_epoch_utc)
    return row_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_empty_alerts_table_returns_empty_list() -> None:
    """GET /object/{norad_id}/anomalies with no rows returns []."""
    db = _make_in_memory_db()
    with TestClient(app) as client:
        _setup_app_state([25544], db)
        response = client.get("/object/25544/anomalies")
    assert response.status_code == 200
    assert response.json() == []


def test_three_records_returned_in_descending_epoch_order() -> None:
    """Three inserted records are returned newest-first."""
    db = _make_in_memory_db()
    base = datetime.datetime(2026, 3, 28, 12, 0, 0, tzinfo=datetime.timezone.utc)
    _insert_alert(db, 25544, base, anomaly_type="filter_divergence", nis_value=13.0)
    _insert_alert(db, 25544, base + datetime.timedelta(minutes=30), anomaly_type="drag_anomaly", nis_value=14.0)
    _insert_alert(db, 25544, base + datetime.timedelta(minutes=60), anomaly_type="maneuver", nis_value=21.0)

    with TestClient(app) as client:
        _setup_app_state([25544], db)
        response = client.get("/object/25544/anomalies")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 3
    # Newest (60-minute offset) should come first.
    assert data[0]["anomaly_type"] == "maneuver"
    assert data[1]["anomaly_type"] == "drag_anomaly"
    assert data[2]["anomaly_type"] == "filter_divergence"


def test_limit_of_20_records() -> None:
    """Endpoint returns at most 20 records even when more exist."""
    db = _make_in_memory_db()
    base = datetime.datetime(2026, 3, 28, 0, 0, 0, tzinfo=datetime.timezone.utc)
    for i in range(25):
        _insert_alert(
            db, 25544,
            base + datetime.timedelta(minutes=30 * i),
            nis_value=13.0 + i,
        )

    with TestClient(app) as client:
        _setup_app_state([25544], db)
        response = client.get("/object/25544/anomalies")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 20


def test_resolved_anomaly_includes_resolution_fields() -> None:
    """A resolved anomaly record includes resolution_epoch_utc and recalibration_duration_s."""
    db = _make_in_memory_db()
    detection = datetime.datetime(2026, 3, 28, 19, 0, 0, tzinfo=datetime.timezone.utc)
    resolution = detection + datetime.timedelta(minutes=30)
    _insert_alert(
        db, 25544, detection,
        anomaly_type="maneuver",
        nis_value=25.0,
        resolution_epoch_utc=resolution,
        status="active",  # status is updated to 'resolved' by record_recalibration_complete
    )

    with TestClient(app) as client:
        _setup_app_state([25544], db)
        response = client.get("/object/25544/anomalies")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    record = data[0]
    assert record["status"] == "resolved"
    assert record["resolution_epoch_utc"] is not None
    assert record["recalibration_duration_s"] == pytest.approx(1800.0)
    assert record["norad_id"] == 25544
    assert record["anomaly_type"] == "maneuver"
    assert record["nis_value"] == pytest.approx(25.0)


def test_unresolved_anomaly_has_null_resolution_fields() -> None:
    """An active anomaly has null resolution_epoch_utc and recalibration_duration_s."""
    db = _make_in_memory_db()
    detection = datetime.datetime(2026, 3, 28, 19, 0, 0, tzinfo=datetime.timezone.utc)
    _insert_alert(db, 25544, detection, anomaly_type="filter_divergence", nis_value=14.0)

    with TestClient(app) as client:
        _setup_app_state([25544], db)
        response = client.get("/object/25544/anomalies")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    record = data[0]
    assert record["status"] == "active"
    assert record["resolution_epoch_utc"] is None
    assert record["recalibration_duration_s"] is None


def test_invalid_norad_id_returns_404() -> None:
    """NORAD ID not in catalog returns HTTP 404."""
    db = _make_in_memory_db()
    with TestClient(app) as client:
        _setup_app_state([25544], db)
        response = client.get("/object/99999/anomalies")
    assert response.status_code == 404


def test_endpoint_is_read_only() -> None:
    """Calling the endpoint does not insert or modify any rows in the alerts table."""
    db = _make_in_memory_db()
    detection = datetime.datetime(2026, 3, 28, 19, 0, 0, tzinfo=datetime.timezone.utc)
    _insert_alert(db, 25544, detection, anomaly_type="maneuver", nis_value=18.0)

    # Count rows before.
    before_count = db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]

    with TestClient(app) as client:
        _setup_app_state([25544], db)
        response = client.get("/object/25544/anomalies")

    assert response.status_code == 200
    # Count rows after.
    after_count = db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    assert before_count == after_count


def test_only_requested_norad_id_returned() -> None:
    """Records for a different NORAD ID are not included in the response."""
    db = _make_in_memory_db()
    base = datetime.datetime(2026, 3, 28, 12, 0, 0, tzinfo=datetime.timezone.utc)
    _insert_alert(db, 25544, base, anomaly_type="maneuver", nis_value=20.0)
    _insert_alert(db, 99001, base, anomaly_type="drag_anomaly", nis_value=15.0)

    with TestClient(app) as client:
        _setup_app_state([25544, 99001], db)
        response = client.get("/object/25544/anomalies")

    assert response.status_code == 200
    data = response.json()
    assert all(r["norad_id"] == 25544 for r in data)
    assert len(data) == 1
