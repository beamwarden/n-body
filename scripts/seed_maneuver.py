"""Synthetic maneuver injection script for demos.

Introduces a delta-V event into a selected object's cached TLE sequence
to trigger anomaly detection through the Kalman filter pipeline.

Usage:
    python scripts/seed_maneuver.py --object 25544 --delta-v 0.5
"""
import argparse
import datetime


def inject_maneuver(
    norad_id: int,
    delta_v_m_s: float,
    direction: str,
    epoch_offset_s: float,
    db_path: str,
) -> None:
    """Inject a synthetic maneuver into the cached TLE sequence.

    Args:
        norad_id: NORAD catalog ID of the target object.
        delta_v_m_s: Delta-V magnitude in m/s.
        direction: One of 'along-track', 'cross-track', 'radial'.
        epoch_offset_s: Seconds from current time for the maneuver epoch.
        db_path: Path to SQLite database.
    """
    raise NotImplementedError("not implemented")


def main() -> None:
    """Parse arguments and inject maneuver."""
    parser = argparse.ArgumentParser(description="Inject synthetic maneuver for demo")
    parser.add_argument("--object", type=int, required=True, help="NORAD ID")
    parser.add_argument("--delta-v", type=float, default=0.5, help="Delta-V in m/s")
    parser.add_argument("--direction", type=str, default="along-track",
                        choices=["along-track", "cross-track", "radial"],
                        help="Maneuver direction")
    parser.add_argument("--epoch-offset", type=float, default=0.0,
                        help="Seconds offset from now for maneuver epoch")
    parser.add_argument("--db", type=str, default="data/catalog/tle_cache.db",
                        help="SQLite DB path")
    args = parser.parse_args()
    inject_maneuver(args.object, args.delta_v, args.direction, args.epoch_offset, args.db)


if __name__ == "__main__":
    main()
