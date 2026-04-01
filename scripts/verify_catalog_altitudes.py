"""One-time script to verify that all NORAD IDs in a catalog JSON have TLEs
cached at or below the 600 km altitude cutoff.

Usage:
    python scripts/verify_catalog_altitudes.py [catalog_json_path]

Arguments:
    catalog_json_path   Path to the catalog JSON file.
                        Default: data/catalog/catalog.json

The TLE cache is read from data/catalog/tle_cache.db (the same SQLite file
used by ingest.py). The table is tle_catalog with columns:
    norad_id INTEGER, epoch_utc TEXT, tle_line1 TEXT, tle_line2 TEXT, fetched_at TEXT

Altitude is derived from TLE line 2 mean motion (revolutions/day), columns
53-63 (1-indexed), using:
    n_rad_s = n_rev_day * 2 * pi / 86400
    a_km    = (mu_earth / n_rad_s**2) ** (1/3)
    alt_km  = a_km - R_earth

where mu_earth = 398600.4418 km^3/s^2 and R_earth = 6378.137 km.

IMPORTANT: Any NORAD ID flagged NO_TLE must be verified against Space-Track
directly before the catalog is used in production. Run scripts/replay.py to
populate the TLE cache first.

This script does NOT call Space-Track. It reads only from the local cache.
All Space-Track calls must go through ingest.py per architecture constraints.
"""
import json
import math
import os
import sqlite3
import sys

# Physical constants
_MU_EARTH_KM3_S2: float = 398600.4418  # km^3/s^2
_R_EARTH_KM: float = 6378.137          # km
_ALT_CUTOFF_KM: float = 600.0          # hard cutoff per plan 2026-04-01

_DEFAULT_CATALOG_PATH: str = "data/catalog/catalog.json"
_DEFAULT_DB_PATH: str = "data/catalog/tle_cache.db"


def _compute_alt_km_from_mean_motion(n_rev_day: float) -> float:
    """Compute mean orbital altitude from TLE mean motion.

    Args:
        n_rev_day: Mean motion in revolutions per day (from TLE line 2).

    Returns:
        Mean altitude above Earth's surface in km.
    """
    n_rad_s: float = n_rev_day * 2.0 * math.pi / 86400.0
    a_km: float = (_MU_EARTH_KM3_S2 / n_rad_s ** 2) ** (1.0 / 3.0)
    alt_km: float = a_km - _R_EARTH_KM
    return alt_km


def _parse_mean_motion_from_tle_line2(tle_line2: str) -> float:
    """Extract mean motion (rev/day) from TLE line 2.

    TLE line 2 mean motion field occupies columns 53-63 (1-indexed), which
    corresponds to indices 52:63 (0-indexed, Python slice notation).

    Args:
        tle_line2: Second line of the TLE set (must be at least 63 characters).

    Returns:
        Mean motion in revolutions per day.

    Raises:
        ValueError: If the mean motion field cannot be parsed.
    """
    field: str = tle_line2[52:63].strip()
    try:
        return float(field)
    except ValueError as exc:
        raise ValueError(
            f"Cannot parse mean motion from TLE line 2 field '{field}': {exc}"
        ) from exc


def main() -> None:
    """Entry point: verify catalog altitudes against local TLE cache."""
    catalog_path: str = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_CATALOG_PATH

    # Load catalog JSON
    try:
        with open(catalog_path, "r", encoding="utf-8") as fh:
            catalog: list = json.load(fh)
    except FileNotFoundError:
        print(f"ERROR: Catalog file not found: {catalog_path}")
        sys.exit(1)
    except json.JSONDecodeError as exc:
        print(f"ERROR: Catalog JSON is malformed: {exc}")
        sys.exit(1)

    if not isinstance(catalog, list) or not catalog:
        print("ERROR: Catalog must be a non-empty JSON array.")
        sys.exit(1)

    # Check for TLE cache
    if not os.path.exists(_DEFAULT_DB_PATH):
        print(
            f"No TLE cache found at {_DEFAULT_DB_PATH} — run replay.py first.\n"
            "WARNING: All NORAD IDs flagged NO_TLE must be verified against "
            "Space-Track directly before the catalog is used in production."
        )
        sys.exit(1)

    conn: sqlite3.Connection = sqlite3.connect(_DEFAULT_DB_PATH)
    conn.row_factory = sqlite3.Row

    # Build report rows
    report_rows: list[dict] = []

    for entry in catalog:
        norad_id: int = int(entry["norad_id"])
        name: str = entry["name"]

        # Retrieve the most recent TLE for this NORAD ID
        cursor = conn.execute(
            """
            SELECT tle_line2
            FROM tle_catalog
            WHERE norad_id = ?
            ORDER BY epoch_utc DESC
            LIMIT 1
            """,
            (norad_id,),
        )
        row = cursor.fetchone()

        if row is None:
            report_rows.append(
                {
                    "norad_id": norad_id,
                    "name": name,
                    "n_rev_day": None,
                    "alt_km": None,
                    "status": "NO_TLE",
                }
            )
            continue

        tle_line2: str = row[0]
        try:
            n_rev_day: float = _parse_mean_motion_from_tle_line2(tle_line2)
            alt_km: float = _compute_alt_km_from_mean_motion(n_rev_day)
            status: str = "PASS" if alt_km <= _ALT_CUTOFF_KM else "FAIL"
        except ValueError as exc:
            report_rows.append(
                {
                    "norad_id": norad_id,
                    "name": name,
                    "n_rev_day": None,
                    "alt_km": None,
                    "status": f"ERROR: {exc}",
                }
            )
            continue

        report_rows.append(
            {
                "norad_id": norad_id,
                "name": name,
                "n_rev_day": n_rev_day,
                "alt_km": alt_km,
                "status": status,
            }
        )

    conn.close()

    # Format the table
    header: str = (
        f"{'NORAD ID':>9}  {'Name':<30}  {'Mean Motion':>13}  {'Alt (km)':>10}  {'Status':<10}"
    )
    separator: str = "-" * len(header)

    lines: list[str] = [
        "Altitude Verification Report",
        f"Catalog: {catalog_path}",
        f"TLE cache: {_DEFAULT_DB_PATH}",
        f"Altitude cutoff: {_ALT_CUTOFF_KM} km (hard, no tolerance)",
        "",
        header,
        separator,
    ]

    for row in report_rows:
        n_str: str = (
            f"{row['n_rev_day']:13.8f}" if row["n_rev_day"] is not None else f"{'N/A':>13}"
        )
        alt_str: str = (
            f"{row['alt_km']:10.1f}" if row["alt_km"] is not None else f"{'N/A':>10}"
        )
        lines.append(
            f"{row['norad_id']:>9}  {row['name']:<30}  {n_str}  {alt_str}  {row['status']:<10}"
        )

    lines.append(separator)

    # Summary counts
    total: int = len(report_rows)
    pass_count: int = sum(1 for r in report_rows if r["status"] == "PASS")
    fail_count: int = sum(1 for r in report_rows if r["status"] == "FAIL")
    no_tle_count: int = sum(1 for r in report_rows if r["status"] == "NO_TLE")
    error_count: int = total - pass_count - fail_count - no_tle_count

    lines += [
        "",
        f"Summary: total={total}  pass={pass_count}  fail={fail_count}  "
        f"no_tle={no_tle_count}  error={error_count}",
        "",
    ]

    if no_tle_count > 0:
        lines += [
            "WARNING: The following NORAD IDs have NO cached TLE and MUST be verified",
            "against Space-Track before this catalog is used in production:",
            "",
        ]
        for row in report_rows:
            if row["status"] == "NO_TLE":
                lines.append(f"  {row['norad_id']:>9}  {row['name']}")
        lines.append("")

    if fail_count > 0:
        lines += [
            f"WARNING: {fail_count} object(s) have computed altitude > {_ALT_CUTOFF_KM} km",
            "and should be removed from the catalog:",
            "",
        ]
        for row in report_rows:
            if row["status"] == "FAIL":
                lines.append(
                    f"  {row['norad_id']:>9}  {row['name']:<30}  {row['alt_km']:.1f} km"
                )
        lines.append("")

    report_text: str = "\n".join(lines)
    print(report_text)

    # Write report file
    report_path: str = "data/catalog/altitude_verification_report.txt"
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(report_text)

    print(f"Report written to: {report_path}")


if __name__ == "__main__":
    main()
