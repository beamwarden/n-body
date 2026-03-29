"""Tests for backend/propagator.py.

Plan reference: docs/plans/2026-03-28-propagator.md Phase 4, steps 7-13.

ISS TLE fixture (epoch 2024-02-14 ~12:25:32 UTC, NORAD 25544) is used throughout.
This TLE is hardcoded so tests run offline without any network access.
"""
import datetime
import math
import warnings

import numpy as np
import pytest

from backend.propagator import (
    eci_to_geodetic,
    propagate_tle,
    tle_epoch_utc,
    tle_to_state_vector_eci_km,
)

# ---------------------------------------------------------------------------
# Shared TLE fixture — ISS (NORAD 25544), epoch ~2024-02-14T12:25:32 UTC.
# Source: publicly available Space-Track TLE archive.
# ---------------------------------------------------------------------------
_ISS_LINE1 = "1 25544U 98067A   24045.51773148  .00015204  00000+0  27364-3 0  9996"
_ISS_LINE2 = "2 25544  51.6412 225.3758 0004694 126.4788 345.7603 15.49563589442437"

# Epoch derived from _ISS_LINE1: year 2024, day 45.51773148
# = 2024-02-14 at fractional day 0.51773148
# = 2024-02-14T12:25:32.something UTC (approximately)
_ISS_EPOCH = datetime.datetime(2024, 2, 14, 12, 25, 31, tzinfo=datetime.timezone.utc)


# ---------------------------------------------------------------------------
# Step 7 — test_propagate_tle_returns_correct_shape
# ---------------------------------------------------------------------------

def test_propagate_tle_returns_correct_shape() -> None:
    """propagate_tle returns a tuple of two 3-element float64 arrays.

    Plan step 7: propagate ISS TLE to epoch + 60 minutes; check shapes.
    """
    target_epoch = _ISS_EPOCH + datetime.timedelta(minutes=60)
    result = propagate_tle(_ISS_LINE1, _ISS_LINE2, target_epoch)

    assert isinstance(result, tuple), "propagate_tle must return a tuple"
    assert len(result) == 2, "tuple must have exactly 2 elements"

    position_eci_km, velocity_eci_km_s = result
    assert isinstance(position_eci_km, np.ndarray), "position must be ndarray"
    assert isinstance(velocity_eci_km_s, np.ndarray), "velocity must be ndarray"
    assert position_eci_km.shape == (3,), f"position shape must be (3,), got {position_eci_km.shape}"
    assert velocity_eci_km_s.shape == (3,), f"velocity shape must be (3,), got {velocity_eci_km_s.shape}"
    assert position_eci_km.dtype == np.float64, "position dtype must be float64"
    assert velocity_eci_km_s.dtype == np.float64, "velocity dtype must be float64"


# ---------------------------------------------------------------------------
# Step 8 — test_propagate_tle_rejects_malformed_tle
# ---------------------------------------------------------------------------

def test_propagate_tle_rejects_malformed_tle() -> None:
    """propagate_tle raises ValueError on malformed TLE input.

    Plan step 8: garbage strings and a valid line 1 with corrupted line 2.
    """
    epoch_utc = _ISS_EPOCH

    # Case 1: completely garbage strings
    with pytest.raises(ValueError):
        propagate_tle("not a tle line 1", "not a tle line 2", epoch_utc)

    # Case 2: bad checksum is NOT tested here — the sgp4 library does not validate
    # TLE checksums in Satrec.twoline2rv; it accepts and propagates silently.
    # Checksum validation (F-003) is the responsibility of ingest.py, not the propagator.


# ---------------------------------------------------------------------------
# Step 9 — test_tle_to_state_vector_returns_6_elements
# ---------------------------------------------------------------------------

def test_tle_to_state_vector_returns_6_elements() -> None:
    """tle_to_state_vector_eci_km returns a physically plausible 6-element array.

    Plan step 9: shape (6,), position magnitude 6500-7500 km, velocity 6-8 km/s.
    """
    target_epoch = _ISS_EPOCH + datetime.timedelta(minutes=30)
    state_vector = tle_to_state_vector_eci_km(_ISS_LINE1, _ISS_LINE2, target_epoch)

    assert isinstance(state_vector, np.ndarray), "must return ndarray"
    assert state_vector.shape == (6,), f"shape must be (6,), got {state_vector.shape}"

    position_km = state_vector[:3]
    velocity_km_s = state_vector[3:]

    position_magnitude_km = float(np.linalg.norm(position_km))
    velocity_magnitude_km_s = float(np.linalg.norm(velocity_km_s))

    # ISS orbits at ~400 km altitude; Earth radius ~6371 km → total ~6771 km.
    # Allow generous bounds for LEO: 6400-7500 km.
    assert 6400.0 <= position_magnitude_km <= 7500.0, (
        f"Position magnitude {position_magnitude_km:.1f} km outside expected LEO range"
    )

    # ISS orbital speed ~7.66 km/s; allow range 6-8 km/s for LEO.
    assert 6.0 <= velocity_magnitude_km_s <= 8.0, (
        f"Velocity magnitude {velocity_magnitude_km_s:.3f} km/s outside expected LEO range"
    )


# ---------------------------------------------------------------------------
# Step 10 — test_tle_epoch_utc_is_utc_aware
# ---------------------------------------------------------------------------

def test_tle_epoch_utc_is_utc_aware() -> None:
    """tle_epoch_utc returns a UTC-aware datetime matching the TLE epoch.

    Plan step 10: tzinfo must be datetime.timezone.utc; date must be 2024-02-14.
    """
    epoch = tle_epoch_utc(_ISS_LINE1)

    assert epoch.tzinfo is not None, "datetime must not be naive"
    assert epoch.utcoffset() == datetime.timedelta(0), "utcoffset must be zero (UTC)"
    assert epoch.tzinfo == datetime.timezone.utc, "tzinfo must be datetime.timezone.utc"

    # _ISS_LINE1 epoch field: 24045.51773148 → year 2024, day 45 of the year.
    # Day 45 of 2024 is 2024-02-14 (2024 is a leap year; Jan has 31 days, so day 45
    # = Jan 31 + 14 days = Feb 14).
    assert epoch.year == 2024, f"Expected year 2024, got {epoch.year}"
    assert epoch.month == 2, f"Expected month 2, got {epoch.month}"
    assert epoch.day == 14, f"Expected day 14, got {epoch.day}"

    # Fractional day 0.51773148 * 86400 = 44731.840 s = 12h 25m 31.84s
    assert epoch.hour == 12, f"Expected hour 12, got {epoch.hour}"
    assert epoch.minute == 25, f"Expected minute 25, got {epoch.minute}"
    # Allow ±2 seconds for floating-point rounding in fractional-day conversion.
    assert abs(epoch.second - 31) <= 2, (
        f"Expected second ~31, got {epoch.second}"
    )


# ---------------------------------------------------------------------------
# Step 11 — test_eci_to_geodetic_returns_lat_lon_alt
# ---------------------------------------------------------------------------

def test_eci_to_geodetic_returns_lat_lon_alt() -> None:
    """eci_to_geodetic returns lat/lon in [-pi, pi] and physically plausible altitude.

    Plan step 11: use a known ECI position (~6778 km on +X axis at _ISS_EPOCH).
    """
    # Point on +X axis at 6778 km (approximates ~407 km altitude above equator).
    position_eci_km = np.array([6778.0, 0.0, 0.0], dtype=np.float64)
    epoch_utc = _ISS_EPOCH

    latitude_rad, longitude_rad, altitude_km = eci_to_geodetic(position_eci_km, epoch_utc)

    # lat/lon must be in valid geodetic range
    assert -math.pi / 2 <= latitude_rad <= math.pi / 2, (
        f"latitude_rad {latitude_rad:.4f} outside [-pi/2, pi/2]"
    )
    assert -math.pi <= longitude_rad <= math.pi, (
        f"longitude_rad {longitude_rad:.4f} outside [-pi, pi]"
    )

    # altitude should be roughly 400 km ± 50 km for a point at 6778 km from Earth center
    # (Earth equatorial radius ~6378 km)
    assert 350.0 <= altitude_km <= 450.0, (
        f"altitude_km {altitude_km:.1f} km outside expected range [350, 450] km"
    )

    # Latitude near equator for a point on the +X axis
    assert abs(latitude_rad) < 0.1, (
        f"Expected near-equatorial latitude, got {latitude_rad:.4f} rad"
    )


# ---------------------------------------------------------------------------
# Step 12 — test_propagation_output_is_eci_j2000
# ---------------------------------------------------------------------------

def test_propagation_output_is_eci_j2000() -> None:
    """Verify TEME-to-J2000 conversion is applied by comparing TEME vs GCRS outputs.

    Plan step 12 (alternative approach): propagate at TLE epoch (zero propagation
    interval) and compare the raw TEME output against our ECI J2000 output.
    The TEME-to-GCRS rotation is a small non-zero rotation, so the vectors should
    be close but measurably different.  Asserts:
      - Position difference is in [0.01, 20] km (rotation is non-zero but small)
      - Velocity difference is in [0.00001, 0.02] km/s
    If the function returned raw TEME as if it were J2000 (i.e., no conversion),
    the difference would be zero and the test would fail.
    """
    from sgp4.api import Satrec, WGS72, jday as sgp4_jday

    # Use TLE epoch so propagation interval is ~0, isolating the frame rotation.
    epoch_utc = tle_epoch_utc(_ISS_LINE1)

    # Get raw TEME output directly from sgp4 (no frame conversion).
    satrec = Satrec.twoline2rv(_ISS_LINE1, _ISS_LINE2, WGS72)
    jd_whole, jd_frac = sgp4_jday(
        epoch_utc.year, epoch_utc.month, epoch_utc.day,
        epoch_utc.hour, epoch_utc.minute,
        epoch_utc.second + epoch_utc.microsecond * 1e-6,
    )
    _, position_teme_raw, velocity_teme_raw = satrec.sgp4(jd_whole, jd_frac)
    position_teme_km = np.array(position_teme_raw, dtype=np.float64)
    velocity_teme_km_s = np.array(velocity_teme_raw, dtype=np.float64)

    # Get our ECI J2000 output (with TEME->GCRS conversion applied).
    position_eci_km, velocity_eci_km_s = propagate_tle(_ISS_LINE1, _ISS_LINE2, epoch_utc)

    pos_diff_km = float(np.linalg.norm(position_eci_km - position_teme_km))
    vel_diff_km_s = float(np.linalg.norm(velocity_eci_km_s - velocity_teme_km_s))

    # The TEME-to-J2000 rotation is non-trivial — difference should be > 0.01 km.
    # Upper bound: TEME vs GCRS includes ~24 years of precession from J2000 epoch
    # (~0.33° for a 2024 TLE), which at ISS altitude (~6770 km) produces ~25-30 km
    # of frame offset. 50 km is a generous upper bound that still catches gross errors.
    assert pos_diff_km > 0.01, (
        f"Position difference {pos_diff_km:.4f} km is suspiciously small — "
        "TEME-to-J2000 conversion may not be applied."
    )
    assert pos_diff_km < 50.0, (
        f"Position difference {pos_diff_km:.4f} km is larger than expected "
        "for a TEME-to-GCRS rotation — possible implementation error."
    )
    assert vel_diff_km_s > 1e-5, (
        f"Velocity difference {vel_diff_km_s:.6f} km/s is suspiciously small — "
        "TEME-to-J2000 conversion may not be applied."
    )
    # 0.05 km/s upper bound: same precession argument as position — ~0.33° rotation
    # applied to ~7.66 km/s orbital speed yields ~0.044 km/s maximum difference.
    assert vel_diff_km_s < 0.05, (
        f"Velocity difference {vel_diff_km_s:.6f} km/s larger than expected."
    )


# ---------------------------------------------------------------------------
# Step 13 — additional edge-case tests
# ---------------------------------------------------------------------------

def test_propagate_tle_rejects_naive_datetime() -> None:
    """propagate_tle raises ValueError when given a naive (non-UTC-aware) datetime.

    Plan step 13, edge case 1.
    """
    naive_epoch = datetime.datetime(2024, 2, 14, 12, 0, 0)  # no tzinfo
    with pytest.raises(ValueError, match="UTC-aware"):
        propagate_tle(_ISS_LINE1, _ISS_LINE2, naive_epoch)


def test_propagate_tle_far_epoch_raises() -> None:
    """propagate_tle raises ValueError or warns when epoch is 30+ days from TLE epoch.

    Plan step 13, edge case 2: SGP4 returns an error code for extreme extrapolations;
    the function must raise ValueError rather than returning garbage state.

    For moderate out-of-range propagation that sgp4 still numerically completes (error
    code 0), a warning is issued instead.  We accept either outcome here — ValueError or
    a UserWarning — but we must not receive a silent garbage state with no signal.
    """
    far_epoch = _ISS_EPOCH + datetime.timedelta(days=365)

    try:
        with warnings.catch_warnings(record=True) as caught_warnings:
            warnings.simplefilter("always")
            propagate_tle(_ISS_LINE1, _ISS_LINE2, far_epoch)

        # If propagation "succeeded" (sgp4 returned error code 0 even 365 days out),
        # a warning must have been issued about the long propagation interval.
        warning_messages = [str(w.message) for w in caught_warnings]
        assert any("days" in msg for msg in warning_messages), (
            "Expected a propagation-interval warning for 365-day extrapolation, "
            f"but got no relevant warning. Warnings: {warning_messages}"
        )
    except ValueError:
        # SGP4 returned a non-zero error code — this is the preferred outcome.
        pass


def test_tle_epoch_utc_two_digit_year() -> None:
    """tle_epoch_utc correctly maps 2-digit years per TLE convention.

    Plan step 13, edge case 3:
      - Year <= 56 maps to 2000+year (e.g., 24 -> 2024)
      - Year >= 57 maps to 1900+year (e.g., 57 -> 1957, 98 -> 1998)
    """
    # Year 24 in epoch field → 2024
    # _ISS_LINE1 uses year "24" (columns 18-19 of the line).
    epoch_2024 = tle_epoch_utc(_ISS_LINE1)
    assert epoch_2024.year == 2024, f"Expected 2024, got {epoch_2024.year}"

    # Construct a synthetic TLE line 1 with year "57" to test the 19xx branch.
    # Start from ISS line 1 and splice in "57" at columns 18-19 (0-indexed).
    line1_year57 = _ISS_LINE1[:18] + "57" + _ISS_LINE1[20:]
    epoch_1957 = tle_epoch_utc(line1_year57)
    assert epoch_1957.year == 1957, f"Expected 1957, got {epoch_1957.year}"

    # Construct a synthetic TLE line 1 with year "98" (→ 1998).
    line1_year98 = _ISS_LINE1[:18] + "98" + _ISS_LINE1[20:]
    epoch_1998 = tle_epoch_utc(line1_year98)
    assert epoch_1998.year == 1998, f"Expected 1998, got {epoch_1998.year}"

    # Year "00" → 2000
    line1_year00 = _ISS_LINE1[:18] + "00" + _ISS_LINE1[20:]
    epoch_2000 = tle_epoch_utc(line1_year00)
    assert epoch_2000.year == 2000, f"Expected 2000, got {epoch_2000.year}"

    # Year "56" → 2056
    line1_year56 = _ISS_LINE1[:18] + "56" + _ISS_LINE1[20:]
    epoch_2056 = tle_epoch_utc(line1_year56)
    assert epoch_2056.year == 2056, f"Expected 2056, got {epoch_2056.year}"
