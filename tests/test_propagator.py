"""Tests for backend/propagator.py."""
import datetime
import numpy as np
import pytest


def test_propagate_tle_returns_correct_shape() -> None:
    """propagate_tle returns two 3-element arrays."""
    pytest.skip("not implemented")


def test_propagate_tle_rejects_malformed_tle() -> None:
    """propagate_tle raises ValueError on bad TLE."""
    pytest.skip("not implemented")


def test_tle_to_state_vector_returns_6_elements() -> None:
    """tle_to_state_vector_eci_km returns a 6-element array."""
    pytest.skip("not implemented")


def test_tle_epoch_utc_is_utc_aware() -> None:
    """tle_epoch_utc returns a timezone-aware UTC datetime."""
    pytest.skip("not implemented")


def test_eci_to_geodetic_returns_lat_lon_alt() -> None:
    """eci_to_geodetic returns (lat_rad, lon_rad, alt_km)."""
    pytest.skip("not implemented")


def test_propagation_output_is_eci_j2000() -> None:
    """Verify output frame is ECI J2000 by comparing with known values."""
    pytest.skip("not implemented")
