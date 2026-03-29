"""Tests for scripts/replay.py.

Covers the replay_tles() function: state_history writes, empty cache handling,
and progress output.
"""
import datetime
import sqlite3
import sys
import tempfile
import os
from pathlib import Path
from unittest.mock import patch
import json

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backend.anomaly as anomaly
import backend.ingest as ingest
import backend.processing as processing
from scripts.replay import replay_tles


# ---------------------------------------------------------------------------
# ISS TLE for use in replay tests (consistent with test_processing.py)
# ---------------------------------------------------------------------------
_ISS_TLE_LINE1 = "1 25544U 98067A   26087.50000000  .00002182  00000-0  40768-4 0  9990"
_ISS_TLE_LINE2 = "2 25544  51.6431 117.2927 0006703  73.5764 286.6011 15.49559025498826"


def _make_test_db_with_catalog_json(
    norad_ids: list[int],
    tles_per_object: int,
    start_epoch_utc: datetime.datetime,
    interval_hours: float = 0.5,  # 30-minute spacing
) -> tuple[str, str]:
    """Create a temporary DB and catalog JSON file for replay testing.

    Returns:
        (db_path, catalog_json_path) as strings.
    """
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test_tle_cache.db")
    catalog_path = os.path.join(tmpdir, "catalog.json")

    # Create catalog JSON
    catalog = [
        {
            "norad_id": nid,
            "name": f"OBJECT-{nid}",
            "object_class": "active_satellite",
        }
        for nid in norad_ids
    ]
    with open(catalog_path, "w") as f:
        json.dump(catalog, f)

    # Populate the DB with TLE records
    db = ingest.init_catalog_db(db_path)
    processing._ensure_state_history_table(db)
    anomaly.ensure_alerts_table(db)

    fetched_at = datetime.datetime.now(datetime.timezone.utc)
    for norad_id in norad_ids:
        for i in range(tles_per_object):
            epoch = start_epoch_utc + datetime.timedelta(hours=i * interval_hours)
            epoch_str = epoch.strftime("%Y-%m-%dT%H:%M:%SZ")
            tle_record = {
                "norad_id": norad_id,
                "epoch_utc": epoch_str,
                "tle_line1": _ISS_TLE_LINE1,
                "tle_line2": _ISS_TLE_LINE2,
            }
            ingest.cache_tles(db, [tle_record], fetched_at_utc=fetched_at)

    db.close()
    return db_path, catalog_path


class TestReplayTles:

    def test_three_tles_produce_state_history_rows(self) -> None:
        """3 TLEs for 1 object should produce at least 1 state_history row.

        Cold start consumes the first TLE (no predict+update, just init).
        Subsequent TLEs produce predict+update rows. So 3 TLEs -> 3 rows
        (1 cold start + 2 warm updates), unless duplicate epochs occur.
        """
        start = datetime.datetime(2026, 3, 28, 10, 0, 0, tzinfo=datetime.timezone.utc)
        db_path, catalog_path = _make_test_db_with_catalog_json(
            norad_ids=[25544],
            tles_per_object=3,
            start_epoch_utc=start,
        )

        replay_tles(
            hours=72,
            db_path=db_path,
            catalog_config_path=catalog_path,
            delay_ms=0,
        )

        db = sqlite3.connect(db_path)
        cursor = db.execute("SELECT COUNT(*) FROM state_history WHERE norad_id=25544")
        count = cursor.fetchone()[0]
        db.close()

        assert count >= 1, f"Expected at least 1 state_history row, got {count}"

    def test_empty_cache_exits_without_error(self, capsys) -> None:
        """Empty TLE cache should print a message and exit cleanly (exit code 0)."""
        import tempfile, json, os
        tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(tmpdir, "empty.db")
        catalog_path = os.path.join(tmpdir, "catalog.json")
        # Create catalog with one object but no TLEs in DB
        with open(catalog_path, "w") as f:
            json.dump([{"norad_id": 25544, "name": "ISS", "object_class": "active_satellite"}], f)

        # Should not raise — exits cleanly
        with pytest.raises(SystemExit) as exc_info:
            replay_tles(
                hours=72,
                db_path=db_path,
                catalog_config_path=catalog_path,
                delay_ms=0,
            )

        # Exit code 0 for empty cache (not an error)
        assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert "No cached TLEs" in captured.out or "No cached TLEs" in captured.err

    def test_multiple_objects_all_processed(self) -> None:
        """Multiple objects should each get state_history rows."""
        start = datetime.datetime(2026, 3, 28, 10, 0, 0, tzinfo=datetime.timezone.utc)
        norad_ids = [25544, 44713]
        db_path, catalog_path = _make_test_db_with_catalog_json(
            norad_ids=norad_ids,
            tles_per_object=2,
            start_epoch_utc=start,
        )

        replay_tles(
            hours=72,
            db_path=db_path,
            catalog_config_path=catalog_path,
            delay_ms=0,
        )

        db = sqlite3.connect(db_path)
        for nid in norad_ids:
            cursor = db.execute(
                "SELECT COUNT(*) FROM state_history WHERE norad_id=?", (nid,)
            )
            count = cursor.fetchone()[0]
            assert count >= 1, f"Expected state_history rows for NORAD {nid}, got {count}"
        db.close()

    def test_progress_printed_to_stdout(self, capsys) -> None:
        """replay_tles should print [replay] progress lines to stdout."""
        start = datetime.datetime(2026, 3, 28, 10, 0, 0, tzinfo=datetime.timezone.utc)
        db_path, catalog_path = _make_test_db_with_catalog_json(
            norad_ids=[25544],
            tles_per_object=2,
            start_epoch_utc=start,
        )

        replay_tles(
            hours=72,
            db_path=db_path,
            catalog_config_path=catalog_path,
            delay_ms=0,
        )

        captured = capsys.readouterr()
        assert "[replay]" in captured.out

    def test_invalid_catalog_exits_with_1(self) -> None:
        """Non-existent catalog path should exit with code 1."""
        import tempfile
        tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(tmpdir, "test.db")

        with pytest.raises(SystemExit) as exc_info:
            replay_tles(
                hours=72,
                db_path=db_path,
                catalog_config_path="/nonexistent/catalog.json",
                delay_ms=0,
            )
        assert exc_info.value.code == 1
