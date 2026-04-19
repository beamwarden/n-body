"""Conjunction risk screening module. Pure synchronous, no FastAPI or asyncio imports.

Screens the post-recalibration predicted trajectory of an anomalous object against
all other tracked objects to identify first-order (within 5 km) and second-order
(within 10 km of any first-order object) conjunction risks.

This module is called from main.py via asyncio.run_in_executor so it does not block
the event loop. It is also directly importable in synchronous test contexts.

Coordinate frame: all positions are ECI J2000 km as produced by propagator.propagate_tle.
Units: km for distances, seconds for time intervals, UTC-aware datetimes for all epochs.
"""

import datetime
import logging

import numpy as np
from numpy.typing import NDArray

import backend.propagator as propagator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level screening constants
# ---------------------------------------------------------------------------

# One LEO orbital period at ~400 km altitude is approximately 5560 s. 5400 s
# (90 minutes) provides coverage of one full orbit for ISS-altitude objects.
SCREENING_HORIZON_S: int = 5400

# 60-second steps yield 90 trajectory points per object.
# Trade-off: 90 points x 20 objects = 1800 SGP4+astropy calls, ~9-18 s total.
# See plan docs/plans/2026-03-29-conjunction-risk.md Risks section for mitigation.
SCREENING_STEP_S: int = 60

# First-order risk threshold: 5 km spherical miss distance.
# POST-POC: replace with RSW pizza-box screening (TD-027).
FIRST_ORDER_THRESHOLD_KM: float = 5.0

# Second-order risk threshold: 10 km spherical miss distance between a
# first-order object and any remaining catalog object.
SECOND_ORDER_THRESHOLD_KM: float = 10.0


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def generate_trajectory_eci_km(
    tle_line1: str,
    tle_line2: str,
    start_epoch_utc: datetime.datetime,
    horizon_s: int,
    step_s: int,
) -> list[tuple[datetime.datetime, NDArray[np.float64]]]:
    """Propagate a TLE forward and return (epoch, position_eci_km) tuples.

    Propagates from start_epoch_utc at step_s intervals for horizon_s seconds.
    Uses propagator.propagate_tle for each step. Skips failed propagation points
    with a warning log rather than aborting the entire trajectory.

    Args:
        tle_line1: TLE line 1.
        tle_line2: TLE line 2.
        start_epoch_utc: UTC epoch to begin propagation. Must be UTC-aware.
        horizon_s: Total propagation window in seconds.
        step_s: Time step between trajectory points in seconds.

    Returns:
        List of (epoch_utc, position_eci_km) tuples. position_eci_km is a
        3-element numpy array in ECI J2000 km. Length may be shorter than
        horizon_s // step_s if some propagation steps fail.

    Raises:
        ValueError: If start_epoch_utc is not UTC-aware.
    """
    if start_epoch_utc.tzinfo is None or start_epoch_utc.utcoffset() is None:
        raise ValueError("start_epoch_utc must be UTC-aware (tzinfo set to datetime.timezone.utc).")

    trajectory: list[tuple[datetime.datetime, NDArray[np.float64]]] = []
    num_steps = horizon_s // step_s

    for i in range(1, num_steps + 1):
        point_epoch_utc: datetime.datetime = start_epoch_utc + datetime.timedelta(seconds=i * step_s)
        try:
            position_eci_km, _ = propagator.propagate_tle(tle_line1, tle_line2, point_epoch_utc)
            trajectory.append((point_epoch_utc, position_eci_km))
        except ValueError as exc:
            logger.warning(
                "generate_trajectory_eci_km: propagation failed at step %d (t=+%d s): %s — skipping point",
                i,
                i * step_s,
                exc,
            )

    return trajectory


def compute_min_distance_km(
    traj_a: list[tuple[datetime.datetime, NDArray[np.float64]]],
    traj_b: list[tuple[datetime.datetime, NDArray[np.float64]]],
) -> tuple[float, datetime.datetime]:
    """Compute the minimum Euclidean separation between two co-epoch trajectories.

    Assumes both trajectories have aligned time steps (same length, same epochs).
    If trajectories differ in length (due to skipped propagation points), the
    shorter length is used. Returns a large sentinel distance if either trajectory
    is empty.

    Args:
        traj_a: List of (epoch_utc, position_eci_km) tuples for object A.
        traj_b: List of (epoch_utc, position_eci_km) tuples for object B.

    Returns:
        Tuple of (min_distance_km, time_of_closest_approach_utc).
        If either trajectory is empty, returns (1e9, first epoch of non-empty or
        datetime.min UTC-aware).
    """
    # Sentinel: return very large distance if no comparison can be made.
    _sentinel_epoch = datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC)

    if not traj_a or not traj_b:
        logger.warning(
            "compute_min_distance_km: one or both trajectories are empty — returning sentinel distance 1e9 km"
        )
        return 1e9, _sentinel_epoch

    n: int = min(len(traj_a), len(traj_b))

    min_dist_km: float = float("inf")
    tca_utc: datetime.datetime = traj_a[0][0]

    for i in range(n):
        epoch_utc, pos_a = traj_a[i]
        _, pos_b = traj_b[i]
        dist_km: float = float(np.linalg.norm(pos_a - pos_b))
        if dist_km < min_dist_km:
            min_dist_km = dist_km
            tca_utc = epoch_utc

    return min_dist_km, tca_utc


def screen_conjunctions(
    anomalous_norad_id: int,
    anomalous_tle_line1: str,
    anomalous_tle_line2: str,
    screening_epoch_utc: datetime.datetime,
    other_objects: list[dict],
    catalog_name_map: dict[int, str],
) -> dict:
    """Screen an anomalous object's predicted trajectory against all other tracked objects.

    Identifies first-order conjunction risks (min distance <= FIRST_ORDER_THRESHOLD_KM
    between the anomalous object and any other object) and second-order risks (min
    distance <= SECOND_ORDER_THRESHOLD_KM between any first-order object and any
    remaining object, excluding the anomalous object).

    This function is CPU-bound (~9-18 seconds for 20 objects x 90 SGP4 steps).
    Call it via asyncio.run_in_executor to avoid blocking the event loop.

    Args:
        anomalous_norad_id: NORAD ID of the object that triggered the anomaly.
        anomalous_tle_line1: TLE line 1 for the anomalous object (post-recalibration).
        anomalous_tle_line2: TLE line 2 for the anomalous object.
        screening_epoch_utc: UTC epoch to begin trajectory propagation. Must be
            UTC-aware. Typically the epoch from the anomaly WS message.
        other_objects: List of dicts, each with keys 'norad_id' (int), 'tle_line1'
            (str), 'tle_line2' (str). One entry per non-anomalous tracked object
            with an initialized filter.
        catalog_name_map: Dict mapping norad_id (int) to object name (str).

    Returns:
        Dict matching the conjunction_risk WebSocket message schema:
        {
            "type": "conjunction_risk",
            "anomalous_norad_id": int,
            "screening_epoch_utc": str,   # ISO-8601 with Z suffix
            "horizon_s": int,
            "threshold_km": float,
            "first_order": [
                {
                    "norad_id": int,
                    "name": str,
                    "min_distance_km": float,
                    "time_of_closest_approach_utc": str,  # ISO-8601 with Z suffix
                }
            ],
            "second_order": [
                {
                    "norad_id": int,
                    "name": str,
                    "min_distance_km": float,
                    "via_norad_id": int,
                    "time_of_closest_approach_utc": str,  # ISO-8601 with Z suffix
                }
            ],
        }
    """
    logger.info(
        "screen_conjunctions: starting for anomalous NORAD %d at epoch %s, %d other objects",
        anomalous_norad_id,
        screening_epoch_utc.isoformat(),
        len(other_objects),
    )

    # Step 1: Generate trajectory for the anomalous object.
    anomalous_traj = generate_trajectory_eci_km(
        anomalous_tle_line1,
        anomalous_tle_line2,
        screening_epoch_utc,
        SCREENING_HORIZON_S,
        SCREENING_STEP_S,
    )
    logger.debug("screen_conjunctions: anomalous trajectory has %d points", len(anomalous_traj))

    first_order: list[dict] = []
    # Cache trajectories for first-order objects so second-order screening can reuse them.
    first_order_trajs: dict[int, list[tuple[datetime.datetime, NDArray[np.float64]]]] = {}
    # All trajectories cached by norad_id for second-order screening.
    all_other_trajs: dict[int, list[tuple[datetime.datetime, NDArray[np.float64]]]] = {}

    # Step 2: Screen each other object against the anomalous object.
    for obj in other_objects:
        other_norad_id: int = int(obj["norad_id"])
        other_line1: str = obj["tle_line1"]
        other_line2: str = obj["tle_line2"]

        traj = generate_trajectory_eci_km(
            other_line1,
            other_line2,
            screening_epoch_utc,
            SCREENING_HORIZON_S,
            SCREENING_STEP_S,
        )
        all_other_trajs[other_norad_id] = traj

        min_dist_km, tca_utc = compute_min_distance_km(anomalous_traj, traj)

        # Step 3: Classify first-order.
        if min_dist_km <= FIRST_ORDER_THRESHOLD_KM:
            name: str = catalog_name_map.get(other_norad_id, str(other_norad_id))
            first_order.append(
                {
                    "norad_id": other_norad_id,
                    "name": name,
                    "min_distance_km": float(min_dist_km),
                    "time_of_closest_approach_utc": _format_epoch(tca_utc),
                }
            )
            first_order_trajs[other_norad_id] = traj
            logger.info(
                "screen_conjunctions: first-order risk NORAD %d vs %d, min dist=%.3f km, TCA=%s",
                anomalous_norad_id,
                other_norad_id,
                min_dist_km,
                tca_utc.isoformat(),
            )

    # Step 4: Second-order: screen each first-order object against all remaining objects.
    second_order: list[dict] = []
    # Track (norad_id, via_norad_id) pairs already added to avoid duplicates.
    second_order_seen: set[tuple[int, int]] = set()

    for fo_entry in first_order:
        fo_norad_id: int = fo_entry["norad_id"]
        fo_traj = first_order_trajs[fo_norad_id]

        for other_norad_id, other_traj in all_other_trajs.items():
            # Skip the anomalous object itself and the first-order object.
            if other_norad_id == anomalous_norad_id:
                continue
            if other_norad_id == fo_norad_id:
                continue

            pair = (other_norad_id, fo_norad_id)
            if pair in second_order_seen:
                continue

            min_dist_km, tca_utc = compute_min_distance_km(fo_traj, other_traj)

            if min_dist_km <= SECOND_ORDER_THRESHOLD_KM:
                name = catalog_name_map.get(other_norad_id, str(other_norad_id))
                second_order.append(
                    {
                        "norad_id": other_norad_id,
                        "name": name,
                        "min_distance_km": float(min_dist_km),
                        "via_norad_id": fo_norad_id,
                        "time_of_closest_approach_utc": _format_epoch(tca_utc),
                    }
                )
                second_order_seen.add(pair)
                logger.info(
                    "screen_conjunctions: second-order risk NORAD %d via %d, min dist=%.3f km, TCA=%s",
                    other_norad_id,
                    fo_norad_id,
                    min_dist_km,
                    tca_utc.isoformat(),
                )

    # Step 5: Build and return result dict.
    result: dict = {
        "type": "conjunction_risk",
        "anomalous_norad_id": anomalous_norad_id,
        "screening_epoch_utc": _format_epoch(screening_epoch_utc),
        "horizon_s": SCREENING_HORIZON_S,
        "threshold_km": FIRST_ORDER_THRESHOLD_KM,
        "first_order": first_order,
        "second_order": second_order,
    }

    logger.info(
        "screen_conjunctions: complete for NORAD %d — %d first-order, %d second-order risks",
        anomalous_norad_id,
        len(first_order),
        len(second_order),
    )
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _format_epoch(epoch_utc: datetime.datetime) -> str:
    """Format a UTC-aware datetime to ISO-8601 string with Z suffix.

    Args:
        epoch_utc: UTC-aware datetime.

    Returns:
        ISO-8601 string ending with 'Z', e.g. '2026-03-29T12:00:00Z'.
    """
    return epoch_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
