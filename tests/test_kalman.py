"""Tests for backend/kalman.py."""
import datetime
import numpy as np
import pytest
from unittest.mock import patch

from backend import kalman
from backend.kalman import (
    CHI2_THRESHOLD_6DOF,
    OBJECT_CLASS_Q,
    OBJECT_CLASS_DEBRIS,
    OBJECT_CLASS_ACTIVE,
)

# Realistic ISS-like initial state: LEO, units km and km/s
ISS_STATE = np.array([6728.0, 0.0, 0.0, 0.0, 7.67, 0.0], dtype=np.float64)

# Fixed UTC epoch for tests
T0 = datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
T1 = datetime.datetime(2026, 1, 1, 0, 30, 0, tzinfo=datetime.timezone.utc)

# ISS TLE used for tests that exercise predict() path
ISS_TLE_LINE1 = "1 25544U 98067A   24045.51773148  .00015204  00000+0  27364-3 0  9996"
ISS_TLE_LINE2 = "2 25544  51.6412 225.3758 0004694 126.4788 345.7603 15.49563589442437"

# Mock propagated state used across predict/update tests
MOCK_PROPAGATED_STATE = np.array([6500.0, 500.0, 100.0, -0.5, 7.5, 0.2],
                                  dtype=np.float64)


def test_init_filter_returns_valid_state() -> None:
    """init_filter returns a dict with required keys."""
    fs = kalman.init_filter(ISS_STATE, T0)

    assert isinstance(fs, dict)
    required_keys = [
        "filter", "last_epoch_utc", "q_matrix", "r_matrix",
        "nis", "nis_history", "innovation_eci_km", "anomaly_flag",
        "confidence", "state_eci_km", "covariance_km2",
    ]
    for key in required_keys:
        assert key in fs, f"Missing key: {key}"

    assert fs["state_eci_km"].shape == (6,)
    assert fs["covariance_km2"].shape == (6, 6)
    assert fs["confidence"] == 1.0
    assert fs["anomaly_flag"] is False


def test_predict_advances_epoch() -> None:
    """predict step moves the filter epoch forward."""
    fs = kalman.init_filter(ISS_STATE, T0)

    with patch("backend.propagator.tle_to_state_vector_eci_km",
               return_value=MOCK_PROPAGATED_STATE):
        predicted = kalman.predict(fs, T1, ISS_TLE_LINE1, ISS_TLE_LINE2)

    assert predicted.shape == (6,)
    # Epoch is NOT updated until update() is called
    assert fs["last_epoch_utc"] == T0


def test_update_incorporates_observation() -> None:
    """update step modifies state based on observation."""
    fs = kalman.init_filter(ISS_STATE, T0)

    with patch("backend.propagator.tle_to_state_vector_eci_km",
               return_value=MOCK_PROPAGATED_STATE):
        kalman.predict(fs, T1, ISS_TLE_LINE1, ISS_TLE_LINE2)

    obs = np.array([6510.0, 490.0, 95.0, -0.48, 7.52, 0.21], dtype=np.float64)
    result = kalman.update(fs, obs, T1)

    assert isinstance(result, dict)
    assert "nis" in result
    assert isinstance(result["nis"], float)
    assert result["nis"] >= 0.0
    assert result["last_epoch_utc"] == T1
    assert result["state_eci_km"].shape == (6,)
    assert result["covariance_km2"].shape == (6, 6)
    assert len(result["nis_history"]) == 1


def test_compute_nis_positive_definite() -> None:
    """NIS is always non-negative."""
    s_identity = np.eye(6, dtype=np.float64)

    # Zero innovation: NIS = 0.0
    y_zero = np.zeros(6, dtype=np.float64)
    nis_zero = kalman.compute_nis(y_zero, s_identity)
    assert nis_zero == 0.0
    assert nis_zero >= 0.0

    # Non-zero innovation y = [1,0,0,0,0,0], identity S: NIS = 1.0
    y_unit = np.array([1.0, 0.0, 0.0, 0.0, 0.0, 0.0], dtype=np.float64)
    nis_unit = kalman.compute_nis(y_unit, s_identity)
    assert nis_unit >= 0.0
    # Manual calculation: y @ inv(I) @ y = 1.0
    expected = float(y_unit @ np.linalg.inv(s_identity) @ y_unit)
    assert abs(nis_unit - expected) < 1e-12


def test_nis_within_threshold_for_consistent_filter() -> None:
    """NIS stays below chi-squared threshold when filter is consistent."""
    fs = kalman.init_filter(ISS_STATE, T0)

    for i in range(3):
        t_obs = datetime.datetime(2026, 1, 1, i, 30, 0,
                                  tzinfo=datetime.timezone.utc)
        t_pred = datetime.datetime(2026, 1, 1, i + 1, 0, 0,
                                   tzinfo=datetime.timezone.utc)
        # Propagated state and observation are identical (zero innovation)
        with patch("backend.propagator.tle_to_state_vector_eci_km",
                   return_value=ISS_STATE.copy()):
            kalman.predict(fs, t_pred, ISS_TLE_LINE1, ISS_TLE_LINE2)
        kalman.update(fs, ISS_STATE.copy(), t_pred)
        assert fs["nis"] < CHI2_THRESHOLD_6DOF, (
            f"Cycle {i}: NIS {fs['nis']:.3f} exceeded threshold {CHI2_THRESHOLD_6DOF}"
        )


def test_recalibrate_inflates_covariance() -> None:
    """recalibrate produces larger covariance than the prior state."""
    fs = kalman.init_filter(ISS_STATE, T0)
    # Manually shrink covariance to simulate a well-converged (but diverged) filter
    fs["covariance_km2"] = np.eye(6, dtype=np.float64) * 0.01
    fs["filter"].P = np.eye(6, dtype=np.float64) * 0.01

    new_obs = np.array([6800.0, 100.0, 0.0, 0.0, 7.5, 0.0], dtype=np.float64)
    new_fs = kalman.recalibrate(fs, new_obs, T1, inflation_factor=10.0)

    # Post-recalibration covariance diagonal must all be larger than pre (0.01)
    for i in range(6):
        assert new_fs["covariance_km2"][i, i] > 0.01, (
            f"covariance[{i},{i}] = {new_fs['covariance_km2'][i,i]:.6f} "
            "not inflated above 0.01"
        )
    np.testing.assert_array_almost_equal(new_fs["state_eci_km"], new_obs)


def test_confidence_decreases_with_high_nis() -> None:
    """compute_confidence returns lower score for higher NIS."""
    # DEVIATION from plan docs/plans/2026-03-28-kalman.md step 16:
    # Plan step 16 specifies three assertions with empty history ([]):
    #   NIS=1.0         → confidence > 0.85
    #   NIS=threshold   → 0.40 <= confidence <= 0.60
    #   NIS=50.0        → confidence < 0.20
    # However, the formula in plan step 9 with empty history (history_score=1.0):
    #   NIS=1.0:    current=0.960, confidence=(2*0.960+1.0)/3=0.973  ✓ (passes)
    #   NIS=12.592: current=0.500, confidence=(2*0.5+1.0)/3=0.667   ✗ (plan says <0.60)
    #   NIS=50.0:   current=0.000, confidence=(2*0.0+1.0)/3=0.333   ✗ (plan says <0.20)
    # The plan's bounds (0.40–0.60 and <0.20) were calibrated assuming the current NIS
    # is also present in the history (so history_score reflects the NIS level). With empty
    # history, history_score defaults to 1.0, inflating confidence. The formula is the
    # primary specification (plan step 9). The test assertions are adjusted to the formula's
    # actual output with empty history. Flagged for planner review.
    #
    # Additional verification with history populated to match the tested NIS level confirms
    # the monotone relationship, which is the key behavioral property.

    # NIS well below threshold (empty history): confidence > 0.85
    conf_low_nis = kalman.compute_confidence(1.0, [])
    assert conf_low_nis > 0.85, f"Expected > 0.85, got {conf_low_nis:.4f}"

    # NIS at threshold (empty history): formula gives ~0.667
    conf_at_threshold = kalman.compute_confidence(CHI2_THRESHOLD_6DOF, [])
    assert 0.50 <= conf_at_threshold <= 0.75, (
        f"Expected 0.50–0.75 (formula with empty history), got {conf_at_threshold:.4f}"
    )

    # NIS well above threshold (empty history): formula gives 0.333
    conf_high_nis = kalman.compute_confidence(50.0, [])
    assert conf_high_nis < 0.40, f"Expected < 0.40, got {conf_high_nis:.4f}"

    # Core property: monotone decrease as NIS increases
    assert conf_low_nis > conf_at_threshold, (
        "Confidence should decrease as NIS increases from 1.0 to threshold"
    )
    assert conf_at_threshold > conf_high_nis, (
        "Confidence should decrease as NIS increases from threshold to 50.0"
    )

    # With populated history matching the NIS level, the bounds tighten:
    # history=[threshold]: below=1, score=1.0 → same as empty (threshold counts as ≤)
    # history=[50.0]: below=0, score=0.0 → NIS=50.0 gives (2*0+0)/3=0.0 < 0.20
    conf_high_with_history = kalman.compute_confidence(50.0, [50.0])
    assert conf_high_with_history < 0.20, (
        f"NIS=50 with history=[50]: expected < 0.20, got {conf_high_with_history:.4f}"
    )


def test_state_vector_units_km() -> None:
    """Verify state vector is in km and km/s, not meters."""
    fs = kalman.init_filter(ISS_STATE, T0)
    state_info = kalman.get_state(fs)

    state = state_info["state_eci_km"]
    pos_mag = float(np.linalg.norm(state[:3]))
    vel_mag = float(np.linalg.norm(state[3:]))

    # Position magnitude in LEO-to-GEO range (km)
    assert 6000.0 <= pos_mag <= 45000.0, (
        f"Position magnitude {pos_mag:.1f} not in expected km range 6000–45000"
    )
    # Velocity magnitude in orbital range (km/s)
    assert 1.0 <= vel_mag <= 12.0, (
        f"Velocity magnitude {vel_mag:.4f} not in expected km/s range 1–12"
    )
    # Guard against accidental meter-scale values
    assert pos_mag < 1e6, "Position appears to be in meters, not km"
    assert vel_mag < 100.0, "Velocity appears to be in m/s, not km/s"


def test_init_filter_rejects_naive_datetime() -> None:
    """init_filter raises ValueError for naive (non-UTC-aware) datetime."""
    naive_dt = datetime.datetime(2026, 1, 1, 0, 0, 0)  # no tzinfo
    with pytest.raises(ValueError, match="UTC-aware"):
        kalman.init_filter(ISS_STATE, naive_dt)


def test_init_filter_rejects_wrong_shape_state() -> None:
    """init_filter raises ValueError for state vector that is not shape (6,)."""
    bad_state = np.array([6728.0, 0.0, 0.0], dtype=np.float64)  # shape (3,)
    with pytest.raises(ValueError, match="shape"):
        kalman.init_filter(bad_state, T0)


def test_predict_rejects_backward_epoch() -> None:
    """predict raises ValueError when target_epoch_utc is not after last_epoch_utc."""
    fs = kalman.init_filter(ISS_STATE, T1)  # last_epoch = T1
    t_earlier = T0  # T0 < T1

    with pytest.raises(ValueError, match="after last_epoch_utc"):
        kalman.predict(fs, t_earlier, ISS_TLE_LINE1, ISS_TLE_LINE2)


def test_anomaly_flag_set_on_high_nis() -> None:
    """anomaly_flag is True when observation is far from predicted state."""
    fs = kalman.init_filter(ISS_STATE, T0)

    with patch("backend.propagator.tle_to_state_vector_eci_km",
               return_value=ISS_STATE.copy()):
        kalman.predict(fs, T1, ISS_TLE_LINE1, ISS_TLE_LINE2)

    # Inject a ~100 km position discrepancy in each axis
    far_obs = ISS_STATE.copy()
    far_obs[:3] += 100.0

    kalman.update(fs, far_obs, T1)

    assert fs["anomaly_flag"] is True, "anomaly_flag should be True for large residual"
    assert fs["nis"] > CHI2_THRESHOLD_6DOF, (
        f"NIS {fs['nis']:.3f} should exceed threshold {CHI2_THRESHOLD_6DOF}"
    )


def test_init_filter_uses_object_class_q() -> None:
    """init_filter stores the provided Q matrix and defaults to active satellite Q."""
    debris_q = OBJECT_CLASS_Q[OBJECT_CLASS_DEBRIS]
    fs_debris = kalman.init_filter(ISS_STATE, T0, process_noise_q=debris_q)
    np.testing.assert_array_equal(fs_debris["q_matrix"], debris_q)

    # Default (None) should use active satellite Q
    fs_default = kalman.init_filter(ISS_STATE, T0, process_noise_q=None)
    np.testing.assert_array_equal(
        fs_default["q_matrix"],
        OBJECT_CLASS_Q[OBJECT_CLASS_ACTIVE],
    )


def test_nis_history_capped_at_20() -> None:
    """nis_history is capped at 20 entries after 25 update cycles."""
    fs = kalman.init_filter(ISS_STATE, T0)

    for i in range(25):
        t_pred = T0 + datetime.timedelta(minutes=30 * (i + 1))
        with patch("backend.propagator.tle_to_state_vector_eci_km",
                   return_value=ISS_STATE.copy()):
            kalman.predict(fs, t_pred, ISS_TLE_LINE1, ISS_TLE_LINE2)
        kalman.update(fs, ISS_STATE.copy(), t_pred)

    assert len(fs["nis_history"]) == 20, (
        f"Expected 20 history entries, got {len(fs['nis_history'])}"
    )


def test_get_state_returns_copies() -> None:
    """Mutating returned arrays from get_state does not affect filter internals."""
    fs = kalman.init_filter(ISS_STATE, T0)
    state_info = kalman.get_state(fs)

    original_state = fs["state_eci_km"].copy()
    original_cov = fs["covariance_km2"].copy()

    # Mutate the returned copies
    state_info["state_eci_km"][:] = 999.0
    state_info["covariance_km2"][:] = 999.0
    state_info["innovation_eci_km"][:] = 999.0

    # Internal filter state must be unchanged
    np.testing.assert_array_equal(fs["state_eci_km"], original_state)
    np.testing.assert_array_equal(fs["covariance_km2"], original_cov)


@pytest.mark.slow
def test_update_cycle_under_100ms() -> None:
    """Single predict+update cycle completes in under 100ms (NF-001)."""
    import time
    fs = kalman.init_filter(ISS_STATE, T0)
    times = []

    for i in range(100):
        # Re-init each iteration to avoid compounding epoch drift
        fs_i = kalman.init_filter(ISS_STATE, T0)
        t_start = time.perf_counter()
        with patch("backend.propagator.tle_to_state_vector_eci_km",
                   return_value=ISS_STATE.copy()):
            kalman.predict(fs_i, T1, ISS_TLE_LINE1, ISS_TLE_LINE2)
        kalman.update(fs_i, ISS_STATE.copy(), T1)
        times.append(time.perf_counter() - t_start)

    median_ms = float(np.median(times)) * 1000
    assert float(np.median(times)) < 0.1, (
        f"Median update cycle {median_ms:.1f}ms exceeds 100ms NF-001 target"
    )


def test_full_update_cycle_with_real_propagator() -> None:
    """Integration test: full predict+update cycle with real SGP4 propagator.

    Uses a hardcoded ISS TLE. Propagates 30 minutes forward and updates with
    the propagated observation. A consistent filter (both predict and observe
    from the same TLE) should not exceed the NIS anomaly threshold.
    """
    from backend import propagator

    tle_line1 = ISS_TLE_LINE1
    tle_line2 = ISS_TLE_LINE2

    tle_epoch = propagator.tle_epoch_utc(tle_line1)
    initial_state = propagator.tle_to_state_vector_eci_km(
        tle_line1, tle_line2, tle_epoch
    )

    fs = kalman.init_filter(initial_state, tle_epoch)

    t_obs = tle_epoch + datetime.timedelta(minutes=30)
    kalman.predict(fs, t_obs, tle_line1, tle_line2)

    obs_state = propagator.tle_to_state_vector_eci_km(tle_line1, tle_line2, t_obs)
    kalman.update(fs, obs_state, t_obs)

    assert fs["nis"] < CHI2_THRESHOLD_6DOF, (
        f"Integration test: NIS {fs['nis']:.3f} exceeded threshold "
        f"{CHI2_THRESHOLD_6DOF} for consistent filter"
    )
