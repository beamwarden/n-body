"""Freeze a historical TLE window as a reproducible control dataset.

Copies tle_catalog, state_history, and alerts records within a date window
from the live database into a standalone control database.  Writes a
machine-readable manifest documenting the event coverage and any known gaps.

Usage:
    python scripts/freeze_dataset.py
    python scripts/freeze_dataset.py --start 2026-03-28 --end 2026-04-13
    python scripts/freeze_dataset.py --source-db data/catalog/tle_cache.db \\
        --output data/control/control_2026-03-28_2026-04-13.db

Replaying the frozen dataset:
    # Re-run processing against the control TLEs (full 16-day window):
    python scripts/replay.py --hours 400 --db data/control/control_<dates>.db

    # For a clean replay (no pre-existing state), clear state tables first:
    sqlite3 data/control/control_<dates>.db \\
        "DELETE FROM state_history; DELETE FROM alerts; DELETE FROM sqlite_sequence;"
"""
import argparse
import datetime
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backend.anomaly as anomaly
import backend.ingest as ingest
import backend.processing as processing

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Default window: the 16-day period that contains all three event classes.
_DEFAULT_START = "2026-03-28T00:00:00Z"
_DEFAULT_END = "2026-04-13T23:59:59Z"
_DEFAULT_SOURCE_DB = "data/catalog/tle_cache.db"
_DEFAULT_OUTPUT_DIR = "data/control"

# ---------------------------------------------------------------------------
# Known coverage gaps (objects requested by design spec but deorbited)
# ---------------------------------------------------------------------------
COVERAGE_GAPS = [
    {
        "requested": "Cosmos 1408 debris (NORAD 49863–49879)",
        "status": "deorbited",
        "notes": (
            "All 15 Cosmos 1408 ASAT fragments (Nov 2021 event, ~485 km) have "
            "deorbited. Each has a single final TLE from 2021–2023 — no continuous "
            "window is possible. High-drag regime is represented in this dataset by "
            "STARLINK-1990 (NORAD 46075, peak NIS 261,965) and STARLINK-1706 "
            "(NORAD 45706, peak NIS 2,707)."
        ),
    },
    {
        "requested": "CZ-5B rocket bodies (NORAD 48275, 52765)",
        "status": "deorbited",
        "notes": (
            "CZ-5B R/B (NORAD 48275) deorbited 2021-05-09; "
            "CZ-5B R/B (NORAD 52765) deorbited 2025-11-08. "
            "Neither is available in the 2026-03-28/2026-04-13 window."
        ),
    },
]

# ---------------------------------------------------------------------------
# Key events that make this window representative
# ---------------------------------------------------------------------------
KEY_EVENTS = [
    {
        "criterion": "deliberate_maneuver",
        "norad_id": 25544,
        "name": "ISS (ZARYA)",
        "object_class": "active_satellite",
        "event_start_utc": "2026-03-29T02:31:46Z",
        "event_end_utc": "2026-03-30T22:32:53Z",
        "peak_nis": 1050.4,
        "alert_classification": "maneuver",
        "notes": (
            "Four consecutive maneuver alerts between 02:31–02:41 UTC on 2026-03-29 "
            "(NIS 904–1050), consistent with a documented ISS reboost event. "
            "A second filter_divergence at 2026-03-30T03:57 (NIS 722) follows "
            "the reboost settling period. Confidence drops to 0.0 during the burst, "
            "recovers to 0.944 within one observation cycle."
        ),
    },
    {
        "criterion": "high_drag_regime",
        "norad_id": 46075,
        "name": "STARLINK-1990",
        "object_class": "active_satellite",
        "event_start_utc": "2026-04-09T16:00:01Z",
        "event_end_utc": "2026-04-10T14:00:01Z",
        "peak_nis": 261965.4,
        "alert_classification": "filter_divergence",
        "notes": (
            "Peak NIS 261,965 — 20,800× the chi²(6, p=0.05) threshold of 12.592. "
            "Two back-to-back filter_divergence/maneuver alerts at 16:00 and 20:00 UTC "
            "on 2026-04-09. This object sits in a decaying Starlink shell experiencing "
            "enhanced solar-driven atmospheric expansion; the TLE-to-TLE state jump "
            "exceeds the R=900 km² measurement noise floor by orders of magnitude. "
            "Analogous to the Cosmos 1408 debris drag regime requested in the spec."
        ),
    },
    {
        "criterion": "high_drag_regime_secondary",
        "norad_id": 45706,
        "name": "STARLINK-1706",
        "object_class": "active_satellite",
        "event_start_utc": "2026-03-28T19:10:36Z",
        "event_end_utc": "2026-04-13T05:15:19Z",
        "peak_nis": 2707.6,
        "alert_classification": "filter_divergence",
        "notes": (
            "23 NIS exceedances across the full 16-day window. Average NIS 18.83 "
            "(above threshold). Provides continuous baseline drag divergence as a "
            "complement to STARLINK-1990's acute event. Good for testing filter "
            "steady-state behaviour under persistent model mismatch."
        ),
    },
    {
        "criterion": "filter_divergence_extreme",
        "norad_id": 53088,
        "name": "UMBRA-06",
        "object_class": "active_satellite",
        "event_start_utc": "2026-04-09T11:48:55Z",
        "event_end_utc": "2026-04-10T18:07:13Z",
        "peak_nis": 126600.3,
        "alert_classification": "filter_divergence",
        "notes": (
            "SAR satellite with NIS 126,600 on 2026-04-09. The April 9 multi-object "
            "divergence event (STARLINK-1990, STARLINK-1706, UMBRA-06 all spiking "
            "within 8 hours) is consistent with a space weather event driving "
            "simultaneous atmospheric density enhancement across multiple VLEO shells."
        ),
    },
    {
        "criterion": "documented_fragmentation",
        "norad_id": 64157,
        "name": "STARLINK-34343 (FRAGMENTED)",
        "object_class": "active_satellite",
        "event_start_utc": "2026-04-09T09:16:37Z",
        "event_end_utc": "2026-04-13T02:48:42Z",
        "peak_nis": 0.0,
        "alert_classification": None,
        "notes": (
            "STARLINK-34343 fragmentation event. 7 TLE epochs from 2026-04-09 to "
            "2026-04-13. Filter NIS stays near 0 because the UKF re-initialises from "
            "each new TLE cold — the filter accepts the post-fragmentation orbit as "
            "valid. The fragmentation is documented at the catalog layer "
            "(object_class=active_satellite, name includes FRAGMENTED). "
            "This tests the filter_divergence catch-all: any attempt to predict the "
            "post-fragmentation track from a pre-fragmentation prior would produce "
            "extreme NIS. The current behaviour (no prior, fresh init) is correct "
            "for the cold-start path."
        ),
    },
]


def _checkpoint_wal(db: sqlite3.Connection) -> None:
    """Force a WAL checkpoint so the main DB file is fully up to date."""
    try:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    except sqlite3.OperationalError:
        pass  # WAL mode may not be active; harmless


def _copy_schema_and_data(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    start: str,
    end: str,
) -> dict:
    """Copy windowed records from src to dst.  Returns row counts."""
    counts: dict = {}

    cur = src.cursor()

    # tle_catalog — filtered by epoch_utc
    cur.execute(
        "SELECT norad_id, epoch_utc, tle_line1, tle_line2, fetched_at, source "
        "FROM tle_catalog "
        "WHERE epoch_utc >= ? AND epoch_utc <= ? "
        "ORDER BY epoch_utc, norad_id",
        (start, end),
    )
    rows = cur.fetchall()
    dst.executemany(
        "INSERT OR IGNORE INTO tle_catalog "
        "(norad_id, epoch_utc, tle_line1, tle_line2, fetched_at, source) "
        "VALUES (?,?,?,?,?,?)",
        rows,
    )
    counts["tle_catalog"] = len(rows)

    # state_history — filtered by epoch_utc
    cur.execute(
        "SELECT norad_id, epoch_utc, x_km, y_km, z_km, "
        "       vx_km_s, vy_km_s, vz_km_s, "
        "       cov_x_km2, cov_y_km2, cov_z_km2, "
        "       nis, confidence, anomaly_type, message_type "
        "FROM state_history "
        "WHERE epoch_utc >= ? AND epoch_utc <= ? "
        "ORDER BY epoch_utc, norad_id",
        (start, end),
    )
    rows = cur.fetchall()
    dst.executemany(
        "INSERT INTO state_history "
        "(norad_id, epoch_utc, x_km, y_km, z_km, "
        " vx_km_s, vy_km_s, vz_km_s, "
        " cov_x_km2, cov_y_km2, cov_z_km2, "
        " nis, confidence, anomaly_type, message_type) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    counts["state_history"] = len(rows)

    # alerts — filtered by detection_epoch_utc
    cur.execute(
        "SELECT norad_id, detection_epoch_utc, anomaly_type, nis_value, "
        "       resolution_epoch_utc, recalibration_duration_s, status, created_at "
        "FROM alerts "
        "WHERE detection_epoch_utc >= ? AND detection_epoch_utc <= ? "
        "ORDER BY detection_epoch_utc",
        (start, end),
    )
    rows = cur.fetchall()
    dst.executemany(
        "INSERT INTO alerts "
        "(norad_id, detection_epoch_utc, anomaly_type, nis_value, "
        " resolution_epoch_utc, recalibration_duration_s, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    counts["alerts"] = len(rows)

    dst.commit()
    return counts


def _unique_objects(db: sqlite3.Connection) -> int:
    return db.execute("SELECT COUNT(DISTINCT norad_id) FROM tle_catalog").fetchone()[0]


def freeze(
    start: str,
    end: str,
    source_db_path: str,
    output_db_path: str,
) -> None:
    """Extract and freeze the windowed control dataset."""
    output_path = Path(output_db_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists():
        logger.warning("Output DB already exists — overwriting: %s", output_db_path)
        output_path.unlink()

    logger.info("Opening source DB: %s", source_db_path)
    src = sqlite3.connect(source_db_path)
    _checkpoint_wal(src)

    logger.info("Creating control DB: %s", output_db_path)
    dst = ingest.init_catalog_db(output_db_path)
    processing._ensure_state_history_table(dst)
    anomaly.ensure_alerts_table(dst)

    logger.info("Copying window %s → %s …", start, end)
    counts = _copy_schema_and_data(src, dst, start, end)
    n_objects = _unique_objects(dst)
    src.close()

    start_dt = datetime.datetime.fromisoformat(start.replace("Z", "+00:00"))
    end_dt = datetime.datetime.fromisoformat(end.replace("Z", "+00:00"))
    window_days = (end_dt - start_dt).total_seconds() / 86400

    manifest = {
        "schema_version": "1.0",
        "created_at": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "window_start": start,
        "window_end": end,
        "window_days": round(window_days, 1),
        "source_db": str(Path(source_db_path).resolve()),
        "output_db": str(output_path.resolve()),
        "coverage": {
            "tle_records": counts["tle_catalog"],
            "state_history_records": counts["state_history"],
            "alerts": counts["alerts"],
            "unique_objects": n_objects,
        },
        "key_events": KEY_EVENTS,
        "coverage_gaps": COVERAGE_GAPS,
        "replay_instructions": {
            "full_window": (
                f"python scripts/replay.py --hours {int(window_days * 24) + 1} "
                f"--db {output_db_path}"
            ),
            "clean_replay": (
                f'sqlite3 {output_db_path} '
                '"DELETE FROM state_history; DELETE FROM alerts; '
                'DELETE FROM sqlite_sequence WHERE name IN (\'state_history\',\'alerts\');" '
                f"&& python scripts/replay.py --hours {int(window_days * 24) + 1} "
                f"--db {output_db_path}"
            ),
        },
    }

    manifest_path = output_path.parent / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    # Print summary
    print()
    print("=" * 60)
    print("Control dataset frozen")
    print("=" * 60)
    print(f"  Window   : {start}  →  {end}  ({window_days:.0f} days)")
    print(f"  Objects  : {n_objects}")
    print(f"  TLE rows : {counts['tle_catalog']}")
    print(f"  State rows: {counts['state_history']}")
    print(f"  Alerts   : {counts['alerts']}")
    print()
    print("Key events in window:")
    for ev in KEY_EVENTS:
        peak = f"NIS {ev['peak_nis']:,.0f}" if ev["peak_nis"] else "NIS ~0 (cold-start)"
        print(f"  [{ev['criterion']:30s}] NORAD {ev['norad_id']:6d}  {ev['name']}")
        print(f"    {ev['event_start_utc']}  {peak}  ({ev['alert_classification']})")
    print()
    print("Coverage gaps (deorbited objects):")
    for gap in COVERAGE_GAPS:
        print(f"  {gap['requested']}: {gap['status']}")
    print()
    print(f"  Control DB : {output_db_path}")
    print(f"  Manifest   : {manifest_path}")
    print()
    print("To replay against the control dataset:")
    print(f"  {manifest['replay_instructions']['full_window']}")
    print()
    dst.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Freeze a historical TLE window as a reproducible control dataset."
    )
    parser.add_argument(
        "--start",
        default=_DEFAULT_START,
        help=f"Window start (ISO 8601 UTC, default: {_DEFAULT_START})",
    )
    parser.add_argument(
        "--end",
        default=_DEFAULT_END,
        help=f"Window end (ISO 8601 UTC, default: {_DEFAULT_END})",
    )
    parser.add_argument(
        "--source-db",
        default=_DEFAULT_SOURCE_DB,
        help=f"Path to live SQLite database (default: {_DEFAULT_SOURCE_DB})",
    )
    parser.add_argument(
        "--output",
        default=None,
        help=(
            "Path for the output control DB. "
            "Defaults to data/control/control_<start>_<end>.db"
        ),
    )
    args = parser.parse_args()

    start = args.start
    end = args.end
    source_db = args.source_db or _DEFAULT_SOURCE_DB

    if args.output:
        output_db = args.output
    else:
        start_slug = start[:10]
        end_slug = end[:10]
        output_db = f"{_DEFAULT_OUTPUT_DIR}/control_{start_slug}_{end_slug}.db"

    try:
        freeze(start=start, end=end, source_db_path=source_db, output_db_path=output_db)
    except Exception as exc:  # noqa: BLE001
        logger.error("freeze_dataset failed: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
