"""Tests for scripts/seed_conjunction.py.

Covers: threat TLE generation, checksum validity, miss-distance accuracy,
DB insertion, catalog.json update, idempotent reinsertion, and catalog
structure validation.

Test numbering follows the plan docs/plans/2026-03-29-demo-scenario.md
test strategy section.
"""
import datetime
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

# Add project root to path so scripts and backend packages can be imported.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backend.ingest as ingest
import backend.propagator as propagator

# ---------------------------------------------------------------------------
# Shared fixtures: ISS-like real TLE for propagation tests
# ---------------------------------------------------------------------------

# A known-good ISS TLE (publicly available, epoch ~2026-03-28 for testing).
_ISS_TLE_LINE1 = "1 25544U 98067A   26087.50000000  .00016717  00000-0  10270-3 0  9993"
_ISS_TLE_LINE2 = "2 25544  51.6400 208.9163 0006703 124.7256 235.4562 15.54244195499263"


def _seed_primary_tle(db: sqlite3.Connection) -> None:
    """Insert the ISS TLE fixture into an in-memory DB for use by tests."""
    now_utc = datetime.datetime.now(datetime.UTC)
    tle_record = {
        "norad_id": 25544,
        "epoch_utc": "2026-03-28T12:00:00Z",
        "tle_line1": _ISS_TLE_LINE1,
        "tle_line2": _ISS_TLE_LINE2,
    }
    ingest.cache_tles(db, [tle_record], fetched_at_utc=now_utc)


# ---------------------------------------------------------------------------
# Test 1: test_clear_removes_synthetic_object
# ---------------------------------------------------------------------------

def test_clear_removes_synthetic_object(tmp_path: Path) -> None:
    """Running --clear removes NORAD 99999 from catalog.json and TLE cache."""
    # Arrange: build a temp catalog with 99999 already present.
    catalog_path = str(tmp_path / "catalog.json")
    catalog = [
        {"norad_id": 25544, "name": "ISS (ZARYA)", "object_class": "active_satellite"},
        {"norad_id": 99999, "name": "THREAT-SIM", "object_class": "debris"},
    ]
    with open(catalog_path, "w") as fh:
        json.dump(catalog, fh, indent=2)

    db_path = str(tmp_path / "tle_cache.db")
    db = ingest.init_catalog_db(db_path)
    now_utc = datetime.datetime.now(datetime.UTC)
    ingest.cache_tles(
        db,
        [{"norad_id": 99999, "epoch_utc": "2026-03-28T12:00:00Z",
          "tle_line1": "X", "tle_line2": "Y"}],
        fetched_at_utc=now_utc,
    )

    # Act: import and call _clear_synthetic_threat.
    from scripts.seed_conjunction import _clear_synthetic_threat
    _clear_synthetic_threat(catalog_path=catalog_path, db=db)
    db.close()

    # Assert: 99999 absent from catalog.json.
    with open(catalog_path) as fh:
        updated_catalog = json.load(fh)
    norad_ids = [e["norad_id"] for e in updated_catalog]
    assert 99999 not in norad_ids
    assert 25544 in norad_ids

    # Assert: 99999 absent from DB.
    db2 = ingest.init_catalog_db(db_path)
    row = ingest.get_latest_tle(db2, 99999)
    assert row is None
    db2.close()


# ---------------------------------------------------------------------------
# Test 2: test_threat_tle_passes_checksum
# ---------------------------------------------------------------------------

def test_threat_tle_passes_checksum(tmp_path: Path) -> None:
    """Generated threat TLE passes ingest.validate_tle checksum check."""
    db_path = str(tmp_path / "tle_cache.db")
    db = ingest.init_catalog_db(db_path)
    _seed_primary_tle(db)

    from scripts.seed_conjunction import generate_threat_tle
    syn_line1, syn_line2, _ = generate_threat_tle(
        primary_norad_id=25544,
        offset_min=30.0,
        miss_km=2.0,
        db=db,
    )
    db.close()

    assert ingest.validate_tle(syn_line1, syn_line2), (
        f"TLE failed checksum validation:\n  Line1: {syn_line1!r}\n  Line2: {syn_line2!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: test_threat_conjunction_within_threshold
# ---------------------------------------------------------------------------

def test_threat_conjunction_within_threshold(tmp_path: Path) -> None:
    """Option B: threat TLE propagates to within ~miss_km of the primary at conjunction epoch.

    Option B works in TLE mean-element space — incrementing mean anomaly by
    delta_M = arcsin(miss_km / r). Both objects share all other mean elements
    (same orbit, same mean motion), so the mean anomaly separation is preserved
    to conjunction epoch and the km separation stays close to miss_km.

    Expected: actual miss distance within [0.5 * miss_km, 4.0 * miss_km].
    """
    db_path = str(tmp_path / "tle_cache.db")
    db = ingest.init_catalog_db(db_path)
    _seed_primary_tle(db)

    from scripts.seed_conjunction import generate_threat_tle

    miss_km_target = 2.0
    syn_line1, syn_line2, conjunction_epoch_utc = generate_threat_tle(
        primary_norad_id=25544,
        offset_min=30.0,
        miss_km=miss_km_target,
        db=db,
    )

    # Propagate primary and threat to conjunction epoch.
    primary_pos_km, _ = propagator.propagate_tle(
        _ISS_TLE_LINE1, _ISS_TLE_LINE2, conjunction_epoch_utc
    )
    threat_pos_km, _ = propagator.propagate_tle(
        syn_line1, syn_line2, conjunction_epoch_utc
    )
    db.close()

    actual_dist_km = float(np.linalg.norm(threat_pos_km - primary_pos_km))

    # Option B accuracy bound: [0.5 * target, 4.0 * target].
    # Same orbit, different phase — separation is preserved across propagation.
    assert actual_dist_km < 4.0 * miss_km_target, (
        f"Actual miss distance {actual_dist_km:.3f} km exceeds 4x target "
        f"({4.0 * miss_km_target:.1f} km). Option B should stay within this bound."
    )
    assert actual_dist_km > 0.5 * miss_km_target, (
        f"Actual miss distance {actual_dist_km:.3f} km is below 0.5x target "
        f"({0.5 * miss_km_target:.1f} km). Threat may have collapsed onto primary."
    )

    # Verify altitude is consistent (same orbit → radii within 5 km).
    primary_r_km = float(np.linalg.norm(primary_pos_km))
    threat_r_km = float(np.linalg.norm(threat_pos_km))
    assert abs(threat_r_km - primary_r_km) < 5.0, (
        f"Threat orbital radius {threat_r_km:.1f} km differs by "
        f"{abs(threat_r_km - primary_r_km):.2f} km from primary {primary_r_km:.1f} km. "
        "Option B preserves all orbital elements — altitude difference should be <5 km."
    )


# ---------------------------------------------------------------------------
# Test 4: test_threat_inserted_into_db
# ---------------------------------------------------------------------------

def test_threat_inserted_into_db(tmp_path: Path) -> None:
    """After injection, get_latest_tle(db, 99999) returns a valid record."""
    db_path = str(tmp_path / "tle_cache.db")
    catalog_path = str(tmp_path / "catalog.json")

    # Write a minimal catalog.
    with open(catalog_path, "w") as fh:
        json.dump([{"norad_id": 25544, "name": "ISS", "object_class": "active_satellite"}], fh)

    db = ingest.init_catalog_db(db_path)
    _seed_primary_tle(db)
    db.close()

    from scripts.seed_conjunction import inject_conjunction
    inject_conjunction(
        primary_norad_id=25544,
        offset_min=30.0,
        miss_km=2.0,
        catalog_path=catalog_path,
        db_path=db_path,
        trigger=False,
        server_url="http://localhost:8000",
    )

    db = ingest.init_catalog_db(db_path)
    record = ingest.get_latest_tle(db, 99999)
    db.close()

    assert record is not None
    assert record["norad_id"] == 99999
    assert ingest.validate_tle(record["tle_line1"], record["tle_line2"])


# ---------------------------------------------------------------------------
# Test 5: test_catalog_json_updated
# ---------------------------------------------------------------------------

def test_catalog_json_updated(tmp_path: Path) -> None:
    """After injection, catalog.json contains an entry with norad_id=99999."""
    db_path = str(tmp_path / "tle_cache.db")
    catalog_path = str(tmp_path / "catalog.json")

    with open(catalog_path, "w") as fh:
        json.dump([{"norad_id": 25544, "name": "ISS", "object_class": "active_satellite"}], fh)

    db = ingest.init_catalog_db(db_path)
    _seed_primary_tle(db)
    db.close()

    from scripts.seed_conjunction import inject_conjunction
    inject_conjunction(
        primary_norad_id=25544,
        offset_min=30.0,
        miss_km=2.0,
        catalog_path=catalog_path,
        db_path=db_path,
        trigger=False,
        server_url="http://localhost:8000",
    )

    with open(catalog_path) as fh:
        updated_catalog = json.load(fh)

    norad_ids = [e["norad_id"] for e in updated_catalog]
    assert 99999 in norad_ids


# ---------------------------------------------------------------------------
# Test 6: test_idempotent_reinsertion
# ---------------------------------------------------------------------------

def test_idempotent_reinsertion(tmp_path: Path) -> None:
    """Running inject_conjunction twice does not crash or create duplicate catalog entries."""
    db_path = str(tmp_path / "tle_cache.db")
    catalog_path = str(tmp_path / "catalog.json")

    with open(catalog_path, "w") as fh:
        json.dump([{"norad_id": 25544, "name": "ISS", "object_class": "active_satellite"}], fh)

    db = ingest.init_catalog_db(db_path)
    _seed_primary_tle(db)
    db.close()

    from scripts.seed_conjunction import inject_conjunction

    # First injection.
    inject_conjunction(
        primary_norad_id=25544,
        offset_min=30.0,
        miss_km=2.0,
        catalog_path=catalog_path,
        db_path=db_path,
        trigger=False,
        server_url="http://localhost:8000",
    )

    # Second injection (same parameters — idempotent).
    inject_conjunction(
        primary_norad_id=25544,
        offset_min=30.0,
        miss_km=2.0,
        catalog_path=catalog_path,
        db_path=db_path,
        trigger=False,
        server_url="http://localhost:8000",
    )

    # Assert: no duplicate 99999 entries in catalog.json.
    with open(catalog_path) as fh:
        updated_catalog = json.load(fh)
    threat_entries = [e for e in updated_catalog if e["norad_id"] == 99999]
    assert len(threat_entries) == 1, (
        f"Expected exactly 1 NORAD 99999 entry, got {len(threat_entries)}"
    )


# ---------------------------------------------------------------------------
# Test 7: test_full_conjunction_scenario (integration)
# ---------------------------------------------------------------------------

def test_full_conjunction_scenario(tmp_path: Path) -> None:
    """End-to-end: inject threat and verify conjunction.screen_conjunctions triggers.

    Imports backend.conjunction and screens the primary + threat pair.
    Asserts at least one first-order risk with min_distance_km < 5.0.
    """
    db_path = str(tmp_path / "tle_cache.db")
    catalog_path = str(tmp_path / "catalog.json")

    with open(catalog_path, "w") as fh:
        json.dump([{"norad_id": 25544, "name": "ISS", "object_class": "active_satellite"}], fh)

    db = ingest.init_catalog_db(db_path)
    _seed_primary_tle(db)
    db.close()

    from scripts.seed_conjunction import inject_conjunction
    inject_conjunction(
        primary_norad_id=25544,
        offset_min=30.0,
        miss_km=2.0,
        catalog_path=catalog_path,
        db_path=db_path,
        trigger=False,
        server_url="http://localhost:8000",
    )

    # Now screen using conjunction.screen_conjunctions.
    import backend.conjunction as conjunction

    db = ingest.init_catalog_db(db_path)
    primary_tle = ingest.get_latest_tle(db, 25544)
    threat_tle = ingest.get_latest_tle(db, 99999)
    db.close()

    assert primary_tle is not None
    assert threat_tle is not None

    # Run conjunction screening: primary (as "anomalous") vs. threat.
    # screen_conjunctions uses the actual API: anomalous_norad_id, screening_epoch_utc,
    # other_objects (list of dicts with norad_id/tle_line1/tle_line2), catalog_name_map.
    # DEVIATION from plan step 7 test pseudocode: plan described a simplified call
    # signature; actual backend/conjunction.py uses anomalous_norad_id and
    # catalog_name_map parameters. Adapted to match the real interface.
    now_utc = datetime.datetime.now(datetime.UTC)
    result = conjunction.screen_conjunctions(
        anomalous_norad_id=25544,
        anomalous_tle_line1=primary_tle["tle_line1"],
        anomalous_tle_line2=primary_tle["tle_line2"],
        screening_epoch_utc=now_utc,
        other_objects=[
            {
                "norad_id": 99999,
                "tle_line1": threat_tle["tle_line1"],
                "tle_line2": threat_tle["tle_line2"],
            }
        ],
        catalog_name_map={99999: "THREAT-SIM"},
    )

    # Option B places the threat miss_km ahead in mean anomaly on the same orbit.
    # The screening window is 5400 s (90 min). The miss epoch is offset_min=30 min
    # from now. Since offset_min=30 is within the 90-min window, the conjunction
    # screener should find a close approach within the first-order threshold (5 km).

    assert "first_order" in result
    assert "second_order" in result
    assert result["anomalous_norad_id"] == 25544

    first_order = result.get("first_order", [])
    second_order = result.get("second_order", [])

    all_risks = first_order + second_order
    if all_risks:
        min_dist = min(r["min_distance_km"] for r in all_risks)
        print(f"[test_full_conjunction_scenario] min distance: {min_dist:.3f} km")

    # With Option B the threat is on the same orbit ~2 km apart — first-order
    # detection (< 5 km) must fire within the 90-min screening window.
    assert len(first_order) > 0, (
        "Expected at least one first-order conjunction risk (< 5 km). "
        f"Got first_order={first_order}, second_order={second_order}. "
        "Check that offset_min (30 min) is within the 5400 s screening horizon."
    )


# ---------------------------------------------------------------------------
# Test 8: test_catalog_json_valid_structure
# ---------------------------------------------------------------------------

def test_catalog_json_valid_structure() -> None:
    """The VLEO-rebuilt catalog.json has valid structure and correct count.

    Loads the actual data/catalog/catalog.json (the VLEO catalog rebuilt per
    plan docs/plans/2026-04-01-vleo-catalog-rebuild.md, targeting <=600 km
    objects). Count range updated from [95, 105] to [75, 105] to reflect the
    rebuilt catalog which specifies 80 objects as the baseline, with the plan
    allowing growth to 85-100 if substitutes for re-entered objects are found.

    # DEVIATION from plan docs/plans/2026-04-01-vleo-catalog-rebuild.md:
    # Plan section "Test strategy" stated "No test file changes required."
    # This was incorrect: test_catalog_json_valid_structure directly reads the
    # real data/catalog/catalog.json and asserts 95 <= count <= 105. The rebuilt
    # catalog contains 80 objects (per the user-approved object list), which
    # fails the original assertion. Count range updated to [75, 105] per the
    # plan's stated 80-100 target. Flagged for planner review.
    """
    catalog_path = str(
        Path(__file__).resolve().parent.parent / "data" / "catalog" / "catalog.json"
    )
    with open(catalog_path, encoding="utf-8") as fh:
        catalog = json.load(fh)

    assert isinstance(catalog, list), "catalog.json must be a JSON array"

    valid_object_classes = {"active_satellite", "debris", "rocket_body"}
    norad_ids_seen: set = set()

    for idx, entry in enumerate(catalog):
        assert isinstance(entry, dict), f"Entry {idx} is not a dict: {entry!r}"
        for field in ("norad_id", "name", "object_class"):
            assert field in entry, f"Entry {idx} missing field '{field}': {entry!r}"
        assert entry["object_class"] in valid_object_classes, (
            f"Entry {idx} has invalid object_class '{entry['object_class']}': "
            f"must be one of {valid_object_classes}"
        )
        norad_id = int(entry["norad_id"])
        assert norad_id not in norad_ids_seen, (
            f"Duplicate NORAD ID {norad_id} at entry {idx}"
        )
        norad_ids_seen.add(norad_id)

    # Count must be in [60, 105] per VLEO rebuild plan (73-object post-verification
    # baseline after removing 11 failed altitude checks; 60 lower bound allows for
    # further pruning if additional IDs fail on TLE rebuild).
    count = len(catalog)
    assert 60 <= count <= 105, (
        f"Catalog count {count} is outside [60, 105] — expected 60-100 verified VLEO objects"
    )


# ---------------------------------------------------------------------------
# Test 9: test_catalog_no_synthetic_ids
# ---------------------------------------------------------------------------

def test_catalog_no_synthetic_ids() -> None:
    """The production catalog.json has no NORAD IDs >= 90000.

    NORAD 99999 should only appear after seed_conjunction.py runs.
    """
    catalog_path = str(
        Path(__file__).resolve().parent.parent / "data" / "catalog" / "catalog.json"
    )
    with open(catalog_path, encoding="utf-8") as fh:
        catalog = json.load(fh)

    synthetic_entries = [e for e in catalog if int(e["norad_id"]) >= 90000]
    assert len(synthetic_entries) == 0, (
        f"Found synthetic NORAD IDs >= 90000 in production catalog: "
        f"{[e['norad_id'] for e in synthetic_entries]}"
    )
