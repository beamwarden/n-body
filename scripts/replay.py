"""Historical TLE replay script for demos.

Replays cached TLE data through the observe-predict-validate loop
to simulate real-time operation without live Space-Track connectivity.

Usage:
    python scripts/replay.py --hours 72
"""
import argparse
import datetime


def replay_tles(hours: int, db_path: str, backend_url: str) -> None:
    """Replay cached TLEs over the specified time window.

    Args:
        hours: Number of hours of history to replay.
        db_path: Path to SQLite database with cached TLEs.
        backend_url: Backend API base URL.
    """
    raise NotImplementedError("not implemented")


def main() -> None:
    """Parse arguments and run replay."""
    parser = argparse.ArgumentParser(description="Replay historical TLEs for demo")
    parser.add_argument("--hours", type=int, default=72, help="Hours of history to replay")
    parser.add_argument("--db", type=str, default="data/catalog/tle_cache.db", help="SQLite DB path")
    parser.add_argument("--backend", type=str, default="http://localhost:8000", help="Backend URL")
    args = parser.parse_args()
    replay_tles(args.hours, args.db, args.backend)


if __name__ == "__main__":
    main()
