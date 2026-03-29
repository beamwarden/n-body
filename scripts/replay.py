"""Historical TLE replay script for demos.

Replays cached TLE data through the observe-predict-validate loop
to simulate real-time operation without live Space-Track connectivity.

Satisfies F-060, F-063, NF-020, TD-023.

Usage:
    python scripts/replay.py [--hours N] [--db PATH] [--catalog PATH] [--delay-ms N]

Examples:
    python scripts/replay.py --hours 72
    python scripts/replay.py --hours 24 --db data/catalog/tle_cache.db --delay-ms 100
"""
import argparse
import datetime
import logging
import os
import sys
import time
from pathlib import Path

# Add the project root to sys.path so that backend package imports work when
# this script is run as `python scripts/replay.py` from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backend.anomaly as anomaly
import backend.ingest as ingest
import backend.processing as processing

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def replay_tles(
    hours: int,
    db_path: str,
    catalog_config_path: str,
    delay_ms: int,
) -> None:
    """Replay cached TLEs over the specified time window.

    Loads TLEs from the SQLite cache for all catalog objects within the time
    window, sorts them by (epoch_utc, norad_id) for deterministic ordering,
    and drives each through processing.process_single_object(). Writes
    state_history and anomaly rows to the database.

    Prints progress to stdout and a summary at completion.
    Exits with code 1 on unrecoverable error.

    Args:
        hours: Number of hours of history to replay (default 72).
        db_path: Path to the SQLite database with cached TLEs.
        catalog_config_path: Path to the catalog JSON config file.
        delay_ms: If > 0, sleep this many milliseconds between each TLE step
                  (useful for pacing a live demo). Default 0 (batch mode).
    """
    # Open DB and ensure required tables exist.
    db = ingest.init_catalog_db(db_path)
    processing._ensure_state_history_table(db)
    anomaly.ensure_alerts_table(db)

    # Load catalog config.
    try:
        catalog_entries = ingest.load_catalog_config(catalog_config_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[replay] ERROR: Could not load catalog config from {catalog_config_path!r}: {exc}")
        sys.exit(1)

    # Build a dict for quick lookup: norad_id -> catalog_entry.
    catalog_map: dict[int, dict] = {int(e["norad_id"]): e for e in catalog_entries}

    # Compute the lower time bound for TLE retrieval.
    now_utc: datetime.datetime = datetime.datetime.now(datetime.timezone.utc)
    since_utc: datetime.datetime = now_utc - datetime.timedelta(hours=hours)

    # Collect all TLEs across all catalog objects within the time window.
    all_tle_records: list[tuple[str, int, dict]] = []  # (epoch_utc_str, norad_id, tle_record)
    for norad_id, entry in catalog_map.items():
        tle_records = ingest.get_cached_tles(db, norad_id, since_utc=since_utc)
        for tle_record in tle_records:
            all_tle_records.append((tle_record["epoch_utc"], norad_id, tle_record))

    if not all_tle_records:
        print(
            f"[replay] No cached TLEs found for any catalog object "
            f"in the last {hours} hours. Populate the cache first via ingest.py."
        )
        sys.exit(0)

    # Sort by (epoch_utc, norad_id) for deterministic ordering (plan step 3g).
    all_tle_records.sort(key=lambda t: (t[0], t[1]))

    print(
        f"[replay] Starting replay: {len(all_tle_records)} TLE steps, "
        f"{len(catalog_map)} objects, window={hours}h, delay={delay_ms}ms"
    )

    # Shared filter_states dict, mutated in place by process_single_object.
    filter_states: dict[int, dict] = {}

    total_updates: int = 0
    total_anomalies: int = 0

    for epoch_utc_str, norad_id, tle_record in all_tle_records:
        entry = catalog_map[norad_id]
        try:
            messages = processing.process_single_object(
                db=db,
                entry=entry,
                norad_id=norad_id,
                filter_states=filter_states,
                tle_record=tle_record,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "replay: error processing NORAD %d @ %s: %s",
                norad_id,
                epoch_utc_str,
                exc,
                exc_info=True,
            )
            continue

        if not messages:
            # Skipped (duplicate or out-of-order epoch).
            continue

        total_updates += 1

        # Extract NIS and confidence from the first message (cold-start or
        # state_update/anomaly — all carry these fields).
        first_msg = messages[0]
        nis_val: float = first_msg.get("nis", 0.0)
        confidence_val: float = first_msg.get("confidence", 1.0)

        anomaly_type_str = first_msg.get("anomaly_type")
        if anomaly_type_str is not None:
            total_anomalies += 1

        status_str = anomaly_type_str if anomaly_type_str is not None else "nominal"
        print(
            f"[replay] {norad_id} @ {epoch_utc_str} | "
            f"NIS={nis_val:.2f} | conf={confidence_val:.2f} | {status_str}"
        )

        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)

    print(
        f"[replay] Complete: {len(catalog_map)} objects, "
        f"{total_updates} updates, {total_anomalies} anomalies detected."
    )
    db.close()


def main() -> None:
    """Parse arguments and run replay."""
    parser = argparse.ArgumentParser(
        description=(
            "Replay historical TLEs through the observe-predict-validate loop "
            "without a running server (F-060)."
        )
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=72,
        help="Hours of history to replay (default: 72)",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help=(
            "Path to SQLite TLE cache database. "
            "Defaults to NBODY_DB_PATH env var or data/catalog/tle_cache.db"
        ),
    )
    parser.add_argument(
        "--catalog",
        type=str,
        default="data/catalog/catalog.json",
        help="Path to catalog JSON config (default: data/catalog/catalog.json)",
    )
    parser.add_argument(
        "--delay-ms",
        type=int,
        default=0,
        help=(
            "Milliseconds to sleep between each TLE step. "
            "0 = batch mode (default). Set > 0 to pace live demo narration."
        ),
    )
    args = parser.parse_args()

    # Resolve DB path: CLI arg > env var > default.
    db_path: str = (
        args.db
        or os.environ.get("NBODY_DB_PATH")
        or "data/catalog/tle_cache.db"
    )

    try:
        replay_tles(
            hours=args.hours,
            db_path=db_path,
            catalog_config_path=args.catalog,
            delay_ms=args.delay_ms,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[replay] FATAL: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
