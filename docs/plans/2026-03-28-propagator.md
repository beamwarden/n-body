# Implementation Plan: SGP4 Orbital Propagation Engine
Date: 2026-03-28
Status: Draft

## Summary

Implement the four stub functions in `backend/propagator.py` to provide stateless SGP4-based orbital propagation from TLE input to ECI J2000 state vector output. This is the foundational computation layer that the Kalman filter engine (`kalman.py`) depends on for both its predict step and its observation model. The critical technical challenge is the TEME-to-ECI J2000 frame rotation, which SGP4 does not perform natively.

## Requirements addressed

- **F-010**: Propagate orbital state using SGP4 from current TLE to arbitrary future epoch.
- **F-011**: All propagated state vectors in ECI J2000, units km and km/s.
- **F-012**: Propagator accepts TLE + target UTC epoch, returns 6-element state vector `[x, y, z, vx, vy, vz]`.
- **F-013**: Propagator is stateless -- no memory between calls, independently testable.
- **NF-030** (partial): All public functions have type annotations and docstrings (already stubbed).
- **NF-031** (partial): Unit test coverage target >= 70% on `propagator.py`.

## Files affected

- `backend/propagator.py` -- Implement the four stub functions: `propagate_tle`, `tle_to_state_vector_eci_km`, `tle_epoch_utc`, `eci_to_geodetic`. Add a private `_teme_to_eci_j2000` rotation function.
- `backend/requirements.txt` -- Add `astropy>=6.0` (see justification below in Phase 1 Step 1).
- `tests/test_propagator.py` -- Replace all `pytest.skip` stubs with working assertions. Add additional edge-case tests.

## Data flow changes

**Before:** `propagator.py` functions raise `NotImplementedError`. No data flows through this module.

**After:**
```
TLE (line1, line2) + epoch_utc
        |
        v
  Satrec.twoline2rv()   -- parse TLE into SGP4 satellite record
        |
        v
  Satrec.sgp4(jd, fr)   -- propagate to target epoch, output in TEME (km, km/s)
        |
        v
  _teme_to_eci_j2000()  -- rotate TEME vectors to ECI J2000 using astropy
        |
        v
  (position_eci_km, velocity_eci_km_s)  -- returned to caller
```

The `kalman.py` module (not yet implemented) will call `propagate_tle` and `tle_to_state_vector_eci_km` as its process model and observation model respectively. This plan does not change that intended interface.

## Implementation steps

### Phase 1: TEME-to-J2000 Conversion Strategy

1. **Add `astropy` dependency** (`backend/requirements.txt`)
   - Action: Add `astropy>=6.0` to `requirements.txt`.
   - Why: The `sgp4` library outputs state vectors in the TEME (True Equator Mean Equinox) frame. Requirement F-011 mandates ECI J2000. The TEME-to-J2000 rotation requires precession and nutation corrections that are date-dependent and non-trivial. Three options were evaluated:
     - **(a) `astropy`**: Provides `TEME` and `GCRS` (which is effectively J2000 for our accuracy needs) coordinate frames with validated IAU nutation/precession models. Single function call: `SkyCoord(TEME) -> GCRS`. Battle-tested in the astrodynamics community.
     - **(b) Manual rotation via GMST + nutation**: Requires implementing the IAU-76/FK5 precession-nutation model manually. Error-prone, hard to validate, and duplicates work that astropy already does correctly.
     - **(c) `sgp4` library utilities**: The `sgp4` library does not provide a TEME-to-J2000 conversion. Its `sgp4lib` module only converts to ITRS (Earth-fixed), which is ECEF, not J2000.
   - **Recommendation**: Option (a). `astropy` is the standard Python astrodynamics library. The dependency is large (~150 MB installed) but justified per C-005 because:
     - The conversion correctness is safety-critical for all downstream filter accuracy.
     - Manual implementation would cost significant development time and carry validation risk.
     - `astropy` will also be useful for `eci_to_geodetic` (ECI to geodetic conversion at the API boundary).
     - `scipy` and `numpy` (already dependencies) are sub-dependencies of `astropy`, so the incremental dependency tree is moderate.
   - Dependencies: none
   - Risk: **Medium** -- `astropy` is a large dependency. If install size becomes a concern for deployment, the rotation matrices can be extracted post-POC. For POC on a developer machine (C-002), this is acceptable.

2. **Implement `_teme_to_eci_j2000` private function** (`backend/propagator.py`)
   - Action: Add a private function `_teme_to_eci_j2000(position_teme_km, velocity_teme_km_s, epoch_utc)` that:
     - Creates an `astropy.time.Time` object from the UTC epoch.
     - Creates an `astropy.coordinates.CartesianRepresentation` for position and `CartesianDifferential` for velocity, both in TEME frame.
     - Transforms to GCRS (Geocentric Celestial Reference System, which is the IAU realization of J2000 for near-Earth applications).
     - Extracts and returns the rotated position and velocity as numpy arrays in km and km/s.
   - Why: Isolates the frame conversion into a single, independently testable function. All public functions route through this.
   - Dependencies: Requires step 1.
   - Risk: **Low** -- `astropy` frame transforms are well-documented and widely used.

### Phase 2: Core Propagation Functions

3. **Implement `propagate_tle`** (`backend/propagator.py`)
   - Action: Replace the `NotImplementedError` with:
     - Parse TLE using `sgp4.api.Satrec.twoline2rv(tle_line1, tle_line2, sgp4.api.WGS72)`. Use WGS72 gravity model as this is what SGP4/TLE data assumes.
     - Validate that parsing succeeded (check `satrec.error` code; if non-zero, raise `ValueError` with the SGP4 error message).
     - Convert `epoch_utc` to Julian date pair `(jd, fr)` using `sgp4.api.jday(year, month, day, hour, minute, second)`. Ensure the datetime is UTC-aware; raise `ValueError` if naive.
     - Call `satrec.sgp4(jd, fr)` to get TEME position (km) and velocity (km/s).
     - Check the SGP4 error code from the return value; raise `ValueError` if propagation failed (e.g., epoch too far from TLE epoch, satellite decayed).
     - Call `_teme_to_eci_j2000` to rotate from TEME to J2000.
     - Return `(position_eci_km, velocity_eci_km_s)` as a tuple of two 3-element `NDArray[np.float64]`.
   - Why: This is the primary propagation entry point used by `kalman.py` for both predict and observe steps.
   - Dependencies: Requires steps 1 and 2.
   - Risk: **Low**

4. **Implement `tle_to_state_vector_eci_km`** (`backend/propagator.py`)
   - Action: Replace the `NotImplementedError` with a call to `propagate_tle`, then concatenate the position and velocity arrays into a single 6-element array using `np.concatenate([pos, vel])`.
   - Why: Convenience wrapper that returns the format `kalman.py` needs for its state vector: `[x, y, z, vx, vy, vz]`. Satisfies F-012 directly.
   - Dependencies: Requires step 3.
   - Risk: **Low**

5. **Implement `tle_epoch_utc`** (`backend/propagator.py`)
   - Action: Replace the `NotImplementedError` with:
     - Parse the TLE line 1 using `sgp4.api.Satrec.twoline2rv` (only line 1 is needed for epoch, but the API requires both lines; alternatively, parse the epoch fields manually from line 1 columns 18-32).
     - **Recommended approach**: Parse manually from TLE line 1 format: columns 18-19 are 2-digit year, columns 20-32 are fractional day of year. Convert to `datetime.datetime` with `tzinfo=datetime.timezone.utc`.
     - The 2-digit year convention: years 0-56 map to 2000-2056, years 57-99 map to 1957-1999 (standard TLE convention).
   - Why: Needed by `kalman.py` to determine the observation epoch when a new TLE arrives, and by `ingest.py` to check TLE recency.
   - Dependencies: none (no SGP4 propagation needed, just TLE parsing).
   - Risk: **Low**

### Phase 3: API Boundary Conversion

6. **Implement `eci_to_geodetic`** (`backend/propagator.py`)
   - Action: Replace the `NotImplementedError` with:
     - Use `astropy.coordinates` to convert from GCRS (ECI J2000) to ITRS (Earth-fixed) at the given epoch, then extract geodetic latitude, longitude, and altitude.
     - Alternatively, use `astropy.coordinates.EarthLocation.from_geocentric()` after GCRS-to-ITRS transform.
     - Return `(latitude_rad, longitude_rad, altitude_km)` -- note the existing signature already specifies `_rad` suffix for angles and `_km` for altitude, which is correct per domain rules.
   - Why: Required by the API layer (`main.py`) to convert ECI positions to lat/lon/alt for the CesiumJS frontend. The architecture document specifies this conversion happens only at the API boundary, never internally.
   - Dependencies: Requires step 1 (astropy).
   - Risk: **Low**

### Phase 4: Tests

7. **Implement `test_propagate_tle_returns_correct_shape`** (`tests/test_propagator.py`)
   - Action: Use a known ISS TLE (hardcoded in the test as a constant). Propagate to TLE epoch + 60 minutes. Assert that the return value is a tuple of two numpy arrays, each with shape `(3,)`.
   - Why: Basic shape contract test.
   - Dependencies: Requires steps 3.
   - Risk: **Low**

8. **Implement `test_propagate_tle_rejects_malformed_tle`** (`tests/test_propagator.py`)
   - Action: Pass garbage strings as TLE lines. Assert `ValueError` is raised. Also test with a valid line 1 but corrupted line 2 (bad checksum).
   - Why: F-003 requires the system to reject malformed TLEs without crashing. The propagator is the first point where a bad TLE would cause a computation failure.
   - Dependencies: Requires step 3.
   - Risk: **Low**

9. **Implement `test_tle_to_state_vector_returns_6_elements`** (`tests/test_propagator.py`)
   - Action: Use the same ISS TLE. Assert return shape is `(6,)`. Assert that position components (indices 0-2) are in the range typical for LEO (6,500-7,500 km magnitude). Assert velocity components (indices 3-5) are in the range 6-8 km/s magnitude.
   - Why: Validates the F-012 contract and provides a basic sanity check on physical plausibility.
   - Dependencies: Requires step 4.
   - Risk: **Low**

10. **Implement `test_tle_epoch_utc_is_utc_aware`** (`tests/test_propagator.py`)
    - Action: Parse a known TLE. Assert the returned datetime has `tzinfo` set to `datetime.timezone.utc`. Assert the date matches the expected epoch from the TLE.
    - Why: All timestamps must be UTC-aware per domain rules. A naive datetime propagating through the system would cause silent frame errors.
    - Dependencies: Requires step 5.
    - Risk: **Low**

11. **Implement `test_eci_to_geodetic_returns_lat_lon_alt`** (`tests/test_propagator.py`)
    - Action: Use a known ECI position (e.g., point on the +X axis at 6778 km, which should be ~0 latitude, some longitude dependent on epoch, ~400 km altitude). Assert latitude and longitude are in `[-pi, pi]` range and altitude is physically reasonable.
    - Why: Validates the API boundary conversion.
    - Dependencies: Requires step 6.
    - Risk: **Low**

12. **Implement `test_propagation_output_is_eci_j2000`** (`tests/test_propagator.py`)
    - Action: This is the critical validation test. Approach:
      - Use a known TLE and epoch where a reference ECI J2000 state vector is available (e.g., from Vallado's SGP4 test cases or from a trusted external tool like STK or GMAT).
      - Propagate using our function and compare against the reference.
      - Assert position agreement within 1 km and velocity agreement within 0.001 km/s. These tolerances account for differences in nutation/precession model versions between tools.
      - **Alternative if no external reference is available**: Propagate the same TLE at TLE epoch (zero propagation time) and compare the TEME output (from raw sgp4) against our J2000 output. The difference between TEME and J2000 is small (a few km for LEO) but non-zero and predictable. Assert that the difference is in the expected range (0.1-10 km position, depending on nutation angle at that epoch).
    - Why: This test validates that the TEME-to-J2000 conversion is actually being applied and producing correct results. Without this, a bug that returns TEME as if it were J2000 would pass all other tests (since the values are close) and silently degrade filter accuracy.
    - Dependencies: Requires steps 3 and 2.
    - Risk: **Medium** -- finding a good reference value requires care. The plan recommends using Vallado's published SGP4 verification TLEs with known TEME outputs, then applying astropy's conversion independently to produce expected J2000 values.

13. **Add new edge-case tests** (`tests/test_propagator.py`)
    - Action: Add the following tests beyond the existing stubs:
      - `test_propagate_tle_rejects_naive_datetime`: Pass a naive (non-UTC-aware) datetime. Assert `ValueError`.
      - `test_propagate_tle_far_epoch_raises`: Propagate a TLE 30+ days from its epoch. SGP4 accuracy degrades significantly; the sgp4 library may return an error code. Assert that the function raises `ValueError` rather than returning garbage.
      - `test_tle_epoch_utc_two_digit_year`: Test both a year < 57 (maps to 20xx) and a year >= 57 (maps to 19xx) to validate the TLE year convention.
    - Why: Edge cases that affect downstream reliability.
    - Dependencies: Requires steps 3 and 5.
    - Risk: **Low**

## Test strategy

- **Unit tests**: All six test stubs in `tests/test_propagator.py` will be implemented, plus three new edge-case tests (13 total test functions targeting `propagator.py`). This should comfortably exceed the 70% coverage target (NF-031).
- **Reference data**: Use ISS TLE `1 25544U 98067A   24045.51773148  .00015204  00000+0  27364-3 0  9996` / `2 25544  51.6412 225.3758 0004694 126.4788 345.7603 15.49563589442437` (or similar recent TLE -- the implementer should select one and hardcode it as a test fixture).
- **Integration test**: After implementation, run the full validation sequence:
  1. `python -m py_compile backend/propagator.py`
  2. `mypy backend/ --ignore-missing-imports`
  3. `pytest tests/test_propagator.py -v`
- **Frame validation**: The `test_propagation_output_is_eci_j2000` test is the most important. If it passes with correct reference values, the TEME-to-J2000 pipeline is confirmed working.

## Risks and mitigations

- **Risk**: `astropy` is a large dependency (~150 MB), which conflicts with C-005's directive to minimize dependencies. -- **Mitigation**: The dependency is justified because manual TEME-to-J2000 conversion is error-prone and the correctness of this conversion is critical to every downstream computation. Document the justification in a code comment at the import site. Post-POC, the specific rotation matrices could be extracted to remove the full astropy dependency.

- **Risk**: `astropy` TEME frame support may require `astropy >= 6.0` which is relatively recent. Older astropy versions have a different TEME implementation. -- **Mitigation**: Pin `astropy>=6.0` in requirements.txt. Verify the import works in CI. The `astropy.coordinates.builtin_frames.TEME` frame was stabilized in astropy 4.3+; version 6.0 is a conservative minimum.

- **Risk**: SGP4 propagation far from TLE epoch produces unreliable results. Callers might not know this. -- **Mitigation**: Add a check in `propagate_tle`: if the propagation interval exceeds a configurable threshold (suggest 7 days for POC), log a warning. Do not raise an error by default, as the Kalman filter may intentionally propagate across longer gaps during data outages. The 30-day test in step 13 should raise because SGP4 itself returns an error code for extreme extrapolations.

- **Risk**: Performance -- `astropy` coordinate transforms involve loading Earth orientation data and can be slow on first call. -- **Mitigation**: For POC with 20-50 objects at 30-minute intervals, even 100ms per transform is acceptable (NF-001 allows 100ms per Kalman update, and propagation is one component of that). If profiling shows astropy is a bottleneck, the rotation matrices can be cached or precomputed. Flag this for the implementer to measure.

## Open questions

1. **Astropy dependency approval**: Adding `astropy>=6.0` is a significant new dependency. Per C-005, this requires explicit justification. The justification is provided above, but this should be confirmed by a human reviewer before the implementer proceeds. If rejected, the fallback is manual rotation implementation (option b), which increases implementation risk and time.

2. **GCRS vs. J2000 distinction**: Strictly speaking, GCRS (Geocentric Celestial Reference System) and J2000 (FK5-based) differ by up to ~20 milliarcseconds due to the frame tie rotation. For LEO objects, this translates to sub-meter position differences -- negligible for POC accuracy. The plan treats GCRS as equivalent to J2000. If reviewers require exact J2000 (FK5), an additional small constant rotation matrix must be applied. **Recommendation**: Accept GCRS as J2000-equivalent for POC; document the approximation in a code comment.

3. **WGS72 vs. WGS84 gravity model**: SGP4 TLEs are generated using WGS72. The `sgp4` library's `Satrec.twoline2rv` should use `WGS72` (not `WGS84`) for consistency with the TLE generation model. The implementer should use `sgp4.api.WGS72` explicitly. This is a known subtlety -- using WGS84 introduces a small but avoidable systematic error.

4. **Reference values for frame validation test**: The implementer needs a known ECI J2000 state vector for a specific TLE and epoch to validate the TEME-to-J2000 conversion. Options: (a) compute it using an independent tool (STK, GMAT) and hardcode, (b) use Vallado's published verification data (TEME only) and apply the astropy rotation independently to generate the expected J2000 value. Option (b) is self-referential but acceptable for POC if option (a) is not readily available. The human reviewer should indicate a preference.
