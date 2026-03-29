# Implementation Plan: Anomaly History, Historical Ground Track, and Predictive Uncertainty Cone
Date: 2026-03-29
Status: Draft

## Summary

This plan adds three related features to the n-body SSA platform: (1) a per-object anomaly history panel in the frontend populated by a new REST endpoint, (2) a historical ground track drawn on the CesiumJS globe for the selected object using SGP4 back-propagation, and (3) a forward predictive track with a widening uncertainty corridor derived from filter covariance. Features 1 and 2 are independent; Feature 3 extends the track endpoint from Feature 2.

## Requirements addressed

- **F-035**: Store all anomaly events with NORAD ID, detection epoch, anomaly type, NIS value, recalibration duration -- the anomaly history endpoint reads this existing data.
- **F-041**: `GET /object/{norad_id}/history` -- this plan extends the object history surface by adding a dedicated anomaly history endpoint and a track endpoint.
- **F-050 [DEMO]**: 3D globe with real-time positions -- ground track and uncertainty cone enhance this.
- **F-052 [DEMO]**: Uncertainty ellipsoid visualization -- the forward uncertainty cone is the temporal extension of this.
- **F-056**: Globe click selection updates per-object panels -- anomaly history section added to the info panel on selection.

## Files affected

### Backend
- `backend/main.py` -- Add two new REST endpoints: `GET /object/{norad_id}/anomalies` and `GET /object/{norad_id}/track`.
- `backend/propagator.py` -- No changes. Existing `propagate_tle()` is sufficient for track generation.
- `backend/kalman.py` -- No changes. `get_state()` already exposes `covariance_km2`.
- `backend/anomaly.py` -- No changes. The `alerts` table schema already stores all needed fields.

### Frontend
- `frontend/src/globe.js` -- Add functions: `drawHistoricalTrack()`, `drawPredictiveTrackWithCone()`, `clearTrackAndCone()`. Export `eciToEcefCartesian3` (currently module-private).
- `frontend/src/main.js` -- On object selection (globe click or alert card click), fetch `/object/{norad_id}/anomalies` and `/object/{norad_id}/track`, render anomaly history in info panel, call globe track drawing functions. On deselection, clear track and cone.
- `frontend/index.html` -- Add CSS for anomaly history section within `#object-info-panel` (scrollable list, anomaly type badges, resolved/unresolved status styling). Add CSS for no new DOM containers (track and cone are Cesium entities, not DOM elements).

### Tests
- `tests/test_anomaly_history_endpoint.py` -- New: test the anomaly history REST endpoint.
- `tests/test_track_endpoint.py` -- New: test the track REST endpoint with back/forward propagation and covariance growth.

## Data flow changes

### Before
```
Globe click -> selectedNoradId set -> _showObjectInfoPanel reads latestStateMap (in-memory)
                                   -> selectObject updates residual chart
```

### After
```
Globe click -> selectedNoradId set -> _showObjectInfoPanel reads latestStateMap (in-memory)
                                   -> fetch GET /object/{norad_id}/anomalies -> render history in info panel
                                   -> fetch GET /object/{norad_id}/track?seconds_back=1500&seconds_forward=1500
                                       -> render historical track polyline on globe
                                       -> render forward track polyline + uncertainty corridor on globe
                                   -> selectObject updates residual chart
```

The new data flow adds two REST calls on selection. Both are read-only and hit SQLite (anomaly) or compute on the fly (track). No WebSocket changes. No writes.

## Implementation steps

### Phase 1: Anomaly History Endpoint (Feature 1)

#### Step 1.1: Add `GET /object/{norad_id}/anomalies` endpoint (`backend/main.py`)

- **Action**: Add a new FastAPI route below the existing `/object/{norad_id}/history` endpoint. The route handler:
  1. Validates `norad_id` is in the catalog (same pattern as `get_object_history`).
  2. Executes a SELECT query against the `alerts` table.
  3. Returns a list of anomaly event dicts.

- **SQLite query**:
  ```sql
  SELECT
      id,
      norad_id,
      detection_epoch_utc,
      anomaly_type,
      nis_value,
      resolution_epoch_utc,
      recalibration_duration_s,
      status
  FROM alerts
  WHERE norad_id = ?
  ORDER BY detection_epoch_utc DESC
  LIMIT 20
  ```
  Note: The `alerts` table schema (defined in `anomaly.py` `ensure_alerts_table`, line 56) has columns: `id`, `norad_id`, `detection_epoch_utc`, `anomaly_type`, `nis_value`, `resolution_epoch_utc`, `recalibration_duration_s`, `status`, `created_at`. The endpoint selects all fields needed by the frontend. `resolution_epoch_utc` is nullable (NULL when unresolved, populated by `record_recalibration_complete`).

- **Response shape** (per event):
  ```json
  {
    "id": 42,
    "norad_id": 25544,
    "detection_epoch_utc": "2026-03-28T19:00:00+00:00",
    "anomaly_type": "maneuver",
    "nis_value": 18.7,
    "resolution_epoch_utc": "2026-03-28T19:30:00+00:00",
    "recalibration_duration_s": 1800.0,
    "status": "resolved"
  }
  ```
  When unresolved: `resolution_epoch_utc` is `null`, `recalibration_duration_s` is `null`, `status` is `"active"` or `"recalibrating"`.

- **Why**: The existing `/object/{norad_id}/history` endpoint returns alerts but does not include `resolution_epoch_utc` or `recalibration_duration_s`. A dedicated endpoint avoids changing the existing endpoint's contract and returns the specific fields needed for the anomaly history UI.

- **Dependencies**: None.
- **Risk**: Low. Read-only SELECT on an existing indexed table.

#### Step 1.2: Frontend anomaly history section in info panel (`frontend/src/main.js`, `frontend/index.html`)

- **Action** in `main.js`:
  1. Add an async function `_fetchAnomalyHistory(baseUrl, noradId)` that calls `GET /object/{norad_id}/anomalies` and returns the JSON array (or empty array on error).
  2. Modify `_showObjectInfoPanel(noradId)` to:
     a. Call `_fetchAnomalyHistory(backendBaseUrl, noradId)`.
     b. Append a scrollable "Anomaly History" section below the existing info rows.
     c. Each entry renders: anomaly type badge (reuse CSS classes from alerts panel: `.alert-type-badge.maneuver`, `.alert-type-badge.drag_anomaly`, `.alert-type-badge.filter_divergence`), detection epoch formatted as `YYYY-MM-DD HH:MM UTC`, NIS value to 1 decimal, and a resolved/unresolved indicator with resolution epoch if available.
  3. If the fetch returns an empty array, show "No anomaly history" text.
  4. Note: `_showObjectInfoPanel` is currently synchronous. It must become async (or the anomaly history section can be appended after the initial render via a `.then()` chain to avoid blocking the panel display). The recommended approach: render the static info immediately, then fetch anomaly history and append it when the response arrives. This avoids a visible delay for the core info fields.

- **Action** in `index.html`:
  1. Add CSS for `.anomaly-history-section` inside `#object-info-panel`: max-height 180px, overflow-y auto, border-top 1px solid #333, margin-top 6px, padding-top 6px.
  2. Add CSS for `.anomaly-history-entry`: font-size 11px, line-height 1.5, padding 3px 0, border-bottom 1px solid #222.
  3. Add CSS for `.anomaly-resolved` (color #66cc66) and `.anomaly-unresolved` (color #ff6666).

- **Why**: The user requirement specifies anomaly history visible in the object info panel on click.
- **Dependencies**: Step 1.1 (endpoint must exist).
- **Risk**: Low. Pure frontend rendering of fetched data.

---

### Phase 2: Historical Ground Track (Feature 2)

#### Step 2.1: Add `GET /object/{norad_id}/track` endpoint (`backend/main.py`)

- **Action**: Add a new FastAPI route. Query parameters:
  - `seconds_back: int = 1500` -- how many seconds into the past to propagate.
  - `seconds_forward: int = 0` -- how many seconds into the future to propagate (Phase 3 extends this).
  - `step_s: int = 60` -- time step between track points in seconds (increased from 30 per decision 1; add TD item for UI configurability).

  The handler:
  1. Validates `norad_id` is in the catalog.
  2. Retrieves the latest cached TLE via `ingest.get_latest_tle(app.state.db, norad_id)`. Returns 404 if no TLE cached.
  3. Determines the reference epoch: uses the filter state's `last_epoch_utc` if the filter is initialized for this object (from `app.state.filter_states`), otherwise uses the TLE epoch parsed via `propagator.tle_epoch_utc(tle_line1)`.
  4. Generates backward track points: for `t` in `range(-seconds_back, 0, step_s)` (inclusive of `-seconds_back`, exclusive of `0`), plus `t=0` (the reference epoch itself):
     - Compute `point_epoch = reference_epoch + timedelta(seconds=t)`.
     - Call `propagator.propagate_tle(tle_line1, tle_line2, point_epoch)` to get `(position_eci_km, velocity_eci_km_s)`.
     - Append `{"epoch_utc": point_epoch.isoformat(), "eci_km": position_eci_km.tolist()}` to the backward list.
  5. Generates forward track points (for Phase 3; returns empty list when `seconds_forward=0`): for `t` in `range(step_s, seconds_forward + step_s, step_s)`:
     - Same propagation as above.
     - Additionally, compute uncertainty at each forward point (see Phase 3, Step 3.1).
  6. Returns JSON:
     ```json
     {
       "norad_id": 25544,
       "reference_epoch_utc": "2026-03-28T19:00:00+00:00",
       "step_s": 30,
       "backward_track": [
         {"epoch_utc": "...", "eci_km": [x, y, z]},
         ...
       ],
       "forward_track": []
     }
     ```

- **Coordinate frame note**: The track points are in ECI J2000 km, consistent with the system's internal frame. The frontend (globe.js) performs the ECI-to-ECEF conversion using the existing `eciToEcefCartesian3` function, which requires the epoch for each point to compute GMST. This is why each track point includes its `epoch_utc`.

- **ECI-to-Cesium conversion decision**: Conversion happens in the **frontend**, not the backend. Rationale:
  1. The backend's architectural rule (architecture.md section 3.2) states "Conversions to ECEF or geodetic happen only at the API boundary." However, the API boundary is the REST response -- the backend returns ECI, the frontend converts. This is consistent with how satellite positions currently work (WebSocket sends ECI, globe.js converts).
  2. The frontend already has `eciToEcefCartesian3()` which handles the GMST rotation per epoch. Reusing this is simpler than adding a new backend conversion function for 100+ points.
  3. The backend `propagator.eci_to_geodetic()` function exists but returns lat/lon/alt, not Cesium Cartesian3. Converting to Cartesian3 requires Cesium APIs only available in the browser.

- **Performance note**: 100 points (50 back + 50 forward) each calling `propagator.propagate_tle()` involves 100 SGP4 propagations + 100 astropy TEME-to-GCRS transforms. The astropy transform is the bottleneck (~5-10ms per call). Total: ~500ms-1s. This is acceptable for a user-initiated click action (not a real-time loop). If this proves too slow, a mitigation is to batch the astropy transforms using vectorized `Time` arrays -- but this is a post-POC optimization.

- **Why**: Ground track visualization requires propagated positions at multiple epochs. The endpoint centralizes this computation on the backend where SGP4 and astropy are available.
- **Dependencies**: None (uses existing `propagator.propagate_tle` and `ingest.get_latest_tle`).
- **Risk**: Medium. The 100-point propagation may take ~1s. Mitigation: the frontend shows a loading indicator or renders the track asynchronously. The step interval (30s) and total window (1500s) are configurable via query params so the caller can reduce point count if needed.

#### Step 2.2: Export `eciToEcefCartesian3` from globe.js (`frontend/src/globe.js`)

- **Action**: Add `export` keyword to the existing `eciToEcefCartesian3` function declaration (line 51). Currently it is a module-level function without `export`. The function must be accessible to main.js for converting track points.

  Actually, on re-reading the code: `eciToEcefCartesian3` is used only within globe.js by `updateSatellitePosition` and `updateUncertaintyEllipsoid`. The track drawing functions (Step 2.3) will also live in globe.js, so they can call it directly as a module-private function. **Decision: do NOT export it.** The track drawing functions will be added to globe.js and will call `eciToEcefCartesian3` internally. main.js will call the exported track drawing functions and pass the raw ECI track data.

- **Why**: Keeps the ECI-to-ECEF conversion encapsulated in globe.js (the only module that should know about Cesium coordinate systems).
- **Dependencies**: None.
- **Risk**: Low.

#### Step 2.3: Add track drawing functions to globe.js (`frontend/src/globe.js`)

- **Action**: Add three new exported functions:

  1. `drawHistoricalTrack(viewer, trackPoints)`:
     - `trackPoints` is an array of `{epoch_utc, eci_km}` objects (the `backward_track` from the endpoint).
     - Converts each point to ECEF Cartesian3 using `eciToEcefCartesian3(point.eci_km, point.epoch_utc)`.
     - Builds an array of Cartesian3 positions.
     - Adds a polyline entity to the viewer:
       ```js
       viewer.entities.add({
           id: 'track-historical',
           polyline: {
               positions: cartesian3Array,
               width: 2,
               material: Cesium.Color.CYAN.withAlpha(0.6),
               clampToGround: false,
           }
       });
       ```
     - Stores a reference in a module-level variable `currentTrackEntity` for cleanup.

  2. `drawPredictiveTrackWithCone(viewer, forwardTrackPoints)`:
     - `forwardTrackPoints` is an array of `{epoch_utc, eci_km, uncertainty_radius_km}` objects.
     - Converts each point to ECEF Cartesian3.
     - Draws the nominal forward track as a polyline:
       ```js
       viewer.entities.add({
           id: 'track-forward',
           polyline: {
               positions: cartesian3Array,
               width: 2,
               material: new Cesium.PolylineDashMaterialProperty({
                   color: Cesium.Color.ORANGE.withAlpha(0.7),
                   dashLength: 16,
               }),
               clampToGround: false,
           }
       });
       ```
     - Draws the uncertainty cone as a `corridor` entity:
       ```js
       viewer.entities.add({
           id: 'track-cone',
           corridor: {
               positions: cartesian3Array,
               width: widthsArray,  // See note below
               material: Cesium.Color.ORANGE.withAlpha(0.3),
               cornerType: Cesium.CornerType.ROUNDED,
           }
       });
       ```
       **Cesium CorridorGeometry limitation**: `Cesium.CorridorGraphics` accepts a single `width` value, not a per-vertex width array. To create a widening corridor, the implementer must use one of these approaches (in order of preference):
       - **Option A (recommended for POC)**: Draw multiple short corridor segments, each with increasing width. For 50 forward points, create ~10 corridor segments (every 5 points), each with `width = 2 * uncertainty_radius_km * 1000` (meters) at that segment's midpoint. This produces a visually stepped but clearly widening cone.
       - **Option B**: Use a `polylineVolume` entity which supports a varying cross-section shape. More complex but produces a smooth cone.
       - **Option C**: Use individual translucent circle entities (billboards or ellipses) at each forward point, sized by uncertainty radius. Simpler but less visually connected.

       The implementer should use **Option A** unless they encounter a technical blocker, in which case **Option C** is the fallback.

     - Stores references for cleanup.

  3. `clearTrackAndCone(viewer)`:
     - Removes entities with IDs starting with `'track-'` from the viewer.
     - Resets module-level references.

- **Why**: Cesium entity creation and management belongs in globe.js per the module separation.
- **Dependencies**: Step 2.1 (endpoint provides the data).
- **Risk**: Medium. The Cesium corridor width limitation requires a workaround. The segmented approach (Option A) is straightforward but produces a stepped visual. Acceptable for POC.

#### Step 2.4: Wire track fetching to selection handler (`frontend/src/main.js`)

- **Action**:
  1. Import `drawHistoricalTrack`, `drawPredictiveTrackWithCone`, `clearTrackAndCone` from `globe.js`.
  2. Add an async function `_fetchAndDrawTrack(noradId)`:
     a. Call `clearTrackAndCone(viewer)` to remove any existing track.
     b. Fetch `GET /object/{norad_id}/track?seconds_back=1500&seconds_forward=1500`.
     c. On success, call `drawHistoricalTrack(viewer, response.backward_track)`.
     d. If `response.forward_track` has entries, call `drawPredictiveTrackWithCone(viewer, response.forward_track)`.
  3. In the `setupSelectionHandler` callback (currently at line 424-430 of main.js), after setting `selectedNoradId`, call `_fetchAndDrawTrack(noradId)` when `noradId` is not null, or `clearTrackAndCone(viewer)` when null.
  4. In the alert card click handler (currently at line 120-124 of main.js, the `onSelect` callback passed to `addAlert`), also call `_fetchAndDrawTrack(clickedId)`.

- **Why**: main.js owns user interaction routing. Track visualization is triggered by selection events.
- **Dependencies**: Steps 2.1, 2.3.
- **Risk**: Low.

---

### Phase 3: Predictive Track with Uncertainty Cone (Feature 3)

#### Step 3.1: Extend track endpoint for forward propagation with covariance growth (`backend/main.py`)

- **Action**: Extend the track endpoint handler (from Step 2.1) to populate `forward_track` when `seconds_forward > 0`:
  1. For each forward time step `t` (in seconds from reference epoch):
     - Propagate TLE to `reference_epoch + timedelta(seconds=t)` using `propagator.propagate_tle()`.
     - Compute uncertainty radius:
       a. If filter state exists for this object (`norad_id in app.state.filter_states`):
          - Get current covariance diagonal: `P = filter_state["covariance_km2"]`, extract `[P[0,0], P[1,1], P[2,2]]`.
          - Get process noise diagonal: `Q = filter_state["q_matrix"]`, extract `[Q[0,0], Q[1,1], Q[2,2]]`.
          - Grow covariance linearly with time: `sigma2_grown = [P[i,i] + Q[i,i] * (t / dt_nominal) for i in 0,1,2]` where `dt_nominal = 1800.0` (30-minute nominal update interval in seconds, matching the Q calibration assumption).
          - Uncertainty radius (3-sigma): `radius_km = 3.0 * sqrt(max(sigma2_grown))`.
       b. If no filter state: use a default growing uncertainty. Start at 1 km, grow linearly at 0.5 km per 300 seconds: `radius_km = 1.0 + 0.5 * (t / 300.0)`.
     - Append to forward track:
       ```json
       {
         "epoch_utc": "...",
         "eci_km": [x, y, z],
         "uncertainty_radius_km": 15.7
       }
       ```

  **Covariance growth model justification**: The Q matrix represents process noise accumulated over the nominal update interval (~1800s). Scaling Q linearly by `t / dt_nominal` is a first-order approximation that assumes Q captures unmodeled acceleration variance per unit time. This is the same simplification used by the UKF predict step (TD-007 notes that SGP4 causes all sigma points to collapse, so covariance growth is dominated by Q). The linear growth produces the visually widening cone required for the demo.

- **Why**: The forward track with growing uncertainty is the "funding moment" visual -- it demonstrates why continuous recalibration matters.
- **Dependencies**: Step 2.1 (the endpoint structure).
- **Risk**: Medium. The covariance growth model is a rough approximation. For the demo, the visual effect (widening cone) is more important than mathematical rigor. The cone must visibly widen -- if the current Q values produce a cone that is too narrow to see or too wide to be meaningful, the `dt_nominal` divisor or a display-scaling factor may need tuning.

#### Step 3.2: Forward track rendering already wired (no additional step)

- **Action**: No additional code needed. Step 2.4 already calls `drawPredictiveTrackWithCone(viewer, response.forward_track)` when forward track data is present. Step 2.3 defines the rendering function. The only dependency is that the endpoint returns `uncertainty_radius_km` in each forward track point (Step 3.1).

---

## Test strategy

### Unit tests

#### `tests/test_anomaly_history_endpoint.py`
- Test `GET /object/{norad_id}/anomalies` with an empty alerts table returns `[]`.
- Test with 3 inserted anomaly records returns them in descending epoch order, limited to 20.
- Test with a resolved anomaly includes `resolution_epoch_utc` and `recalibration_duration_s`.
- Test with invalid NORAD ID returns 404.
- Test that the endpoint performs no writes (read-only).

#### `tests/test_track_endpoint.py`
- Test `GET /object/{norad_id}/track?seconds_back=300&seconds_forward=0&step_s=60` returns 6 backward points (including reference epoch) with valid ECI coordinates.
- Test that all returned positions have magnitude consistent with LEO altitude (~6500-7200 km from Earth center).
- Test `seconds_forward=300` returns forward points with `uncertainty_radius_km` field present and increasing monotonically.
- Test with no cached TLE returns 404.
- Test with invalid NORAD ID returns 404.
- Test that returned `epoch_utc` strings are valid ISO-8601 and span the expected time range.

### Integration test
- Start backend with seed data, inject a maneuver via `seed_maneuver.py`, then:
  1. `GET /object/{norad_id}/anomalies` should return at least one anomaly event.
  2. `GET /object/{norad_id}/track?seconds_back=1500&seconds_forward=1500` should return a non-empty forward track with growing uncertainty.

### Frontend manual test
- Click a satellite on the globe. Verify:
  1. Info panel shows anomaly history section (or "No anomaly history" if none).
  2. Cyan historical track polyline appears on the globe behind the satellite.
  3. Orange dashed forward track appears ahead of the satellite.
  4. Orange translucent corridor visibly widens along the forward track.
  5. Clicking a different satellite replaces the track. Clicking empty space clears it.

---

## Risks and mitigations

- **Risk (Medium)**: SGP4 + astropy propagation for 100 points may take 500ms-1s, causing a noticeable delay on click.
  **Mitigation**: Render the info panel immediately, then fetch track data async. If >1s is measured, reduce default step to 60s (25 back + 25 forward = 50 points). The `step_s` query parameter allows runtime tuning without code changes.

- **Risk (Medium)**: Cesium `CorridorGraphics` does not support per-vertex width. The segmented corridor approach (Option A) may look visually rough.
  **Mitigation**: Use 10 segments (each spanning 5 forward points / 150 seconds) for a reasonable balance between smoothness and entity count. If the visual is unacceptable, fall back to Option C (individual translucent circles). Both approaches produce the "widening uncertainty" visual.

- **Risk (Low)**: The covariance growth model (`P + Q * t/dt_nominal`) may produce unrealistically large or small uncertainty values depending on the object's Q matrix.
  **Mitigation**: Clamp the rendered corridor width to a minimum of 1 km and maximum of 500 km to prevent visual artifacts. The implementer should test with ISS (active satellite) and a debris object to verify the visual range is reasonable.

- **Risk (Low)**: ECI-to-ECEF conversion in globe.js uses simplified GMST (Vallado IAU-1982). For a 1500-second track, the GMST approximation error accumulates slightly but remains sub-kilometer for LEO -- negligible for visualization.
  **Mitigation**: None needed for POC.

- **Risk (Low)**: The `alerts` table is queried with `ORDER BY detection_epoch_utc DESC LIMIT 20`. The existing index `idx_alerts_norad_status` covers `(norad_id, status)` but not `detection_epoch_utc`. For 20 objects with a few anomalies each, this is fast. For production scale, an index on `(norad_id, detection_epoch_utc DESC)` would be needed.
  **Mitigation**: Acceptable for POC. Note in tech debt if performance degrades.

## Constraint verification

- **ECI J2000**: All track points are generated by `propagator.propagate_tle()` which outputs ECI J2000 (via TEME-to-GCRS transform). Conversion to ECEF happens only in the frontend's `eciToEcefCartesian3()`. No frame mixing.
- **UTC timestamps**: All epoch strings in the response are ISO-8601 UTC (from `datetime.isoformat()` with UTC tzinfo). Frontend parses with `Date.parse()` which handles the `Z` and `+00:00` suffixes.
- **Units**: Position in km (`eci_km`), uncertainty radius in km (`uncertainty_radius_km`), time in seconds (`step_s`, `seconds_back`, `seconds_forward`). Variable names are suffixed per convention.
- **ingest.py boundary**: The track endpoint calls `ingest.get_latest_tle()` to read the TLE cache. It does not call Space-Track.org directly. No bypass of the ingest boundary.
- **No new dependencies**: All backend computation uses existing `propagator`, `kalman`, and `numpy`. Frontend uses existing CesiumJS Entity API (not CZML, consistent with TD-024's current POC approach).
- **No credentials in source**: No credential handling involved in these features.

## Decisions (resolved 2026-03-29)

1. **Track point count**: Increase default step to **60s** (25 back + 25 forward = 50 total SGP4 calls per click). Add a tech debt entry for making `step_s` user-configurable via a UI control (post-POC). The query parameter already supports runtime tuning.

2. **Corridor visual approach**: Implement **Option A** (segmented corridors) first. If the stepped visual is insufficient, try **Option B** (polylineVolume) before revising the plan — Option B is likely closer to the intended visual. Do not fall back to Option C unless both A and B are blocked. Document which option was used in a code comment.

3. **Info panel height**: Verify on a 1080p display with 20 history entries and adjust `max-height` as needed. No pre-determined value — measure first.
