"""UKF state estimation engine. One filter instance per tracked object.
State vector: [x, y, z, vx, vy, vz] in ECI J2000 km and km/s.

Implements the Unscented Kalman Filter (UKF) using FilterPy. The UKF is chosen
over the EKF because the SGP4 measurement model is nonlinear and EKF linearization
introduces systematic error that accumulates over time.

The ingest.py -> kalman.py boundary: TLE-derived state vectors are treated as
noisy observations. This is a simulation fidelity boundary — real sensor data is
not used in this POC. Reviewers should note that measurement noise R reflects
TLE accuracy class rather than actual sensor noise.
"""
import datetime
from typing import Optional

import numpy as np
from numpy.typing import NDArray

# Chi-squared critical value for 6 DOF at p=0.05 (per F-030)
CHI2_THRESHOLD_6DOF: float = 12.592


def init_filter(
    state_eci_km: NDArray[np.float64],
    epoch_utc: datetime.datetime,
    process_noise_q: Optional[NDArray[np.float64]] = None,
    measurement_noise_r: Optional[NDArray[np.float64]] = None,
) -> dict:
    """Initialize a UKF instance for a single tracked object.

    Args:
        state_eci_km: 6-element initial state [x,y,z,vx,vy,vz] in ECI km and km/s.
        epoch_utc: UTC epoch of the initial state. Must be UTC-aware.
        process_noise_q: 6x6 process noise covariance matrix. If None, use default.
        measurement_noise_r: 6x6 measurement noise covariance matrix. If None, use default.

    Returns:
        Dict containing the filter object, last epoch, and metadata.
        Keys: 'filter', 'last_epoch_utc', 'norad_id', 'covariance_km2'.
    """
    raise NotImplementedError("not implemented")


def predict(
    filter_state: dict,
    target_epoch_utc: datetime.datetime,
    tle_line1: str,
    tle_line2: str,
) -> NDArray[np.float64]:
    """Run the UKF predict step: propagate state to target epoch.

    Uses SGP4 via propagator.py as the process model.

    Args:
        filter_state: Filter dict from init_filter or previous update.
        target_epoch_utc: UTC epoch to propagate to. Must be UTC-aware.
        tle_line1: Current TLE line 1 (for SGP4 propagation).
        tle_line2: Current TLE line 2 (for SGP4 propagation).

    Returns:
        6-element predicted state vector in ECI km and km/s.
    """
    raise NotImplementedError("not implemented")


def update(
    filter_state: dict,
    observation_eci_km: NDArray[np.float64],
    epoch_utc: datetime.datetime,
) -> dict:
    """Run the UKF update step: incorporate a new observation.

    Args:
        filter_state: Filter dict after predict step.
        observation_eci_km: 6-element observed state [x,y,z,vx,vy,vz] in ECI km and km/s.
        epoch_utc: UTC epoch of the observation. Must be UTC-aware.

    Returns:
        Updated filter state dict with new keys:
        'innovation_eci_km': residual vector,
        'nis': Normalized Innovation Squared scalar,
        'confidence': float 0-1.
    """
    raise NotImplementedError("not implemented")


def compute_nis(
    innovation_eci_km: NDArray[np.float64],
    innovation_covariance_km2: NDArray[np.float64],
) -> float:
    """Compute the Normalized Innovation Squared (NIS) statistic.

    NIS = y^T * S^{-1} * y where y is the innovation and S is
    the innovation covariance.

    Args:
        innovation_eci_km: 6-element innovation (residual) vector.
        innovation_covariance_km2: 6x6 innovation covariance matrix.

    Returns:
        NIS scalar value.
    """
    raise NotImplementedError("not implemented")


def get_state(filter_state: dict) -> dict:
    """Extract the current state estimate and metadata from the filter.

    Args:
        filter_state: Filter dict.

    Returns:
        Dict with keys: 'state_eci_km' (6-element array),
        'covariance_km2' (6x6 matrix), 'last_epoch_utc' (datetime),
        'confidence' (float 0-1).
    """
    raise NotImplementedError("not implemented")


def recalibrate(
    filter_state: dict,
    new_observation_eci_km: NDArray[np.float64],
    epoch_utc: datetime.datetime,
    inflation_factor: float = 10.0,
) -> dict:
    """Re-initialize the filter from a new observation with inflated covariance.

    Called when anomaly detection determines the filter has diverged.

    Args:
        filter_state: Current (diverged) filter dict.
        new_observation_eci_km: 6-element state from new TLE.
        epoch_utc: UTC epoch of the new observation. Must be UTC-aware.
        inflation_factor: Multiply default covariance by this factor.

    Returns:
        Fresh filter state dict with inflated initial uncertainty.
    """
    raise NotImplementedError("not implemented")


def compute_confidence(nis: float, nis_history: list[float]) -> float:
    """Compute a 0-1 confidence score from NIS value and recent history.

    Args:
        nis: Current NIS value.
        nis_history: List of recent NIS values (last N updates).

    Returns:
        Float between 0.0 (no confidence) and 1.0 (full confidence).
    """
    raise NotImplementedError("not implemented")
