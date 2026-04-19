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

import numpy as np
from numpy.typing import NDArray

# Chi-squared critical value for 6 DOF at p=0.05 (per F-030)
CHI2_THRESHOLD_6DOF: float = 12.592

# Object class identifiers for Q matrix selection (F-024)
OBJECT_CLASS_DEBRIS: str = "debris"
OBJECT_CLASS_ACTIVE: str = "active_satellite"
OBJECT_CLASS_ROCKET_BODY: str = "rocket_body"

# Process noise matrices Q per object class (F-024).
# Units: km^2 for position blocks, (km/s)^2 for velocity blocks.
# Hand-tuned for POC; see POST-002 for adaptive noise estimation.
# Position variance: diagonal scaled by assumed unmodeled acceleration over
# a 30-minute update interval. Velocity variance: smaller, reflecting that
# unmodeled forces accumulate primarily in velocity over short intervals.
#
# Debris: higher drag uncertainty, no maneuver capability.
#   sigma_pos ~ 1 km, sigma_vel ~ 0.01 km/s
# Active satellite: moderate uncertainty + maneuver probability.
#   sigma_pos ~ 0.5 km, sigma_vel ~ 0.05 km/s
# Rocket body: intermediate drag uncertainty, inert.
#   sigma_pos ~ 0.75 km, sigma_vel ~ 0.02 km/s
_Q_DEBRIS: NDArray[np.float64] = np.diag(
    [1.0, 1.0, 1.0, 1e-4, 1e-4, 1e-4]
).astype(np.float64)

_Q_ACTIVE_SATELLITE: NDArray[np.float64] = np.diag(
    [0.25, 0.25, 0.25, 25e-4, 25e-4, 25e-4]
).astype(np.float64)

_Q_ROCKET_BODY: NDArray[np.float64] = np.diag(
    [0.5625, 0.5625, 0.5625, 4e-4, 4e-4, 4e-4]
).astype(np.float64)

OBJECT_CLASS_Q: dict[str, NDArray[np.float64]] = {
    OBJECT_CLASS_DEBRIS: _Q_DEBRIS,
    OBJECT_CLASS_ACTIVE: _Q_ACTIVE_SATELLITE,
    OBJECT_CLASS_ROCKET_BODY: _Q_ROCKET_BODY,
}

# Default measurement noise matrix R (F-025).
# Calibrated against measured ISS TLE-to-TLE prediction error: ~30 km position
# (1-sigma), ~0.045 km/s velocity. Variance = sigma^2: 30^2 = 900 km^2 position,
# 0.045^2 ≈ 0.002 (km/s)^2 velocity.  Using tighter values (e.g. 1.0 km^2) causes
# NIS >> threshold on every normal update, driving perpetual spurious recalibration.
# These are diagonal; off-diagonal correlation is neglected for POC.
DEFAULT_R: NDArray[np.float64] = np.diag(
    [900.0, 900.0, 900.0, 2e-3, 2e-3, 2e-3]
).astype(np.float64)


def _make_default_covariance_p0(
    inflation_factor: float = 1.0,
) -> NDArray[np.float64]:
    """Construct a default initial covariance matrix P0.

    Uses diagonal entries corresponding to ~10 km position uncertainty
    and ~0.1 km/s velocity uncertainty (1-sigma), inflatable by a scalar
    factor for recalibration.

    Args:
        inflation_factor: Multiply all diagonal entries by this factor.

    Returns:
        6x6 diagonal covariance matrix in km^2 and (km/s)^2.
    """
    p0_diag = np.array([100.0, 100.0, 100.0, 0.01, 0.01, 0.01],
                       dtype=np.float64)
    return np.diag(p0_diag * inflation_factor)


def _identity_hx(state_eci_km: NDArray[np.float64]) -> NDArray[np.float64]:
    """Measurement function: observation is the full state vector (identity).

    For POC, TLE-derived state vectors are direct observations of the full
    ECI state. No partial observability.

    Args:
        state_eci_km: 6-element state vector.

    Returns:
        The state vector unchanged.
    """
    return state_eci_km


def init_filter(
    state_eci_km: NDArray[np.float64],
    epoch_utc: datetime.datetime,
    process_noise_q: NDArray[np.float64] | None = None,
    measurement_noise_r: NDArray[np.float64] | None = None,
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
    from filterpy.kalman import MerweScaledSigmaPoints, UnscentedKalmanFilter

    if epoch_utc.tzinfo is None:
        raise ValueError("epoch_utc must be UTC-aware")

    if state_eci_km.shape != (6,):
        raise ValueError(
            f"state_eci_km must have shape (6,), got {state_eci_km.shape}"
        )

    q_matrix: NDArray[np.float64] = (
        process_noise_q if process_noise_q is not None
        else OBJECT_CLASS_Q[OBJECT_CLASS_ACTIVE]
    )
    r_matrix: NDArray[np.float64] = (
        measurement_noise_r if measurement_noise_r is not None
        else DEFAULT_R
    )

    points = MerweScaledSigmaPoints(n=6, alpha=1e-3, beta=2.0, kappa=0.0)

    # fx=None is accepted by FilterPy 1.4.5 at construction; it is overridden
    # per predict call via dynamic assignment on the ukf object.
    ukf = UnscentedKalmanFilter(
        dim_x=6,
        dim_z=6,
        dt=1.0,           # nominal dt in seconds; overridden in each predict call
        hx=_identity_hx,  # measurement function (identity: full-state observation)
        fx=None,          # placeholder; set per predict call via closure
        points=points,
    )
    ukf.x = state_eci_km.copy().astype(np.float64)
    ukf.P = _make_default_covariance_p0()
    ukf.Q = q_matrix.copy()
    ukf.R = r_matrix.copy()

    return {
        "filter": ukf,
        "last_epoch_utc": epoch_utc,
        "q_matrix": q_matrix.copy(),
        "r_matrix": r_matrix.copy(),
        "nis": 0.0,
        "nis_history": [],        # list[float], appended on each update
        "innovation_eci_km": np.zeros(6, dtype=np.float64),
        "anomaly_flag": False,
        "confidence": 1.0,
        "state_eci_km": state_eci_km.copy().astype(np.float64),
        "covariance_km2": ukf.P.copy(),
    }


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
    from . import propagator as _propagator

    if target_epoch_utc.tzinfo is None:
        raise ValueError("target_epoch_utc must be UTC-aware")

    if "filter" not in filter_state:
        raise KeyError("filter_state missing required key: 'filter'")
    if "last_epoch_utc" not in filter_state:
        raise KeyError("filter_state missing required key: 'last_epoch_utc'")

    dt_s: float = (
        target_epoch_utc - filter_state["last_epoch_utc"]
    ).total_seconds()

    if dt_s <= 0:
        raise ValueError("target_epoch_utc must be after last_epoch_utc")

    # POST-002: SGP4 is used as the UKF process model. Because SGP4 is a
    # deterministic trajectory model parameterised by TLE elements — not a
    # force-model ODE — all 13 UKF sigma points map to the same SGP4-propagated
    # state. Covariance growth is therefore dominated by Q (the process noise
    # matrix), not by sigma-point divergence through dynamics. This is a known
    # POC simplification. Post-POC, replace with a numerical integrator
    # (POST-003) so sigma points diverge through a proper force model.
    def _fx_sgp4(x: NDArray[np.float64], dt: float) -> NDArray[np.float64]:
        """Process model: propagate sigma point via SGP4 to target_epoch_utc."""
        # x contains [x_km, y_km, z_km, vx_km_s, vy_km_s, vz_km_s] from
        # the UKF sigma-point distribution. For SGP4 as the process model,
        # we propagate using the TLE (not the sigma-point state), because SGP4
        # is a trajectory model, not a force model — see design note above.
        return _propagator.tle_to_state_vector_eci_km(
            tle_line1, tle_line2, target_epoch_utc
        )

    filter_state["filter"].fx = _fx_sgp4
    filter_state["filter"].predict(dt=dt_s)

    filter_state["state_eci_km"] = filter_state["filter"].x.copy()
    filter_state["covariance_km2"] = filter_state["filter"].P.copy()

    return filter_state["filter"].x.copy()


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
    if epoch_utc.tzinfo is None:
        raise ValueError("epoch_utc must be UTC-aware")

    if observation_eci_km.shape != (6,):
        raise ValueError(
            f"observation_eci_km must have shape (6,), got {observation_eci_km.shape}"
        )

    ukf = filter_state["filter"]
    ukf.update(observation_eci_km.astype(np.float64))

    innovation_eci_km: NDArray[np.float64] = ukf.y.copy()
    innovation_covariance_km2: NDArray[np.float64] = ukf.S.copy()

    nis_val: float = compute_nis(innovation_eci_km, innovation_covariance_km2)

    filter_state["innovation_eci_km"] = innovation_eci_km
    filter_state["nis"] = nis_val
    filter_state["last_epoch_utc"] = epoch_utc
    filter_state["state_eci_km"] = ukf.x.copy()
    filter_state["covariance_km2"] = ukf.P.copy()
    filter_state["anomaly_flag"] = nis_val > CHI2_THRESHOLD_6DOF

    filter_state["nis_history"].append(nis_val)
    if len(filter_state["nis_history"]) > 20:
        filter_state["nis_history"] = filter_state["nis_history"][-20:]

    filter_state["confidence"] = compute_confidence(
        nis_val, filter_state["nis_history"]
    )

    return filter_state


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
    if np.linalg.matrix_rank(innovation_covariance_km2) < 6:
        # DEVIATION note location: if this branch is ever reached in testing,
        # flag it. S should be positive definite if Q and R are positive definite.
        s_inv = np.linalg.pinv(innovation_covariance_km2)
    else:
        s_inv = np.linalg.inv(innovation_covariance_km2)
    nis_val: float = float(innovation_eci_km @ s_inv @ innovation_eci_km)
    return nis_val


def get_state(filter_state: dict) -> dict:
    """Extract the current state estimate and metadata from the filter.

    Args:
        filter_state: Filter dict.

    Returns:
        Dict with keys: 'state_eci_km' (6-element array),
        'covariance_km2' (6x6 matrix), 'last_epoch_utc' (datetime),
        'confidence' (float 0-1).
    """
    return {
        "state_eci_km": filter_state["state_eci_km"].copy(),
        "covariance_km2": filter_state["covariance_km2"].copy(),
        "last_epoch_utc": filter_state["last_epoch_utc"],
        "confidence": filter_state["confidence"],
        "nis": filter_state["nis"],
        "anomaly_flag": filter_state["anomaly_flag"],
        "innovation_eci_km": filter_state["innovation_eci_km"].copy(),
    }


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
    if epoch_utc.tzinfo is None:
        raise ValueError("epoch_utc must be UTC-aware")

    q_matrix: NDArray[np.float64] = filter_state["q_matrix"]
    r_matrix: NDArray[np.float64] = filter_state["r_matrix"]

    new_state = init_filter(
        state_eci_km=new_observation_eci_km,
        epoch_utc=epoch_utc,
        process_noise_q=q_matrix,
        measurement_noise_r=r_matrix,
    )
    # Override the default P0 with inflated covariance
    new_state["filter"].P = _make_default_covariance_p0(inflation_factor)
    new_state["covariance_km2"] = new_state["filter"].P.copy()

    # Preserve NIS history so anomaly.py can see the full history including
    # the divergence event for classification.
    new_state["nis_history"] = filter_state["nis_history"].copy()

    return new_state


def compute_confidence(nis: float, nis_history: list[float]) -> float:
    """Compute a 0-1 confidence score from NIS value and recent history.

    Args:
        nis: Current NIS value.
        nis_history: List of recent NIS values (last N updates).

    Returns:
        Float between 0.0 (no confidence) and 1.0 (full confidence).
    """
    # Confidence is based on the fraction of recent NIS values below threshold.
    # Current NIS is weighted 2x relative to history entries.
    # Returns 1.0 if all recent values are well below threshold,
    # 0.0 if current NIS far exceeds threshold.
    if not nis_history:
        # No history yet: use only current NIS
        history_score: float = 1.0
    else:
        below = sum(1.0 for v in nis_history if v <= CHI2_THRESHOLD_6DOF)
        history_score = below / len(nis_history)

    # Current NIS score: map NIS to [0, 1] with threshold as the midpoint.
    # NIS=0 -> 1.0, NIS=threshold -> 0.5, NIS=2*threshold -> ~0.0
    current_score: float = max(0.0, 1.0 - (nis / (2.0 * CHI2_THRESHOLD_6DOF)))

    # Weighted blend: current NIS has 2x weight vs. history average
    confidence: float = (2.0 * current_score + history_score) / 3.0
    return float(np.clip(confidence, 0.0, 1.0))
