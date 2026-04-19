"""Integration tests for GET /object/{norad_id}/conjunctions.

Covers:
- 404 for unknown NORAD ID
- Empty list for known NORAD ID with no conjunction events
- Returns persisted results correctly
- Limits to 5 most recent events
"""
import datetime
import sqlite3

from fastapi.testclient import TestClient

from backend.main import (
    _ensure_conjunction_tables,
    _persist_conjunction_result,
    app,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_catalog_entries(norad_ids: list[int]) -> list[dict]:
    """Build minimal catalog entry dicts."""
    return [
        {
            "norad_id": nid,
            "name": f"OBJECT-{nid}",
            "object_class": "active_satellite",
        }
        for nid in norad_ids
    ]


def _setup_test_db() -> sqlite3.Connection:
    """Create an in-memory SQLite DB with all required tables."""
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    # tle_catalog (needed by ingest.get_latest_tle at catalog endpoint)
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
    # alerts table (needed by anomaly.ensure_alerts_table during lifespan)
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
    # state_history table
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS state_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            norad_id INTEGER NOT NULL,
            epoch_utc TEXT NOT NULL,
            x_km REAL NOT NULL,
            y_km REAL NOT NULL,
            z_km REAL NOT NULL,
            vx_km_s REAL NOT NULL,
            vy_km_s REAL NOT NULL,
            vz_km_s REAL NOT NULL,
            cov_x_km2 REAL NOT NULL,
            cov_y_km2 REAL NOT NULL,
            cov_z_km2 REAL NOT NULL,
            nis REAL NOT NULL,
            confidence REAL NOT NULL,
            anomaly_type TEXT,
            message_type TEXT NOT NULL
        )
        """
    )
    db.commit()
    # Create conjunction tables.
    _ensure_conjunction_tables(db)
    return db


def _make_conjunction_result(
    anomalous_norad_id: int = 25544,
    epoch_str: str = "2026-03-29T12:00:00Z",
    first_order: list | None = None,
    second_order: list | None = None,
) -> dict:
    """Build a minimal conjunction_risk result dict."""
    return {
        "type": "conjunction_risk",
        "anomalous_norad_id": anomalous_norad_id,
        "screening_epoch_utc": epoch_str,
        "horizon_s": 5400,
        "threshold_km": 5.0,
        "first_order": first_order or [],
        "second_order": second_order or [],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_conjunctions_endpoint_404_unknown_norad() -> None:
    """GET /object/{norad_id}/conjunctions returns 404 for unknown NORAD ID."""
    with TestClient(app, raise_server_exceptions=True) as client:
        db = _setup_test_db()
        app.state.db = db
        app.state.catalog_entries = _make_catalog_entries([25544])
        app.state.filter_states = {}

        response = client.get("/object/99999/conjunctions")
        assert response.status_code == 404
        assert "99999" in response.json()["detail"]


def test_conjunctions_endpoint_empty_results() -> None:
    """GET /object/{norad_id}/conjunctions returns [] for an object with no events."""
    with TestClient(app, raise_server_exceptions=True) as client:
        db = _setup_test_db()
        app.state.db = db
        app.state.catalog_entries = _make_catalog_entries([25544])
        app.state.filter_states = {}

        response = client.get("/object/25544/conjunctions")
        assert response.status_code == 200
        assert response.json() == []


def test_conjunctions_endpoint_returns_persisted_results() -> None:
    """Manually insert a conjunction event; verify the endpoint returns it correctly."""
    with TestClient(app, raise_server_exceptions=True) as client:
        db = _setup_test_db()
        app.state.db = db
        app.state.catalog_entries = _make_catalog_entries([25544, 27424])
        app.state.filter_states = {}

        result = _make_conjunction_result(
            anomalous_norad_id=25544,
            epoch_str="2026-03-29T12:00:00Z",
            first_order=[
                {
                    "norad_id": 27424,
                    "name": "DELTA 1 R/B",
                    "min_distance_km": 3.14,
                    "time_of_closest_approach_utc": "2026-03-29T12:30:00Z",
                }
            ],
            second_order=[],
        )
        event_id = _persist_conjunction_result(db, result)
        assert event_id > 0

        response = client.get("/object/25544/conjunctions")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1

        event = data[0]
        assert event["type"] == "conjunction_risk"
        assert event["anomalous_norad_id"] == 25544
        assert event["screening_epoch_utc"] == "2026-03-29T12:00:00Z"
        assert event["horizon_s"] == 5400
        assert abs(event["threshold_km"] - 5.0) < 1e-6

        assert len(event["first_order"]) == 1
        assert event["first_order"][0]["norad_id"] == 27424
        assert abs(event["first_order"][0]["min_distance_km"] - 3.14) < 0.01
        assert event["first_order"][0]["time_of_closest_approach_utc"] == "2026-03-29T12:30:00Z"

        assert event["second_order"] == []


def test_conjunctions_endpoint_limit_5() -> None:
    """Insert 10 conjunction events; verify only the 5 most recent are returned."""
    with TestClient(app, raise_server_exceptions=True) as client:
        db = _setup_test_db()
        app.state.db = db
        app.state.catalog_entries = _make_catalog_entries([25544])
        app.state.filter_states = {}

        base_epoch = datetime.datetime(2026, 3, 29, 0, 0, 0, tzinfo=datetime.UTC)
        inserted_epochs = []
        for i in range(10):
            epoch_utc = base_epoch + datetime.timedelta(hours=i)
            epoch_str = epoch_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
            inserted_epochs.append(epoch_str)
            result = _make_conjunction_result(
                anomalous_norad_id=25544,
                epoch_str=epoch_str,
            )
            _persist_conjunction_result(db, result)

        response = client.get("/object/25544/conjunctions")
        assert response.status_code == 200
        data = response.json()

        # Must return exactly 5 events.
        assert len(data) == 5

        # The endpoint orders by (created_at DESC, id DESC). In a test environment
        # all rows may have the same created_at (second granularity), so the id
        # tiebreaker ensures the last 5 inserted (highest row IDs) are returned.
        returned_epochs = {event["screening_epoch_utc"] for event in data}
        expected_recent = set(inserted_epochs[-5:])
        assert returned_epochs == expected_recent, (
            f"Expected the 5 most recently inserted epochs in results. "
            f"returned={returned_epochs}, expected={expected_recent}"
        )


def test_conjunctions_endpoint_second_order_preserved() -> None:
    """Verify second_order risks are persisted and returned correctly."""
    with TestClient(app, raise_server_exceptions=True) as client:
        db = _setup_test_db()
        app.state.db = db
        app.state.catalog_entries = _make_catalog_entries([25544, 27424, 27386])
        app.state.filter_states = {}

        result = _make_conjunction_result(
            anomalous_norad_id=25544,
            epoch_str="2026-03-29T12:00:00Z",
            first_order=[
                {
                    "norad_id": 27424,
                    "name": "DELTA 1 R/B",
                    "min_distance_km": 3.0,
                    "time_of_closest_approach_utc": "2026-03-29T12:30:00Z",
                }
            ],
            second_order=[
                {
                    "norad_id": 27386,
                    "name": "ATLAS 5 CENTAUR R/B",
                    "min_distance_km": 8.5,
                    "via_norad_id": 27424,
                    "time_of_closest_approach_utc": "2026-03-29T12:45:00Z",
                }
            ],
        )
        _persist_conjunction_result(db, result)

        response = client.get("/object/25544/conjunctions")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1

        event = data[0]
        assert len(event["first_order"]) == 1
        assert len(event["second_order"]) == 1

        so = event["second_order"][0]
        assert so["norad_id"] == 27386
        assert so["via_norad_id"] == 27424
        assert abs(so["min_distance_km"] - 8.5) < 0.01
