"""Tests for GET /object/{norad_id}/track endpoint.

Covers plan docs/plans/2026-03-29-history-tracks-cones.md Step 2.1/3.1 test cases:
- Returns correct number of backward points (including reference epoch).
- Returned ECI positions have magnitudes consistent with LEO altitude.
- seconds_forward > 0 returns forward points with uncertainty_radius_km present
  and monotonically increasing.
- No cached TLE returns 404.
- Invalid NORAD ID returns 404.
- Returned epoch_utc strings are valid ISO-8601 and span the expected time range.
"""
import datetime
import math
import sqlite3

from fastapi.testclient import TestClient

import backend.anomaly as anomaly
import backend.kalman as kalman
import backend.propagator as propagator
from backend.main import app

# ---------------------------------------------------------------------------
# ISS TLE (epoch: day 87 of 2026 = 2026-03-28, same as test_processing.py)
# ---------------------------------------------------------------------------
_ISS_TLE_LINE1 = "1 25544U 98067A   26087.50000000  .00002182  00000-0  40768-4 0  9990"
_ISS_TLE_LINE2 = "2 25544  51.6431 117.2927 0006703  73.5764 286.6011 15.49559025498826"

# LEO altitude range: 200–2000 km above Earth's mean radius (~6371 km).
_LEO_RADIUS_MIN_KM = 6371.0 + 200.0
_LEO_RADIUS_MAX_KM = 6371.0 + 2000.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_in_memory_db() -> sqlite3.Connection:
    """Create an in-memory SQLite DB with tle_catalog, state_history, and alerts tables."""
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
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


def _insert_tle(
    db: sqlite3.Connection,
    norad_id: int,
    tle_line1: str = _ISS_TLE_LINE1,
    tle_line2: str = _ISS_TLE_LINE2,
) -> None:
    """Insert a TLE row into tle_catalog."""
    epoch_utc = propagator.tle_epoch_utc(tle_line1).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute(
        """
        INSERT OR IGNORE INTO tle_catalog
            (norad_id, epoch_utc, tle_line1, tle_line2, fetched_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (norad_id, epoch_utc, tle_line1, tle_line2, "2026-03-28T12:00:00Z"),
    )
    db.commit()


def _setup_app_state(
    norad_ids: list[int],
    db: sqlite3.Connection,
    filter_states: dict | None = None,
) -> None:
    """Configure app.state with catalog, DB, and optional filter states."""
    app.state.catalog_entries = [
        {"norad_id": nid, "name": f"OBJ-{nid}", "object_class": "active_satellite"}
        for nid in norad_ids
    ]
    app.state.filter_states = filter_states or {}
    app.state.db = db


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_backward_track_point_count() -> None:
    """seconds_back=300, step_s=60 returns 6 backward points (t=-300,-240,-180,-120,-60,0)."""
    db = _make_in_memory_db()
    _insert_tle(db, 25544)
    with TestClient(app) as client:
        _setup_app_state([25544], db)
        response = client.get(
            "/object/25544/track?seconds_back=300&seconds_forward=0&step_s=60"
        )
    assert response.status_code == 200
    data = response.json()
    assert len(data["backward_track"]) == 6  # t = -300,-240,-180,-120,-60, 0


def test_backward_track_eci_magnitudes_are_leo_consistent() -> None:
    """All returned ECI positions have magnitudes within LEO altitude range."""
    db = _make_in_memory_db()
    _insert_tle(db, 25544)
    with TestClient(app) as client:
        _setup_app_state([25544], db)
        response = client.get(
            "/object/25544/track?seconds_back=600&seconds_forward=0&step_s=60"
        )
    assert response.status_code == 200
    data = response.json()
    for pt in data["backward_track"]:
        r_km = math.sqrt(sum(c ** 2 for c in pt["eci_km"]))
        assert _LEO_RADIUS_MIN_KM <= r_km <= _LEO_RADIUS_MAX_KM, (
            f"ECI magnitude {r_km:.1f} km is outside LEO range for point {pt}"
        )


def test_forward_track_has_uncertainty_radius_km() -> None:
    """seconds_forward > 0 returns forward points with uncertainty_radius_km field."""
    db = _make_in_memory_db()
    _insert_tle(db, 25544)
    with TestClient(app) as client:
        _setup_app_state([25544], db)
        response = client.get(
            "/object/25544/track?seconds_back=60&seconds_forward=300&step_s=60"
        )
    assert response.status_code == 200
    data = response.json()
    assert len(data["forward_track"]) > 0
    for pt in data["forward_track"]:
        assert "uncertainty_radius_km" in pt
        assert pt["uncertainty_radius_km"] > 0


def test_forward_track_uncertainty_increases_monotonically() -> None:
    """uncertainty_radius_km increases (or stays equal) as t increases (default model)."""
    db = _make_in_memory_db()
    _insert_tle(db, 25544)
    # No filter state → default linear growth model.
    with TestClient(app) as client:
        _setup_app_state([25544], db, filter_states={})
        response = client.get(
            "/object/25544/track?seconds_back=60&seconds_forward=600&step_s=60"
        )
    assert response.status_code == 200
    data = response.json()
    radii = [pt["uncertainty_radius_km"] for pt in data["forward_track"]]
    assert len(radii) >= 2, "Need at least 2 forward points to check monotonicity"
    for i in range(1, len(radii)):
        assert radii[i] >= radii[i - 1], (
            f"Uncertainty radius not monotone: radii[{i}]={radii[i]} < radii[{i-1}]={radii[i-1]}"
        )


def test_forward_track_uncertainty_with_filter_state_increases() -> None:
    """With a real filter state, covariance growth model also produces increasing uncertainty."""
    db = _make_in_memory_db()
    _insert_tle(db, 25544)

    # Create a real filter state.
    tle_epoch = propagator.tle_epoch_utc(_ISS_TLE_LINE1)
    initial_state = propagator.tle_to_state_vector_eci_km(_ISS_TLE_LINE1, _ISS_TLE_LINE2, tle_epoch)
    fs = kalman.init_filter(
        state_eci_km=initial_state,
        epoch_utc=tle_epoch,
        process_noise_q=kalman.OBJECT_CLASS_Q[kalman.OBJECT_CLASS_ACTIVE],
    )

    with TestClient(app) as client:
        _setup_app_state([25544], db, filter_states={25544: fs})
        response = client.get(
            "/object/25544/track?seconds_back=60&seconds_forward=600&step_s=60"
        )

    assert response.status_code == 200
    data = response.json()
    radii = [pt["uncertainty_radius_km"] for pt in data["forward_track"]]
    assert len(radii) >= 2
    for i in range(1, len(radii)):
        assert radii[i] >= radii[i - 1], (
            f"Covariance-grown radius not monotone: radii[{i}]={radii[i]} < radii[{i-1}]={radii[i-1]}"
        )


def test_no_cached_tle_returns_404() -> None:
    """GET /object/{norad_id}/track returns 404 when no TLE is cached."""
    db = _make_in_memory_db()
    with TestClient(app) as client:
        _setup_app_state([25544], db)
        response = client.get("/object/25544/track")
    assert response.status_code == 404
    assert "No cached TLE" in response.json()["detail"]


def test_invalid_norad_id_returns_404() -> None:
    """GET /object/{norad_id}/track returns 404 for a NORAD ID not in the catalog."""
    db = _make_in_memory_db()
    with TestClient(app) as client:
        _setup_app_state([25544], db)
        response = client.get("/object/99999/track")
    assert response.status_code == 404


def test_epoch_utc_strings_are_valid_iso8601() -> None:
    """All epoch_utc strings in backward and forward track are valid ISO-8601."""
    db = _make_in_memory_db()
    _insert_tle(db, 25544)
    with TestClient(app) as client:
        _setup_app_state([25544], db)
        response = client.get(
            "/object/25544/track?seconds_back=300&seconds_forward=300&step_s=60"
        )
    assert response.status_code == 200
    data = response.json()
    all_points = data["backward_track"] + data["forward_track"]
    for pt in all_points:
        # datetime.fromisoformat raises ValueError on invalid strings.
        parsed = datetime.datetime.fromisoformat(pt["epoch_utc"])
        assert parsed.tzinfo is not None, "epoch_utc must be timezone-aware"


def test_epoch_utc_strings_span_expected_time_range() -> None:
    """Backward track spans seconds_back before reference; forward spans seconds_forward after."""
    db = _make_in_memory_db()
    _insert_tle(db, 25544)
    seconds_back = 300
    seconds_forward = 300
    step_s = 60
    with TestClient(app) as client:
        _setup_app_state([25544], db)
        response = client.get(
            f"/object/25544/track?seconds_back={seconds_back}&seconds_forward={seconds_forward}&step_s={step_s}"
        )
    assert response.status_code == 200
    data = response.json()

    ref_epoch = datetime.datetime.fromisoformat(data["reference_epoch_utc"])

    # Backward track: earliest point should be ~seconds_back before reference.
    back_epochs = sorted(
        datetime.datetime.fromisoformat(pt["epoch_utc"])
        for pt in data["backward_track"]
    )
    earliest_back = back_epochs[0]
    earliest_offset_s = (ref_epoch - earliest_back).total_seconds()
    assert abs(earliest_offset_s - seconds_back) <= step_s, (
        f"Earliest backward point is {earliest_offset_s:.0f}s before reference, "
        f"expected ~{seconds_back}s"
    )

    # Forward track: latest point should be ~seconds_forward after reference.
    fwd_epochs = sorted(
        datetime.datetime.fromisoformat(pt["epoch_utc"])
        for pt in data["forward_track"]
    )
    latest_fwd = fwd_epochs[-1]
    latest_offset_s = (latest_fwd - ref_epoch).total_seconds()
    assert abs(latest_offset_s - seconds_forward) <= step_s, (
        f"Latest forward point is {latest_offset_s:.0f}s after reference, "
        f"expected ~{seconds_forward}s"
    )


def test_response_includes_norad_id_and_step_s() -> None:
    """Response JSON contains norad_id and step_s fields."""
    db = _make_in_memory_db()
    _insert_tle(db, 25544)
    with TestClient(app) as client:
        _setup_app_state([25544], db)
        response = client.get(
            "/object/25544/track?seconds_back=120&seconds_forward=0&step_s=60"
        )
    assert response.status_code == 200
    data = response.json()
    assert data["norad_id"] == 25544
    assert data["step_s"] == 60
    assert "reference_epoch_utc" in data


def test_uncertainty_radius_clamped_within_bounds() -> None:
    """uncertainty_radius_km is clamped between 1 km and 500 km."""
    db = _make_in_memory_db()
    _insert_tle(db, 25544)
    # No filter state — default model: 1 + 0.5 * (t/300).
    # At t=60s: 1 + 0.5 * (60/300) = 1.1 km (well within bounds).
    with TestClient(app) as client:
        _setup_app_state([25544], db, filter_states={})
        response = client.get(
            "/object/25544/track?seconds_back=0&seconds_forward=300&step_s=60"
        )
    assert response.status_code == 200
    data = response.json()
    for pt in data["forward_track"]:
        assert 1.0 <= pt["uncertainty_radius_km"] <= 500.0
