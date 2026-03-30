# Implementation Plan: Fix Maneuver Classification Across Recalibration Boundary
Date: 2026-03-30
Status: Draft

## Summary

The maneuver classifier in `anomaly.py` requires `MANEUVER_CONSECUTIVE_CYCLES >= 2` consecutive NIS exceedances on an active satellite before classifying an event as `maneuver`. However, the current processing pipeline in `processing.py` calls `kalman.recalibrate()` immediately after the **first** NIS exceedance (classified as `filter_divergence`). The `recalibrate()` function re-initializes the filter from the observation with inflated covariance, which resets NIS to 0.0 and produces a below-threshold NIS on the next cycle even if the underlying maneuver is still present. The consecutive-exceedance counter can therefore never reach 2, making it impossible for any real event to be classified as `maneuver`.

## Requirements addressed

- **F-031 [DEMO]**: Three-way anomaly classification (maneuver, drag_anomaly, filter_divergence)
- **F-032**: Maneuver classification requires NIS elevation on at least 2 consecutive update cycles for active satellites
- **F-033 [DEMO]**: Recalibration triggered on anomaly detection

## Root cause analysis

### The classification-before-recalibration race

The code path for a single object update cycle in `processing.py` lines 296-339 is:

1. `kalman.update()` incorporates the new observation (line 297). NIS is computed and appended to `nis_history` (kalman.py lines 267-278).
2. `anomaly.classify_anomaly()` examines `nis_history` (processing.py line 304).
3. If any anomaly is detected (line 313), `kalman.recalibrate()` is called **immediately** (line 329).

On the **first** cycle with elevated NIS (e.g., NIS=247.2 for ISS):
- `nis_history` ends with `[..., 247.2]` -- only 1 consecutive exceedance.
- `_count_consecutive_tail_exceedances` returns 1, which is less than `MANEUVER_CONSECUTIVE_CYCLES` (2).
- The maneuver check at anomaly.py line 179 fails.
- Classification falls through to `filter_divergence` (anomaly.py line 208).
- `processing.py` line 329 calls `kalman.recalibrate()`.

`kalman.recalibrate()` (kalman.py lines 335-374):
- Calls `init_filter()` which creates a **fresh** filter state with `nis=0.0`, `innovation_eci_km=zeros(6)`, `anomaly_flag=False`, `confidence=1.0` (kalman.py lines 163-167).
- **Critically**, line 372 copies the old `nis_history` to the new state: `new_state["nis_history"] = filter_state["nis_history"].copy()`. This preserves the history list itself.

However, the preservation of `nis_history` is **insufficient** to fix the problem. Here is why:

On the **second** cycle (e.g., NIS=722.4 for ISS):
- The recalibrated filter starts with the observation as its state and inflated covariance (P0 * 10.0 for `filter_divergence`).
- The **predict** step propagates using the previous TLE (processing.py line 284).
- The **update** step incorporates the new TLE observation (processing.py line 297).
- Because recalibration reset the filter state **to the previous observation**, and the new observation reflects the same maneuver, NIS will again be elevated.
- `nis_history` now ends with `[..., 247.2, 722.4]` -- 2 consecutive exceedances.
- `_count_consecutive_tail_exceedances` returns 2, which meets `MANEUVER_CONSECUTIVE_CYCLES`.

**Wait -- this suggests the bug should NOT occur if `nis_history` is preserved.**

### Re-examining: does recalibrate actually preserve nis_history correctly?

Looking more carefully at `processing.py` line 329:

```python
filter_state = kalman.recalibrate(
    filter_state=filter_state,
    new_observation_eci_km=observation_eci_km,
    epoch_utc=epoch_utc,
    inflation_factor=recal_params["inflation_factor"],
)
```

`kalman.recalibrate()` returns a **new** dict (from `init_filter`). Line 372 copies `nis_history`. This looks correct.

But then processing.py line 339:
```python
filter_states[norad_id] = filter_state
```

This replaces the old filter_state in the shared dict. The new `filter_state` has the preserved `nis_history`. So the history **is** available on the next cycle.

### The actual bug: recalibration resets the filter so the next NIS is NOT elevated

The real issue is subtler. After recalibration:

1. `recalibrate()` sets the filter state to `new_observation_eci_km` (the current TLE observation) with **inflated** covariance P0 * inflation_factor.
2. On the next cycle, `predict()` propagates using the **current** TLE (stored as `last_tle_line1/2` at processing.py line 338) to the new observation epoch.
3. `update()` incorporates the **new** TLE observation.
4. Because the inflated covariance makes the filter very uncertain, the innovation covariance S = H*P*H^T + R is **large**.
5. NIS = y^T * S^{-1} * y. Even if the innovation y is large (due to the ongoing maneuver), the inflated S in the denominator **suppresses NIS below the threshold**.

This is the actual mechanism: the inflated covariance from recalibration absorbs the second maneuver signal, producing a low NIS that breaks the consecutive-exceedance chain.

To confirm: `_make_default_covariance_p0(10.0)` produces diagonal entries of `[1000, 1000, 1000, 0.1, 0.1, 0.1]` (kalman.py lines 83-86: `[100, 100, 100, 0.01, 0.01, 0.01] * 10`). Combined with R diagonal `[900, 900, 900, 0.002, 0.002, 0.002]`, the position block of S is approximately `1000 + 900 = 1900 km^2`. For a 383 km position residual: NIS_pos ~ 383^2 / 1900 ~ 77. But this is only the position block contribution. With all 6 DOF, the inflated velocity covariance further dilutes the total NIS. The exact value depends on the full matrix, but the inflation clearly reduces NIS dramatically compared to the non-inflated case.

For the **maneuver** inflation factor of 20.0, P0 diagonal would be `[2000, 2000, 2000, 0.2, 0.2, 0.2]`, making S even larger and NIS even more suppressed. But the maneuver classification never fires, so the inflation_factor=10.0 (for `filter_divergence`) is what actually applies.

**However**, even with inflation_factor=10.0, the reported second-event NIS was 722.4 -- still far above 12.592. This contradicts the hypothesis that inflated covariance suppresses the NIS.

### Re-examining with the actual data

The two events reported:
- Cycle 1: NIS=247.2, residual=383 km -> classified `filter_divergence` -> recalibration with inflation_factor=10.0
- Cycle 2: NIS=722.4, residual=648 km -> classified `filter_divergence`

Both NIS values are far above threshold. The second event's NIS=722.4 means the nis_history **does** have 2 consecutive exceedances IF the first value was preserved.

### The actual root cause: nis_history is NOT preserved across the recalibration boundary in the processing flow

Re-reading `processing.py` lines 329-339 very carefully:

```python
filter_state = kalman.recalibrate(
    filter_state=filter_state,
    new_observation_eci_km=observation_eci_km,
    epoch_utc=epoch_utc,
    inflation_factor=recal_params["inflation_factor"],
)
# Preserve TLE lines...
filter_state["last_tle_line1"] = tle_line1
filter_state["last_tle_line2"] = tle_line2
filter_states[norad_id] = filter_state
```

`kalman.recalibrate()` DOES copy `nis_history` (kalman.py line 372). So the history list is preserved.

But look at what happens on cycle 2 in `processing.py`:

Line 266: `filter_state = filter_states[norad_id]` -- gets the recalibrated state with preserved `nis_history`.
Line 297: `kalman.update(filter_state, observation_eci_km, epoch_utc)` -- appends new NIS to `nis_history`.
Line 300: `nis_history: list = filter_state["nis_history"]` -- this has the full history including cycle 1's 247.2 AND cycle 2's new NIS.

Line 304: `anomaly.classify_anomaly(... nis_history=nis_history ...)` -- `_count_consecutive_tail_exceedances` should find 2 consecutive exceedances.

**Unless** the recalibration itself appends a NIS value to the history that is below threshold.

Looking at `kalman.recalibrate()` -> calls `init_filter()` which sets `nis_history: []` (empty list) at kalman.py line 164. Then line 372 overwrites: `new_state["nis_history"] = filter_state["nis_history"].copy()`.

But `recalibrate()` does NOT append any additional NIS value. The history should be: `[..., 247.2]` after cycle 1.

**Wait -- re-read the processing flow more carefully.**

After recalibration at processing.py line 329, the code continues to lines 346-378 which build WS messages and record the anomaly. It does NOT run another update cycle. The filter_state at this point has `nis=0.0` (from init_filter at kalman.py line 163), but `nis_history` still ends with `[..., 247.2]`.

On cycle 2:
- `kalman.update()` at processing.py line 297 appends the new NIS (722.4) to `nis_history`.
- `nis_history` is now `[..., 247.2, 722.4]`.
- `_count_consecutive_tail_exceedances([..., 247.2, 722.4], 12.592)` returns >= 2.
- `classify_anomaly` at line 179: `consecutive_count >= 2 and is_active_satellite` -> True.
- Should return `ANOMALY_MANEUVER`.

**This contradicts the reported behavior.** The hypothesis as originally stated appears incorrect based on the code. The nis_history IS preserved and should enable maneuver classification on cycle 2.

### Alternative hypothesis: processing loop does not call process_single_object twice for the same TLE epoch

Look at the duplicate-epoch guard at processing.py lines 269-277:

```python
last_epoch_utc: datetime.datetime = filter_state["last_epoch_utc"]
if epoch_utc <= last_epoch_utc:
    ...
    return []
```

After recalibration, `filter_state["last_epoch_utc"]` is set to `epoch_utc` (the same epoch as the observation that triggered the anomaly) because `init_filter` in `recalibrate` is called with that epoch (kalman.py line 360-364).

The **second** TLE (the one that would produce cycle 2's NIS=722.4) has a different epoch -- it is the next TLE update ~30 minutes later. So this guard should not block it.

### Alternative hypothesis: the _processing_loop_task or admin_trigger_process calls process_single_object once per TLE, not once per poll

Looking at `_processing_loop_task` in main.py (lines 416-481): it processes each catalog object once per `catalog_update` event. Each event corresponds to one poll cycle. Only one TLE per object per cycle.

The `admin_trigger_process` endpoint (main.py lines 1162-1247) also processes each object once per call.

So each ISS event (2026-03-29 03:11 and 2026-03-30 03:57) corresponds to a separate processing cycle, separated by approximately 24 hours (multiple poll cycles apart).

### The real root cause: the events are NOT on consecutive cycles

The two events are ~24 hours apart:
- 2026-03-29 03:11 UTC
- 2026-03-30 03:57 UTC

With a 30-minute polling interval, there are approximately **48 poll cycles** between these two events. In the intervening cycles, TLE updates arrive with **normal** NIS values (below threshold), because the recalibrated filter converges back to tracking the post-maneuver orbit.

After recalibration from the first event:
- Cycle N+1 (30 min later): new TLE arrives, NIS is below threshold (recalibrated filter tracks the post-maneuver orbit). This value is appended to `nis_history`.
- The consecutive exceedance chain is broken: `nis_history` = `[..., 247.2, 4.3, ...]`.
- Cycles N+2 through N+47: all normal NIS values appended.
- Cycle N+48 (~24 hours later): second maneuver event, NIS=722.4. Only 1 consecutive exceedance at the tail. Classified as `filter_divergence`.

**This is the root cause.** These are two separate maneuver events, each producing only a single NIS exceedance per event. The `MANEUVER_CONSECUTIVE_CYCLES >= 2` requirement means a maneuver is only classified if the same maneuver produces elevated NIS for 2+ consecutive TLE update cycles (~30 min apart). If a maneuver produces a large residual on only one TLE update and then the recalibrated filter absorbs it, the classifier can never accumulate 2 consecutive exceedances.

The fundamental issue is: **recalibration on the first exceedance prevents the second consecutive exceedance from occurring**, because the recalibrated filter successfully absorbs the maneuver signal on the next cycle.

This is a variant of the original hypothesis but the mechanism is: recalibration makes the filter so uncertain that the NEXT normal TLE update (not a second maneuver) produces a normal NIS, breaking the chain before a second exceedance can register.

## The fix

The fix must defer recalibration for active satellites until the anomaly classification is final. Two approaches:

### Approach A: Defer recalibration for active satellites (recommended)

When the first NIS exceedance occurs on an active satellite, do NOT recalibrate immediately. Instead, record that the object is in a "pending classification" state. On the next cycle, if a second consecutive exceedance occurs, classify as `maneuver` and THEN recalibrate (with the maneuver-specific inflation_factor=20.0). If the second cycle's NIS is below threshold, retroactively classify the first event as `filter_divergence` and recalibrate.

This requires:
1. A new filter_state key to track pending classification state.
2. Modified control flow in `processing.py` to defer recalibration.
3. The anomaly record for the first exceedance may need its `anomaly_type` updated retroactively once classification is final.

### Approach B: Classify before recalibrating using only history (simpler, less accurate)

Lower `MANEUVER_CONSECUTIVE_CYCLES` to 1 for the first detection, then require confirmation on the next cycle. This is essentially the same as approach A but conflates the threshold change with the deferral.

### Recommended: Approach A

Approach A is minimal, correct, and does not change the classification threshold.

## Files affected

- `backend/processing.py` -- modify `process_single_object` to defer recalibration for active satellites when `consecutive_count == 1` (first exceedance, pending maneuver confirmation). Add handling for the "pending classification" path on the next cycle.
- `backend/anomaly.py` -- no changes to classification logic itself. The `classify_anomaly` function is correct; the bug is in the calling code that recalibrates too early.
- `backend/kalman.py` -- no changes. `recalibrate()` correctly preserves `nis_history`.
- `tests/test_processing.py` -- add test cases for the deferred-recalibration path.
- `tests/test_anomaly.py` -- no changes needed (existing tests for classify_anomaly are correct).
- `docs/tech-debt.md` -- add TD item for the retroactive anomaly_type update if Approach A is chosen.

## Data flow changes

### Before (current)

```
Cycle 1 (active satellite, first NIS exceedance):
  update -> NIS > threshold (1 consecutive)
  -> classify_anomaly -> "filter_divergence" (< 2 consecutive)
  -> recalibrate (inflation_factor=10.0)
  -> filter absorbs maneuver signal

Cycle 2 (same object, next TLE ~30 min later):
  update -> NIS < threshold (recalibrated filter absorbed the maneuver)
  -> classify_anomaly -> None
  -> no anomaly recorded

Result: maneuver classified as filter_divergence. Always.
```

### After (proposed)

```
Cycle 1 (active satellite, first NIS exceedance):
  update -> NIS > threshold (1 consecutive)
  -> classify_anomaly -> None or "pending" (< 2 consecutive, active satellite)
  -> DO NOT recalibrate. Store anomaly detection state in filter_state.
  -> Record a provisional anomaly with status "pending_classification".

Cycle 2 (same object, next TLE ~30 min later):
  update -> NIS may or may not exceed threshold

  Case A: NIS > threshold (2 consecutive):
    -> classify_anomaly -> "maneuver"
    -> Update provisional anomaly record to anomaly_type="maneuver"
    -> recalibrate (inflation_factor=20.0, maneuver-specific)

  Case B: NIS < threshold (chain broken):
    -> classify_anomaly -> None
    -> Update provisional anomaly record to anomaly_type="filter_divergence"
    -> recalibrate retroactively (inflation_factor=10.0, divergence-specific)
    -> Or: the filter naturally recovered, so recalibration may be unnecessary.

Result: maneuver correctly classified when 2+ consecutive exceedances occur.
```

## Implementation steps

### Phase 1: Deferred recalibration in processing.py

1. **Add pending-classification state tracking** (`backend/processing.py`)
   - Action: When `classify_anomaly` returns `filter_divergence` for an active satellite AND `_count_consecutive_tail_exceedances` returns exactly 1, set `filter_state["_pending_maneuver_check"] = True` and `filter_state["_pending_anomaly_epoch_utc"] = epoch_utc` and `filter_state["_pending_nis_value"] = nis_val` and `filter_state["_pending_innovation_eci_km"] = innovation_eci_km_list`. Do NOT call `recalibrate()`. Record a provisional anomaly row with `anomaly_type="filter_divergence"` (to be updated) and store the row ID.
   - Why: Deferring recalibration allows the filter to carry forward the unabsorbed maneuver signal to the next cycle, enabling the consecutive-exceedance check to fire.
   - Dependencies: None.
   - Risk: Medium -- the filter runs one additional cycle without recalibration. For a true filter divergence (not a maneuver), this delays recovery by one cycle (~30 min). Acceptable for POC.

2. **Handle the deferred classification on the next cycle** (`backend/processing.py`)
   - Action: At the top of the warm-path (after the predict-update step), check `filter_state.get("_pending_maneuver_check")`. If True, call `classify_anomaly` with the updated `nis_history`. If the result is `maneuver`: update the provisional anomaly record's `anomaly_type` to `"maneuver"` in the alerts table, then recalibrate with `inflation_factor=20.0`. If the result is `filter_divergence` or NIS is below threshold: keep the original `filter_divergence` classification, recalibrate with `inflation_factor=10.0`. In both cases, clear `_pending_maneuver_check` and related keys.
   - Why: This is where the 2-consecutive-cycle check can finally succeed.
   - Dependencies: Step 1.
   - Risk: Low.

3. **Add anomaly_type update SQL helper** (`backend/anomaly.py`)
   - Action: Add a function `update_anomaly_type(db, anomaly_row_id, new_anomaly_type)` that updates the `anomaly_type` column for an existing alerts row. This is needed to retroactively change `filter_divergence` to `maneuver` when the second exceedance confirms the classification.
   - Why: The provisional anomaly record created in step 1 may need its type corrected.
   - Dependencies: None.
   - Risk: Low.

4. **Emit corrected WebSocket messages** (`backend/processing.py`)
   - Action: When the deferred classification resolves to `maneuver`, broadcast an `anomaly` message with `anomaly_type="maneuver"` and the correct NIS/innovation values from the first detection. Also broadcast the recalibration message.
   - Why: Frontend needs to display the correct anomaly type.
   - Dependencies: Steps 1, 2.
   - Risk: Low.

### Phase 2: Guard non-active satellites against deferral

5. **Ensure non-active satellites still recalibrate immediately** (`backend/processing.py`)
   - Action: The deferral logic (step 1) must only apply when `is_active_satellite is True`. For debris and rocket bodies, the current immediate-recalibration behavior is correct (these objects cannot maneuver).
   - Why: Deferring recalibration for non-maneuverable objects has no benefit and delays recovery.
   - Dependencies: Step 1.
   - Risk: Low.

### Phase 3: Tests

6. **Add integration test: maneuver classification across 2 cycles** (`tests/test_processing.py`)
   - Action: Create a test that calls `process_single_object` twice for an active satellite with two consecutive TLEs producing NIS above threshold. Assert that the second call produces `anomaly_type="maneuver"`, not `"filter_divergence"`.
   - Why: This is the exact scenario described in the bug report.
   - Dependencies: Steps 1, 2.
   - Risk: Low.

7. **Add integration test: single exceedance resolves to filter_divergence** (`tests/test_processing.py`)
   - Action: Create a test where cycle 1 has NIS above threshold but cycle 2 has NIS below threshold. Assert that the final classification is `filter_divergence` and recalibration occurs on cycle 2.
   - Why: Ensures the deferral does not break the filter_divergence path.
   - Dependencies: Steps 1, 2.
   - Risk: Low.

8. **Add integration test: non-active satellite is not deferred** (`tests/test_processing.py`)
   - Action: Create a test where a debris object has a single NIS exceedance. Assert that recalibration happens immediately (no deferral).
   - Why: Regression guard for step 5.
   - Dependencies: Step 5.
   - Risk: Low.

9. **Add unit test for update_anomaly_type** (`tests/test_anomaly.py`)
   - Action: Test that `update_anomaly_type` correctly changes the anomaly_type in the SQLite alerts table.
   - Why: New function needs coverage.
   - Dependencies: Step 3.
   - Risk: Low.

## Test strategy

- **Unit tests**: New `update_anomaly_type` function in anomaly.py. Existing `classify_anomaly` tests remain unchanged (the classifier itself is correct).
- **Integration tests**: Three new scenarios in `test_processing.py` as described in steps 6-8. These tests must use mock TLEs that produce controlled NIS values across consecutive cycles.
- **Edge cases to cover**:
  - Active satellite with exactly 2 consecutive exceedances -> `maneuver`
  - Active satellite with 1 exceedance followed by below-threshold -> `filter_divergence` (deferred)
  - Active satellite with 3+ consecutive exceedances -> `maneuver` on cycle 2, subsequent recalibrations handled correctly
  - Non-active satellite (debris) -> immediate `filter_divergence` regardless of consecutive count
  - Pending classification state survives if no new TLE arrives (epoch guard skips the cycle)
- **Regression**: Run the full existing test suite (`pytest tests/ -v`) to ensure no breakage.

## Risks and mitigations

- **Risk**: Deferring recalibration for one cycle means the filter runs in a diverged state for ~30 minutes longer than before. **Mitigation**: This is only for active satellites where the maneuver classification is operationally important. The filter's inflated NIS already signals low confidence to the frontend. The 30-minute delay is acceptable for POC demo purposes.

- **Risk**: If a genuine filter divergence (not a maneuver) occurs on an active satellite, the first cycle now produces no recalibration. If the second cycle's NIS is also above threshold (due to continued divergence), it will be misclassified as `maneuver`. **Mitigation**: This is an inherent limitation of the 2-consecutive-cycle heuristic (it cannot distinguish a sustained divergence from a maneuver on an active satellite). The existing architecture accepts this tradeoff (architecture section 3.4). Post-POC, incorporate velocity-direction analysis or mission-specific maneuver catalogs to disambiguate.

- **Risk**: The provisional anomaly record with `anomaly_type="filter_divergence"` may be visible to WebSocket clients for up to one cycle before being corrected to `"maneuver"`. **Mitigation**: Emit the corrected classification in the next cycle's anomaly message. Frontend should handle anomaly type updates gracefully. For POC demo, this ~30-second window is not user-visible.

- **Risk**: The `_pending_maneuver_check` state is stored in the in-memory `filter_states` dict, not in SQLite. If the server restarts between cycles, the pending state is lost. **Mitigation**: Acceptable for POC. Document as tech debt for production persistence.

## Related issues uncovered

1. **TD-012 (architecture.md inconsistency)**: Architecture section 3.4 says ">3 consecutive cycles" but the implementation uses >= 2. This plan does not change the threshold, but the inconsistency should be resolved. The architecture doc should be updated to match the implementation.

2. **New tech debt item needed**: The deferred-recalibration state (`_pending_maneuver_check` and related keys) is stored only in memory. A server restart between the first and second exceedance cycles loses the pending state, causing the second cycle to start fresh (classified as a single exceedance -> `filter_divergence`). This should be noted as a new TD item.

3. **Inflation factor mismatch**: When a maneuver is eventually classified, the recalibration should use `inflation_factor=20.0` (maneuver-specific, per anomaly.py line 249). Under the current bug, all events use `inflation_factor=10.0` (filter_divergence). The fix naturally resolves this because the correct anomaly type determines the correct inflation factor.

## Open questions — RESOLVED 2026-03-30

1. **Alert timing:** Emit provisional `filter_divergence` alert on cycle 1. Update to `maneuver` on cycle 2 if confirmed. Monitor for alert saturation; revisit if operators see excessive provisional alerts.

2. **drag_anomaly deferral:** Apply deferral to drag_anomaly as well. Defer recalibration for all anomaly types on active satellites pending the second-cycle confirmation.

3. **Timeout:** Yes — add a configurable timeout (default 2 hours / 4 poll cycles). After timeout, resolve pending state as `filter_divergence` and recalibrate.
