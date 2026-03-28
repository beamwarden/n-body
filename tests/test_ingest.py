"""Tests for backend/ingest.py.

Covers F-001 through F-006:
  F-003 — TLE checksum validation
  F-004 — SQLite caching (cache_tles, get_cached_tles, get_latest_tle)
  F-005 — catalog config loading (load_catalog_config)
  F-004 — DB initialization (init_catalog_db)

Network-dependent functions (authenticate, fetch_tles, poll_once) are not
tested here; they require integration test infrastructure and live credentials.
"""
import datetime
import json
import os
import sqlite3
import tempfile

import pytest

from backend.ingest import (
    _parse_tle_epoch_utc,
    cache_tles,
    get_cached_tles,
    get_latest_tle,
    init_catalog_db,
    load_catalog_config,
    validate_tle,
)

# ---------------------------------------------------------------------------
# TLE constants with verified checksums (computed via _tle_checksum).
#
# ISS (NORAD 25544), epoch 2024 day 87 (2024-03-27):
#   Line 1 checksum = 2  (last digit of line 1)
#   Line 2 checksum = 7  (last digit of line 2)
#
# HST (NORAD 20580), epoch 2024 day 87 (2024-03-27):
#   Line 1 checksum = 8  (last digit of line 1)
#   Line 2 checksum = 1  (last digit of line 2)
# ---------------------------------------------------------------------------
_VALID_TLE_LINE1 = "1 25544U 98067A   24087.54048742  .00022288  00000+0  39948-3 0  9992"
_VALID_TLE_LINE2 = "2 25544  51.6401 124.3667 0003460 345.1208  14.9821 15.49618259445507"

# Same line 1 with the checksum digit corrupted (0 instead of 2)
_BAD_CHECKSUM_LINE1 = "1 25544U 98067A   24087.54048742  .00022288  00000+0  39948-3 0  9990"
_BAD_CHECKSUM_LINE2 = "2 25544  51.6401 124.3667 0003460 345.1208  14.9821 15.49618259445507"

# A second object (Hubble Space Telescope) for multi-object tests
_HST_TLE_LINE1 = "1 20580U 90037B   24087.55327546  .00001538  00000+0  76175-4 0  9998"
_HST_TLE_LINE2 = "2 20580  28.4691 357.3469 0002725 287.3416  72.7240 15.09366591517191"


# ---------------------------------------------------------------------------
# Helper: in-memory SQLite DB with catalog table
# ---------------------------------------------------------------------------
def _make_db() -> sqlite3.Connection:
    """Create an in-memory catalog DB for test isolation."""
    return init_catalog_db(":memory:")


def _utc(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
) -> datetime.datetime:
    """Convenience factory for UTC-aware datetimes."""
    return datetime.datetime(year, month, day, hour, minute, second, tzinfo=datetime.timezone.utc)


# ---------------------------------------------------------------------------
# validate_tle — F-003
# ---------------------------------------------------------------------------

def test_validate_tle_accepts_valid_tle() -> None:
    """validate_tle returns True for a correctly checksummed TLE."""
    assert validate_tle(_VALID_TLE_LINE1, _VALID_TLE_LINE2) is True


def test_validate_tle_accepts_hst_tle() -> None:
    """validate_tle returns True for a second known-good TLE (HST)."""
    assert validate_tle(_HST_TLE_LINE1, _HST_TLE_LINE2) is True


def test_validate_tle_rejects_bad_checksum() -> None:
    """validate_tle returns False when the checksum digit is wrong."""
    assert validate_tle(_BAD_CHECKSUM_LINE1, _VALID_TLE_LINE2) is False


def test_validate_tle_rejects_both_lines_bad() -> None:
    """validate_tle returns False when both checksum digits are wrong."""
    assert validate_tle(_BAD_CHECKSUM_LINE1, _BAD_CHECKSUM_LINE2) is False


def test_validate_tle_rejects_short_line() -> None:
    """validate_tle returns False if a line is shorter than 69 characters."""
    short = _VALID_TLE_LINE1[:60]
    assert validate_tle(short, _VALID_TLE_LINE2) is False


def test_validate_tle_rejects_wrong_line_ids() -> None:
    """validate_tle returns False if lines don't start with '1' and '2'."""
    # Swap lines
    assert validate_tle(_VALID_TLE_LINE2, _VALID_TLE_LINE1) is False


def test_validate_tle_rejects_empty_strings() -> None:
    """validate_tle returns False for empty input strings."""
    assert validate_tle("", "") is False


# ---------------------------------------------------------------------------
# init_catalog_db — F-004
# ---------------------------------------------------------------------------

def test_init_catalog_db_creates_table() -> None:
    """init_catalog_db creates the tle_catalog table if it does not exist."""
    db = _make_db()
    cursor = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='tle_catalog'"
    )
    assert cursor.fetchone() is not None, "tle_catalog table should exist"
    db.close()


def test_init_catalog_db_is_idempotent() -> None:
    """Calling init_catalog_db twice on the same path does not raise."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        db1 = init_catalog_db(db_path)
        db1.close()
        db2 = init_catalog_db(db_path)
        db2.close()


def test_init_catalog_db_creates_parent_dirs() -> None:
    """init_catalog_db creates intermediate directories as needed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "subdir", "nested", "test.db")
        db = init_catalog_db(db_path)
        db.close()
        assert os.path.exists(db_path)


def test_init_catalog_db_returns_connection() -> None:
    """init_catalog_db returns a live sqlite3.Connection."""
    db = _make_db()
    assert isinstance(db, sqlite3.Connection)
    db.close()


# ---------------------------------------------------------------------------
# cache_tles — F-004
# ---------------------------------------------------------------------------

def _sample_tle_list() -> list[dict]:
    return [
        {
            "norad_id": 25544,
            "epoch_utc": "2024-03-27T12:58:17Z",
            "tle_line1": _VALID_TLE_LINE1,
            "tle_line2": _VALID_TLE_LINE2,
        }
    ]


def _sample_hst_tle_list() -> list[dict]:
    return [
        {
            "norad_id": 20580,
            "epoch_utc": "2024-03-27T13:16:43Z",
            "tle_line1": _HST_TLE_LINE1,
            "tle_line2": _HST_TLE_LINE2,
        }
    ]


def test_cache_tles_inserts_rows() -> None:
    """cache_tles writes the expected number of rows and returns inserted count."""
    db = _make_db()
    fetched_at = _utc(2024, 3, 28, 10)
    count = cache_tles(db, _sample_tle_list(), fetched_at)
    assert count == 1

    cursor = db.execute("SELECT COUNT(*) FROM tle_catalog")
    row = cursor.fetchone()
    assert row[0] == 1
    db.close()


def test_cache_tles_ignores_duplicates() -> None:
    """cache_tles returns 0 for a duplicate (norad_id, epoch_utc) insertion."""
    db = _make_db()
    fetched_at = _utc(2024, 3, 28, 10)
    first = cache_tles(db, _sample_tle_list(), fetched_at)
    second = cache_tles(db, _sample_tle_list(), fetched_at)
    assert first == 1
    assert second == 0

    cursor = db.execute("SELECT COUNT(*) FROM tle_catalog")
    assert cursor.fetchone()[0] == 1
    db.close()


def test_cache_tles_multiple_objects() -> None:
    """cache_tles inserts rows for multiple objects in one call."""
    db = _make_db()
    fetched_at = _utc(2024, 3, 28, 10)
    tles = _sample_tle_list() + _sample_hst_tle_list()
    count = cache_tles(db, tles, fetched_at)
    assert count == 2
    db.close()


def test_cache_tles_rejects_naive_datetime() -> None:
    """cache_tles raises ValueError if fetched_at_utc is not UTC-aware."""
    db = _make_db()
    naive_dt = datetime.datetime(2024, 3, 28, 10, 0, 0)  # no tzinfo
    with pytest.raises(ValueError, match="UTC-aware"):
        cache_tles(db, _sample_tle_list(), naive_dt)
    db.close()


def test_cache_tles_stores_iso8601_fetched_at() -> None:
    """cache_tles stores fetched_at as an ISO 8601 UTC string."""
    db = _make_db()
    fetched_at = _utc(2024, 3, 28, 10, 0, 0)
    cache_tles(db, _sample_tle_list(), fetched_at)
    row = db.execute("SELECT fetched_at FROM tle_catalog").fetchone()
    assert row[0] == "2024-03-28T10:00:00Z"
    db.close()


# ---------------------------------------------------------------------------
# get_cached_tles — F-004
# ---------------------------------------------------------------------------

def _populate_db_with_two_epochs(db: sqlite3.Connection) -> None:
    """Insert ISS TLEs at two different epochs for filter tests."""
    tles_early = [
        {
            "norad_id": 25544,
            "epoch_utc": "2024-03-26T12:00:00Z",
            "tle_line1": _VALID_TLE_LINE1,
            "tle_line2": _VALID_TLE_LINE2,
        }
    ]
    tles_late = [
        {
            "norad_id": 25544,
            "epoch_utc": "2024-03-27T12:00:00Z",
            "tle_line1": _VALID_TLE_LINE1,
            "tle_line2": _VALID_TLE_LINE2,
        }
    ]
    cache_tles(db, tles_early, _utc(2024, 3, 26, 12))
    cache_tles(db, tles_late, _utc(2024, 3, 27, 12))


def test_get_cached_tles_returns_all_without_filter() -> None:
    """get_cached_tles returns all rows for a NORAD ID when since_utc is None."""
    db = _make_db()
    _populate_db_with_two_epochs(db)
    result = get_cached_tles(db, 25544)
    assert len(result) == 2
    db.close()


def test_get_cached_tles_respects_since_filter() -> None:
    """get_cached_tles only returns TLEs with epoch_utc after since_utc."""
    db = _make_db()
    _populate_db_with_two_epochs(db)
    # since_utc is between the two epochs: should return only the later one
    since = _utc(2024, 3, 26, 18)
    result = get_cached_tles(db, 25544, since_utc=since)
    assert len(result) == 1
    assert result[0]["epoch_utc"] == "2024-03-27T12:00:00Z"
    db.close()


def test_get_cached_tles_returns_ascending_order() -> None:
    """get_cached_tles returns rows in ascending epoch_utc order."""
    db = _make_db()
    _populate_db_with_two_epochs(db)
    result = get_cached_tles(db, 25544)
    assert result[0]["epoch_utc"] < result[1]["epoch_utc"]
    db.close()


def test_get_cached_tles_returns_empty_for_unknown_norad() -> None:
    """get_cached_tles returns an empty list for an unknown NORAD ID."""
    db = _make_db()
    result = get_cached_tles(db, 99999)
    assert result == []
    db.close()


def test_get_cached_tles_rejects_naive_since_utc() -> None:
    """get_cached_tles raises ValueError if since_utc is not UTC-aware."""
    db = _make_db()
    naive_dt = datetime.datetime(2024, 3, 26, 18)
    with pytest.raises(ValueError, match="UTC-aware"):
        get_cached_tles(db, 25544, since_utc=naive_dt)
    db.close()


def test_get_cached_tles_dict_keys() -> None:
    """get_cached_tles returns dicts with all expected keys."""
    db = _make_db()
    cache_tles(db, _sample_tle_list(), _utc(2024, 3, 27))
    result = get_cached_tles(db, 25544)
    assert len(result) == 1
    for key in ("norad_id", "epoch_utc", "tle_line1", "tle_line2", "fetched_at"):
        assert key in result[0], f"Missing key: {key}"
    db.close()


# ---------------------------------------------------------------------------
# get_latest_tle — F-004
# ---------------------------------------------------------------------------

def test_get_latest_tle_returns_most_recent() -> None:
    """get_latest_tle returns the TLE with the newest epoch_utc."""
    db = _make_db()
    _populate_db_with_two_epochs(db)
    result = get_latest_tle(db, 25544)
    assert result is not None
    assert result["epoch_utc"] == "2024-03-27T12:00:00Z"
    db.close()


def test_get_latest_tle_returns_none_for_unknown_norad() -> None:
    """get_latest_tle returns None for a NORAD ID with no cached data."""
    db = _make_db()
    result = get_latest_tle(db, 99999)
    assert result is None
    db.close()


def test_get_latest_tle_returns_single_entry() -> None:
    """get_latest_tle works when there is exactly one row."""
    db = _make_db()
    cache_tles(db, _sample_tle_list(), _utc(2024, 3, 27))
    result = get_latest_tle(db, 25544)
    assert result is not None
    assert result["norad_id"] == 25544
    db.close()


# ---------------------------------------------------------------------------
# load_catalog_config — F-005
# ---------------------------------------------------------------------------

def _write_json_config(tmpdir: str, data: object) -> str:
    """Write a JSON object to a temp file and return the path."""
    path = os.path.join(tmpdir, "catalog.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return path


def test_load_catalog_config_returns_norad_ids() -> None:
    """load_catalog_config parses a config file into a list of dicts with norad_id."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = [
            {"norad_id": 25544, "name": "ISS (ZARYA)", "object_class": "active_satellite"},
            {"norad_id": 20580, "name": "HST", "object_class": "active_satellite"},
        ]
        path = _write_json_config(tmpdir, config)
        result = load_catalog_config(path)

    assert len(result) == 2
    assert result[0]["norad_id"] == 25544
    assert result[1]["norad_id"] == 20580


def test_load_catalog_config_returns_full_dicts() -> None:
    """load_catalog_config returns dicts with name and object_class fields intact."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = [
            {"norad_id": 25544, "name": "ISS (ZARYA)", "object_class": "active_satellite"},
        ]
        path = _write_json_config(tmpdir, config)
        result = load_catalog_config(path)

    assert result[0]["name"] == "ISS (ZARYA)"
    assert result[0]["object_class"] == "active_satellite"


def test_load_catalog_config_coerces_string_norad_id() -> None:
    """load_catalog_config converts string norad_id values to int."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = [{"norad_id": "25544", "name": "ISS", "object_class": "active_satellite"}]
        path = _write_json_config(tmpdir, config)
        result = load_catalog_config(path)

    assert isinstance(result[0]["norad_id"], int)
    assert result[0]["norad_id"] == 25544


def test_load_catalog_config_rejects_non_array() -> None:
    """load_catalog_config raises ValueError if JSON root is not an array."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = _write_json_config(tmpdir, {"norad_id": 25544})
        with pytest.raises(ValueError, match="JSON array"):
            load_catalog_config(path)


def test_load_catalog_config_rejects_missing_norad_id() -> None:
    """load_catalog_config raises ValueError if norad_id field is absent."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = [{"name": "ISS", "object_class": "active_satellite"}]
        path = _write_json_config(tmpdir, config)
        with pytest.raises(ValueError, match="norad_id"):
            load_catalog_config(path)


def test_load_catalog_config_rejects_missing_name() -> None:
    """load_catalog_config raises ValueError if name field is absent."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = [{"norad_id": 25544, "object_class": "active_satellite"}]
        path = _write_json_config(tmpdir, config)
        with pytest.raises(ValueError, match="name"):
            load_catalog_config(path)


def test_load_catalog_config_rejects_empty_array() -> None:
    """load_catalog_config raises ValueError for an empty catalog."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = _write_json_config(tmpdir, [])
        with pytest.raises(ValueError, match="empty"):
            load_catalog_config(path)


def test_load_catalog_config_raises_file_not_found() -> None:
    """load_catalog_config raises FileNotFoundError for a missing file."""
    with pytest.raises(FileNotFoundError):
        load_catalog_config("/nonexistent/path/catalog.json")


# ---------------------------------------------------------------------------
# _parse_tle_epoch_utc — internal, tested because it is domain-critical
# ---------------------------------------------------------------------------

def test_parse_tle_epoch_utc_known_value() -> None:
    """_parse_tle_epoch_utc correctly decodes a known TLE epoch."""
    # ISS TLE epoch field: "24087.54048742" => day 87 of 2024
    # Day 87 of 2024 = March 27 (2024 is a leap year: 31+29+27=87)
    result = _parse_tle_epoch_utc(_VALID_TLE_LINE1)
    assert result.startswith("2024-03-27T")


def test_parse_tle_epoch_utc_is_utc_string() -> None:
    """_parse_tle_epoch_utc returns an ISO 8601 UTC string ending with 'Z'."""
    result = _parse_tle_epoch_utc(_VALID_TLE_LINE1)
    assert result.endswith("Z")
    # Must be parseable as a datetime
    dt = datetime.datetime.strptime(result, "%Y-%m-%dT%H:%M:%SZ")
    assert dt.year == 2024


def test_parse_tle_epoch_year_2digit_mapping() -> None:
    """Years >= 57 map to 1900s; years < 57 map to 2000s."""
    # Construct a synthetic TLE line 1 with epoch "57001.0" (Jan 1, 1957)
    # We only need columns 18-32 for the epoch; the rest can be zeroed
    # Use a real line structure, replacing the epoch field
    synthetic = "1 25544U 98067A   57001.00000000  .00022288  00000+0  39948-3 0  9993"
    # Recompute checksum so validate_tle would accept it — but here we just test the parser
    result = _parse_tle_epoch_utc(synthetic)
    assert result.startswith("1957-")

    synthetic2 = "1 25544U 98067A   00001.00000000  .00022288  00000+0  39948-3 0  9993"
    result2 = _parse_tle_epoch_utc(synthetic2)
    assert result2.startswith("2000-")
