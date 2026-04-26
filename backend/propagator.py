"""Stateless SGP4 propagation engine. All outputs in ECI J2000, units km and km/s.

This module converts TLE elements into ECI J2000 state vectors at arbitrary epochs.
The propagator holds no internal state between calls and is trivially replaceable with
a higher-fidelity numerical integrator at this interface boundary.

Dependency note: astropy>=6.0 is required for TEME-to-J2000 frame conversion.
The sgp4 library outputs state vectors in TEME (True Equator Mean Equinox). Requirement
F-011 mandates ECI J2000. Manual implementation of the IAU precession/nutation model
would be error-prone and hard to validate; astropy provides a battle-tested, correct
implementation. See docs/plans/2026-03-28-propagator.md Phase 1 for full justification.
"""

import datetime

import astropy.units as u
import numpy as np

# astropy imports for TEME-to-J2000 frame conversion (see module docstring).
from astropy.coordinates import GCRS, TEME, CartesianDifferential, CartesianRepresentation
from astropy.time import Time
from numpy.typing import NDArray
from sgp4.api import WGS72, Satrec, jday

# SGP4 error code descriptions, keyed by integer error code returned by satrec.sgp4().
_SGP4_ERRORS: dict[int, str] = {
    1: "mean eccentricity not in range 0 <= e < 1",
    2: "mean motion less than 0",
    3: "pert eccentricity not in range 0 <= e < 1",
    4: "semi-latus rectum less than 0",
    5: "epoch too far from TLE epoch (satellite may have decayed)",
    6: "satellite has decayed",
}

# Warn (not raise) when propagation interval exceeds this many days. SGP4 accuracy
# degrades significantly beyond a few days; callers should be aware.
_PROPAGATION_WARN_DAYS: float = 7.0


def _teme_to_eci_j2000(
    position_teme_km: NDArray[np.float64],
    velocity_teme_km_s: NDArray[np.float64],
    epoch_utc: datetime.datetime,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Rotate TEME position and velocity into ECI J2000 (GCRS) frame.

    The sgp4 library outputs state vectors in TEME (True Equator Mean Equinox). This
    function applies the IAU precession/nutation rotation via astropy to produce ECI
    J2000-equivalent vectors.

    # POC: GCRS used as J2000 equivalent; sub-meter error for LEO acceptable

    GCRS (Geocentric Celestial Reference System) and FK5-based J2000 differ by up to
    ~20 milliarcseconds, translating to sub-meter position differences for LEO. This
    approximation is acceptable for POC filter accuracy. Exact J2000 (FK5) would require
    an additional small constant frame-tie rotation matrix (post-POC).

    Args:
        position_teme_km: 3-element position vector in TEME frame, km.
        velocity_teme_km_s: 3-element velocity vector in TEME frame, km/s.
        epoch_utc: UTC epoch of the state vector. Must be UTC-aware.

    Returns:
        Tuple of (position_eci_km, velocity_eci_km_s) as numpy arrays in GCRS/J2000.
    """
    obstime = Time(epoch_utc, scale="utc")

    teme_position = CartesianRepresentation(
        position_teme_km[0] * u.km,
        position_teme_km[1] * u.km,
        position_teme_km[2] * u.km,
    )
    teme_velocity = CartesianDifferential(
        velocity_teme_km_s[0] * u.km / u.s,
        velocity_teme_km_s[1] * u.km / u.s,
        velocity_teme_km_s[2] * u.km / u.s,
    )

    teme_coord = TEME(
        teme_position.with_differentials(teme_velocity),
        obstime=obstime,
    )

    gcrs_coord = teme_coord.transform_to(GCRS(obstime=obstime))

    gcrs_cartesian = gcrs_coord.cartesian
    position_eci_km = np.array(
        [
            gcrs_cartesian.x.to(u.km).value,
            gcrs_cartesian.y.to(u.km).value,
            gcrs_cartesian.z.to(u.km).value,
        ],
        dtype=np.float64,
    )

    gcrs_velocity = gcrs_cartesian.differentials["s"]
    velocity_eci_km_s = np.array(
        [
            gcrs_velocity.d_x.to(u.km / u.s).value,
            gcrs_velocity.d_y.to(u.km / u.s).value,
            gcrs_velocity.d_z.to(u.km / u.s).value,
        ],
        dtype=np.float64,
    )

    return position_eci_km, velocity_eci_km_s


def propagate_tle(
    tle_line1: str,
    tle_line2: str,
    epoch_utc: datetime.datetime,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Propagate a TLE to the given epoch using SGP4.

    Parses the TLE with the WGS72 gravity model (which is what TLE generation uses),
    propagates to the target epoch, and rotates the TEME output into ECI J2000 via
    astropy's TEME -> GCRS transform.

    Args:
        tle_line1: First line of the TLE set.
        tle_line2: Second line of the TLE set.
        epoch_utc: Target UTC epoch for propagation. Must be UTC-aware.

    Returns:
        Tuple of (position_eci_km, velocity_eci_km_s) where each is a
        3-element numpy array. Position in km, velocity in km/s,
        both in ECI J2000 frame.

    Raises:
        ValueError: If epoch_utc is not UTC-aware.
        ValueError: If TLE is malformed or SGP4 initialisation fails.
        ValueError: If SGP4 propagation fails (e.g., satellite decayed, epoch too far).
    """
    if epoch_utc.tzinfo is None or epoch_utc.utcoffset() is None:
        raise ValueError(
            "epoch_utc must be UTC-aware (tzinfo set to datetime.timezone.utc). Received a naive datetime."
        )

    # WGS72 is used deliberately: TLE elements are produced with WGS72 constants.
    # Using WGS84 here would introduce a small but avoidable systematic error.
    satrec = Satrec.twoline2rv(tle_line1, tle_line2, WGS72)

    if satrec.error != 0:
        error_msg = _SGP4_ERRORS.get(satrec.error, f"SGP4 error code {satrec.error}")
        raise ValueError(f"TLE parsing / initialisation failed: {error_msg}")

    # Convert epoch to Julian date pair for sgp4 API.
    jd_whole, jd_frac = jday(
        epoch_utc.year,
        epoch_utc.month,
        epoch_utc.day,
        epoch_utc.hour,
        epoch_utc.minute,
        epoch_utc.second + epoch_utc.microsecond * 1e-6,
    )

    # Warn (not raise) when propagation interval is large; SGP4 is unreliable far from
    # its TLE epoch. The TLE epoch is stored as a Julian date in satrec.jdsatepoch.
    tle_jd = satrec.jdsatepoch + satrec.jdsatepochF
    propagation_days = abs((jd_whole + jd_frac) - tle_jd)
    if propagation_days > _PROPAGATION_WARN_DAYS:
        import warnings

        warnings.warn(
            f"Propagation interval {propagation_days:.1f} days exceeds "
            f"{_PROPAGATION_WARN_DAYS} days from TLE epoch. SGP4 accuracy degrades "
            "significantly at this range.",
            stacklevel=2,
        )

    error_code, position_teme_km_raw, velocity_teme_km_s_raw = satrec.sgp4(jd_whole, jd_frac)

    if error_code != 0:
        error_msg = _SGP4_ERRORS.get(error_code, f"SGP4 error code {error_code}")
        raise ValueError(f"SGP4 propagation failed: {error_msg}")

    position_teme_km = np.array(position_teme_km_raw, dtype=np.float64)
    velocity_teme_km_s = np.array(velocity_teme_km_s_raw, dtype=np.float64)

    position_eci_km, velocity_eci_km_s = _teme_to_eci_j2000(position_teme_km, velocity_teme_km_s, epoch_utc)

    return position_eci_km, velocity_eci_km_s


def tle_to_state_vector_eci_km(
    tle_line1: str,
    tle_line2: str,
    epoch_utc: datetime.datetime,
) -> NDArray[np.float64]:
    """Convert a TLE to a full 6-element state vector at the given epoch.

    Convenience wrapper around propagate_tle that satisfies the F-012 interface
    expected by the Kalman filter engine.

    Args:
        tle_line1: First line of the TLE set.
        tle_line2: Second line of the TLE set.
        epoch_utc: Target UTC epoch. Must be UTC-aware.

    Returns:
        6-element numpy array [x_km, y_km, z_km, vx_km_s, vy_km_s, vz_km_s]
        in ECI J2000 frame.

    Raises:
        ValueError: If epoch_utc is not UTC-aware.
        ValueError: If TLE is malformed or SGP4 propagation fails.
    """
    position_eci_km, velocity_eci_km_s = propagate_tle(tle_line1, tle_line2, epoch_utc)
    return np.concatenate([position_eci_km, velocity_eci_km_s])


def tle_epoch_utc(tle_line1: str) -> datetime.datetime:
    """Extract the epoch from TLE line 1 as a UTC datetime.

    Parses the epoch fields from TLE line 1 directly without calling SGP4, following
    the standard TLE epoch convention: columns 19-20 are a 2-digit year, columns 21-32
    are fractional day of year (1-based).

    2-digit year convention (standard for TLEs):
      - 00-56 maps to 2000-2056
      - 57-99 maps to 1957-1999

    Args:
        tle_line1: First line of the TLE set (68 characters, standard TLE format).

    Returns:
        UTC-aware datetime representing the TLE epoch.

    Raises:
        ValueError: If the epoch field cannot be parsed.
    """
    # TLE line 1 epoch: columns 19-32 (0-indexed 18-31).
    # Columns 19-20 (0-indexed 18-19): 2-digit year.
    # Columns 21-32 (0-indexed 20-31): fractional day of year.
    try:
        epoch_field = tle_line1[18:32].strip()
        year_2digit = int(epoch_field[:2])
        day_of_year_frac = float(epoch_field[2:])
    except (ValueError, IndexError) as exc:
        raise ValueError(f"Cannot parse TLE epoch from line 1 field '{tle_line1[18:32]!r}': {exc}") from exc

    # Apply TLE 2-digit year convention.
    if year_2digit <= 56:
        year_4digit = 2000 + year_2digit
    else:
        year_4digit = 1900 + year_2digit

    # day_of_year_frac is 1-based: 1.0 = Jan 1 00:00:00.
    # datetime ordinal for Jan 0 of the year + integer day gives the correct date.
    day_int = int(day_of_year_frac)  # integer day of year (1-based)
    frac_day = day_of_year_frac - day_int  # fractional remainder of that day

    # Build the base date: Jan 1 of year + (day_int - 1) days.
    base_date = datetime.datetime(year_4digit, 1, 1, tzinfo=datetime.timezone.utc)
    base_date += datetime.timedelta(days=day_int - 1)

    # Convert fractional day to hours, minutes, seconds.
    total_seconds = frac_day * 86400.0
    hours = int(total_seconds // 3600)
    remaining_s = total_seconds - hours * 3600
    minutes = int(remaining_s // 60)
    seconds_frac = remaining_s - minutes * 60
    seconds = int(seconds_frac)
    microseconds = round((seconds_frac - seconds) * 1e6)

    epoch = base_date.replace(
        hour=hours,
        minute=minutes,
        second=seconds,
        microsecond=microseconds,
    )

    return epoch


def eci_to_geodetic(
    position_eci_km: NDArray[np.float64],
    epoch_utc: datetime.datetime,
) -> tuple[float, float, float]:
    """Convert ECI position to geodetic coordinates (for API boundary only).

    NOTE: This function exists solely for the API response layer.
    Internal computations must remain in ECI J2000.

    Uses astropy to convert from GCRS (ECI J2000) to ITRS (Earth-fixed), then extracts
    geodetic latitude, longitude, and altitude.

    Args:
        position_eci_km: 3-element ECI position vector in km.
        epoch_utc: UTC epoch (needed for Earth rotation angle). Must be UTC-aware.

    Returns:
        Tuple of (latitude_rad, longitude_rad, altitude_km).

    Raises:
        ValueError: If epoch_utc is not UTC-aware.
    """
    if epoch_utc.tzinfo is None or epoch_utc.utcoffset() is None:
        raise ValueError(
            "epoch_utc must be UTC-aware (tzinfo set to datetime.timezone.utc). Received a naive datetime."
        )

    obstime = Time(epoch_utc, scale="utc")

    gcrs_position = CartesianRepresentation(
        position_eci_km[0] * u.km,
        position_eci_km[1] * u.km,
        position_eci_km[2] * u.km,
    )
    gcrs_coord = GCRS(gcrs_position, obstime=obstime)

    # Transform to ITRS (Earth-fixed) for geodetic extraction.
    from astropy.coordinates import ITRS

    itrs_coord = gcrs_coord.transform_to(ITRS(obstime=obstime))

    earth_location = itrs_coord.earth_location
    geodetic = earth_location.geodetic

    latitude_rad = geodetic.lat.to(u.rad).value
    longitude_rad = geodetic.lon.to(u.rad).value
    altitude_km = geodetic.height.to(u.km).value

    return latitude_rad, longitude_rad, altitude_km
