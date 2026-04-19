"""Unit tests for backend/conjunction.py.

Tests cover:
- Trajectory generation (point count, ECI J2000 validity)
- Minimum distance computation (identical trajectories, different objects)
- Full screen_conjunctions algorithm (no risks, first-order, second-order, schema)
- Timestamp UTC enforcement

Real TLEs from the test database are used for trajectory generation tests.
The conjunction detection tests (first/second order) use mocked propagator output
to guarantee specific miss distances without relying on actual orbital conjunctions.
"""

import datetime
import unittest
from unittest.mock import patch

import numpy as np

import backend.conjunction as conjunction

# ---------------------------------------------------------------------------
# Shared test TLEs (real ISS and DELTA-1 TLEs cached in the test DB)
# ---------------------------------------------------------------------------

_ISS_TLE1 = "1 25544U 98067A   26087.87455012  .00011057  00000-0  21128-3 0  9996"
_ISS_TLE2 = "2 25544  51.6344 337.5169 0006219 244.1555 115.8792 15.48616916559305"

_DELTA1_TLE1 = "1 27424U 02022A   26087.90871115  .00001076  00000-0  22442-3 0  9991"
_DELTA1_TLE2 = "2 27424  98.4206  55.6284 0001137 117.8140 264.0345 14.62020005271547"

_ATLAS_TLE1 = "1 27386U 02009A   26087.86169519  .00000121  00000-0  53150-4 0  9997"
_ATLAS_TLE2 = "2 27386  98.3758  41.0062 0001214  81.5597 285.6451 14.39043458261937"

# Reference epoch (UTC-aware)
_EPOCH_UTC = datetime.datetime(2026, 3, 28, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_trajectory(
    positions: list[tuple[float, float, float]],
    start_epoch: datetime.datetime = _EPOCH_UTC,
    step_s: int = conjunction.SCREENING_STEP_S,
) -> list[tuple[datetime.datetime, np.ndarray]]:
    """Build a synthetic trajectory from a list of (x, y, z) tuples in km."""
    result = []
    for i, pos in enumerate(positions):
        epoch = start_epoch + datetime.timedelta(seconds=(i + 1) * step_s)
        result.append((epoch, np.array(pos, dtype=np.float64)))
    return result


# ---------------------------------------------------------------------------
# Tests: generate_trajectory_eci_km
# ---------------------------------------------------------------------------


class TestGenerateTrajectoryEciKm(unittest.TestCase):
    """Tests for conjunction.generate_trajectory_eci_km."""

    def test_returns_correct_count(self):
        """Verify 90 points for 5400 s horizon at 60 s steps."""
        traj = conjunction.generate_trajectory_eci_km(
            _ISS_TLE1,
            _ISS_TLE2,
            _EPOCH_UTC,
            conjunction.SCREENING_HORIZON_S,
            conjunction.SCREENING_STEP_S,
        )
        expected_count = conjunction.SCREENING_HORIZON_S // conjunction.SCREENING_STEP_S
        self.assertEqual(len(traj), expected_count)

    def test_all_eci_j2000(self):
        """Verify all positions are 3-element arrays with magnitudes in LEO range."""
        traj = conjunction.generate_trajectory_eci_km(
            _ISS_TLE1,
            _ISS_TLE2,
            _EPOCH_UTC,
            conjunction.SCREENING_HORIZON_S,
            conjunction.SCREENING_STEP_S,
        )
        self.assertGreater(len(traj), 0)
        for epoch_utc, pos_eci_km in traj:
            self.assertEqual(pos_eci_km.shape, (3,))
            mag = float(np.linalg.norm(pos_eci_km))
            # ISS altitude ~400 km → total radius ~6771 km. Accept 6400–7400 km range.
            self.assertGreaterEqual(mag, 6400.0, f"Magnitude {mag:.1f} km below LEO floor")
            self.assertLessEqual(mag, 7400.0, f"Magnitude {mag:.1f} km above LEO ceiling")
            # Epoch must be UTC-aware.
            self.assertIsNotNone(epoch_utc.tzinfo)

    def test_raises_on_naive_epoch(self):
        """generate_trajectory_eci_km must raise ValueError for naive datetime."""
        naive_epoch = datetime.datetime(2026, 3, 28, 12, 0, 0)
        with self.assertRaises(ValueError):
            conjunction.generate_trajectory_eci_km(_ISS_TLE1, _ISS_TLE2, naive_epoch, 300, 60)

    def test_skips_failed_propagation_steps(self):
        """Verify that SGP4 failures on individual steps are skipped gracefully."""
        call_count = [0]
        original_propagate = conjunction.propagator.propagate_tle

        def flaky_propagate(line1, line2, epoch):
            call_count[0] += 1
            # Fail every other step.
            if call_count[0] % 2 == 0:
                raise ValueError("Synthetic SGP4 failure")
            return original_propagate(line1, line2, epoch)

        with patch.object(conjunction.propagator, "propagate_tle", side_effect=flaky_propagate):
            traj = conjunction.generate_trajectory_eci_km(_ISS_TLE1, _ISS_TLE2, _EPOCH_UTC, 600, 60)
        # 10 steps total, half fail → expect 5 points.
        self.assertEqual(len(traj), 5)


# ---------------------------------------------------------------------------
# Tests: compute_min_distance_km
# ---------------------------------------------------------------------------


class TestComputeMinDistanceKm(unittest.TestCase):
    """Tests for conjunction.compute_min_distance_km."""

    def test_identical_trajectories_return_zero(self):
        """Two identical trajectories must return min distance ~0."""
        positions = [(6371.0, 0.0, 0.0), (6372.0, 1.0, 0.0), (6373.0, 2.0, 0.0)]
        traj = _make_trajectory(positions)
        min_dist, _ = conjunction.compute_min_distance_km(traj, traj)
        self.assertAlmostEqual(min_dist, 0.0, places=6)

    def test_different_objects_return_positive_distance(self):
        """Objects in different orbits must return a positive min distance."""
        pos_a = [(7000.0, 0.0, 0.0), (7000.0, 100.0, 0.0)]
        pos_b = [(0.0, 7000.0, 0.0), (100.0, 7000.0, 0.0)]
        traj_a = _make_trajectory(pos_a)
        traj_b = _make_trajectory(pos_b)
        min_dist, tca = conjunction.compute_min_distance_km(traj_a, traj_b)
        # Distance between (7000,0,0) and (0,7000,0) = 7000*sqrt(2) ≈ 9899 km.
        self.assertGreater(min_dist, 1000.0)
        self.assertIsNotNone(tca.tzinfo)

    def test_empty_trajectory_returns_sentinel(self):
        """An empty trajectory must return the sentinel distance 1e9 km."""
        traj = _make_trajectory([(6371.0, 0.0, 0.0)])
        min_dist, _ = conjunction.compute_min_distance_km([], traj)
        self.assertEqual(min_dist, 1e9)

    def test_min_distance_selects_correct_step(self):
        """Verify the closest approach is identified at the correct time step."""
        # Step 1: 100 km apart. Step 2: 1 km apart (closest). Step 3: 50 km apart.
        traj_a = _make_trajectory([(0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)])
        traj_b = _make_trajectory([(100.0, 0.0, 0.0), (1.0, 0.0, 0.0), (50.0, 0.0, 0.0)])
        min_dist, tca = conjunction.compute_min_distance_km(traj_a, traj_b)
        self.assertAlmostEqual(min_dist, 1.0, places=6)
        # TCA should be at the second step epoch.
        expected_tca = _EPOCH_UTC + datetime.timedelta(seconds=2 * conjunction.SCREENING_STEP_S)
        self.assertEqual(tca, expected_tca)

    def test_shorter_trajectory_limits_comparison(self):
        """Verify shorter-length trajectory is used when lengths differ."""
        traj_a = _make_trajectory([(0.0, 0.0, 0.0)] * 5)
        traj_b = _make_trajectory([(1.0, 0.0, 0.0)] * 3)  # Shorter
        min_dist, _ = conjunction.compute_min_distance_km(traj_a, traj_b)
        # All 3 comparison points have distance 1 km.
        self.assertAlmostEqual(min_dist, 1.0, places=6)


# ---------------------------------------------------------------------------
# Tests: screen_conjunctions
# ---------------------------------------------------------------------------


class TestScreenConjunctions(unittest.TestCase):
    """Tests for conjunction.screen_conjunctions using mocked propagate_tle."""

    def _make_mock_propagate(self, positions_by_norad: dict[int, list]) -> callable:
        """Create a mock propagate_tle that returns controlled positions by TLE NORAD ID.

        positions_by_norad maps NORAD ID (extracted from tle_line1) to a list of
        (x, y, z) tuples. Positions are served round-robin per object.
        """
        call_counts: dict[int, int] = {}

        def _mock_propagate(line1: str, line2: str, epoch_utc):
            norad_id = int(line1[2:7].strip())
            idx = call_counts.get(norad_id, 0)
            call_counts[norad_id] = idx + 1
            positions = positions_by_norad.get(norad_id, [(7000.0, 0.0, 0.0)])
            pos = positions[idx % len(positions)]
            return np.array(pos, dtype=np.float64), np.array([0.0, 0.0, 0.0])

        return _mock_propagate

    def test_no_risks_empty_lists(self):
        """All objects far away should return empty first_order and second_order lists."""
        other_objects = [
            {
                "norad_id": 27424,
                "tle_line1": _DELTA1_TLE1,
                "tle_line2": _DELTA1_TLE2,
            },
        ]
        # ISS at (7000, 0, 0); DELTA1 at (0, 7000, 0) — ~9899 km apart.
        positions_by_norad = {
            25544: [(7000.0, 0.0, 0.0)] * 90,
            27424: [(0.0, 7000.0, 0.0)] * 90,
        }
        with patch.object(
            conjunction.propagator,
            "propagate_tle",
            side_effect=self._make_mock_propagate(positions_by_norad),
        ):
            result = conjunction.screen_conjunctions(
                anomalous_norad_id=25544,
                anomalous_tle_line1=_ISS_TLE1,
                anomalous_tle_line2=_ISS_TLE2,
                screening_epoch_utc=_EPOCH_UTC,
                other_objects=other_objects,
                catalog_name_map={25544: "ISS", 27424: "DELTA 1 R/B"},
            )
        self.assertEqual(result["first_order"], [])
        self.assertEqual(result["second_order"], [])

    def test_first_order_detected(self):
        """Objects placed within FIRST_ORDER_THRESHOLD_KM should appear in first_order."""
        other_objects = [
            {"norad_id": 27424, "tle_line1": _DELTA1_TLE1, "tle_line2": _DELTA1_TLE2},
        ]
        # ISS at origin; DELTA1 at 3 km offset on x-axis — well within 5 km threshold.
        positions_by_norad = {
            25544: [(7000.0, 0.0, 0.0)] * 90,
            27424: [(7003.0, 0.0, 0.0)] * 90,
        }
        with patch.object(
            conjunction.propagator,
            "propagate_tle",
            side_effect=self._make_mock_propagate(positions_by_norad),
        ):
            result = conjunction.screen_conjunctions(
                anomalous_norad_id=25544,
                anomalous_tle_line1=_ISS_TLE1,
                anomalous_tle_line2=_ISS_TLE2,
                screening_epoch_utc=_EPOCH_UTC,
                other_objects=other_objects,
                catalog_name_map={25544: "ISS", 27424: "DELTA 1 R/B"},
            )
        self.assertEqual(len(result["first_order"]), 1)
        self.assertEqual(result["first_order"][0]["norad_id"], 27424)
        self.assertAlmostEqual(result["first_order"][0]["min_distance_km"], 3.0, places=3)

    def test_second_order_detected(self):
        """Set up A close to B and B close to C — C should appear in second_order via B."""
        # NORAD 25544 (A) close to NORAD 27424 (B); B close to NORAD 27386 (C).
        other_objects = [
            {"norad_id": 27424, "tle_line1": _DELTA1_TLE1, "tle_line2": _DELTA1_TLE2},
            {"norad_id": 27386, "tle_line1": _ATLAS_TLE1, "tle_line2": _ATLAS_TLE2},
        ]
        positions_by_norad = {
            25544: [(7000.0, 0.0, 0.0)] * 90,  # A
            27424: [(7003.0, 0.0, 0.0)] * 90,  # B: 3 km from A → first-order
            27386: [(7003.0, 8.0, 0.0)] * 90,  # C: 8 km from B → second-order
        }
        with patch.object(
            conjunction.propagator,
            "propagate_tle",
            side_effect=self._make_mock_propagate(positions_by_norad),
        ):
            result = conjunction.screen_conjunctions(
                anomalous_norad_id=25544,
                anomalous_tle_line1=_ISS_TLE1,
                anomalous_tle_line2=_ISS_TLE2,
                screening_epoch_utc=_EPOCH_UTC,
                other_objects=other_objects,
                catalog_name_map={25544: "ISS", 27424: "DELTA 1 R/B", 27386: "ATLAS 5 CENTAUR R/B"},
            )
        self.assertEqual(len(result["first_order"]), 1)
        self.assertEqual(result["first_order"][0]["norad_id"], 27424)

        self.assertEqual(len(result["second_order"]), 1)
        self.assertEqual(result["second_order"][0]["norad_id"], 27386)
        self.assertEqual(result["second_order"][0]["via_norad_id"], 27424)
        self.assertAlmostEqual(result["second_order"][0]["min_distance_km"], 8.0, places=3)

    def test_result_schema_all_keys_present(self):
        """Verify all required keys are present in the result dict."""
        required_top_level = {
            "type",
            "anomalous_norad_id",
            "screening_epoch_utc",
            "horizon_s",
            "threshold_km",
            "first_order",
            "second_order",
        }
        positions_by_norad = {25544: [(7000.0, 0.0, 0.0)] * 90}
        with patch.object(
            conjunction.propagator,
            "propagate_tle",
            side_effect=self._make_mock_propagate(positions_by_norad),
        ):
            result = conjunction.screen_conjunctions(
                anomalous_norad_id=25544,
                anomalous_tle_line1=_ISS_TLE1,
                anomalous_tle_line2=_ISS_TLE2,
                screening_epoch_utc=_EPOCH_UTC,
                other_objects=[],
                catalog_name_map={25544: "ISS"},
            )
        self.assertEqual(set(result.keys()), required_top_level)
        self.assertEqual(result["type"], "conjunction_risk")
        self.assertEqual(result["anomalous_norad_id"], 25544)
        self.assertEqual(result["horizon_s"], conjunction.SCREENING_HORIZON_S)
        self.assertEqual(result["threshold_km"], conjunction.FIRST_ORDER_THRESHOLD_KM)
        self.assertIsInstance(result["first_order"], list)
        self.assertIsInstance(result["second_order"], list)

    def test_first_order_entry_schema(self):
        """Each first_order entry must have norad_id, name, min_distance_km, TCA."""
        other_objects = [
            {"norad_id": 27424, "tle_line1": _DELTA1_TLE1, "tle_line2": _DELTA1_TLE2},
        ]
        positions_by_norad = {
            25544: [(7000.0, 0.0, 0.0)] * 90,
            27424: [(7002.0, 0.0, 0.0)] * 90,
        }
        with patch.object(
            conjunction.propagator,
            "propagate_tle",
            side_effect=self._make_mock_propagate(positions_by_norad),
        ):
            result = conjunction.screen_conjunctions(
                anomalous_norad_id=25544,
                anomalous_tle_line1=_ISS_TLE1,
                anomalous_tle_line2=_ISS_TLE2,
                screening_epoch_utc=_EPOCH_UTC,
                other_objects=other_objects,
                catalog_name_map={25544: "ISS", 27424: "DELTA 1 R/B"},
            )
        self.assertEqual(len(result["first_order"]), 1)
        entry = result["first_order"][0]
        self.assertIn("norad_id", entry)
        self.assertIn("name", entry)
        self.assertIn("min_distance_km", entry)
        self.assertIn("time_of_closest_approach_utc", entry)

    def test_second_order_entry_schema(self):
        """Each second_order entry must have norad_id, name, min_distance_km, TCA, via_norad_id."""
        other_objects = [
            {"norad_id": 27424, "tle_line1": _DELTA1_TLE1, "tle_line2": _DELTA1_TLE2},
            {"norad_id": 27386, "tle_line1": _ATLAS_TLE1, "tle_line2": _ATLAS_TLE2},
        ]
        positions_by_norad = {
            25544: [(7000.0, 0.0, 0.0)] * 90,
            27424: [(7003.0, 0.0, 0.0)] * 90,
            27386: [(7003.0, 8.0, 0.0)] * 90,
        }
        with patch.object(
            conjunction.propagator,
            "propagate_tle",
            side_effect=self._make_mock_propagate(positions_by_norad),
        ):
            result = conjunction.screen_conjunctions(
                anomalous_norad_id=25544,
                anomalous_tle_line1=_ISS_TLE1,
                anomalous_tle_line2=_ISS_TLE2,
                screening_epoch_utc=_EPOCH_UTC,
                other_objects=other_objects,
                catalog_name_map={25544: "ISS", 27424: "DELTA 1 R/B", 27386: "ATLAS 5 CENTAUR R/B"},
            )
        self.assertEqual(len(result["second_order"]), 1)
        entry = result["second_order"][0]
        self.assertIn("norad_id", entry)
        self.assertIn("name", entry)
        self.assertIn("min_distance_km", entry)
        self.assertIn("via_norad_id", entry)
        self.assertIn("time_of_closest_approach_utc", entry)

    def test_timestamps_utc_format(self):
        """All epoch strings in the result must end with 'Z' (UTC indicator)."""
        other_objects = [
            {"norad_id": 27424, "tle_line1": _DELTA1_TLE1, "tle_line2": _DELTA1_TLE2},
        ]
        positions_by_norad = {
            25544: [(7000.0, 0.0, 0.0)] * 90,
            27424: [(7002.0, 0.0, 0.0)] * 90,
        }
        with patch.object(
            conjunction.propagator,
            "propagate_tle",
            side_effect=self._make_mock_propagate(positions_by_norad),
        ):
            result = conjunction.screen_conjunctions(
                anomalous_norad_id=25544,
                anomalous_tle_line1=_ISS_TLE1,
                anomalous_tle_line2=_ISS_TLE2,
                screening_epoch_utc=_EPOCH_UTC,
                other_objects=other_objects,
                catalog_name_map={25544: "ISS", 27424: "DELTA 1 R/B"},
            )

        def _is_utc(ts: str) -> bool:
            return ts.endswith("Z") or "+00:00" in ts

        self.assertTrue(_is_utc(result["screening_epoch_utc"]))
        for entry in result["first_order"]:
            self.assertTrue(_is_utc(entry["time_of_closest_approach_utc"]))
        for entry in result["second_order"]:
            self.assertTrue(_is_utc(entry["time_of_closest_approach_utc"]))

    def test_empty_other_objects(self):
        """screen_conjunctions with no other objects returns empty risk lists."""
        positions_by_norad = {25544: [(7000.0, 0.0, 0.0)] * 90}
        with patch.object(
            conjunction.propagator,
            "propagate_tle",
            side_effect=self._make_mock_propagate(positions_by_norad),
        ):
            result = conjunction.screen_conjunctions(
                anomalous_norad_id=25544,
                anomalous_tle_line1=_ISS_TLE1,
                anomalous_tle_line2=_ISS_TLE2,
                screening_epoch_utc=_EPOCH_UTC,
                other_objects=[],
                catalog_name_map={25544: "ISS"},
            )
        self.assertEqual(result["first_order"], [])
        self.assertEqual(result["second_order"], [])

    def test_name_lookup_uses_catalog_name_map(self):
        """Names in results must come from catalog_name_map, falling back to str(norad_id)."""
        other_objects = [
            {"norad_id": 27424, "tle_line1": _DELTA1_TLE1, "tle_line2": _DELTA1_TLE2},
        ]
        positions_by_norad = {
            25544: [(7000.0, 0.0, 0.0)] * 90,
            27424: [(7001.0, 0.0, 0.0)] * 90,  # Within 5 km threshold.
        }
        with patch.object(
            conjunction.propagator,
            "propagate_tle",
            side_effect=self._make_mock_propagate(positions_by_norad),
        ):
            result_named = conjunction.screen_conjunctions(
                anomalous_norad_id=25544,
                anomalous_tle_line1=_ISS_TLE1,
                anomalous_tle_line2=_ISS_TLE2,
                screening_epoch_utc=_EPOCH_UTC,
                other_objects=other_objects,
                catalog_name_map={25544: "ISS", 27424: "MY OBJECT NAME"},
            )
        self.assertEqual(result_named["first_order"][0]["name"], "MY OBJECT NAME")

        # Fallback: norad_id not in catalog_name_map → use str(norad_id).
        with patch.object(
            conjunction.propagator,
            "propagate_tle",
            side_effect=self._make_mock_propagate(positions_by_norad),
        ):
            result_fallback = conjunction.screen_conjunctions(
                anomalous_norad_id=25544,
                anomalous_tle_line1=_ISS_TLE1,
                anomalous_tle_line2=_ISS_TLE2,
                screening_epoch_utc=_EPOCH_UTC,
                other_objects=other_objects,
                catalog_name_map={},  # Empty — no names known.
            )
        self.assertEqual(result_fallback["first_order"][0]["name"], "27424")


if __name__ == "__main__":
    unittest.main()
