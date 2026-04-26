"""Sole interface to external TLE data sources. No other module may call Space-Track or N2YO.

Handles authenticated polling, TLE validation, and local SQLite caching.
All credentials are read exclusively from environment variables.

Data sources:
  - Space-Track.org (primary): polled every POLL_INTERVAL_S for all catalog objects.
  - N2YO (supplemental fallback): consulted per-object when Space-Track returns no TLE
    or the newest Space-Track TLE epoch is older than N2YO_STALE_THRESHOLD_S (7 days).
    Requires N2YO_API_KEY environment variable; if unset the fallback is skipped silently.

Simulation fidelity note: This module treats each new TLE publication as a synthetic
observation for the ingest->kalman pipeline. This is a deliberate POC simulation of
the sensor-to-catalog pipeline, not a real sensor pipeline. Reviewers should be aware
of this distinction.
"""

import asyncio
import datetime
import json
import logging
import os
import sqlite3
from typing import Any

import httpx

# F-002: 30-minute poll interval
POLL_INTERVAL_S: int = 1800

# Space-Track HTTP 429 rate-limit retry policy (H-3)
_ST_429_MAX_RETRIES: int = 4
_ST_429_INITIAL_BACKOFF_S: float = 5.0
_ST_429_MAX_BACKOFF_S: float = 300.0

# httpx timeout applied to all outbound Space-Track / N2YO requests.
# Prevents integration test hangs and guards against CI network stalls.
_HTTP_TIMEOUT: httpx.Timeout = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=5.0)

# Space-Track.org API endpoints
_SPACETRACK_BASE_URL: str = "https://www.space-track.org"
_SPACETRACK_LOGIN_URL: str = f"{_SPACETRACK_BASE_URL}/ajaxauth/login"
_SPACETRACK_TLE_URL: str = (
    f"{_SPACETRACK_BASE_URL}/basicspacedata/query/class/gp/NORAD_CAT_ID"
    "/{{norad_ids}}/orderby/EPOCH desc/limit/1/format/tle"
)

# N2YO supplemental TLE source (fallback only — Space-Track is primary)
_N2YO_BASE_URL: str = "https://api.n2yo.com/rest/v1/satellite"
# Key is appended per-call as &apiKey=<key> — not embedded in the template
_N2YO_TLE_URL_TEMPLATE: str = f"{_N2YO_BASE_URL}/tle/{{norad_id}}"
N2YO_MAX_REQUESTS_PER_CYCLE: int = 50
N2YO_STALE_THRESHOLD_S: int = 7 * 86400  # 7 days in seconds

# Default DB path per resolved open question 2
_DEFAULT_DB_PATH: str = "data/catalog/tle_cache.db"

logger = logging.getLogger(__name__)

# Module-level flag: log the "N2YO_API_KEY not set" message only once per process.
_n2yo_key_missing_logged: bool = False


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
        (norad_id INTEGER, epoch_utc TEXT, tle_line1 TEXT, tle_line2 TEXT,
         fetched_at TEXT, source TEXT)

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

    # check_same_thread=False is safe here because WAL mode (set below) allows
    # concurrent readers alongside the single writer. Without WAL, sharing a
    # connection across threads would risk journal corruption.
    conn = sqlite3.connect(db_path, check_same_thread=False)
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
            source      TEXT    NOT NULL DEFAULT 'space_track',
            UNIQUE(norad_id, epoch_utc)
        )
        """
    )

    # Additive migration: add source column to databases created before this column existed.
    # Using PRAGMA table_info makes this idempotent; ALTER TABLE ADD COLUMN is safe in SQLite.
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tle_catalog)").fetchall()}
    if "source" not in existing_cols:
        conn.execute("ALTER TABLE tle_catalog ADD COLUMN source TEXT NOT NULL DEFAULT 'space_track'")
        logger.info("DB migration: added 'source' column to tle_catalog")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS filter_active_anomaly (
            norad_id      INTEGER PRIMARY KEY,
            anomaly_row_id INTEGER NOT NULL
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
    with open(config_path, encoding="utf-8") as fh:
        raw: Any = json.load(fh)

    if not isinstance(raw, list):
        raise ValueError(f"Catalog config must be a JSON array, got {type(raw).__name__}")

    entries: list[dict] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"Catalog entry {idx} is not a dict: {item!r}")
        for required_field in ("norad_id", "name", "object_class"):
            if required_field not in item:
                raise ValueError(f"Catalog entry {idx} missing required field '{required_field}': {item!r}")
        try:
            item["norad_id"] = int(item["norad_id"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Catalog entry {idx} has non-integer norad_id: {item['norad_id']!r}") from exc
        entries.append(item)

    if not entries:
        raise ValueError("Catalog config is empty — must list at least one object")

    logger.info("Loaded %d catalog entries from %s", len(entries), config_path)
    return entries


def cache_tles(
    db: sqlite3.Connection,
    tles: list[dict],
    fetched_at_utc: datetime.datetime,
    source: str = "space_track",
) -> int:
    """Write validated TLEs to the local SQLite catalog table.

    Table schema: (norad_id INTEGER, epoch_utc TEXT, tle_line1 TEXT,
                   tle_line2 TEXT, fetched_at TEXT, source TEXT)

    Rows are inserted with INSERT OR IGNORE to avoid duplicates on
    (norad_id, epoch_utc). F-004.

    Args:
        db: Open SQLite connection.
        tles: List of validated TLE dicts. Each dict must have keys:
              'norad_id' (int), 'epoch_utc' (str ISO 8601),
              'tle_line1' (str), 'tle_line2' (str).
        fetched_at_utc: UTC timestamp of the fetch operation. Must be UTC-aware.
        source: Provenance tag for inserted rows. Defaults to 'space_track'.
                Use 'n2yo' for TLEs fetched via the N2YO fallback path.

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
                (norad_id, epoch_utc, tle_line1, tle_line2, fetched_at, source)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                tle["norad_id"],
                tle["epoch_utc"],
                tle["tle_line1"],
                tle["tle_line2"],
                fetched_at_str,
                source,
            ),
        )
        inserted += cursor.rowcount

    db.commit()
    logger.debug("cache_tles: inserted %d of %d rows (source=%s)", inserted, len(tles), source)
    return inserted


def get_cached_tles(
    db: sqlite3.Connection,
    norad_id: int,
    since_utc: datetime.datetime | None = None,
) -> list[dict]:
    """Retrieve cached TLEs for a given NORAD ID from local storage.

    Args:
        db: Open SQLite connection.
        norad_id: NORAD catalog ID.
        since_utc: If provided, only return TLEs with epoch_utc after this
                   time (exclusive). Must be UTC-aware if supplied.

    Returns:
        List of TLE dicts ordered by epoch_utc ascending. Each dict has
        keys: 'norad_id', 'epoch_utc', 'tle_line1', 'tle_line2', 'fetched_at', 'source'.

    Raises:
        ValueError: If since_utc is provided but is not UTC-aware.
    """
    if since_utc is not None:
        if since_utc.tzinfo is None or since_utc.tzinfo.utcoffset(since_utc) is None:
            raise ValueError("since_utc must be a UTC-aware datetime")
        since_str = since_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        cursor = db.execute(
            """
            SELECT norad_id, epoch_utc, tle_line1, tle_line2, fetched_at, source
            FROM tle_catalog
            WHERE norad_id = ? AND epoch_utc > ?
            ORDER BY epoch_utc ASC
            """,
            (norad_id, since_str),
        )
    else:
        cursor = db.execute(
            """
            SELECT norad_id, epoch_utc, tle_line1, tle_line2, fetched_at, source
            FROM tle_catalog
            WHERE norad_id = ?
            ORDER BY epoch_utc ASC
            """,
            (norad_id,),
        )

    rows = cursor.fetchall()
    return [dict(row) for row in rows]


def get_latest_tle(db: sqlite3.Connection, norad_id: int) -> dict | None:
    """Retrieve the most recent cached TLE for a given NORAD ID.

    "Most recent" is determined by epoch_utc (the TLE orbital epoch),
    not by fetched_at timestamp.

    Args:
        db: Open SQLite connection.
        norad_id: NORAD catalog ID.

    Returns:
        TLE dict with keys 'norad_id', 'epoch_utc', 'tle_line1', 'tle_line2',
        'fetched_at', 'source', or None if no cached data exists for this NORAD ID.
    """
    cursor = db.execute(
        """
        SELECT norad_id, epoch_utc, tle_line1, tle_line2, fetched_at, source
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
        raise OSError("SPACETRACK_USER environment variable is not set. Set it to your Space-Track.org account email.")
    if not password:
        raise OSError(
            "SPACETRACK_PASS environment variable is not set. Set it to your Space-Track.org account password."
        )

    login_payload = {"identity": user, "password": password}

    call_time_utc = datetime.datetime.now(datetime.UTC)
    logger.info(
        "F-006 SPACETRACK_API_CALL timestamp=%s endpoint=%s",
        call_time_utc.isoformat(),
        _SPACETRACK_LOGIN_URL,
    )

    async with httpx.AsyncClient(follow_redirects=True, timeout=_HTTP_TIMEOUT) as client:
        response = await client.post(_SPACETRACK_LOGIN_URL, data=login_payload)

    logger.info(
        "F-006 SPACETRACK_API_RESPONSE timestamp=%s endpoint=%s status=%d",
        datetime.datetime.now(datetime.UTC).isoformat(),
        _SPACETRACK_LOGIN_URL,
        response.status_code,
    )

    response.raise_for_status()

    cookies = response.cookies
    if not cookies:
        raise RuntimeError(
            "Space-Track.org authentication succeeded (HTTP 200) but no session cookie was returned. Check credentials."
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

    call_time_utc = datetime.datetime.now(datetime.UTC)
    logger.info(
        "F-006 SPACETRACK_API_CALL timestamp=%s endpoint=%s norad_count=%d",
        call_time_utc.isoformat(),
        url,
        len(norad_ids),
    )

    headers = {"Cookie": session_cookie}

    backoff_s: float = _ST_429_INITIAL_BACKOFF_S
    response: httpx.Response

    async with httpx.AsyncClient(follow_redirects=True, timeout=_HTTP_TIMEOUT) as client:
        for attempt in range(_ST_429_MAX_RETRIES + 1):
            response = await client.get(url, headers=headers)

            if response.status_code != 429:
                break

            if attempt == _ST_429_MAX_RETRIES:
                logger.warning(
                    "Space-Track rate limit (HTTP 429): max retries (%d) exhausted — skipping poll cycle",
                    _ST_429_MAX_RETRIES,
                )
                return []

            # Respect Retry-After header; fall back to exponential backoff.
            wait_s: float = backoff_s
            retry_after: str | None = response.headers.get("Retry-After")
            if retry_after is not None:
                try:
                    wait_s = max(float(retry_after), 1.0)
                except ValueError:
                    pass

            logger.warning(
                "Space-Track rate limit (HTTP 429): attempt %d/%d — waiting %.0fs before retry",
                attempt + 1,
                _ST_429_MAX_RETRIES,
                wait_s,
            )
            await asyncio.sleep(wait_s)
            backoff_s = min(backoff_s * 2, _ST_429_MAX_BACKOFF_S)

    logger.info(
        "F-006 SPACETRACK_API_RESPONSE timestamp=%s endpoint=%s status=%d content_length=%d",
        datetime.datetime.now(datetime.UTC).isoformat(),
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
    epoch_dt = datetime.datetime(year, 1, 1, tzinfo=datetime.UTC) + datetime.timedelta(
        days=day_int - 1, seconds=frac_day * 86400.0
    )

    return epoch_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


async def fetch_tle_n2yo(
    norad_id: int,
    api_key: str,
    client: httpx.AsyncClient,
) -> dict | None:
    """Fetch a single TLE from N2YO for the given NORAD ID.

    N2YO is a supplemental fallback source only. The returned dict has the same
    shape as entries from fetch_tles() so it can be passed directly to cache_tles().

    The API key is appended to the URL as &apiKey=<key> (N2YO's documented format).
    The key is redacted in all log messages (replaced with ***).

    Args:
        norad_id: NORAD catalog ID to fetch.
        api_key: N2YO API key (read from N2YO_API_KEY env var by the caller).
        client: An httpx.AsyncClient to use for the request. The caller controls
                connection reuse; tests can inject a mock via httpx.MockTransport.

    Returns:
        Dict with keys 'norad_id' (int), 'epoch_utc' (str ISO 8601),
        'tle_line1' (str), 'tle_line2' (str) on success.
        None on any failure (network error, non-2xx status, missing fields,
        checksum failure, NORAD ID mismatch). Never raises. F-003, NF-010.
    """
    url = f"{_N2YO_TLE_URL_TEMPLATE.format(norad_id=norad_id)}&apiKey={api_key}"
    redacted_url = f"{_N2YO_TLE_URL_TEMPLATE.format(norad_id=norad_id)}&apiKey=***"

    call_time_utc = datetime.datetime.now(datetime.UTC)
    logger.info(
        "F-006 N2YO_API_CALL timestamp=%s endpoint=%s norad_id=%d",
        call_time_utc.isoformat(),
        redacted_url,
        norad_id,
    )

    try:
        response = await client.get(url)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "N2YO network error for NORAD %d: %s (endpoint=%s)",
            norad_id,
            exc,
            redacted_url,
        )
        return None

    logger.info(
        "F-006 N2YO_API_RESPONSE timestamp=%s endpoint=%s norad_id=%d status=%d",
        datetime.datetime.now(datetime.UTC).isoformat(),
        redacted_url,
        norad_id,
        response.status_code,
    )

    if response.status_code != 200:
        logger.warning(
            "N2YO returned HTTP %d for NORAD %d (endpoint=%s)",
            response.status_code,
            norad_id,
            redacted_url,
        )
        return None

    try:
        body = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("N2YO JSON decode error for NORAD %d: %s", norad_id, exc)
        return None

    # Validate response structure
    if "tle" not in body or "info" not in body:
        logger.warning(
            "N2YO response missing 'tle' or 'info' key for NORAD %d: keys=%s",
            norad_id,
            list(body.keys()),
        )
        return None

    tle_field: str = body.get("tle", "")
    info_block: dict = body.get("info", {})

    if not tle_field:
        logger.warning("N2YO returned empty 'tle' field for NORAD %d", norad_id)
        return None

    # Verify the response is for the requested satellite (paranoia check)
    returned_satid = info_block.get("satid")
    if returned_satid != norad_id:
        logger.warning(
            "N2YO satid mismatch for requested NORAD %d: response satid=%s",
            norad_id,
            returned_satid,
        )
        return None

    # Split on \r\n or \n — tolerate either line ending
    raw_lines = [ln.strip() for ln in tle_field.replace("\r\n", "\n").split("\n") if ln.strip()]
    if len(raw_lines) != 2:
        logger.warning(
            "N2YO 'tle' field did not split into exactly 2 lines for NORAD %d: got %d",
            norad_id,
            len(raw_lines),
        )
        return None

    line1, line2 = raw_lines[0], raw_lines[1]

    if not validate_tle(line1, line2):
        logger.warning("N2YO TLE failed checksum validation for NORAD %d", norad_id)
        return None

    try:
        epoch_utc_str = _parse_tle_epoch_utc(line1)
    except ValueError as exc:
        logger.warning("N2YO TLE epoch parse error for NORAD %d: %s", norad_id, exc)
        return None

    return {
        "norad_id": norad_id,
        "epoch_utc": epoch_utc_str,
        "tle_line1": line1,
        "tle_line2": line2,
    }


def _select_n2yo_fallback_ids(
    db: sqlite3.Connection,
    norad_ids: list[int],
    stale_threshold_s: int,
    max_ids: int,
    now_utc: datetime.datetime,
) -> list[int]:
    """Select NORAD IDs from the catalog that need a supplemental N2YO fetch.

    An ID is selected if:
    - No TLE exists in tle_catalog for it (gap), OR
    - The most recent TLE epoch is older than now_utc - stale_threshold_s.

    Results are ordered oldest-first (most-stale objects refreshed first when
    the max_ids cap bites). Catalog ordering would be arbitrary; oldest-first
    is better for demo reproducibility and correctness.

    Args:
        db: Open SQLite connection.
        norad_ids: Full list of NORAD IDs from the catalog config.
        stale_threshold_s: Age threshold in seconds. TLEs older than this are stale.
        max_ids: Maximum number of IDs to return. Caps output to this count.
        now_utc: Current UTC time (UTC-aware datetime). Used for staleness calculation.

    Returns:
        List of NORAD IDs needing an N2YO fetch, oldest-first, capped at max_ids.
    """
    stale_cutoff = now_utc - datetime.timedelta(seconds=stale_threshold_s)

    # Collect (norad_id, epoch_utc_or_None) pairs, sorted oldest-first
    candidates: list[tuple[int, datetime.datetime | None]] = []

    for nid in norad_ids:
        row = get_latest_tle(db, nid)
        if row is None:
            # No TLE at all — always include. Use epoch of epoch_min for sort ordering.
            candidates.append((nid, None))
        else:
            try:
                epoch_dt = datetime.datetime.strptime(row["epoch_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=datetime.UTC
                )
            except ValueError:
                # Unparseable epoch — treat as missing
                logger.warning(
                    "_select_n2yo_fallback_ids: could not parse epoch_utc=%r for NORAD %d",
                    row["epoch_utc"],
                    nid,
                )
                candidates.append((nid, None))
                continue

            if epoch_dt < stale_cutoff:
                candidates.append((nid, epoch_dt))
            # else: fresh — skip

    # Sort: None epochs (no TLE at all) go first, then oldest epoch first
    def _sort_key(item: tuple[int, datetime.datetime | None]) -> datetime.datetime:
        if item[1] is None:
            return datetime.datetime.min.replace(tzinfo=datetime.UTC)
        return item[1]

    candidates.sort(key=_sort_key)

    return [nid for nid, _ in candidates[:max_ids]]


async def poll_once(
    db: sqlite3.Connection,
    catalog_entries: list[dict],
    event_bus: asyncio.Queue | None = None,
) -> int:
    """Perform a single poll cycle: authenticate, fetch, validate, cache, emit.

    This is the top-level polling action called by the background loop. It
    implements the full F-001 through F-006 cycle in one atomic call:
    1. Authenticate with Space-Track (F-001)
    2. Fetch TLEs for configured catalog objects (F-001, F-005)
    3. Validate each TLE checksum (F-003)
    4. Cache new records to SQLite (F-004)
    5. Optionally supplement missing/stale TLEs from N2YO (if N2YO_API_KEY is set)
    6. Emit a catalog_update event on the event bus when new TLEs arrive (arch 3.1)
    7. Log all API calls (F-006)

    N2YO fallback: After the Space-Track fetch, objects with no TLE or a TLE epoch
    older than N2YO_STALE_THRESHOLD_S are queried individually via N2YO. This
    block is wrapped in a broad try/except so N2YO failures never interrupt the
    Space-Track pipeline (NF-010).

    Args:
        db: Open SQLite connection to the catalog database.
        catalog_entries: List of catalog config dicts from load_catalog_config().
                         Each must have 'norad_id'.
        event_bus: Optional asyncio.Queue for emitting catalog_update events.
                   If provided and new TLEs are inserted, an event dict is placed
                   on the queue: {'type': 'catalog_update', 'count': <int>,
                                   'timestamp_utc': <str>}.

    Returns:
        Number of new TLE rows inserted into the cache (both sources combined).

    Raises:
        EnvironmentError: If Space-Track credentials are missing (from authenticate()).
        httpx.HTTPStatusError: If any Space-Track request fails.
    """
    global _n2yo_key_missing_logged

    norad_ids = [int(entry["norad_id"]) for entry in catalog_entries]

    session_cookie = await authenticate()
    tles = await fetch_tles(norad_ids, session_cookie)

    fetched_at_utc = datetime.datetime.now(datetime.UTC)
    st_inserted = cache_tles(db, tles, fetched_at_utc, source="space_track")
    n2yo_inserted = 0

    # --- N2YO supplemental fallback -------------------------------------------
    try:
        api_key = os.environ.get("N2YO_API_KEY")
        if not api_key:
            if not _n2yo_key_missing_logged:
                logger.info("N2YO_API_KEY not set; skipping supplemental N2YO fallback.")
                _n2yo_key_missing_logged = True
        else:
            gap_ids = _select_n2yo_fallback_ids(
                db,
                norad_ids,
                stale_threshold_s=N2YO_STALE_THRESHOLD_S,
                max_ids=N2YO_MAX_REQUESTS_PER_CYCLE,
                now_utc=fetched_at_utc,
            )
            if gap_ids:
                n2yo_tles: list[dict] = []
                async with httpx.AsyncClient(follow_redirects=True, timeout=_HTTP_TIMEOUT) as n2yo_client:
                    for nid in gap_ids:
                        result = await fetch_tle_n2yo(nid, api_key, n2yo_client)
                        if result is not None:
                            n2yo_tles.append(result)
                        await asyncio.sleep(0.1)  # polite pacing

                n2yo_inserted = cache_tles(db, n2yo_tles, fetched_at_utc, source="n2yo")
                fetched_ids = [t["norad_id"] for t in n2yo_tles]
                logger.info(
                    "N2YO fallback: fetched %d TLEs for %s",
                    n2yo_inserted,
                    fetched_ids,
                )
    except Exception as exc:  # noqa: BLE001
        logger.error("N2YO fallback block failed (Space-Track inserts are intact): %s", exc)
    # --------------------------------------------------------------------------

    inserted = st_inserted + n2yo_inserted

    if inserted > 0 and event_bus is not None:
        event = {
            "type": "catalog_update",
            "count": inserted,
            "timestamp_utc": fetched_at_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        await event_bus.put(event)
        logger.debug("Emitted catalog_update event: %s", event)

    logger.info(
        "poll_once complete: %d new TLEs inserted (space_track=%d, n2yo=%d), %d catalog entries, fetched_at=%s",
        inserted,
        st_inserted,
        n2yo_inserted,
        len(catalog_entries),
        fetched_at_utc.isoformat(),
    )
    return inserted


async def run_ingest_loop(
    db_path: str | None = None,
    catalog_config_path: str | None = None,
    event_bus: asyncio.Queue | None = None,
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
        except OSError:
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
