# Implementation Plan: Real-Time Satellite Animation via SampledPositionProperty

Date: 2026-04-17
Status: Draft

## Summary

Add smooth real-time satellite animation to the ne-body globe. After each processing cycle, the backend emits a `track_update` WebSocket message per object containing pre-computed ECI J2000 position samples (60 samples at 60-second spacing, covering 60 minutes forward). The frontend creates a CesiumJS `SampledPositionProperty` for each satellite billboard entity and configures the Cesium clock to animate in real time. This replaces the current snap-to-position behavior where billboards teleport between processing cycles.

## Requirements addressed

- **F-050 [DEMO]** 3D globe with positions of all tracked objects updated in real time
- **F-051 [DEMO]** Confidence color-coding (unchanged, still applied)
- **F-052 [DEMO]** Uncertainty ellipsoid (snap-per-cycle, known limitation noted)
- **F-053 [DEMO]** Anomaly highlight (unchanged)
- **NF-003** 30 FPS with 50+ objects (performance risk addressed below)
- **NF-002** WebSocket latency < 500ms (message size risk addressed below)

## Files affected

- `backend/processing.py` -- Add `generate_track_samples()` function; call it from `process_single_object()` and append a `track_update` message to the return list.
- `backend/main.py` -- Add `WS_TYPE_TRACK_UPDATE` import; the new message type flows through the existing broadcast path with no changes to `_process_single_object()` or `ws_manager.broadcast()`. Add `track_update` to the NF-012 initial-connect burst in `websocket_live()`.
- `frontend/src/globe.js` -- Add `applySampledTrack()` function that creates/updates a `SampledPositionProperty` on the billboard entity. Modify `initGlobe()` to configure the Cesium clock for real-time animation. Export `eciToEcefCartesian3` (currently module-private) so it remains available for track drawing.
- `frontend/src/main.js` -- Add `track_update` case to `routeMessage()`. Pass messages to `globe.applySampledTrack()`.

## Data flow changes

### Before (current)

```
processing cycle -> state_update WS msg -> updateSatellitePosition()
                                           sets ConstantPositionProperty (snap)
```

### After (proposed)

```
processing cycle -> state_update WS msg  -> updateSatellitePosition() [still sets color, label]
                 -> track_update WS msg  -> applySampledTrack()
                                            creates/replaces SampledPositionProperty
                                            (Cesium clock interpolates between samples)
```

The billboard entity's `position` property changes from `ConstantPositionProperty` to `SampledPositionProperty`. The `state_update` handler still calls `updateSatellitePosition()` for color/label/metadata updates but no longer sets `entity.position` (that is now owned by the sampled track).

The ellipsoid entity's `position` remains a `ConstantPositionProperty` updated on each `state_update` (snap-per-cycle). This is a known limitation documented below.

## Implementation steps

### Phase 1: Backend -- generate and emit track samples

#### Step 1.1: Define track sample generation function (`backend/processing.py`)

- **Action:** Add a new function `generate_track_samples()` with signature:
  ```
  def generate_track_samples(
      tle_line1: str,
      tle_line2: str,
      start_epoch_utc: datetime.datetime,
      num_samples: int = 60,
      interval_s: float = 60.0,
  ) -> list[dict]:
  ```
  The function propagates the TLE forward from `start_epoch_utc` at `interval_s` spacing for `num_samples` points. Each point is a dict:
  ```python
  {
      "epoch_utc": "2026-04-17T12:01:00Z",   # ISO-8601 UTC string
      "eci_km": [x, y, z],                    # ECI J2000, km (list of float)
  }
  ```
  Uses `propagator.propagate_tle()` for each sample. Catches `ValueError` per-sample (SGP4 decay/divergence) and skips that point, logging at DEBUG level. Returns the list of successfully propagated samples.
- **Why:** Centralizes sample generation in the processing module where TLE and filter state are available. Keeps propagator.py stateless (no batch API needed).
- **Dependencies:** None.
- **Risk:** Low. Calls existing `propagator.propagate_tle()` which is well tested.

**Coordinate frame compliance:** Input TLE produces TEME output internally; `propagate_tle()` applies TEME-to-GCRS/J2000 conversion. Output is ECI J2000 km. Compliant with F-011.

**Performance note:** 60 SGP4+TEME-to-GCRS calls per object, ~71 objects = ~4,260 propagations per cycle. Each propagation takes ~5ms (astropy TEME-to-GCRS dominates). Total: ~21 seconds of CPU time per cycle. Since the processing loop runs in the main asyncio thread and the ingest cycle is 30 minutes, this additional ~21 seconds is acceptable for POC. If it becomes a bottleneck, batch astropy transforms (vectorized `Time` array) can reduce this by 10-20x. Note as tech debt.

#### Step 1.2: Build `track_update` message in `process_single_object()` (`backend/processing.py`)

- **Action:** At the end of `process_single_object()`, after all existing message construction, generate track samples and append a `track_update` message to the `messages` list. The message schema:
  ```python
  {
      "type": "track_update",
      "norad_id": 25544,
      "epoch_utc": "2026-04-17T12:00:00Z",   # filter's last_epoch_utc
      "samples": [
          {"epoch_utc": "2026-04-17T12:00:00Z", "eci_km": [x, y, z]},
          {"epoch_utc": "2026-04-17T12:01:00Z", "eci_km": [x, y, z]},
          # ... 60 total
      ]
  }
  ```
  Use the filter's `last_epoch_utc` as `start_epoch_utc`. Use `filter_state["last_tle_line1"]` and `filter_state["last_tle_line2"]` for propagation (the most recent TLE, which matches the filter's corrected state).
  
  Only emit `track_update` when `process_single_object()` returns at least one non-empty message (i.e., the object was actually processed, not skipped due to duplicate epoch).
- **Why:** Keeps track generation coupled to the processing cycle. Each cycle produces fresh samples from the latest filter-corrected TLE.
- **Dependencies:** Step 1.1.
- **Risk:** Low.

#### Step 1.3: Add `WS_TYPE_TRACK_UPDATE` constant (`backend/processing.py`)

- **Action:** Add `WS_TYPE_TRACK_UPDATE: str = "track_update"` alongside existing WS type constants.
- **Why:** Consistent with pattern used for other message types.
- **Dependencies:** None.
- **Risk:** Low.

#### Step 1.4: Import and re-export in `main.py` (`backend/main.py`)

- **Action:** Add `WS_TYPE_TRACK_UPDATE` to the import from `backend.processing`. No other changes needed in `main.py` for broadcast -- `_process_single_object()` already iterates `messages` and broadcasts each one.
- **Why:** Consistency with existing import pattern.
- **Dependencies:** Step 1.3.
- **Risk:** Low.

#### Step 1.5: Emit track samples on WebSocket connect (`backend/main.py`)

- **Action:** In `websocket_live()`, after the existing NF-012 initial state burst loop, add a second loop that generates and sends `track_update` messages for all objects that have a filter state. For each `(norad_id, filter_state)` in `filter_states`:
  1. Look up the latest TLE via `ingest.get_latest_tle(db, norad_id)`.
  2. If TLE exists, call `processing.generate_track_samples()` using `filter_state["last_epoch_utc"]` as start, and `filter_state["last_tle_line1"]` / `["last_tle_line2"]` as the TLE.
  3. Build the `track_update` dict and send it.
  
  Wrap in try/except per-object so one failure does not block others.
- **Why:** Ensures a newly connected client gets animated tracks immediately, not just static positions. Without this, satellites would sit motionless until the next processing cycle (~30 min).
- **Dependencies:** Steps 1.1, 1.3.
- **Risk:** Medium. Generating ~71 track sets (4,260 propagations) during WebSocket connect could take ~21 seconds, blocking the connect handler. **Mitigation:** Run `generate_track_samples()` via `asyncio.get_event_loop().run_in_executor(None, ...)` for each object, or accept the delay for POC. For POC, the synchronous approach is acceptable -- the initial state burst already blocks similarly. Document as tech debt for production (pre-compute and cache track samples in app.state).

### Phase 2: Frontend -- Cesium clock and SampledPositionProperty

#### Step 2.1: Configure Cesium clock (`frontend/src/globe.js`)

- **Action:** In `initGlobe()`, after creating the viewer, add clock configuration:
  ```
  viewer.clock.shouldAnimate = true
  viewer.clock.clockRange = Cesium.ClockRange.UNBOUNDED
  viewer.clock.multiplier = 1.0
  viewer.clock.currentTime = Cesium.JulianDate.now()
  ```
  The timeline and animation widgets are already disabled (`timeline: false, animation: false` in the existing Viewer options), so no changes there.
- **Why:** Enables Cesium's internal clock to advance in real time, which drives `SampledPositionProperty` interpolation.
- **Dependencies:** None.
- **Risk:** Low.

#### Step 2.2: Add `applySampledTrack()` function (`frontend/src/globe.js`)

- **Action:** Add a new exported function:
  ```
  export function applySampledTrack(viewer, trackUpdate)
  ```
  Where `trackUpdate` is the parsed `track_update` WebSocket message.

  **Pseudocode:**
  ```
  function applySampledTrack(viewer, trackUpdate):
      noradId = trackUpdate.norad_id
      samples = trackUpdate.samples
      if samples is empty: return

      // Build SampledPositionProperty
      sampledPosition = new Cesium.SampledPositionProperty()
      sampledPosition.setInterpolationOptions({
          interpolationDegree: 5,
          interpolationAlgorithm: Cesium.LagrangePolynomialApproximation
      })

      for each sample in samples:
          julianDate = Cesium.JulianDate.fromIso8601(sample.epoch_utc)
          cartesian3 = eciToEcefCartesian3(sample.eci_km, sample.epoch_utc)
          sampledPosition.addSample(julianDate, cartesian3)

      // Get or create entity
      entity = entityMap.get(noradId)
      if entity exists:
          entity.position = sampledPosition
      else:
          // Entity doesn't exist yet; create a placeholder.
          // updateSatellitePosition will set color/label on next state_update.
          entity = viewer.entities.add({
              id: 'sat-' + noradId,
              position: sampledPosition,
              billboard: {
                  image: _createSatelliteDot(),
                  color: Cesium.Color.LIME,
                  scale: 0.6,
                  verticalOrigin: Cesium.VerticalOrigin.CENTER,
                  horizontalOrigin: Cesium.HorizontalOrigin.CENTER,
              },
              label: {
                  text: String(noradId),
                  font: '14px monospace',
                  fillColor: Cesium.Color.WHITE,
                  style: Cesium.LabelStyle.FILL,
                  verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
                  pixelOffset: new Cesium.Cartesian2(0, -12),
                  show: false,
              },
              properties: {
                  norad_id: noradId,
                  _lastConfidence: 0.5,
              },
          })
          entityMap.set(noradId, entity)
  ```

  **Interpolation choice:** Lagrange polynomial degree 5 with 60-second sample spacing gives smooth curves for LEO objects (orbital period ~90 min). Linear interpolation would produce visible kinks at sample boundaries. Hermite interpolation (which also uses velocity) is more accurate but `SampledPositionProperty` works with positions only; velocity-based interpolation would require `SampledProperty` with a custom `Cartesian3` pack/unpack, adding complexity beyond POC scope.

  **ECI-to-ECEF note:** Each sample is converted from ECI J2000 to ECEF at the sample's own epoch. This is correct because GMST rotation depends on the specific time. The resulting ECEF Cartesian3 positions are what Cesium interpolates. This means the interpolation is in ECEF space, which is slightly less accurate than interpolating in ECI and converting per-frame, but the error over 60-second intervals at LEO altitudes is sub-kilometer and visually negligible.
- **Why:** This is the core of the animation feature.
- **Dependencies:** Step 2.1 (clock must be running for interpolation to work).
- **Risk:** Medium. Performance with 71 entities each having 60 samples needs testing. Cesium handles thousands of sampled entities in CZML demos, so 71 should be fine. Flag for validation.

#### Step 2.3: Modify `updateSatellitePosition()` to not overwrite position when sampled (`frontend/src/globe.js`)

- **Action:** Modify `updateSatellitePosition()` so that if the entity already exists AND its `position` property is a `SampledPositionProperty`, it does NOT replace the position. It still updates:
  - `billboard.color` (confidence + conjunction risk color)
  - `properties._lastConfidence`

  **Pseudocode for the "entity already exists" branch:**
  ```
  if entityMap.has(norad_id):
      entity = entityMap.get(norad_id)
      // Only set position if it's NOT a SampledPositionProperty
      // (i.e., it's still the initial ConstantPositionProperty from before
      //  any track_update arrived, or from catalog seed)
      if !(entity.position instanceof Cesium.SampledPositionProperty):
          entity.position = new Cesium.ConstantPositionProperty(cartesian3)
      entity.billboard.color = new Cesium.ConstantProperty(effectiveColor)
      entity.properties._lastConfidence = new Cesium.ConstantProperty(confidence)
  ```

  The "entity does not exist" branch remains unchanged (creates with `ConstantPositionProperty`; the next `track_update` will replace it with `SampledPositionProperty`).
- **Why:** Without this guard, every `state_update` would replace the `SampledPositionProperty` with a `ConstantPositionProperty`, breaking animation.
- **Dependencies:** Step 2.2.
- **Risk:** Low. The `instanceof` check is straightforward in CesiumJS.

#### Step 2.4: Route `track_update` in `routeMessage()` (`frontend/src/main.js`)

- **Action:** Add a new `else if` branch in `routeMessage()`:
  ```
  } else if (type === 'track_update') {
      applySampledTrack(viewer, message);
  }
  ```
  Import `applySampledTrack` from `./globe.js`.
- **Why:** Completes the frontend message routing.
- **Dependencies:** Step 2.2.
- **Risk:** Low.

#### Step 2.5: Update import statement in `main.js`

- **Action:** Add `applySampledTrack` to the import from `./globe.js`.
- **Why:** Required for step 2.4.
- **Dependencies:** Step 2.2.
- **Risk:** Low.

### Phase 3: Integration and polish

#### Step 3.1: Handle `track_update` in `admin_trigger_process()` (`backend/main.py`)

- **Action:** No code change needed. The `track_update` messages are already included in the `messages` list returned by `process_single_object()`, and `admin_trigger_process()` already iterates and broadcasts all messages. Verify this works during testing.
- **Why:** Demo flow uses `admin_trigger_process()` to seed the display.
- **Dependencies:** Steps 1.1-1.2.
- **Risk:** Low.

#### Step 3.2: Verify `_seedFromCatalog` synthetic messages (`frontend/src/main.js`)

- **Action:** No code change needed. The `_seedFromCatalog` function sends synthetic `state_update` messages which will still call `updateSatellitePosition()`. Since no `track_update` has arrived yet at seed time, entities will be created with `ConstantPositionProperty` (static). Once the WebSocket connect handler sends the initial `track_update` burst (step 1.5) or the first processing cycle runs, entities will animate. This is acceptable -- there is a brief static period between catalog seed and first track_update.
- **Why:** Validates that the existing catalog seed path is not broken.
- **Dependencies:** Step 2.3.
- **Risk:** Low.

## WebSocket message schema: `track_update`

```json
{
    "type": "track_update",
    "norad_id": 25544,
    "epoch_utc": "2026-04-17T12:00:00Z",
    "samples": [
        {
            "epoch_utc": "2026-04-17T12:00:00Z",
            "eci_km": [-4078.12, 2451.89, 4892.33]
        },
        {
            "epoch_utc": "2026-04-17T12:01:00Z",
            "eci_km": [-4120.45, 2389.12, 4930.67]
        }
    ]
}
```

- `type`: Always `"track_update"`.
- `norad_id`: Integer NORAD catalog ID.
- `epoch_utc`: ISO-8601 UTC string, the filter's `last_epoch_utc` (start of the sample window).
- `samples`: Array of 60 objects (fewer if SGP4 propagation failed for some points).
  - `samples[].epoch_utc`: ISO-8601 UTC string for this sample point.
  - `samples[].eci_km`: 3-element array `[x, y, z]` in ECI J2000, kilometers.

**Estimated message size:** 60 samples x ~80 bytes/sample (JSON) + overhead = ~5 KB per object. 71 objects x 5 KB = ~355 KB per processing cycle. Well within WebSocket capacity.

## Test strategy

### Unit tests

- **`test_generate_track_samples`** (`tests/test_processing.py`):
  - Valid TLE, 60 samples, 60s spacing: verify 60 dicts returned with correct epoch spacing and 3-element `eci_km` lists.
  - Invalid TLE (decayed satellite): verify returns partial list without raising.
  - Edge case: `num_samples=0` returns empty list.
  - Edge case: `num_samples=1` returns exactly one sample at `start_epoch_utc`.

- **`test_track_update_message_in_process_single_object`** (`tests/test_processing.py`):
  - Cold start path: verify returned messages include a `track_update` message after the `state_update`.
  - Warm path (no anomaly): verify `track_update` is present.
  - Warm path (anomaly): verify `track_update` is present after anomaly + recalibration messages.
  - Duplicate epoch (skipped): verify empty messages list (no `track_update`).

- **`test_track_update_schema`** (`tests/test_processing.py`):
  - Verify `track_update` message has all required keys: `type`, `norad_id`, `epoch_utc`, `samples`.
  - Verify each sample has `epoch_utc` (valid ISO-8601) and `eci_km` (3-element list of floats).

### Frontend validation (manual)

- Open browser, run `admin/trigger-process`, verify satellites animate smoothly along orbital arcs.
- Verify no visible teleporting between processing cycles.
- Verify confidence colors still update correctly on `state_update`.
- Verify anomaly highlighting still works (billboard flashes yellow).
- Verify clicking a satellite still shows the info panel and draws historical/forward tracks.
- Verify the Cesium clock advances in real time (check `viewer.clock.currentTime` in console).
- Verify 30+ FPS with 71 animated entities (NF-003).

### Integration test

- Start backend, connect WebSocket, trigger processing cycle.
- Verify `track_update` messages appear in WebSocket stream (capture with browser dev tools Network tab or `scripts/test_ws_connect.py`).
- Verify message count: one `track_update` per processed object per cycle.

## Risks and mitigations

- **Risk: CPU cost of 4,260 propagations per cycle (~21s).** This runs in the synchronous processing loop on the asyncio event loop. For a 30-minute cycle, 21 seconds of blocking is 1.2% duty cycle -- acceptable for POC. **Mitigation (post-POC):** Vectorize astropy time conversions or move to `run_in_executor`. Add as tech debt TD-030.

- **Risk: WebSocket connect burst for 71 objects takes ~21s.** New clients would wait ~21 seconds before receiving track data. **Mitigation:** For POC, this is acceptable (demo starts the backend, then opens the browser). For production, pre-compute and cache track samples in `app.state` so the connect handler sends cached data instantly. Note as tech debt TD-031.

- **Risk: ECEF interpolation vs. ECI interpolation.** Samples are converted to ECEF at generation time; Cesium interpolates in ECEF. Over 60-second intervals, Earth rotates ~0.25 degrees, introducing ~sub-km error at LEO. Visually negligible. **Mitigation:** None needed for POC. Post-POC could use a `CallbackPositionProperty` that converts ECI-to-ECEF per-frame.

- **Risk: Ellipsoid does not animate (snap-per-cycle).** The uncertainty ellipsoid still uses `ConstantPositionProperty` and will visually lag the animated billboard between processing cycles. **Mitigation:** Acceptable for POC. The ellipsoid is translucent and large enough that the mismatch is not jarring. Post-POC could attach the ellipsoid to the same `SampledPositionProperty` as the billboard.

- **Risk: Entity created by catalog seed with ConstantPositionProperty, then track_update replaces it.** Brief static period (~seconds) between page load and first track_update. **Mitigation:** Acceptable. The NF-012 connect burst sends track_update data immediately after state data.

## Known limitations

1. **Ellipsoid snap-per-cycle:** The uncertainty ellipsoid updates position only on `state_update`, not continuously. It will visually detach from the animated billboard between cycles. Acceptable for POC demo.

2. **No velocity in interpolation:** `SampledPositionProperty` interpolates position only (Lagrange). Including velocity would enable Hermite interpolation for higher accuracy, but CesiumJS does not support this natively on `SampledPositionProperty`. Lagrange degree 5 with 60-second spacing is sufficient for smooth LEO visualization.

3. **Sample window is forward-only from filter epoch.** If the filter epoch is stale (e.g., TLE is hours old), the sample window starts in the past and may not cover "now". The Cesium clock will be at "now", which may be beyond the sample window. In this case, Cesium extrapolates (or shows nothing, depending on `forwardExtrapolationType`). **Decision:** Set `forwardExtrapolationType = Cesium.ExtrapolationType.HOLD` on the `SampledPositionProperty` so the entity holds at its last sample position rather than disappearing. This is better than nothing for stale objects. The `backwardExtrapolationType` should be `HOLD` as well.

4. **Processing cycle latency:** Track samples are generated after Kalman filter update, adding ~0.3s per object. Total cycle time increases from ~X to ~X+21s. Not user-visible (cycle runs in background).

## Open questions

None. All design decisions have been made per the user's specifications at the top of this plan.
