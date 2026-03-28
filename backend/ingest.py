"""Sole interface to Space-Track.org. No other module may call the Space-Track API.

Handles authenticated polling, TLE validation, and local SQLite caching.
All Space-Track credentials are read exclusively from environment variables.

Simulation fidelity note: This module treats each new TLE publication from
Space-Track.org as a synthetic observation for the ingest->kalman pipeline.
This is a deliberate POC simulation of the sensor-to-catalog pipeline, not
a real sensor pipeline. Reviewers should be aware of this distinction.
"""
import asyncio
import datetime
import json
import logging
import os
import sqlite3
from typing import Any, Optional

import httpx

# F-002: 30-minute poll interval
POLL_INTERVAL_S: int = 1800

# Space-Track.org API endpoints
_SPACETRACK_BASE_URL: str = "https://www.space-track.org"
_SPACETRACK_LOGIN_URL: str = f"{_SPACETRACK_BASE_URL}/ajaxauth/login"
_SPACETRACK_TLE_URL: str = (
    f"{_SPACETRACK_BASE_URL}/basicspacedata/query/class/gp/NORAD_CAT_ID"
    "/{{norad_ids}}/orderby/EPOCH desc/limit/1/format/tle"
)

# Default DB path per resolved open question 2
_DEFAULT_DB_PATH: str = "data/catalog/tle_cache.db"

logger = logging.getLogger(__name__)


def _tle_checksum(line: str) -> int:
    """Compute the standard TLE modulo-10 checksum for a single line.

    The checksum is computed over characters 0–67 (positions 1–68 in 1-indexed
    TLE notation). Digits sum as their numeric value; minus signs count as 1;
    all other characters count as 0. The checksum digit is the sum modulo 10.

    Args:
        line: A single TLE line string (must be at least 68 characters).

    Returns:
        Computed checksum integer (0–9).
    """
    total = 0
    for ch in line[:68]:
        if ch.isdigit():
            total += int(ch)
        elif ch == "-":
            total += 1
    return total % 10


def validate_tle(tle_line1: str, tle_line2: str) -> bool:
    """Validate TLE checksum integrity for both lines.

    Performs the standard modulo-10 checksum on each TLE line and verifies
    that the computed value matches the checksum digit stored in column 69
    (index 68) of each line. F-003.

    Args:
        tle_line1: First line of the TLE (line starting with '1').
        tle_line2: Second line of the TLE (line starting with '2').

    Returns:
        True if both lines pass checksum validation, False otherwise.
    """
    try:
        if len(tle_line1) < 69 or len(tle_line2) < 69:
            logger.warning("TLE line too short for checksum validation")
            return False
        if tle_line1[0] != "1" or tle_line2[0] != "2":
            logger.warning("TLE line identifiers are incorrect (expected '1' and '2')")
            return False
        expected_cs1 = int(tle_line1[68])
        expected_cs2 = int(tle_line2[68])
        computed_cs1 = _tle_checksum(tle_line1)
        computed_cs2 = _tle_checksum(tle_line2)
        if computed_cs1 != expected_cs1:
            logger.warning(
                "TLE line 1 checksum mismatch: computed %d, expected %d",
                computed_cs1,
                expected_cs1,
            )
            return False
        if computed_cs2 != expected_cs2:
            logger.warning(
                "TLE line 2 checksum mismatch: computed %d, expected %d",
                computed_cs2,
                expected_cs2,
            )
            return False
        return True
    except (ValueError, IndexError) as exc:
        logger.warning("TLE validation error: %s", exc)
        return False


def init_catalog_db(db_path: str) -> sqlite3.Connection:
    """Initialize the SQLite database and create the catalog table if it does not exist.

    Creates parent directories if they do not exist. The catalog table schema is:
        (norad_id INTEGER, epoch_utc TEXT, tle_line1 TEXT, tle_line2 TEXT, fetched_at TEXT)

    The combination (norad_id, epoch_utc) is used as a unique key to avoid
    duplicate insertions on repeated polls. F-004.

    Args:
        db_path: File path for the SQLite database.

    Returns:
        Open SQLite connection with WAL journal mode enabled.
    """
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

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
    conn.commit()
    logger.debug("Catalog DB initialized at %s", db_path)
    return conn


# DEVIATION from plan docs/plans/2026-03-28-initial-scaffold.md step 7:
# The initial scaffold stub declared load_catalog_config() -> list[int].
# The plan's resolved open question (section "Open questions", item 1) explicitly
# supersedes the stub: "load_catalog_config shall return list[dict]".
# Return type corrected to list[dict] per the resolved question. Not a code bug.
def load_catalog_config(config_path: str) -> list[dict]:
    """Load the catalog of tracked objects from a JSON configuration file.

    The config file must be a JSON array of objects, each with at minimum:
        {"norad_id": <int>, "name": <str>, "object_class": <str>}

    Valid object_class values: "active_satellite", "debris", "rocket_body".
    F-005 specifies 20–50 objects for POC.

    Args:
        config_path: Path to a JSON file containing the catalog list.

    Returns:
        List of catalog entry dicts, each containing at minimum
        'norad_id' (int), 'name' (str), 'object_class' (str).

    Raises:
        FileNotFoundError: If the config file does not exist.
        ValueError: If the JSON is malformed or missing required fields.
    """
    with open(config_path, "r", encoding="utf-8") as fh:
        raw: Any = json.load(fh)

    if not isinstance(raw, list):
        raise ValueError(
            f"Catalog config must be a JSON array, got {type(raw).__name__}"
        )

    entries: list[dict] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"Catalog entry {idx} is not a dict: {item!r}")
        for required_field in ("norad_id", "name", "object_class"):
            if required_field not in item:
                raise ValueError(
                    f"Catalog entry {idx} missing required field '{required_field}': {item!r}"
                )
        try:
            item["norad_id"] = int(item["norad_id"])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Catalog entry {idx} has non-integer norad_id: {item['norad_id']!r}"
            ) from exc
        entries.append(item)

    if not entries:
        raise ValueError("Catalog config is empty — must list at least one object")

    logger.info("Loaded %d catalog entries from %s", len(entries), config_path)
    return entries


def cache_tles(
    db: sqlite3.Connection,
    tles: list[dict],
    fetched_at_utc: datetime.datetime,
) -> int:
    """Write validated TLEs to the local SQLite catalog table.

    Table schema: (norad_id INTEGER, epoch_utc TEXT, tle_line1 TEXT,
                   tle_line2 TEXT, fetched_at TEXT)

    Rows are inserted with INSERT OR IGNORE to avoid duplicates on
    (norad_id, epoch_utc). F-004.

    Args:
        db: Open SQLite connection.
        tles: List of validated TLE dicts. Each dict must have keys:
              'norad_id' (int), 'epoch_utc' (str ISO 8601),
              'tle_line1' (str), 'tle_line2' (str).
        fetched_at_utc: UTC timestamp of the fetch operation. Must be UTC-aware.

    Returns:
        Number of rows actually inserted (ignored duplicates not counted).

    Raises:
        ValueError: If fetched_at_utc is not UTC-aware.
    """
    if fetched_at_utc.tzinfo is None or fetched_at_utc.tzinfo.utcoffset(fetched_at_utc) is None:
        raise ValueError("fetched_at_utc must be a UTC-aware datetime")

    fetched_at_str = fetched_at_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    inserted = 0
    for tle in tles:
        cursor = db.execute(
            """
            INSERT OR IGNORE INTO tle_catalog
                (norad_id, epoch_utc, tle_line1, tle_line2, fetched_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                tle["norad_id"],
                tle["epoch_utc"],
                tle["tle_line1"],
                tle["tle_line2"],
                fetched_at_str,
            ),
        )
        inserted += cursor.rowcount

    db.commit()
    logger.debug("cache_tles: inserted %d of %d rows", inserted, len(tles))
    return inserted


def get_cached_tles(
    db: sqlite3.Connection,
    norad_id: int,
    since_utc: Optional[datetime.datetime] = None,
) -> list[dict]:
    """Retrieve cached TLEs for a given NORAD ID from local storage.

    Args:
        db: Open SQLite connection.
        norad_id: NORAD catalog ID.
        since_utc: If provided, only return TLEs with epoch_utc after this
                   time (exclusive). Must be UTC-aware if supplied.

    Returns:
        List of TLE dicts ordered by epoch_utc ascending. Each dict has
        keys: 'norad_id', 'epoch_utc', 'tle_line1', 'tle_line2', 'fetched_at'.

    Raises:
        ValueError: If since_utc is provided but is not UTC-aware.
    """
    if since_utc is not None:
        if since_utc.tzinfo is None or since_utc.tzinfo.utcoffset(since_utc) is None:
            raise ValueError("since_utc must be a UTC-aware datetime")
        since_str = since_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        cursor = db.execute(
            """
            SELECT norad_id, epoch_utc, tle_line1, tle_line2, fetched_at
            FROM tle_catalog
            WHERE norad_id = ? AND epoch_utc > ?
            ORDER BY epoch_utc ASC
            """,
            (norad_id, since_str),
        )
    else:
        cursor = db.execute(
            """
            SELECT norad_id, epoch_utc, tle_line1, tle_line2, fetched_at
            FROM tle_catalog
            WHERE norad_id = ?
            ORDER BY epoch_utc ASC
            """,
            (norad_id,),
        )

    rows = cursor.fetchall()
    return [dict(row) for row in rows]


def get_latest_tle(db: sqlite3.Connection, norad_id: int) -> Optional[dict]:
    """Retrieve the most recent cached TLE for a given NORAD ID.

    "Most recent" is determined by epoch_utc (the TLE orbital epoch),
    not by fetched_at timestamp.

    Args:
        db: Open SQLite connection.
        norad_id: NORAD catalog ID.

    Returns:
        TLE dict with keys 'norad_id', 'epoch_utc', 'tle_line1', 'tle_line2',
        'fetched_at', or None if no cached data exists for this NORAD ID.
    """
    cursor = db.execute(
        """
        SELECT norad_id, epoch_utc, tle_line1, tle_line2, fetched_at
        FROM tle_catalog
        WHERE norad_id = ?
        ORDER BY epoch_utc DESC
        LIMIT 1
        """,
        (norad_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return dict(row)


async def authenticate() -> str:
    """Authenticate with Space-Track.org using credentials from environment variables.

    Reads SPACETRACK_USER and SPACETRACK_PASS from os.environ.
    Posts credentials to the Space-Track login endpoint and returns
    the resulting session cookie string for subsequent requests. F-001, F-006.

    Returns:
        Session cookie string in the format required by Space-Track.org
        (the raw 'Cookie' header value).

    Raises:
        EnvironmentError: If SPACETRACK_USER or SPACETRACK_PASS are not set.
        httpx.HTTPStatusError: If authentication fails (non-2xx response).
        RuntimeError: If the login response does not contain a valid session cookie.
    """
    user = os.environ.get("SPACETRACK_USER")
    password = os.environ.get("SPACETRACK_PASS")
    if not user:
        raise EnvironmentError(
            "SPACETRACK_USER environment variable is not set. "
            "Set it to your Space-Track.org account email."
        )
    if not password:
        raise EnvironmentError(
            "SPACETRACK_PASS environment variable is not set. "
            "Set it to your Space-Track.org account password."
        )

    login_payload = {"identity": user, "password": password}

    call_time_utc = datetime.datetime.now(datetime.timezone.utc)
    logger.info(
        "F-006 SPACETRACK_API_CALL timestamp=%s endpoint=%s",
        call_time_utc.isoformat(),
        _SPACETRACK_LOGIN_URL,
    )

    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.post(_SPACETRACK_LOGIN_URL, data=login_payload)

    logger.info(
        "F-006 SPACETRACK_API_RESPONSE timestamp=%s endpoint=%s status=%d",
        datetime.datetime.now(datetime.timezone.utc).isoformat(),
        _SPACETRACK_LOGIN_URL,
        response.status_code,
    )

    response.raise_for_status()

    cookies = response.cookies
    if not cookies:
        raise RuntimeError(
            "Space-Track.org authentication succeeded (HTTP 200) but "
            "no session cookie was returned. Check credentials."
        )

    cookie_header = "; ".join(f"{name}={value}" for name, value in cookies.items())
    logger.debug("Space-Track authentication successful, session cookie obtained")
    return cookie_header


async def fetch_tles(norad_ids: list[int], session_cookie: str) -> list[dict]:
    """Fetch current TLEs for the given NORAD IDs from Space-Track.org.

    Queries the Space-Track basicspacedata API for the most recent TLE
    for each NORAD ID in the catalog. Validates each TLE checksum (F-003)
    and discards malformed records without raising. Logs the API call per
    F-006.

    Args:
        norad_ids: List of NORAD catalog IDs to retrieve (20–50 for POC).
        session_cookie: Valid session cookie string from authenticate().

    Returns:
        List of validated TLE dicts. Each dict has keys:
            'norad_id' (int), 'epoch_utc' (str, ISO 8601 UTC),
            'tle_line1' (str), 'tle_line2' (str).
        Malformed or checksum-failing TLEs are silently dropped with a
        warning log entry.

    Raises:
        httpx.HTTPStatusError: If the Space-Track request fails (non-2xx).
        ValueError: If norad_ids is empty.
    """
    if not norad_ids:
        raise ValueError("norad_ids must not be empty")

    norad_ids_str = ",".join(str(n) for n in norad_ids)
    url = (
        f"{_SPACETRACK_BASE_URL}/basicspacedata/query/class/gp"
        f"/NORAD_CAT_ID/{norad_ids_str}"
        f"/orderby/EPOCH desc/limit/{len(norad_ids)}/format/tle"
    )

    call_time_utc = datetime.datetime.now(datetime.timezone.utc)
    logger.info(
        "F-006 SPACETRACK_API_CALL timestamp=%s endpoint=%s norad_count=%d",
        call_time_utc.isoformat(),
        url,
        len(norad_ids),
    )

    headers = {"Cookie": session_cookie}

    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(url, headers=headers)

    logger.info(
        "F-006 SPACETRACK_API_RESPONSE timestamp=%s endpoint=%s status=%d content_length=%d",
        datetime.datetime.now(datetime.timezone.utc).isoformat(),
        url,
        response.status_code,
        len(response.content),
    )

    response.raise_for_status()

    raw_text = response.text.strip()
    validated_tles = _parse_and_validate_tle_response(raw_text)

    logger.info(
        "F-006 fetch_tles: received %d valid TLEs for %d requested NORAD IDs",
        len(validated_tles),
        len(norad_ids),
    )
    return validated_tles


def _parse_and_validate_tle_response(raw_text: str) -> list[dict]:
    """Parse a raw TLE text response and validate each record's checksum.

    Space-Track TLE format: alternating lines of tle_line1 and tle_line2
    (two-line format, no name line).

    Args:
        raw_text: Raw response body from the Space-Track TLE endpoint.

    Returns:
        List of validated TLE dicts. Malformed entries are dropped with a
        warning log. Each dict has: 'norad_id', 'epoch_utc', 'tle_line1',
        'tle_line2'.
    """
    lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
    results: list[dict] = []

    i = 0
    while i + 1 < len(lines):
        line1 = lines[i]
        line2 = lines[i + 1]

        # Skip name lines (TLE format sometimes includes a name as line 0)
        if not (line1.startswith("1 ") or line1.startswith("1")):
            i += 1
            continue

        if not validate_tle(line1, line2):
            logger.warning(
                "Dropping TLE with invalid checksum: line1=%r line2=%r",
                line1[:20],
                line2[:20],
            )
            i += 2
            continue

        try:
            norad_id = int(line1[2:7].strip())
            epoch_utc_str = _parse_tle_epoch_utc(line1)
        except (ValueError, IndexError) as exc:
            logger.warning("Could not parse TLE metadata: %s — line1=%r", exc, line1[:40])
            i += 2
            continue

        results.append(
            {
                "norad_id": norad_id,
                "epoch_utc": epoch_utc_str,
                "tle_line1": line1,
                "tle_line2": line2,
            }
        )
        i += 2

    return results


def _parse_tle_epoch_utc(tle_line1: str) -> str:
    """Extract the epoch from TLE line 1 and return it as an ISO 8601 UTC string.

    TLE epoch format: YYDDD.DDDDDDDD (two-digit year, day-of-year with fractional day).
    Years 57–99 are interpreted as 1957–1999; 00–56 as 2000–2056.

    Args:
        tle_line1: First line of the TLE set (69 characters).

    Returns:
        ISO 8601 UTC string, e.g. "2024-03-15T06:30:00Z".

    Raises:
        ValueError: If the epoch field cannot be parsed.
    """
    epoch_field = tle_line1[18:32].strip()
    if not epoch_field:
        raise ValueError(f"Empty epoch field in TLE line 1: {tle_line1!r}")

    try:
        year_2digit = int(epoch_field[:2])
        day_of_year_frac = float(epoch_field[2:])
    except ValueError as exc:
        raise ValueError(f"Cannot parse TLE epoch field '{epoch_field}': {exc}") from exc

    if year_2digit >= 57:
        year = 1900 + year_2digit
    else:
        year = 2000 + year_2digit

    # day_of_year_frac is 1-based: 1.0 = Jan 1 00:00:00
    day_int = int(day_of_year_frac)
    frac_day = day_of_year_frac - day_int
    epoch_dt = datetime.datetime(year, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(
        days=day_int - 1, seconds=frac_day * 86400.0
    )

    return epoch_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


async def poll_once(
    db: sqlite3.Connection,
    catalog_entries: list[dict],
    event_bus: Optional[asyncio.Queue] = None,
) -> int:
    """Perform a single poll cycle: authenticate, fetch, validate, cache, emit.

    This is the top-level polling action called by the background loop. It
    implements the full F-001 through F-006 cycle in one atomic call:
    1. Authenticate with Space-Track (F-001)
    2. Fetch TLEs for configured catalog objects (F-001, F-005)
    3. Validate each TLE checksum (F-003)
    4. Cache new records to SQLite (F-004)
    5. Emit a catalog_update event on the event bus when new TLEs arrive (arch 3.1)
    6. Log all API calls (F-006)

    Args:
        db: Open SQLite connection to the catalog database.
        catalog_entries: List of catalog config dicts from load_catalog_config().
                         Each must have 'norad_id'.
        event_bus: Optional asyncio.Queue for emitting catalog_update events.
                   If provided and new TLEs are inserted, an event dict is placed
                   on the queue: {'type': 'catalog_update', 'count': <int>,
                                   'timestamp_utc': <str>}.

    Returns:
        Number of new TLE rows inserted into the cache.

    Raises:
        EnvironmentError: If credentials are missing (from authenticate()).
        httpx.HTTPStatusError: If any Space-Track request fails.
    """
    norad_ids = [int(entry["norad_id"]) for entry in catalog_entries]

    session_cookie = await authenticate()
    tles = await fetch_tles(norad_ids, session_cookie)

    fetched_at_utc = datetime.datetime.now(datetime.timezone.utc)
    inserted = cache_tles(db, tles, fetched_at_utc)

    if inserted > 0 and event_bus is not None:
        event = {
            "type": "catalog_update",
            "count": inserted,
            "timestamp_utc": fetched_at_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        await event_bus.put(event)
        logger.debug("Emitted catalog_update event: %s", event)

    logger.info(
        "poll_once complete: %d new TLEs inserted, %d catalog entries, fetched_at=%s",
        inserted,
        len(catalog_entries),
        fetched_at_utc.isoformat(),
    )
    return inserted


async def run_ingest_loop(
    db_path: Optional[str] = None,
    catalog_config_path: Optional[str] = None,
    event_bus: Optional[asyncio.Queue] = None,
) -> None:
    """Run the continuous TLE polling loop.

    Polls Space-Track.org every POLL_INTERVAL_S seconds. Recovers from
    transient errors (network failures, HTTP errors) without crashing,
    per NF-010. Logs failures and waits for the next scheduled poll.

    The DB path defaults to the NBODY_DB_PATH environment variable, falling
    back to 'data/catalog/tle_cache.db'. The catalog config path defaults
    to 'data/catalog/catalog.json'.

    Args:
        db_path: Override path to the SQLite database. If None, uses
                 NBODY_DB_PATH env var or the default path.
        catalog_config_path: Override path to the catalog JSON config.
                             If None, defaults to 'data/catalog/catalog.json'.
        event_bus: Optional asyncio.Queue for emitting catalog_update events.

    Raises:
        EnvironmentError: If SPACETRACK_USER or SPACETRACK_PASS are not set
                          (detected on first poll attempt, not at startup).
    """
    resolved_db_path = db_path or os.environ.get("NBODY_DB_PATH") or _DEFAULT_DB_PATH
    resolved_catalog_path = catalog_config_path or "data/catalog/catalog.json"

    logger.info("Starting ingest loop: db=%s catalog=%s", resolved_db_path, resolved_catalog_path)

    db = init_catalog_db(resolved_db_path)
    catalog_entries = load_catalog_config(resolved_catalog_path)

    logger.info(
        "Ingest loop ready: tracking %d objects, poll interval %ds",
        len(catalog_entries),
        POLL_INTERVAL_S,
    )

    while True:
        try:
            await poll_once(db, catalog_entries, event_bus=event_bus)
        except EnvironmentError:
            # Credential errors are not recoverable — re-raise to surface immediately
            raise
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Space-Track HTTP error during poll: %s — will retry in %ds",
                exc,
                POLL_INTERVAL_S,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Unexpected error during ingest poll: %s — will retry in %ds",
                exc,
                POLL_INTERVAL_S,
            )

        await asyncio.sleep(POLL_INTERVAL_S)
