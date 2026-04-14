"""Tests for N2YO supplemental TLE fallback in backend/ingest.py.

Covers:
  - fetch_tle_n2yo: happy path, error paths, key redaction
  - _select_n2yo_fallback_ids: gap detection, staleness, freshness, cap
  - cache_tles: source column storage
  - init_catalog_db: migration of pre-migration schema
  - poll_once: N2YO fallback integration, key-not-set skip, N2YO total failure
"""
import asyncio
import datetime
import logging
import sqlite3
import tempfile
import os
import unittest.mock
from typing import Optional

import httpx
import pytest

from backend.ingest import (
    _select_n2yo_fallback_ids,
    cache_tles,
    fetch_tle_n2yo,
    init_catalog_db,
    poll_once,
    N2YO_STALE_THRESHOLD_S,
)

# ---------------------------------------------------------------------------
# TLE constants reused from test_ingest.py — ISS (NORAD 25544) with valid checksums
# ---------------------------------------------------------------------------
_VALID_TLE_LINE1 = "1 25544U 98067A   24087.54048742  .00022288  00000+0  39948-3 0  9992"
_VALID_TLE_LINE2 = "2 25544  51.6401 124.3667 0003460 345.1208  14.9821 15.49618259445507"

# HST (NORAD 20580) — second object for multi-object tests
_HST_TLE_LINE1 = "1 20580U 90037B   24087.55327546  .00001538  00000+0  76175-4 0  9998"
_HST_TLE_LINE2 = "2 20580  28.4691 357.3469 0002725 287.3416  72.7240 15.09366591517191"

# Same as _VALID_TLE_LINE1 but with a corrupted checksum digit (0 instead of 2)
_BAD_CHECKSUM_LINE1 = "1 25544U 98067A   24087.54048742  .00022288  00000+0  39948-3 0  9990"

_FAKE_API_KEY = "s3cr3t-n2yo-key"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc(year: int, month: int, day: int, hour: int = 0) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, tzinfo=datetime.timezone.utc)


def _make_db() -> sqlite3.Connection:
    return init_catalog_db(":memory:")


def _n2yo_response_json(
    norad_id: int = 25544,
    satname: str = "ISS (ZARYA)",
    tle_separator: str = "\r\n",
    line1: str = _VALID_TLE_LINE1,
    line2: str = _VALID_TLE_LINE2,
) -> dict:
    """Build a mock N2YO API response dict."""
    return {
        "info": {
            "satname": satname,
            "satid": norad_id,
            "transactionscount": 1,
        },
        "tle": f"{line1}{tle_separator}{line2}",
    }


def _mock_httpx_response(status_code: int, json_body: Optional[dict] = None) -> httpx.Response:
    """Build a minimal httpx.Response for mocking."""
    import json as _json
    content = _json.dumps(json_body).encode() if json_body is not None else b""
    return httpx.Response(status_code, content=content)


# ---------------------------------------------------------------------------
# fetch_tle_n2yo — happy paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_tle_n2yo_success() -> None:
    """fetch_tle_n2yo returns correct dict on a well-formed response."""
    mock_resp = _mock_httpx_response(200, _n2yo_response_json())

    with unittest.mock.patch.object(httpx.AsyncClient, "get", return_value=mock_resp):
        async with httpx.AsyncClient() as client:
            result = await fetch_tle_n2yo(25544, _FAKE_API_KEY, client)

    assert result is not None
    assert result["norad_id"] == 25544
    assert result["tle_line1"] == _VALID_TLE_LINE1
    assert result["tle_line2"] == _VALID_TLE_LINE2
    assert result["epoch_utc"].startswith("2024-03-27T")


@pytest.mark.asyncio
async def test_fetch_tle_n2yo_accepts_unix_newlines() -> None:
    """fetch_tle_n2yo succeeds when the tle field uses \\n instead of \\r\\n."""
    mock_resp = _mock_httpx_response(
        200, _n2yo_response_json(tle_separator="\n")
    )

    with unittest.mock.patch.object(httpx.AsyncClient, "get", return_value=mock_resp):
        async with httpx.AsyncClient() as client:
            result = await fetch_tle_n2yo(25544, _FAKE_API_KEY, client)

    assert result is not None
    assert result["tle_line1"] == _VALID_TLE_LINE1
    assert result["tle_line2"] == _VALID_TLE_LINE2


# ---------------------------------------------------------------------------
# fetch_tle_n2yo — error / None paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_tle_n2yo_returns_none_on_http_error() -> None:
    """fetch_tle_n2yo returns None and does not raise on a non-200 response."""
    mock_resp = _mock_httpx_response(500)

    with unittest.mock.patch.object(httpx.AsyncClient, "get", return_value=mock_resp):
        async with httpx.AsyncClient() as client:
            result = await fetch_tle_n2yo(25544, _FAKE_API_KEY, client)

    assert result is None


@pytest.mark.asyncio
async def test_fetch_tle_n2yo_returns_none_on_network_exception() -> None:
    """fetch_tle_n2yo returns None when an httpx exception is raised."""
    with unittest.mock.patch.object(
        httpx.AsyncClient, "get", side_effect=httpx.ConnectError("connection refused")
    ):
        async with httpx.AsyncClient() as client:
            result = await fetch_tle_n2yo(25544, _FAKE_API_KEY, client)

    assert result is None


@pytest.mark.asyncio
async def test_fetch_tle_n2yo_returns_none_on_missing_tle_key() -> None:
    """fetch_tle_n2yo returns None when the 'tle' key is absent from the response."""
    body = {"info": {"satname": "ISS", "satid": 25544, "transactionscount": 1}}
    mock_resp = _mock_httpx_response(200, body)

    with unittest.mock.patch.object(httpx.AsyncClient, "get", return_value=mock_resp):
        async with httpx.AsyncClient() as client:
            result = await fetch_tle_n2yo(25544, _FAKE_API_KEY, client)

    assert result is None


@pytest.mark.asyncio
async def test_fetch_tle_n2yo_returns_none_on_checksum_fail() -> None:
    """fetch_tle_n2yo returns None when TLE lines fail checksum validation."""
    bad_body = _n2yo_response_json(line1=_BAD_CHECKSUM_LINE1, line2=_VALID_TLE_LINE2)
    mock_resp = _mock_httpx_response(200, bad_body)

    with unittest.mock.patch.object(httpx.AsyncClient, "get", return_value=mock_resp):
        async with httpx.AsyncClient() as client:
            result = await fetch_tle_n2yo(25544, _FAKE_API_KEY, client)

    assert result is None


@pytest.mark.asyncio
async def test_fetch_tle_n2yo_returns_none_on_satid_mismatch() -> None:
    """fetch_tle_n2yo returns None when response satid does not match requested NORAD ID."""
    # Response claims satid=99999 but we requested 25544
    body = _n2yo_response_json(norad_id=99999)
    mock_resp = _mock_httpx_response(200, body)

    with unittest.mock.patch.object(httpx.AsyncClient, "get", return_value=mock_resp):
        async with httpx.AsyncClient() as client:
            result = await fetch_tle_n2yo(25544, _FAKE_API_KEY, client)

    assert result is None


@pytest.mark.asyncio
async def test_fetch_tle_n2yo_redacts_api_key_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    """fetch_tle_n2yo never writes the literal API key value to any log record."""
    mock_resp = _mock_httpx_response(200, _n2yo_response_json())

    with caplog.at_level(logging.DEBUG, logger="backend.ingest"):
        with unittest.mock.patch.object(httpx.AsyncClient, "get", return_value=mock_resp):
            async with httpx.AsyncClient() as client:
                await fetch_tle_n2yo(25544, _FAKE_API_KEY, client)

    for record in caplog.records:
        assert _FAKE_API_KEY not in record.getMessage(), (
            f"API key found in log record: {record.getMessage()!r}"
        )


# ---------------------------------------------------------------------------
# _select_n2yo_fallback_ids
# ---------------------------------------------------------------------------

def test_select_fallback_ids_includes_missing() -> None:
    """IDs with no TLE in the cache are included in fallback selection."""
    db = _make_db()
    now = _utc(2026, 4, 6)
    result = _select_n2yo_fallback_ids(db, [25544, 20580], N2YO_STALE_THRESHOLD_S, 50, now)
    assert 25544 in result
    assert 20580 in result
    db.close()


def test_select_fallback_ids_includes_stale() -> None:
    """IDs with a TLE epoch older than the stale threshold are selected."""
    db = _make_db()
    now = _utc(2026, 4, 6)
    # Insert a TLE with epoch 8 days before now
    stale_epoch = (now - datetime.timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute(
        "INSERT INTO tle_catalog (norad_id, epoch_utc, tle_line1, tle_line2, fetched_at, source) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (25544, stale_epoch, _VALID_TLE_LINE1, _VALID_TLE_LINE2, "2026-03-29T00:00:00Z", "space_track"),
    )
    db.commit()

    result = _select_n2yo_fallback_ids(db, [25544], N2YO_STALE_THRESHOLD_S, 50, now)
    assert 25544 in result
    db.close()


def test_select_fallback_ids_excludes_fresh() -> None:
    """IDs with a TLE epoch within the stale threshold are NOT selected."""
    db = _make_db()
    now = _utc(2026, 4, 6)
    # Insert a TLE with epoch 1 day before now (fresh)
    fresh_epoch = (now - datetime.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute(
        "INSERT INTO tle_catalog (norad_id, epoch_utc, tle_line1, tle_line2, fetched_at, source) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (25544, fresh_epoch, _VALID_TLE_LINE1, _VALID_TLE_LINE2, "2026-04-05T00:00:00Z", "space_track"),
    )
    db.commit()

    result = _select_n2yo_fallback_ids(db, [25544], N2YO_STALE_THRESHOLD_S, 50, now)
    assert 25544 not in result
    db.close()


def test_select_fallback_ids_caps_at_max() -> None:
    """_select_n2yo_fallback_ids returns at most max_ids entries."""
    db = _make_db()
    now = _utc(2026, 4, 6)
    # 80 NORAD IDs with no TLEs — all would qualify
    norad_ids = list(range(10000, 10080))
    result = _select_n2yo_fallback_ids(db, norad_ids, N2YO_STALE_THRESHOLD_S, 50, now)
    assert len(result) == 50
    db.close()


def test_select_fallback_ids_oldest_first_ordering() -> None:
    """_select_n2yo_fallback_ids returns stale IDs ordered oldest epoch first."""
    db = _make_db()
    now = _utc(2026, 4, 6)
    # norad 10001 is 30 days old, norad 10002 is 10 days old — both stale
    for nid, days_ago in [(10001, 30), (10002, 10)]:
        epoch = (now - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        db.execute(
            "INSERT INTO tle_catalog (norad_id, epoch_utc, tle_line1, tle_line2, fetched_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (nid, epoch, _VALID_TLE_LINE1, _VALID_TLE_LINE2, "2026-03-01T00:00:00Z", "space_track"),
        )
    db.commit()

    result = _select_n2yo_fallback_ids(db, [10001, 10002], N2YO_STALE_THRESHOLD_S, 50, now)
    assert result == [10001, 10002], f"Expected oldest-first but got: {result}"
    db.close()


# ---------------------------------------------------------------------------
# cache_tles — source column
# ---------------------------------------------------------------------------

def test_cache_tles_records_source_tag() -> None:
    """cache_tles stores the supplied source tag in the source column."""
    db = _make_db()
    tles = [
        {
            "norad_id": 25544,
            "epoch_utc": "2024-03-27T12:58:17Z",
            "tle_line1": _VALID_TLE_LINE1,
            "tle_line2": _VALID_TLE_LINE2,
        }
    ]
    fetched_at = _utc(2024, 3, 28)
    cache_tles(db, tles, fetched_at, source="n2yo")

    row = db.execute("SELECT source FROM tle_catalog WHERE norad_id=25544").fetchone()
    assert row is not None
    assert row[0] == "n2yo"
    db.close()


def test_cache_tles_default_source_is_space_track() -> None:
    """cache_tles defaults source to 'space_track' when not specified."""
    db = _make_db()
    tles = [
        {
            "norad_id": 25544,
            "epoch_utc": "2024-03-27T12:58:17Z",
            "tle_line1": _VALID_TLE_LINE1,
            "tle_line2": _VALID_TLE_LINE2,
        }
    ]
    fetched_at = _utc(2024, 3, 28)
    cache_tles(db, tles, fetched_at)  # no source kwarg

    row = db.execute("SELECT source FROM tle_catalog WHERE norad_id=25544").fetchone()
    assert row is not None
    assert row[0] == "space_track"
    db.close()


# ---------------------------------------------------------------------------
# init_catalog_db — migration test
# ---------------------------------------------------------------------------

def test_init_catalog_db_migrates_missing_source_column() -> None:
    """init_catalog_db adds the source column to a pre-migration database."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "pre_migration.db")

        # Build a pre-migration schema without the source column
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tle_catalog (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                norad_id    INTEGER NOT NULL,
                epoch_utc   TEXT    NOT NULL,
                tle_line1   TEXT    NOT NULL,
                tle_line2   TEXT    NOT NULL,
                fetched_at  TEXT    NOT NULL,
                UNIQUE(norad_id, epoch_utc)
            )
            """
        )
        # Insert a row using the old schema
        conn.execute(
            "INSERT INTO tle_catalog (norad_id, epoch_utc, tle_line1, tle_line2, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (25544, "2024-03-27T12:58:17Z", _VALID_TLE_LINE1, _VALID_TLE_LINE2, "2024-03-28T10:00:00Z"),
        )
        conn.commit()
        conn.close()

        # Now call init_catalog_db — should add the source column
        db = init_catalog_db(db_path)

        # Verify source column exists
        cols = {row["name"] for row in db.execute("PRAGMA table_info(tle_catalog)").fetchall()}
        assert "source" in cols, f"source column not found; columns: {cols}"

        # Verify existing row received the default value
        row = db.execute("SELECT source FROM tle_catalog WHERE norad_id=25544").fetchone()
        assert row is not None
        assert row[0] == "space_track", f"Expected 'space_track', got {row[0]!r}"

        db.close()


def test_init_catalog_db_migration_is_idempotent() -> None:
    """Calling init_catalog_db twice on a migrated DB does not raise."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "idempotent.db")
        db1 = init_catalog_db(db_path)
        db1.close()
        db2 = init_catalog_db(db_path)
        db2.close()


# ---------------------------------------------------------------------------
# poll_once — N2YO integration (mocked Space-Track and N2YO)
# ---------------------------------------------------------------------------

_CATALOG_ENTRIES = [
    {"norad_id": 25544, "name": "ISS", "object_class": "active_satellite"},
    {"norad_id": 20580, "name": "HST", "object_class": "active_satellite"},
]


def _make_st_tles_for_ids(norad_ids: list[int]) -> list[dict]:
    """Build a list of Space-Track-shaped TLE dicts for the given NORAD IDs."""
    line1_map = {25544: _VALID_TLE_LINE1, 20580: _HST_TLE_LINE1}
    line2_map = {25544: _VALID_TLE_LINE2, 20580: _HST_TLE_LINE2}
    epoch_map = {25544: "2024-03-27T12:58:17Z", 20580: "2024-03-27T13:16:43Z"}
    return [
        {
            "norad_id": nid,
            "epoch_utc": epoch_map[nid],
            "tle_line1": line1_map[nid],
            "tle_line2": line2_map[nid],
        }
        for nid in norad_ids
        if nid in line1_map
    ]


@pytest.mark.asyncio
async def test_poll_once_falls_back_to_n2yo_for_gap_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """poll_once uses N2YO to fill gaps that Space-Track left.

    Space-Track returns TLE only for ISS (25544); HST (20580) is missing.
    N2YO returns a TLE for HST.
    Total inserted should equal 2; catalog_update event count should be 2.
    """
    db = _make_db()
    event_bus: asyncio.Queue = asyncio.Queue()

    # Space-Track returns only ISS
    st_tles = _make_st_tles_for_ids([25544])
    n2yo_tle = {
        "norad_id": 20580,
        "epoch_utc": "2024-03-27T13:16:43Z",
        "tle_line1": _HST_TLE_LINE1,
        "tle_line2": _HST_TLE_LINE2,
    }

    monkeypatch.setenv("N2YO_API_KEY", _FAKE_API_KEY)

    with (
        unittest.mock.patch("backend.ingest.authenticate", return_value="fake-cookie"),
        unittest.mock.patch("backend.ingest.fetch_tles", return_value=st_tles),
        unittest.mock.patch("backend.ingest.fetch_tle_n2yo", return_value=n2yo_tle),
        unittest.mock.patch("asyncio.sleep", return_value=None),
    ):
        # Reset module-level flag to ensure the log-once logic doesn't interfere
        import backend.ingest as ingest_mod
        ingest_mod._n2yo_key_missing_logged = False

        total = await poll_once(db, _CATALOG_ENTRIES, event_bus=event_bus)

    assert total == 2, f"Expected 2 total insertions, got {total}"

    # Verify source distribution
    st_rows = db.execute("SELECT COUNT(*) FROM tle_catalog WHERE source='space_track'").fetchone()[0]
    n2yo_rows = db.execute("SELECT COUNT(*) FROM tle_catalog WHERE source='n2yo'").fetchone()[0]
    assert st_rows == 1
    assert n2yo_rows == 1

    # Verify event was emitted
    assert not event_bus.empty()
    event = await event_bus.get()
    assert event["type"] == "catalog_update"
    assert event["count"] == 2

    db.close()


@pytest.mark.asyncio
async def test_poll_once_skips_n2yo_when_api_key_unset(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """poll_once skips the N2YO fallback entirely when N2YO_API_KEY is not set."""
    db = _make_db()
    st_tles = _make_st_tles_for_ids([25544])
    monkeypatch.delenv("N2YO_API_KEY", raising=False)

    import backend.ingest as ingest_mod
    ingest_mod._n2yo_key_missing_logged = False  # ensure fresh state

    n2yo_call_count = {"count": 0}

    async def _mock_fetch_n2yo(*args, **kwargs):  # type: ignore[no-untyped-def]
        n2yo_call_count["count"] += 1
        return None

    with caplog.at_level(logging.INFO, logger="backend.ingest"):
        with (
            unittest.mock.patch("backend.ingest.authenticate", return_value="fake-cookie"),
            unittest.mock.patch("backend.ingest.fetch_tles", return_value=st_tles),
            unittest.mock.patch("backend.ingest.fetch_tle_n2yo", side_effect=_mock_fetch_n2yo),
        ):
            await poll_once(db, _CATALOG_ENTRIES[:1])  # only ISS in catalog

    assert n2yo_call_count["count"] == 0, "fetch_tle_n2yo should not be called"

    log_messages = [r.getMessage() for r in caplog.records]
    assert any("N2YO_API_KEY not set" in msg for msg in log_messages), (
        f"Expected skip log message not found. Messages: {log_messages}"
    )

    db.close()


@pytest.mark.asyncio
async def test_poll_once_survives_n2yo_total_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """poll_once returns successfully even when the entire N2YO block raises.

    The Space-Track inserts must be intact; the function must not raise.
    """
    db = _make_db()
    st_tles = _make_st_tles_for_ids([25544])
    monkeypatch.setenv("N2YO_API_KEY", _FAKE_API_KEY)

    import backend.ingest as ingest_mod
    ingest_mod._n2yo_key_missing_logged = False

    def _boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("N2YO exploded")

    with (
        unittest.mock.patch("backend.ingest.authenticate", return_value="fake-cookie"),
        unittest.mock.patch("backend.ingest.fetch_tles", return_value=st_tles),
        unittest.mock.patch("backend.ingest._select_n2yo_fallback_ids", side_effect=_boom),
    ):
        # Must not raise
        total = await poll_once(db, _CATALOG_ENTRIES[:1])

    # Space-Track insert for ISS should still be present
    assert total == 1, f"Expected 1 Space-Track insertion, got {total}"
    row = db.execute("SELECT source FROM tle_catalog WHERE norad_id=25544").fetchone()
    assert row is not None
    assert row[0] == "space_track"

    db.close()


@pytest.mark.asyncio
async def test_poll_once_skips_n2yo_when_gap_list_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """poll_once skips N2YO fetch calls when there are no gap IDs."""
    db = _make_db()
    # Both objects have fresh TLEs — no gaps
    st_tles = _make_st_tles_for_ids([25544, 20580])
    monkeypatch.setenv("N2YO_API_KEY", _FAKE_API_KEY)

    import backend.ingest as ingest_mod
    ingest_mod._n2yo_key_missing_logged = False

    n2yo_call_count = {"count": 0}

    async def _mock_fetch_n2yo(*args, **kwargs):  # type: ignore[no-untyped-def]
        n2yo_call_count["count"] += 1
        return None

    with (
        unittest.mock.patch("backend.ingest.authenticate", return_value="fake-cookie"),
        unittest.mock.patch("backend.ingest.fetch_tles", return_value=st_tles),
        # All objects are fresh — fallback selector returns empty list
        unittest.mock.patch("backend.ingest._select_n2yo_fallback_ids", return_value=[]),
        unittest.mock.patch("backend.ingest.fetch_tle_n2yo", side_effect=_mock_fetch_n2yo),
    ):
        await poll_once(db, _CATALOG_ENTRIES)

    assert n2yo_call_count["count"] == 0, "fetch_tle_n2yo should not be called when gap_ids is empty"
    db.close()
