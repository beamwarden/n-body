"""Validate catalog.json NORAD IDs against Space-Track satcat.

For every entry in data/catalog/catalog.json, queries the Space-Track satcat
endpoint and checks:

  1. Object found     — NORAD ID exists in Space-Track
  2. Name match       — our label matches the official SATNAME (fuzzy)
  3. Active in orbit  — DECAY field is null (not deorbited)
  4. Object type      — expected class (active_satellite/debris/rocket_body)
                        matches Space-Track OBJECT_TYPE

Prints a per-object report and exits non-zero if any hard errors are found.

Usage:
    python scripts/verify_catalog_ids.py [--catalog PATH] [--raw]

Options:
    --catalog PATH   Path to catalog.json (default: data/catalog/catalog.json)
    --raw            Dump raw Space-Track response JSON after the report
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SPACETRACK_BASE: str = "https://www.space-track.org"
_LOGIN_URL: str = f"{_SPACETRACK_BASE}/ajaxauth/login"
_SATCAT_URL: str = (
    f"{_SPACETRACK_BASE}/basicspacedata/query/class/satcat"
    "/NORAD_CAT_ID/{ids}/orderby/NORAD_CAT_ID/format/json"
)

_DEFAULT_CATALOG: str = "data/catalog/catalog.json"

# Space-Track rate limit: max 1 request per second.
_REQUEST_DELAY_S: float = 1.0

# Batch size for satcat queries (Space-Track supports comma-separated IDs).
_BATCH_SIZE: int = 50

# Map our object_class values to the OBJECT_TYPE strings Space-Track returns.
_CLASS_TO_ST_TYPE: dict[str, list[str]] = {
    "active_satellite": ["PAYLOAD"],
    "debris":           ["DEBRIS"],
    "rocket_body":      ["ROCKET BODY"],
}

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def _authenticate(client: httpx.Client) -> str:
    """Log in to Space-Track and return the session cookie string.

    Args:
        client: An open httpx.Client instance.

    Returns:
        Cookie header string suitable for subsequent requests.

    Raises:
        RuntimeError: If credentials are missing or login fails.
    """
    user: Optional[str] = os.environ.get("SPACETRACK_USER")
    password: Optional[str] = os.environ.get("SPACETRACK_PASS")
    if not user or not password:
        raise RuntimeError(
            "SPACETRACK_USER and SPACETRACK_PASS must be set in the environment."
        )

    resp = client.post(
        _LOGIN_URL,
        data={"identity": user, "password": password},
        timeout=30.0,
    )
    resp.raise_for_status()
    if not resp.cookies:
        raise RuntimeError("Space-Track login returned no session cookie — check credentials.")

    return "; ".join(f"{k}={v}" for k, v in resp.cookies.items())


# ---------------------------------------------------------------------------
# Satcat fetch
# ---------------------------------------------------------------------------


def _fetch_satcat(
    norad_ids: list[int],
    cookie: str,
    client: httpx.Client,
) -> list[dict]:
    """Fetch satcat records for a list of NORAD IDs, in batches.

    Args:
        norad_ids: NORAD catalog IDs to query.
        cookie: Session cookie from _authenticate().
        client: Open httpx.Client instance.

    Returns:
        List of satcat record dicts from Space-Track.
    """
    results: list[dict] = []
    headers = {"Cookie": cookie}

    for i in range(0, len(norad_ids), _BATCH_SIZE):
        batch = norad_ids[i : i + _BATCH_SIZE]
        ids_str = ",".join(str(n) for n in batch)
        url = _SATCAT_URL.format(ids=ids_str)

        resp = client.get(url, headers=headers, timeout=30.0)
        resp.raise_for_status()
        results.extend(resp.json())

        if i + _BATCH_SIZE < len(norad_ids):
            time.sleep(_REQUEST_DELAY_S)

    return results


# ---------------------------------------------------------------------------
# Name comparison
# ---------------------------------------------------------------------------

_NON_ALNUM = re.compile(r"[^a-z0-9]")


def _normalize(name: str) -> str:
    """Lowercase and strip non-alphanumeric characters for loose comparison.

    Args:
        name: Satellite name string.

    Returns:
        Normalised string for comparison.
    """
    return _NON_ALNUM.sub("", name.lower())


def _names_match(our_name: str, official_name: str) -> bool:
    """Return True if our label is a plausible match for the official name.

    Accepts exact normalised equality OR substring containment (in either
    direction) to handle abbreviations like 'ISS (ZARYA)' vs 'ISS'.

    Args:
        our_name: Name from catalog.json.
        official_name: SATNAME from Space-Track satcat.

    Returns:
        True if considered a match.
    """
    a = _normalize(our_name)
    b = _normalize(official_name)
    return a == b or a in b or b in a


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _run(catalog_path: str, raw: bool) -> int:
    """Load catalog, query Space-Track, print report.

    Args:
        catalog_path: Path to catalog.json.
        raw: If True, print raw Space-Track JSON after the report.

    Returns:
        Exit code: 0 if no hard errors, 1 otherwise.
    """
    # Load .env if present (best-effort; real deployments use environment).
    _load_dotenv()

    catalog: list[dict] = json.loads(Path(catalog_path).read_text())
    norad_ids: list[int] = [int(e["norad_id"]) for e in catalog]
    our_by_id: dict[int, dict] = {int(e["norad_id"]): e for e in catalog}

    print(f"Validating {len(catalog)} catalog entries against Space-Track satcat…")
    print()

    with httpx.Client() as client:
        print("Authenticating with Space-Track…")
        cookie = _authenticate(client)
        print("Authenticated. Fetching satcat records…")
        st_records = _fetch_satcat(norad_ids, cookie, client)

    st_by_id: dict[int, dict] = {int(r["NORAD_CAT_ID"]): r for r in st_records}

    # -----------------------------------------------------------------------
    # Per-object analysis
    # -----------------------------------------------------------------------

    ok: list[str] = []
    warnings: list[str] = []
    errors: list[str] = []

    for entry in catalog:
        nid: int = int(entry["norad_id"])
        our_name: str = entry["name"]
        our_class: str = entry.get("object_class", "")

        if nid not in st_by_id:
            errors.append(
                f"  [{nid:5d}] {our_name!r:40s} — NOT FOUND in Space-Track satcat"
            )
            continue

        rec = st_by_id[nid]
        official_name: str = rec.get("SATNAME") or rec.get("OBJECT_NAME") or ""
        decay: Optional[str] = rec.get("DECAY") or None
        st_type: str = (rec.get("OBJECT_TYPE") or "").upper()
        current: str = rec.get("CURRENT", "N")

        issues: list[str] = []

        # Name check
        if not _names_match(our_name, official_name):
            issues.append(f"name mismatch: ours={our_name!r} official={official_name!r}")

        # Decay check
        if decay:
            issues.append(f"DECAYED {decay}")

        # Current flag
        if current != "Y":
            issues.append("not in current catalog (CURRENT=N)")

        # Object type check
        expected_types = _CLASS_TO_ST_TYPE.get(our_class, [])
        if expected_types and st_type not in expected_types:
            issues.append(
                f"type mismatch: ours={our_class!r} official={st_type!r}"
            )

        label = f"  [{nid:5d}] {our_name!r:40s} (ST: {official_name!r})"

        if not issues:
            ok.append(f"{label} — OK")
        else:
            # Decay or not-found = error; name/type mismatch = warning
            is_error = any("DECAYED" in i or "not in current" in i for i in issues)
            msg = f"{label}\n           !! {' | '.join(issues)}"
            if is_error:
                errors.append(msg)
            else:
                warnings.append(msg)

    # -----------------------------------------------------------------------
    # Print report
    # -----------------------------------------------------------------------

    if ok:
        print(f"=== OK ({len(ok)}) ===")
        for line in ok:
            print(line)
        print()

    if warnings:
        print(f"=== WARNINGS ({len(warnings)}) — review, may need correction ===")
        for line in warnings:
            print(line)
        print()

    if errors:
        print(f"=== ERRORS ({len(errors)}) — action required ===")
        for line in errors:
            print(line)
        print()

    # Summary
    total = len(catalog)
    print(
        f"Summary: {len(ok)} OK / {len(warnings)} warnings / {len(errors)} errors "
        f"({total} total)"
    )

    if raw:
        print()
        print("=== RAW SPACE-TRACK RESPONSE ===")
        print(json.dumps(st_records, indent=2))

    return 1 if errors else 0


# ---------------------------------------------------------------------------
# .env loader (minimal — avoids a dependency on python-dotenv)
# ---------------------------------------------------------------------------


def _load_dotenv() -> None:
    """Load key=value pairs from .env into os.environ if not already set.

    Silently does nothing if .env does not exist.
    """
    env_path = Path(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    """Parse arguments and run the catalog validation."""
    parser = argparse.ArgumentParser(
        description="Validate catalog.json NORAD IDs against Space-Track satcat."
    )
    parser.add_argument(
        "--catalog",
        default=_DEFAULT_CATALOG,
        metavar="PATH",
        help=f"Path to catalog.json (default: {_DEFAULT_CATALOG})",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Dump raw Space-Track JSON after the report.",
    )
    args = parser.parse_args()
    sys.exit(_run(catalog_path=args.catalog, raw=args.raw))


if __name__ == "__main__":
    main()
