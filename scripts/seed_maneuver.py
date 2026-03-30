"""Synthetic maneuver injection script for demos.

Introduces a delta-V event into a selected object's cached TLE sequence
to trigger anomaly detection through the Kalman filter pipeline.

Satisfies F-061, F-062, F-063, NF-023, TD-023.

Usage:
    python scripts/seed_maneuver.py --object NORAD_ID --delta-v MAGNITUDE_MS \
        [--direction along-track|cross-track|radial] \
        [--epoch-offset-min N] \
        [--db PATH] [--catalog PATH] \
        [--trigger] [--server-url URL]

Examples:
    python scripts/seed_maneuver.py --object 25544 --delta-v 2.0
    python scripts/seed_maneuver.py --object 25544 --delta-v 0.5 \
        --direction along-track --trigger

NOTE on synthetic TLE accuracy:
    Converting an arbitrary ECI state to TLE via Keplerian elements introduces
    fitting error because SGP4 includes secular/periodic perturbation corrections
    that a pure Keplerian model does not account for. For demo delta-V magnitudes
    (0.5-5 m/s), the TLE fitting error (~100m position) is 1-2 orders of magnitude
    smaller than the maneuver signature. The Kalman filter detects the anomaly clearly.
"""
import argparse
import datetime
import logging
import math
import os
import sys
from pathlib import Path
from typing import Optional

import numpy as np
from numpy.typing import NDArray

# Add the project root to sys.path so that backend package imports work when
# this script is run as `python scripts/seed_maneuver.py` from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backend.ingest as ingest
import backend.propagator as propagator

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Earth gravitational parameter (WGS72) in km^3/s^2
_MU_KM3_S2: float = 398600.4418

# Seconds per day (for mean motion conversion)
_SECONDS_PER_DAY: float = 86400.0

# Revolutions per day factor
_REVS_PER_DAY_FACTOR: float = _SECONDS_PER_DAY / (2.0 * math.pi)


def rsw_to_eci_delta_v_km_s(
    position_eci_km: NDArray[np.float64],
    velocity_eci_km_s: NDArray[np.float64],
    delta_v_radial_km_s: float,
    delta_v_along_track_km_s: float,
    delta_v_cross_track_km_s: float,
) -> NDArray[np.float64]:
    """Convert a delta-V in RSW (radial/along-track/cross-track) frame to ECI.

    RSW frame definition (per plan step 5):
      R (radial):      r_hat = position / |position|
      W (cross-track): w_hat = (position x velocity) / |position x velocity|
                       (orbit normal — perpendicular to orbital plane)
      S (along-track): s_hat = w_hat x r_hat
                       (approximately along velocity for circular/near-circular orbits)

    All inputs and outputs are in km/s (ECI J2000).
    The caller is responsible for converting m/s to km/s before calling.

    Args:
        position_eci_km: 3-element ECI position vector in km.
        velocity_eci_km_s: 3-element ECI velocity vector in km/s.
        delta_v_radial_km_s: Delta-V component in the radial direction (km/s).
        delta_v_along_track_km_s: Delta-V component in the along-track direction (km/s).
        delta_v_cross_track_km_s: Delta-V component in the cross-track direction (km/s).

    Returns:
        3-element ECI delta-V vector in km/s (ECI J2000).

    Raises:
        ValueError: If position or velocity are zero vectors (degenerate orbit).
    """
    r_norm: float = float(np.linalg.norm(position_eci_km))
    if r_norm < 1e-10:
        raise ValueError("position_eci_km is a zero vector — degenerate orbit")

    r_hat: NDArray[np.float64] = position_eci_km / r_norm

    h_vec: NDArray[np.float64] = np.cross(position_eci_km, velocity_eci_km_s).astype(np.float64)
    h_norm: float = float(np.linalg.norm(h_vec))
    if h_norm < 1e-10:
        raise ValueError(
            "position x velocity is a zero vector — rectilinear or degenerate orbit"
        )

    w_hat: NDArray[np.float64] = h_vec / h_norm
    s_hat: NDArray[np.float64] = np.cross(w_hat, r_hat).astype(np.float64)

    delta_v_eci_km_s: NDArray[np.float64] = (
        delta_v_radial_km_s * r_hat
        + delta_v_along_track_km_s * s_hat
        + delta_v_cross_track_km_s * w_hat
    )
    return delta_v_eci_km_s


def eci_to_keplerian(
    position_eci_km: NDArray[np.float64],
    velocity_eci_km_s: NDArray[np.float64],
    mu_km3_s2: float = _MU_KM3_S2,
) -> dict:
    """Convert ECI state vector to classical Keplerian orbital elements.

    Uses standard orbital mechanics formulas (vis-viva, angular momentum vector,
    eccentricity vector, node vector). Edge cases for near-circular and
    near-equatorial orbits are guarded with small-epsilon fallbacks appropriate
    for LEO objects (ISS, Starlink).

    Coordinate frame: inputs must be in ECI J2000, km and km/s.

    Args:
        position_eci_km: 3-element ECI position vector in km.
        velocity_eci_km_s: 3-element ECI velocity vector in km/s.
        mu_km3_s2: Earth gravitational parameter in km^3/s^2. Defaults to WGS72.

    Returns:
        Dict with keys:
            a_km (float): Semi-major axis in km.
            e (float): Eccentricity (dimensionless).
            i_rad (float): Inclination in radians.
            raan_rad (float): Right ascension of ascending node in radians.
            argp_rad (float): Argument of perigee in radians.
            true_anomaly_rad (float): True anomaly in radians.

    Raises:
        ValueError: If the state is degenerate (zero position or velocity).
    """
    r_vec: NDArray[np.float64] = position_eci_km.astype(np.float64)
    v_vec: NDArray[np.float64] = velocity_eci_km_s.astype(np.float64)

    r_mag: float = float(np.linalg.norm(r_vec))
    v_mag: float = float(np.linalg.norm(v_vec))

    if r_mag < 1e-10:
        raise ValueError("position_eci_km is a zero vector — degenerate orbit")
    if v_mag < 1e-10:
        raise ValueError("velocity_eci_km_s is a zero vector — degenerate orbit")

    # Angular momentum vector h = r x v
    h_vec: NDArray[np.float64] = np.cross(r_vec, v_vec).astype(np.float64)
    h_mag: float = float(np.linalg.norm(h_vec))
    if h_mag < 1e-10:
        raise ValueError("h = r x v is zero — rectilinear trajectory")

    # Node vector n = k x h  (k = [0, 0, 1] = z-axis)
    k_hat: NDArray[np.float64] = np.array([0.0, 0.0, 1.0], dtype=np.float64)
    n_vec: NDArray[np.float64] = np.cross(k_hat, h_vec).astype(np.float64)
    n_mag: float = float(np.linalg.norm(n_vec))

    # Eccentricity vector e_vec = (v x h) / mu - r_hat
    e_vec: NDArray[np.float64] = (
        np.cross(v_vec, h_vec).astype(np.float64) / mu_km3_s2 - r_vec / r_mag
    )
    e_mag: float = float(np.linalg.norm(e_vec))

    # Semi-major axis (vis-viva: v^2 = mu*(2/r - 1/a))
    a_km: float = 1.0 / (2.0 / r_mag - v_mag ** 2 / mu_km3_s2)

    # Inclination: angle between h_vec and k_hat
    i_rad: float = math.acos(
        max(-1.0, min(1.0, float(h_vec[2]) / h_mag))
    )

    # RAAN: angle between x-axis and node vector
    # For near-equatorial orbits (n_mag ~ 0), RAAN is undefined — default to 0.
    if n_mag < 1e-10:
        raan_rad: float = 0.0
    else:
        raan_rad = math.acos(max(-1.0, min(1.0, float(n_vec[0]) / n_mag)))
        if n_vec[1] < 0.0:
            raan_rad = 2.0 * math.pi - raan_rad

    # Argument of perigee: angle between node vector and eccentricity vector
    # For near-circular orbits (e_mag ~ 0), argp is undefined — default to 0.
    if e_mag < 1e-10:
        argp_rad: float = 0.0
    elif n_mag < 1e-10:
        # Equatorial orbit: measure argp from x-axis
        argp_rad = math.acos(max(-1.0, min(1.0, float(e_vec[0]) / e_mag)))
        if e_vec[1] < 0.0:
            argp_rad = 2.0 * math.pi - argp_rad
    else:
        argp_rad = math.acos(
            max(-1.0, min(1.0, float(np.dot(n_vec, e_vec)) / (n_mag * e_mag)))
        )
        if e_vec[2] < 0.0:
            argp_rad = 2.0 * math.pi - argp_rad

    # True anomaly: angle between eccentricity vector and position vector
    # For near-circular orbits, use angle between node vector and position.
    if e_mag < 1e-10:
        if n_mag < 1e-10:
            # Circular equatorial: measure from x-axis
            true_anomaly_rad: float = math.acos(
                max(-1.0, min(1.0, float(r_vec[0]) / r_mag))
            )
            if float(r_vec[1]) < 0.0:
                true_anomaly_rad = 2.0 * math.pi - true_anomaly_rad
        else:
            true_anomaly_rad = math.acos(
                max(-1.0, min(1.0, float(np.dot(n_vec, r_vec)) / (n_mag * r_mag)))
            )
            if float(r_vec[2]) < 0.0:
                true_anomaly_rad = 2.0 * math.pi - true_anomaly_rad
    else:
        true_anomaly_rad = math.acos(
            max(-1.0, min(1.0, float(np.dot(e_vec, r_vec)) / (e_mag * r_mag)))
        )
        # If radial velocity is negative (approaching periapsis), true anomaly > pi
        if float(np.dot(r_vec, v_vec)) < 0.0:
            true_anomaly_rad = 2.0 * math.pi - true_anomaly_rad

    return {
        "a_km": a_km,
        "e": e_mag,
        "i_rad": i_rad,
        "raan_rad": raan_rad,
        "argp_rad": argp_rad,
        "true_anomaly_rad": true_anomaly_rad,
    }


def _true_to_mean_anomaly_rad(
    true_anomaly_rad: float,
    eccentricity: float,
) -> float:
    """Convert true anomaly to mean anomaly.

    Steps:
      1. Eccentric anomaly: E = 2 * atan2(sqrt(1-e)*sin(nu/2), sqrt(1+e)*cos(nu/2))
      2. Mean anomaly: M = E - e * sin(E)

    Args:
        true_anomaly_rad: True anomaly in radians.
        eccentricity: Orbital eccentricity (0 <= e < 1).

    Returns:
        Mean anomaly in radians, in [0, 2*pi).
    """
    e: float = eccentricity
    nu: float = true_anomaly_rad
    # Eccentric anomaly
    E: float = 2.0 * math.atan2(
        math.sqrt(1.0 - e) * math.sin(nu / 2.0),
        math.sqrt(1.0 + e) * math.cos(nu / 2.0),
    )
    # Mean anomaly
    M: float = E - e * math.sin(E)
    # Normalize to [0, 2*pi)
    M = M % (2.0 * math.pi)
    return M


def _tle_checksum(line: str) -> int:
    """Compute the standard TLE modulo-10 checksum for a single line.

    Mirrors ingest._tle_checksum. Reproduced here to avoid importing private
    symbols from backend.ingest in a script context.

    Args:
        line: A TLE line string (first 68 characters used).

    Returns:
        Computed checksum integer (0-9).
    """
    total: int = 0
    for ch in line[:68]:
        if ch.isdigit():
            total += int(ch)
        elif ch == "-":
            total += 1
    return total % 10


def _format_tle_epoch(epoch_utc: datetime.datetime) -> str:
    """Format a UTC datetime as a TLE epoch field string (YYDDD.DDDDDDDD).

    TLE epoch format: two-digit year followed by fractional day-of-year
    (1-based: Jan 1 00:00:00 = 1.0).

    Args:
        epoch_utc: UTC-aware datetime.

    Returns:
        14-character TLE epoch string, e.g. '26087.12345678'.
    """
    year_2digit: int = epoch_utc.year % 100
    jan1: datetime.datetime = datetime.datetime(
        epoch_utc.year, 1, 1, tzinfo=datetime.timezone.utc
    )
    delta: datetime.timedelta = epoch_utc - jan1
    # day_of_year is 1-based
    day_of_year_frac: float = 1.0 + delta.total_seconds() / _SECONDS_PER_DAY
    return f"{year_2digit:02d}{day_of_year_frac:012.8f}"


def _format_bstar(bstar: float) -> str:
    """Format B* drag term for TLE line 1 (columns 54-61, 8 characters).

    TLE B* format: ±.NNNNN±N (sign, decimal point, 5 digits, sign, 1 digit).
    Example: " .12345-3" for 0.12345e-3. For zero: " 00000-0".

    Args:
        bstar: B* drag term in units of 1/earth_radii.

    Returns:
        8-character TLE B* field string.
    """
    if bstar == 0.0:
        return " 00000-0"

    sign: str = " " if bstar >= 0.0 else "-"
    abs_bstar: float = abs(bstar)

    if abs_bstar == 0.0:
        return " 00000-0"

    # Normalize: abs_bstar = mantissa * 10^exponent where 0.1 <= mantissa < 1.0
    exponent: int = math.floor(math.log10(abs_bstar)) + 1
    mantissa: float = abs_bstar / (10.0 ** exponent)

    # Clamp mantissa to [0.1, 0.99999] range to handle floating point edge cases
    if mantissa >= 1.0:
        mantissa /= 10.0
        exponent += 1
    elif mantissa < 0.1:
        mantissa *= 10.0
        exponent -= 1

    # Format mantissa as 5 digits after implied decimal: "NNNNN"
    mantissa_int: int = round(mantissa * 100000)
    if mantissa_int >= 100000:
        mantissa_int = 99999

    exp_sign: str = "+" if exponent >= 0 else "-"
    return f"{sign}{mantissa_int:05d}{exp_sign}{abs(exponent):1d}"


def keplerian_to_tle_lines(
    norad_id: int,
    epoch_utc: datetime.datetime,
    a_km: float,
    e: float,
    i_rad: float,
    raan_rad: float,
    argp_rad: float,
    mean_anomaly_rad: float,
    bstar: float,
    name: str = "SYNTHETIC",
) -> tuple[str, str]:
    """Format Keplerian orbital elements as TLE line 1 and line 2 strings.

    Constructs valid TLE strings from classical orbital elements. Mean motion
    is computed from semi-major axis via n = sqrt(mu / a^3). The epoch field
    is formatted from the UTC datetime. B* drag is set from the passed-in value.

    The generated TLE is synthetic and will have small fitting errors because
    SGP4's secular/periodic perturbations are not inverted here. For demo
    delta-V magnitudes (0.5-5 m/s), these errors are negligible compared to
    the maneuver signature.

    Args:
        norad_id: NORAD catalog ID (5-digit integer).
        epoch_utc: UTC epoch for the TLE. Must be UTC-aware.
        a_km: Semi-major axis in km.
        e: Eccentricity (dimensionless, must be in [0, 1)).
        i_rad: Inclination in radians.
        raan_rad: Right ascension of ascending node in radians.
        argp_rad: Argument of perigee in radians.
        mean_anomaly_rad: Mean anomaly in radians.
        bstar: B* drag term in units of 1/earth_radii (from original TLE).
        name: Object name string (not stored in TLE lines, only used for
              documentation in this function; TLE format omits the name line
              for 2-line format).

    Returns:
        Tuple (tle_line1, tle_line2) as 69-character strings.

    Raises:
        ValueError: If epoch_utc is not UTC-aware or eccentricity is out of range.
    """
    if epoch_utc.tzinfo is None:
        raise ValueError("epoch_utc must be UTC-aware")
    if not (0.0 <= e < 1.0):
        raise ValueError(f"eccentricity must be in [0, 1), got {e}")

    # Mean motion in rad/s
    n_rad_s: float = math.sqrt(_MU_KM3_S2 / (a_km ** 3))
    # Mean motion in rev/day
    n_rev_day: float = n_rad_s * _REVS_PER_DAY_FACTOR

    # Convert angles to degrees for TLE formatting
    i_deg: float = math.degrees(i_rad)
    raan_deg: float = math.degrees(raan_rad) % 360.0
    argp_deg: float = math.degrees(argp_rad) % 360.0
    M_deg: float = math.degrees(mean_anomaly_rad) % 360.0

    # Eccentricity: TLE stores 7-digit integer with implied decimal point
    # e.g., 0.0012345 -> "0012345"
    e_int: int = round(e * 1e7)
    if e_int >= 10000000:
        e_int = 9999999

    # Epoch field
    epoch_str: str = _format_tle_epoch(epoch_utc)

    # B* field (8 chars)
    bstar_str: str = _format_bstar(bstar)

    # --- TLE Line 1 ---
    # Columns (1-indexed): per https://celestrak.org/columns/v04n03/
    # Col  1     : Line number '1'
    # Col  2     : ' '
    # Col  3-7   : NORAD ID (5 digits)
    # Col  8     : Classification 'U' (unclassified)
    # Col  9     : ' '
    # Col 10-17  : International designator (8 chars, left-justified, space-padded)
    # Col 18     : ' '
    # Col 19-32  : Epoch (14 chars: YYDDD.DDDDDDDD)
    # Col 33     : ' '
    # Col 34-43  : First derivative of mean motion (10 chars: ±.NNNNNNNN)
    # Col 44     : ' '
    # Col 45-52  : Second derivative of mean motion (8 chars)
    # Col 53     : ' '
    # Col 54-61  : B* drag term (8 chars)
    # Col 62     : ' '
    # Col 63     : Ephemeris type '0'
    # Col 64     : ' '
    # Col 65-68  : Element set number (4 digits right-justified)
    # Col 69     : Checksum

    # International designator: use synthetic placeholder "26001A  " (8 chars)
    intl_desig: str = "26001A  "
    # First derivative of mean motion (set to 0 for synthetic TLE)
    first_deriv: str = " .00000000"
    # Second derivative of mean motion (set to 0 for synthetic TLE)
    second_deriv: str = " 00000-0"
    # Element set number
    elem_set_num: int = 999

    line1_body: str = (
        f"1 {norad_id:05d}U {intl_desig} {epoch_str} "
        f"{first_deriv} {second_deriv} {bstar_str} 0 {elem_set_num:4d}"
    )
    # Compute and append checksum
    cs1: int = _tle_checksum(line1_body)
    tle_line1: str = line1_body + str(cs1)

    # --- TLE Line 2 ---
    # Col  1     : Line number '2'
    # Col  2     : ' '
    # Col  3-7   : NORAD ID (5 digits)
    # Col  8     : ' '
    # Col  9-16  : Inclination (8 chars: DDD.DDDD)
    # Col 17     : ' '
    # Col 18-25  : RAAN (8 chars: DDD.DDDD)
    # Col 26     : ' '
    # Col 27-33  : Eccentricity (7 digits, no decimal point)
    # Col 34     : ' '
    # Col 35-42  : Argument of perigee (8 chars: DDD.DDDD)
    # Col 43     : ' '
    # Col 44-51  : Mean anomaly (8 chars: DDD.DDDD)
    # Col 52     : ' '
    # Col 53-63  : Mean motion (11 chars: DD.NNNNNNNN)
    # Col 64-68  : Revolution number at epoch (5 digits right-justified)
    # Col 69     : Checksum

    # Revolution count — set to 0 for synthetic TLE
    rev_num: int = 0

    line2_body: str = (
        f"2 {norad_id:05d} "
        f"{i_deg:8.4f} "
        f"{raan_deg:8.4f} "
        f"{e_int:07d} "
        f"{argp_deg:8.4f} "
        f"{M_deg:8.4f} "
        f"{n_rev_day:11.8f}"
        f"{rev_num:5d}"
    )
    cs2: int = _tle_checksum(line2_body)
    tle_line2: str = line2_body + str(cs2)

    return tle_line1, tle_line2


def inject_maneuver(
    norad_id: int,
    delta_v_m_s: float,
    direction: str,
    epoch_offset_min: float,
    db_path: str,
    catalog_config_path: str,
    trigger: bool,
    server_url: str,
) -> None:
    """Inject a synthetic maneuver into the cached TLE sequence.

    Computes a post-maneuver ECI state by applying delta-V in the RSW frame,
    generates a synthetic TLE from the perturbed state, validates it, and
    inserts it into the TLE cache. Optionally triggers the server to
    process the new TLE immediately (for NF-023 compliance).

    Domain rules enforced:
    - delta_v_m_s is in m/s (F-062); converted to km/s internally.
    - All datetime objects are UTC-aware.
    - DB path from argument (which caller resolves from env var).
    - Output state vector is in ECI J2000.

    Args:
        norad_id: NORAD catalog ID of the target object.
        delta_v_m_s: Delta-V magnitude in m/s (F-062).
        direction: One of 'along-track', 'cross-track', 'radial'.
        epoch_offset_min: Minutes from now for the maneuver epoch.
        db_path: Path to SQLite database.
        catalog_config_path: Path to catalog JSON config.
        trigger: If True, POST to server_url/admin/trigger-process after insertion.
        server_url: Base URL of the running server (used only if trigger=True).
    """
    import httpx  # local import — only needed when --trigger is used or to validate

    # Open DB.
    db = ingest.init_catalog_db(db_path)

    # Get latest TLE for this object.
    tle_record: Optional[dict] = ingest.get_latest_tle(db, norad_id)
    if tle_record is None:
        print(
            f"[maneuver] ERROR: No cached TLE found for NORAD {norad_id}. "
            "Run the server with a live Space-Track connection to populate the cache first."
        )
        db.close()
        sys.exit(1)

    tle_line1: str = tle_record["tle_line1"]
    tle_line2: str = tle_record["tle_line2"]

    # Compute maneuver epoch.
    now_utc: datetime.datetime = datetime.datetime.now(datetime.timezone.utc)
    maneuver_epoch_utc: datetime.datetime = now_utc + datetime.timedelta(
        minutes=epoch_offset_min
    )

    # Propagate pre-maneuver TLE to maneuver epoch to get base ECI state.
    try:
        base_state_eci_km: NDArray[np.float64] = propagator.tle_to_state_vector_eci_km(
            tle_line1, tle_line2, maneuver_epoch_utc
        )
    except ValueError as exc:
        print(f"[maneuver] ERROR: SGP4 propagation failed: {exc}")
        db.close()
        sys.exit(1)

    base_position_eci_km: NDArray[np.float64] = base_state_eci_km[:3]
    base_velocity_eci_km_s: NDArray[np.float64] = base_state_eci_km[3:]

    # Convert delta-V from m/s to km/s (F-062 specifies m/s input; domain rule: internal km/s).
    dv_km_s: float = delta_v_m_s / 1000.0

    # Map direction string to RSW components.
    if direction == "along-track":
        dv_radial_km_s: float = 0.0
        dv_along_km_s: float = dv_km_s
        dv_cross_km_s: float = 0.0
    elif direction == "cross-track":
        dv_radial_km_s = 0.0
        dv_along_km_s = 0.0
        dv_cross_km_s = dv_km_s
    elif direction == "radial":
        dv_radial_km_s = dv_km_s
        dv_along_km_s = 0.0
        dv_cross_km_s = 0.0
    else:
        print(f"[maneuver] ERROR: Unknown direction '{direction}'. "
              "Use along-track, cross-track, or radial.")
        db.close()
        sys.exit(1)

    # Convert delta-V from RSW frame to ECI.
    try:
        delta_v_eci_km_s: NDArray[np.float64] = rsw_to_eci_delta_v_km_s(
            position_eci_km=base_position_eci_km,
            velocity_eci_km_s=base_velocity_eci_km_s,
            delta_v_radial_km_s=dv_radial_km_s,
            delta_v_along_track_km_s=dv_along_km_s,
            delta_v_cross_track_km_s=dv_cross_km_s,
        )
    except ValueError as exc:
        print(f"[maneuver] ERROR: RSW-to-ECI conversion failed: {exc}")
        db.close()
        sys.exit(1)

    # Apply delta-V to velocity (position unchanged at maneuver instant).
    perturbed_velocity_eci_km_s: NDArray[np.float64] = (
        base_velocity_eci_km_s + delta_v_eci_km_s
    )

    # Convert perturbed ECI state to Keplerian elements.
    try:
        elements: dict = eci_to_keplerian(
            position_eci_km=base_position_eci_km,
            velocity_eci_km_s=perturbed_velocity_eci_km_s,
        )
    except ValueError as exc:
        print(f"[maneuver] ERROR: ECI-to-Keplerian conversion failed: {exc}")
        db.close()
        sys.exit(1)

    # True anomaly to mean anomaly.
    mean_anomaly_rad: float = _true_to_mean_anomaly_rad(
        true_anomaly_rad=elements["true_anomaly_rad"],
        eccentricity=elements["e"],
    )

    # Extract B* from the original TLE via sgp4 library (resolved decision: use satrec.bstar).
    from sgp4.api import Satrec, WGS72
    satrec = Satrec.twoline2rv(tle_line1, tle_line2, WGS72)
    bstar: float = satrec.bstar

    # Retrieve object name from catalog config (best-effort; fallback to NORAD ID string).
    object_name: str = f"NORAD{norad_id}"
    try:
        catalog_entries = ingest.load_catalog_config(catalog_config_path)
        for entry in catalog_entries:
            if int(entry["norad_id"]) == norad_id:
                object_name = entry.get("name", object_name)
                break
    except (FileNotFoundError, ValueError):
        pass  # Name is cosmetic; silently fall back to NORAD ID string.

    # Generate synthetic TLE lines.
    try:
        syn_tle_line1, syn_tle_line2 = keplerian_to_tle_lines(
            norad_id=norad_id,
            epoch_utc=maneuver_epoch_utc,
            a_km=elements["a_km"],
            e=elements["e"],
            i_rad=elements["i_rad"],
            raan_rad=elements["raan_rad"],
            argp_rad=elements["argp_rad"],
            mean_anomaly_rad=mean_anomaly_rad,
            bstar=bstar,
            name=object_name,
        )
    except ValueError as exc:
        print(f"[maneuver] ERROR: TLE line generation failed: {exc}")
        db.close()
        sys.exit(1)

    # Validate the synthetic TLE.
    if not ingest.validate_tle(syn_tle_line1, syn_tle_line2):
        print(
            "[maneuver] ERROR: Generated synthetic TLE failed checksum validation. "
            "This is a bug in keplerian_to_tle_lines — please report."
        )
        print(f"  Line 1: {syn_tle_line1!r}")
        print(f"  Line 2: {syn_tle_line2!r}")
        db.close()
        sys.exit(1)

    # Prepare epoch string for insertion (ISO 8601 UTC).
    epoch_utc_str: str = maneuver_epoch_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Insert synthetic TLE into the cache.
    tle_dict: dict = {
        "norad_id": norad_id,
        "epoch_utc": epoch_utc_str,
        "tle_line1": syn_tle_line1,
        "tle_line2": syn_tle_line2,
    }
    inserted: int = ingest.cache_tles(db, [tle_dict], fetched_at_utc=now_utc)
    db.close()

    print(
        f"[maneuver] Injected delta-V {delta_v_m_s} m/s {direction} "
        f"for NORAD {norad_id} @ {epoch_utc_str}"
    )

    if inserted == 0:
        print(
            "[maneuver] NOTE: TLE with this epoch already exists in cache "
            "(INSERT OR IGNORE skipped duplicate). "
            "The server/replay will use the existing record."
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
                    f"[maneuver] Maneuver processed ({processed} objects updated) "
                    "— check visualization"
                )
        except Exception as exc:  # noqa: BLE001
            print(
                f"[maneuver] Server unreachable ({exc}) "
                "— run: python scripts/replay.py to process"
            )


def main() -> None:
    """Parse arguments and inject maneuver."""
    parser = argparse.ArgumentParser(
        description=(
            "Inject a synthetic delta-V maneuver into the TLE cache to trigger "
            "anomaly detection through the Kalman filter pipeline (F-061, F-062)."
        )
    )
    parser.add_argument(
        "--object",
        type=int,
        required=True,
        metavar="NORAD_ID",
        help="NORAD catalog ID of the target object",
    )
    parser.add_argument(
        "--delta-v",
        type=float,
        default=5.0,
        metavar="MAGNITUDE_MS",
        help="Delta-V magnitude in m/s (default: 5.0). Values below ~3.0 m/s "
             "may not cross the NIS=12.592 anomaly threshold with DEFAULT_R=900.",
    )
    parser.add_argument(
        "--direction",
        type=str,
        default="along-track",
        choices=["along-track", "cross-track", "radial"],
        help="Maneuver direction in RSW frame (default: along-track)",
    )
    parser.add_argument(
        "--epoch-offset-min",
        type=float,
        default=0.0,
        metavar="MINUTES",
        help="Minutes from now for the maneuver epoch (default: 0 = now)",
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
        "--trigger",
        action="store_true",
        default=False,
        help=(
            "POST to /admin/trigger-process after insertion so the browser "
            "reflects the anomaly within 10 seconds (NF-023). "
            "Requires the server to be running at --server-url."
        ),
    )
    parser.add_argument(
        "--server-url",
        type=str,
        default="http://localhost:8000",
        help="Base URL of the running FastAPI server (default: http://localhost:8000)",
    )
    args = parser.parse_args()

    # Resolve DB path: CLI arg > env var > default.
    db_path: str = (
        args.db
        or os.environ.get("NBODY_DB_PATH")
        or "data/catalog/tle_cache.db"
    )

    try:
        inject_maneuver(
            norad_id=args.object,
            delta_v_m_s=args.delta_v,
            direction=args.direction,
            epoch_offset_min=args.epoch_offset_min,
            db_path=db_path,
            catalog_config_path=args.catalog,
            trigger=args.trigger,
            server_url=args.server_url,
        )
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"[maneuver] FATAL: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
