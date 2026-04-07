"""Synthetic conjunction injection script for demos.

Inserts a synthetic threat object (NORAD 99999) into the TLE cache at a position
engineered to trigger a conjunction detection against a specified primary object.
The threat is placed at the primary's along-track position but offset by `--miss-km`
in the cross-track direction at the conjunction epoch.

Satisfies F-030, F-061, F-063, NF-023, NF-040.

Usage:
    python scripts/seed_conjunction.py [--object NORAD_ID] [--offset-min MINUTES]
        [--miss-km DISTANCE] [--catalog PATH] [--db PATH]
        [--trigger] [--server-url URL] [--clear]

Examples:
    python scripts/seed_conjunction.py --object 25544 --miss-km 2.0
    python scripts/seed_conjunction.py --object 25544 --miss-km 1.5 --trigger
    python scripts/seed_conjunction.py --clear

NOTE on TLE generation (Option B — mean-element space manipulation):
    Instead of the ECI→Keplerian→TLE roundtrip (which introduces 15-50 km fitting
    error due to SGP4 secular perturbation corrections), this script works directly
    in TLE mean-element space. It copies all mean elements from the primary TLE and
    increments only the mean anomaly by delta_M = arcsin(miss_km / r), where r is
    the orbital radius at the conjunction epoch. This keeps the threat object on the
    same orbit as the primary — separated only in phase — and eliminates the fitting
    error to within the SGP4 numerical precision (~10 m). The verification step prints
    the actual miss distance after insertion.

NOTE on --clear and catalog.json:
    --clear modifies catalog.json which is checked into git. After a demo session,
    run `git checkout data/catalog/catalog.json` to restore the clean state.
"""
import argparse
import datetime
import json
import logging
import math
import os
import sys
from pathlib import Path
from typing import Optional

import numpy as np
from numpy.typing import NDArray

# Add the project root to sys.path so that backend package imports work when
# this script is run as `python scripts/seed_conjunction.py` from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backend.ingest as ingest
import backend.propagator as propagator

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# NORAD ID reserved for the synthetic threat object.
_THREAT_NORAD_ID: int = 99999
_THREAT_NAME: str = "THREAT-SIM"
_THREAT_OBJECT_CLASS: str = "debris"

# Default DB path (mirrors ingest._DEFAULT_DB_PATH).
_DEFAULT_DB_PATH: str = "data/catalog/tle_cache.db"


def _clear_synthetic_threat(
    catalog_path: str,
    db: object,
) -> None:
    """Remove NORAD 99999 from catalog.json and the TLE cache.

    Per plan step 2.

    Args:
        catalog_path: Path to catalog.json.
        db: Open SQLite connection.
    """
    # Remove from catalog.json.
    try:
        with open(catalog_path, "r", encoding="utf-8") as fh:
            catalog: list = json.load(fh)
        before_count = len(catalog)
        catalog = [e for e in catalog if int(e.get("norad_id", 0)) != _THREAT_NORAD_ID]
        with open(catalog_path, "w", encoding="utf-8") as fh:
            json.dump(catalog, fh, indent=2)
        removed = before_count - len(catalog)
        print(
            f"Removed {removed} entry/entries with NORAD {_THREAT_NORAD_ID} "
            f"from {catalog_path}."
        )
    except FileNotFoundError:
        print(f"WARNING: Catalog file not found at {catalog_path} — skipping catalog cleanup.")

    # Remove from TLE cache.
    import sqlite3 as _sqlite3
    db.execute(
        "DELETE FROM tle_catalog WHERE norad_id = ?",
        (_THREAT_NORAD_ID,),
    )
    db.commit()
    print(
        f"Synthetic threat object (NORAD {_THREAT_NORAD_ID}) removed "
        "from catalog and TLE cache."
    )


def _tle_checksum(line: str) -> int:
    """Compute the TLE checksum digit for a 68-character line body.

    The checksum is the sum of all digit characters plus 1 for each '-' sign,
    modulo 10.

    Args:
        line: The first 68 characters of a TLE line (positions 0–67).

    Returns:
        Single-digit checksum (0–9).
    """
    total: int = 0
    for ch in line[:68]:
        if ch.isdigit():
            total += int(ch)
        elif ch == "-":
            total += 1
    return total % 10


def generate_threat_tle(
    primary_norad_id: int,
    offset_min: float,
    miss_km: float,
    db: object,
) -> tuple[str, str, datetime.datetime]:
    """Generate a synthetic TLE for the threat object (NORAD 99999).

    Per plan steps 3 and 4, using Option B (TLE mean-element space manipulation).

    Algorithm:
      1. Fetch the primary's latest TLE from the DB.
      2. Compute the conjunction epoch = now + offset_min minutes.
      3. Propagate the primary to the conjunction epoch to get orbital radius.
      4. Compute delta_M = arcsin(miss_km / r) — the along-track mean anomaly
         shift that produces the desired along-track separation at epoch.
      5. Copy all mean elements from the primary TLE; increment the mean anomaly
         field only. Reformat TLE lines with updated NORAD ID and checksum.

    This approach works entirely in SGP4 mean-element space, avoiding the
    15-50 km fitting error of the ECI→Keplerian→TLE roundtrip.

    Args:
        primary_norad_id: NORAD ID of the primary object.
        offset_min: Minutes from now to the conjunction epoch.
        miss_km: Target along-track separation distance in km at the conjunction
                 epoch. The threat will be delta_M ahead of the primary in mean
                 anomaly, producing ~miss_km separation at that instant.
        db: Open SQLite connection to the TLE cache.

    Returns:
        Tuple of (syn_line1, syn_line2, conjunction_epoch_utc).

    Raises:
        SystemExit: If no TLE is found for the primary, or if propagation fails,
                    or if the generated TLE fails validation.
    """
    from sgp4.api import Satrec, WGS72

    # Step 1: Load primary TLE.
    tle_record: Optional[dict] = ingest.get_latest_tle(db, primary_norad_id)
    if tle_record is None:
        print(
            f"[conjunction] ERROR: No cached TLE found for NORAD {primary_norad_id}. "
            "Run the server with a live Space-Track connection to populate the cache first."
        )
        sys.exit(1)

    tle_line1: str = tle_record["tle_line1"]
    tle_line2: str = tle_record["tle_line2"]

    # Step 2: Compute conjunction epoch.
    now_utc: datetime.datetime = datetime.datetime.now(datetime.timezone.utc)
    conjunction_epoch_utc: datetime.datetime = now_utc + datetime.timedelta(minutes=offset_min)

    # Step 3: Parse primary TLE mean elements and propagate to get orbital radius.
    satrec = Satrec.twoline2rv(tle_line1, tle_line2, WGS72)
    try:
        position_eci_km, _ = propagator.propagate_tle(
            tle_line1, tle_line2, conjunction_epoch_utc
        )
    except ValueError as exc:
        print(f"[conjunction] ERROR: SGP4 propagation failed: {exc}")
        sys.exit(1)

    orbital_radius_km: float = float(np.linalg.norm(position_eci_km))

    # Step 4: Compute mean anomaly delta.
    # delta_M ≈ arcsin(miss_km / r) for small angles. This places the threat
    # object miss_km ahead of the primary along the orbit at the conjunction epoch.
    ratio: float = miss_km / orbital_radius_km
    if ratio >= 1.0:
        print(
            f"[conjunction] ERROR: miss_km ({miss_km:.3f} km) exceeds orbital "
            f"radius ({orbital_radius_km:.1f} km) — unphysical."
        )
        sys.exit(1)
    delta_M_deg: float = math.degrees(math.asin(ratio))

    # satrec.mo is the mean anomaly at TLE epoch in radians.
    primary_M_deg: float = math.degrees(satrec.mo) % 360.0
    threat_M_deg: float = (primary_M_deg + delta_M_deg) % 360.0

    # Step 5: Build synthetic TLE lines.
    #
    # TLE line 1 layout (0-indexed, 69 chars total incl. checksum):
    #   [0]      line number '1'
    #   [1]      ' '
    #   [2:7]    NORAD catalog number (5 chars)
    #   [7:68]   classification through element set number (unchanged from primary)
    #   [68]     checksum
    #
    # TLE line 2 layout:
    #   [0]      line number '2'
    #   [1]      ' '
    #   [2:7]    NORAD catalog number (5 chars)
    #   [7:43]   ' ' + inclination + ' ' + RAAN + ' ' + eccentricity + ' ' + argp + ' '
    #   [43:51]  mean anomaly (8 chars, format: %8.4f degrees)
    #   [51:68]  ' ' + mean motion + revolution number
    #   [68]     checksum
    #
    # Only the NORAD ID and mean anomaly fields change; all other mean elements
    # are copied verbatim from the primary TLE to preserve SGP4 accuracy.

    norad_str: str = f"{_THREAT_NORAD_ID:5d}"  # '99999'

    line1_body: str = "1 " + norad_str + tle_line1[7:68]
    syn_line1: str = line1_body + str(_tle_checksum(line1_body))

    mean_anomaly_str: str = f"{threat_M_deg:8.4f}"
    line2_body: str = "2 " + norad_str + tle_line2[7:43] + mean_anomaly_str + tle_line2[51:68]
    syn_line2: str = line2_body + str(_tle_checksum(line2_body))

    # Validate the generated TLE.
    if not ingest.validate_tle(syn_line1, syn_line2):
        print(
            "[conjunction] ERROR: Generated synthetic TLE failed checksum validation."
        )
        print(f"  Line 1: {syn_line1!r}")
        print(f"  Line 2: {syn_line2!r}")
        sys.exit(1)

    return syn_line1, syn_line2, conjunction_epoch_utc


def inject_conjunction(
    primary_norad_id: int,
    offset_min: float,
    miss_km: float,
    catalog_path: str,
    db_path: str,
    trigger: bool,
    server_url: str,
) -> None:
    """Inject a synthetic threat object to trigger conjunction detection.

    Per plan steps 5–8. Orchestrates the full injection workflow:
      1. Generate the threat TLE.
      2. Insert NORAD 99999 into catalog.json.
      3. Insert the synthetic TLE into the SQLite cache.
      4. Verify the conjunction via re-propagation (sanity check).
      5. Print confirmation and optionally trigger the server.

    Args:
        primary_norad_id: NORAD ID of the primary object.
        offset_min: Minutes from now to the conjunction epoch.
        miss_km: Target cross-track miss distance in km.
        catalog_path: Path to catalog.json.
        db_path: Path to the SQLite TLE cache.
        trigger: If True, POST to /admin/trigger-process after insertion.
        server_url: Base URL of the running server (used only if trigger=True).
    """
    import httpx  # local import — only needed when --trigger is used

    # Open DB.
    db = ingest.init_catalog_db(db_path)

    # Step 3-4: Generate synthetic TLE.
    syn_line1, syn_line2, conjunction_epoch_utc = generate_threat_tle(
        primary_norad_id=primary_norad_id,
        offset_min=offset_min,
        miss_km=miss_km,
        db=db,
    )

    # Step 5: Insert NORAD 99999 into catalog.json.
    try:
        with open(catalog_path, "r", encoding="utf-8") as fh:
            catalog: list = json.load(fh)
    except FileNotFoundError:
        print(f"[conjunction] ERROR: Catalog file not found at {catalog_path}.")
        db.close()
        sys.exit(1)

    # Remove any existing NORAD 99999 entry to avoid duplicates.
    catalog = [e for e in catalog if int(e.get("norad_id", 0)) != _THREAT_NORAD_ID]
    catalog.append(
        {
            "norad_id": _THREAT_NORAD_ID,
            "name": _THREAT_NAME,
            "object_class": _THREAT_OBJECT_CLASS,
        }
    )
    with open(catalog_path, "w", encoding="utf-8") as fh:
        json.dump(catalog, fh, indent=2)

    # Step 6: Insert synthetic TLE into SQLite cache.
    now_utc: datetime.datetime = datetime.datetime.now(datetime.timezone.utc)
    epoch_utc_str: str = conjunction_epoch_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    tle_dict: dict = {
        "norad_id": _THREAT_NORAD_ID,
        "epoch_utc": epoch_utc_str,
        "tle_line1": syn_line1,
        "tle_line2": syn_line2,
    }
    inserted: int = ingest.cache_tles(db, [tle_dict], fetched_at_utc=now_utc)

    if inserted == 0:
        print(
            "[conjunction] NOTE: TLE with this epoch already exists in cache "
            "(INSERT OR IGNORE skipped duplicate). "
            "The server/replay will use the existing record."
        )

    # Step 7: Verification propagation — confirm the actual miss distance.
    try:
        primary_tle_record = ingest.get_latest_tle(db, primary_norad_id)
        if primary_tle_record is not None:
            primary_pos_eci_km, _ = propagator.propagate_tle(
                primary_tle_record["tle_line1"],
                primary_tle_record["tle_line2"],
                conjunction_epoch_utc,
            )
            threat_pos_eci_km, _ = propagator.propagate_tle(
                syn_line1, syn_line2, conjunction_epoch_utc
            )
            actual_dist_km: float = float(
                np.linalg.norm(threat_pos_eci_km - primary_pos_eci_km)
            )
            print(
                f"Verification: miss distance at conjunction epoch = "
                f"{actual_dist_km:.3f} km (target: {miss_km} km)"
            )
            if actual_dist_km > 2.0 * miss_km:
                print(
                    f"WARNING: Actual miss distance ({actual_dist_km:.3f} km) exceeds "
                    f"2x target ({2.0 * miss_km:.3f} km). "
                    "TLE fitting error may be large."
                )
    except ValueError as exc:
        print(f"[conjunction] WARNING: Verification propagation failed: {exc}")

    db.close()

    # Step 8: Print confirmation.
    print(f"Synthetic threat inserted (NORAD {_THREAT_NORAD_ID}, {_THREAT_NAME}).")
    print(
        f"To detect conjunction: run trigger-process, then "
        f"seed_maneuver --object {primary_norad_id} --delta-v 5.0 --trigger "
        "to demonstrate conjunction detection."
    )

    if trigger:
        trigger_url: str = f"{server_url.rstrip('/')}/admin/trigger-process"
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(trigger_url)
                resp.raise_for_status()
                result: dict = resp.json()
                processed: int = result.get("processed", 0)
                print(
                    f"[conjunction] Server processed {processed} objects — "
                    "check visualization"
                )
        except Exception as exc:  # noqa: BLE001
            print(
                f"[conjunction] Server unreachable ({exc}) "
                "— run: python scripts/replay.py to process"
            )


def main() -> None:
    """Parse arguments and run conjunction injection or teardown."""
    parser = argparse.ArgumentParser(
        description=(
            "Inject a synthetic threat object (NORAD 99999) into the TLE cache "
            "to trigger conjunction detection in the ne-body pipeline (F-030, F-061)."
        )
    )
    parser.add_argument(
        "--object",
        type=int,
        default=25544,
        metavar="NORAD_ID",
        help="NORAD catalog ID of the primary object (default: 25544 / ISS)",
    )
    parser.add_argument(
        "--offset-min",
        type=float,
        default=30.0,
        metavar="MINUTES",
        help="Minutes from now for the conjunction epoch (default: 30.0)",
    )
    parser.add_argument(
        "--miss-km",
        type=float,
        default=2.0,
        metavar="DISTANCE",
        help="Target cross-track miss distance in km (default: 2.0). "
             "For values below 0.3 km, TLE fitting error may dominate.",
    )
    parser.add_argument(
        "--catalog",
        type=str,
        default="data/catalog/catalog.json",
        metavar="CATALOG_PATH",
        help="Path to catalog.json (default: data/catalog/catalog.json)",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        metavar="DB_PATH",
        help=(
            "Path to SQLite TLE cache (default: NBODY_DB_PATH env var "
            "or data/catalog/tle_cache.db)"
        ),
    )
    parser.add_argument(
        "--trigger",
        action="store_true",
        help="POST to /admin/trigger-process after insertion (requires running server)",
    )
    parser.add_argument(
        "--server-url",
        type=str,
        default="http://localhost:8001",
        metavar="URL",
        help="Base URL of the running server (default: http://localhost:8001)",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help=(
            "Remove NORAD 99999 from catalog.json and TLE cache (demo teardown). "
            "Run `git checkout data/catalog/catalog.json` afterwards to restore "
            "the clean git state."
        ),
    )
    args = parser.parse_args()

    # Resolve DB path: argument > env var > default.
    db_path: str = (
        args.db
        or os.environ.get("NBODY_DB_PATH")
        or _DEFAULT_DB_PATH
    )

    if args.clear:
        db = ingest.init_catalog_db(db_path)
        _clear_synthetic_threat(catalog_path=args.catalog, db=db)
        db.close()
        return

    inject_conjunction(
        primary_norad_id=args.object,
        offset_min=args.offset_min,
        miss_km=args.miss_km,
        catalog_path=args.catalog,
        db_path=db_path,
        trigger=args.trigger,
        server_url=args.server_url,
    )


if __name__ == "__main__":
    main()
