# Implementation Plan: Conjunction Risk Analysis on Anomaly Detection
Date: 2026-03-29
Status: Draft

## Summary
When the system detects any anomaly, this feature screens the anomalous object's post-recalibration predicted trajectory against all other tracked objects to identify first-order conjunction risks (within 5 km) and second-order risks (within 10 km of first-order objects). Results are broadcast via a new `conjunction_risk` WebSocket message type, persisted to SQLite, and displayed on the globe (color overrides) and in enriched alert cards.

## Requirements addressed
- **F-030 [DEMO]**: Anomaly detection triggers conjunction screening as a follow-on action.
- **F-033 [DEMO]**: Screening uses post-recalibration state (not diverged state).
- **F-042 [DEMO]**: New `conjunction_risk` message type on WebSocket `/ws/live`.
- **F-043**: Message conforms to a documented JSON schema (new type added to architecture).
- **F-050 [DEMO]**: At-risk objects visually highlighted on globe.
- **F-055 [DEMO]**: Conjunction risk data appended to anomaly alert cards.
- **F-011**: All screening positions in ECI J2000 km (enforced by `propagator.propagate_tle`).
- **POST-004**: This feature is a simplified precursor to the full Pc-based conjunction assessment. The spherical miss-distance approach is explicitly a POC simplification (see new TD-027, TD-028).

## Files affected
- `backend/conjunction.py` (NEW) -- Pure synchronous screening algorithm module.
- `backend/main.py` -- Wire async conjunction screening task after anomaly detection by extracting inputs from existing `filter_states` and `tle_record` data (no changes to `processing.py` interface). New SQLite tables, new REST endpoint, broadcast `conjunction_risk` messages.
- `frontend/src/globe.js` -- New `conjunctionRiskMap`, `applyConjunctionRisk()`, `clearConjunctionRisk()` functions for color overrides.
- `frontend/src/alerts.js` -- New `updateAlertConjunctions()` function to append conjunction section to existing alert cards in-place (same pattern as `_appendAnomalyHistorySection` in `main.js`). Visual pulse on card if scrolled out of view.
- `frontend/src/main.js` -- Route new `conjunction_risk` WS message type; call globe and alerts handlers; on `state_update` for a flagged object, clear conjunction risk and show dismissible toast notification.
- `frontend/index.html` -- CSS for conjunction risk display elements and toast notification.
- `docs/tech-debt.md` -- Add TD-027 (RSW pizza-box screening) and TD-028 (debris cloud extension).
- `tests/test_conjunction.py` (NEW) -- Unit tests for `conjunction.py`.
- `tests/test_conjunction_endpoint.py` (NEW) -- Integration tests for `GET /object/{norad_id}/conjunctions`.

## Data flow changes

### Before
```
anomaly detected -> recalibrate filter -> broadcast anomaly + recalibration WS messages -> done
```

### After
```
anomaly detected -> recalibrate filter -> broadcast anomaly + recalibration WS messages
    |
    +-> main.py detects anomaly messages in the returned list (type == "anomaly")
    |   and extracts conjunction screening inputs from data already available
    |   in _process_single_object scope: filter_states[norad_id], tle_record
    |
    +-> main.py schedules asyncio.create_task(_run_conjunction_screening(...))
         |
         +-> calls conjunction.screen_conjunctions() (synchronous, CPU-bound)
         +-> persists results to conjunction_events + conjunction_risks tables
         +-> broadcasts conjunction_risk WS message to all clients

Frontend auto-clear flow (new):
    state_update arrives for norad_id in conjunctionRiskMap
        -> clearConjunctionRisk(viewer, [norad_id, ...all flagged ids for that screening])
        -> show toast: "Conjunction risk cleared -- [name] [epoch UTC]"
        -> toast auto-dismisses after 8 seconds
```

The conjunction screening inputs are extracted in `main.py._process_single_object` from data already in scope:
- `norad_id`: function parameter
- `tle_record["tle_line1"]`, `tle_record["tle_line2"]`: from the `tle_record` already fetched by `_process_single_object`
- `filter_states`: function parameter (read-only snapshot of TLE lines per object)
- `app.state.catalog_entries`: for name lookup

**Key design decision:** `processing.py` return type is NOT changed. The `process_single_object` function continues to return `list[dict]`. The `main.py._process_single_object` wrapper already has access to all needed data (`filter_states`, `tle_record`, `app.state.catalog_entries`) and can detect anomalies by checking if any returned message has `type == "anomaly"`. This avoids breaking `replay.py` and is the least-invasive wiring point.

## Implementation steps

### Phase 1: Backend -- conjunction.py (new module)

1. **Create `backend/conjunction.py`** (`backend/conjunction.py`)
   - Action: Create a new module with the following public functions. The module must be pure synchronous (no asyncio, no FastAPI imports). It imports only `propagator`, `numpy`, `datetime`, and `logging`.
   - Why: Isolate the screening algorithm so it is testable independently and callable from both sync and async contexts.
   - Dependencies: None (new file).
   - Risk: Low

   Constants to define:
   - `SCREENING_HORIZON_S: int = 5400` -- one LEO orbit
   - `SCREENING_STEP_S: int = 60` -- 60-second propagation steps (90 points)
   - `FIRST_ORDER_THRESHOLD_KM: float = 5.0`
   - `SECOND_ORDER_THRESHOLD_KM: float = 10.0`

   Functions:

   a. `generate_trajectory_eci_km(tle_line1: str, tle_line2: str, start_epoch_utc: datetime.datetime, horizon_s: int, step_s: int) -> list[tuple[datetime.datetime, NDArray[np.float64]]]`
      - Propagate the TLE forward from `start_epoch_utc` at `step_s` intervals for `horizon_s` seconds.
      - Returns list of `(epoch_utc, position_eci_km)` tuples (position only, 3-element array).
      - Uses `propagator.propagate_tle()` for each step. Catches `ValueError` per step and skips failed points with a warning log.

   b. `compute_min_distance_km(traj_a: list[tuple[datetime, NDArray]], traj_b: list[tuple[datetime, NDArray]]) -> tuple[float, datetime.datetime]`
      - Assumes both trajectories have the same time steps (same length, same epochs).
      - For each matching time step, compute Euclidean distance in ECI km.
      - Returns `(min_distance_km, time_of_closest_approach_utc)`.
      - If trajectories have different lengths (due to skipped points), use the shorter length.

   c. `screen_conjunctions(anomalous_norad_id: int, anomalous_tle_line1: str, anomalous_tle_line2: str, screening_epoch_utc: datetime.datetime, other_objects: list[dict], catalog_name_map: dict[int, str]) -> dict`
      - `other_objects`: list of dicts with keys `norad_id`, `tle_line1`, `tle_line2` (one per non-anomalous tracked object with an initialized filter).
      - `catalog_name_map`: dict mapping norad_id to object name string.
      - Algorithm:
        1. Generate trajectory for the anomalous object.
        2. For each other object, generate trajectory and compute min distance.
        3. Classify first-order: min_distance_km <= FIRST_ORDER_THRESHOLD_KM.
        4. For each first-order object, check its trajectory against all remaining objects (not the anomalous one). Classify second-order: min_distance_km <= SECOND_ORDER_THRESHOLD_KM.
        5. Build and return result dict (schema below).
      - Returns a dict matching the `conjunction_risk` WS message schema:
        ```python
        {
            "type": "conjunction_risk",
            "anomalous_norad_id": int,
            "screening_epoch_utc": str,  # ISO-8601
            "horizon_s": 5400,
            "threshold_km": 5.0,
            "first_order": [
                {"norad_id": int, "name": str, "min_distance_km": float, "time_of_closest_approach_utc": str}
            ],
            "second_order": [
                {"norad_id": int, "name": str, "min_distance_km": float, "via_norad_id": int, "time_of_closest_approach_utc": str}
            ]
        }
        ```
   - Risk: Medium -- performance risk: 20 objects x 90 SGP4+astropy calls each = 1800 calls. At 5-10ms per call this is 9-18 seconds. This is why the call must be async (fire-and-forget). If this is too slow for the demo, a mitigation is to reduce `SCREENING_STEP_S` to 120 (halving calls) or to pre-cache trajectories. Document this in Risks section.

### Phase 2: Backend -- main.py wiring (no processing.py changes)

2. **Wire conjunction screening into `_process_single_object`** (`backend/main.py`)
   - Action: In `_process_single_object`, after calling `processing.process_single_object` and getting back the `messages` list, check if any message has `type == "anomaly"`. If so, extract conjunction screening inputs from data already in scope:
     - `anomalous_norad_id`: the `norad_id` parameter
     - `screening_epoch_utc`: parse from the anomaly message's `epoch_utc` field
     - `tle_line1`, `tle_line2`: from the `tle_record` already fetched at line 291 of the current `main.py`
     - `filter_states`: the `filter_states` parameter (for building `other_objects` list)
     - `catalog_entries`: from `app.state.catalog_entries` (for name lookup)
   - Then call `asyncio.get_event_loop().create_task(_run_conjunction_screening(app, screening_inputs))`.
   - Why: This approach requires NO changes to `processing.py` or its return type. The `_process_single_object` wrapper in `main.py` already has access to `tle_record` (line 291), `filter_states` (parameter), and `app.state.catalog_entries`. Detecting an anomaly is trivial: check `any(m.get("type") == "anomaly" for m in messages)`. This is the least-invasive wiring point and preserves full backward compatibility with `replay.py`.
   - Dependencies: Phase 1 (conjunction.py exists).
   - Risk: Low

3. **Wire conjunction screening into `admin_trigger_process`** (`backend/main.py`)
   - Action: In `admin_trigger_process`, after calling `processing.process_single_object` and iterating messages, check if any message has `type == "anomaly"`. If so, build the same screening_inputs dict as in step 2 and schedule the async task.
   - The `tle_record` is already available in the loop variable. `filter_states` is a local. `app.state.catalog_entries` is accessible.
   - Why: Same least-invasive approach as step 2.
   - Dependencies: Phase 1, step 2 (for the `_run_conjunction_screening` function).
   - Risk: Low

   **Note:** `scripts/replay.py` requires NO changes. It calls `processing.process_single_object` directly and that function's return type is unchanged (`list[dict]`). replay.py is a synchronous offline tool; the 9-18 second screening cost per event makes conjunction screening impractical in that context. Conjunction screening is a live-system feature only.

### Phase 3: Backend -- main.py async task and persistence

4. **Add SQLite tables for conjunction persistence** (`backend/main.py`)
   - Action: Add `_ensure_conjunction_tables(db)` function, called during lifespan startup (after `_ensure_state_history_table`).
   - Schema:
     ```sql
     CREATE TABLE IF NOT EXISTS conjunction_events (
         id                    INTEGER PRIMARY KEY AUTOINCREMENT,
         anomalous_norad_id    INTEGER NOT NULL,
         screening_epoch_utc   TEXT    NOT NULL,
         horizon_s             INTEGER NOT NULL,
         threshold_km          REAL    NOT NULL,
         created_at            TEXT    NOT NULL DEFAULT (datetime('now'))
     );
     CREATE INDEX IF NOT EXISTS idx_conjunction_events_norad
     ON conjunction_events (anomalous_norad_id);

     CREATE TABLE IF NOT EXISTS conjunction_risks (
         id                              INTEGER PRIMARY KEY AUTOINCREMENT,
         conjunction_event_id            INTEGER NOT NULL,
         risk_order                      INTEGER NOT NULL,  -- 1 or 2
         norad_id                        INTEGER NOT NULL,
         min_distance_km                 REAL    NOT NULL,
         time_of_closest_approach_utc    TEXT    NOT NULL,
         via_norad_id                    INTEGER,           -- NULL for first-order
         FOREIGN KEY (conjunction_event_id) REFERENCES conjunction_events(id)
     );
     CREATE INDEX IF NOT EXISTS idx_conjunction_risks_event
     ON conjunction_risks (conjunction_event_id);
     ```
   - Why: Persist conjunction results for the REST endpoint and audit trail.
   - Dependencies: None (schema creation is independent).
   - Risk: Low

5. **Add `_persist_conjunction_result` helper** (`backend/main.py`)
   - Action: Function that takes a `db` connection and a conjunction result dict, inserts into `conjunction_events` and `conjunction_risks`.
   - Signature: `def _persist_conjunction_result(db: sqlite3.Connection, result: dict) -> int` (returns conjunction_event_id).
   - Why: Encapsulate persistence logic for use by the async task.
   - Dependencies: Step 4.
   - Risk: Low

6. **Add async conjunction screening task** (`backend/main.py`)
   - Action: Define `async def _run_conjunction_screening(app: FastAPI, screening_inputs: dict) -> None`.
   - Behavior:
     1. Build `other_objects` list by iterating `app.state.filter_states` -- for each norad_id != anomalous_norad_id where `last_tle_line1` and `last_tle_line2` exist, include `{norad_id, tle_line1, tle_line2}`.
     2. Build `catalog_name_map` from `app.state.catalog_entries`.
     3. Call `conjunction.screen_conjunctions(...)` via `asyncio.get_event_loop().run_in_executor(None, ...)` to avoid blocking the event loop (CPU-bound work).
     4. Call `_persist_conjunction_result(app.state.db, result)`.
     5. Call `await ws_manager.broadcast(result)`.
   - The `screening_inputs` dict is built by `_process_single_object` (step 2) or `admin_trigger_process` (step 3) and contains:
     - `anomalous_norad_id`: int
     - `screening_epoch_utc`: str (ISO-8601, parsed from anomaly message)
     - `tle_line1`, `tle_line2`: str (from tle_record)
   - Error handling: Wrap entire body in try/except; log errors but do not crash the background task.
   - Why: The screening is too slow (~9-18s) to run synchronously. Fire-and-forget async task broadcasts when ready.
   - Dependencies: Steps 1, 4, 5.
   - Risk: Medium -- `run_in_executor` with default ThreadPoolExecutor is safe for CPU-bound numpy work but shares the thread pool with any `asyncio.to_thread()` calls (TD-016). For POC with one anomaly at a time, this is acceptable.

7. **Add REST endpoint `GET /object/{norad_id}/conjunctions`** (`backend/main.py`)
   - Action: New endpoint returning the last 5 conjunction screening results for a given NORAD ID.
   - Query: Join `conjunction_events` and `conjunction_risks` where `anomalous_norad_id = norad_id`, order by `conjunction_events.created_at DESC`, limit 5 events.
   - Response format: list of dicts, each matching the `conjunction_risk` WS message schema (reconstructed from DB rows).
   - Validation: Return 404 if norad_id not in catalog.
   - Why: Allows the frontend (or external tools) to query historical conjunction screenings.
   - Dependencies: Step 4.
   - Risk: Low

### Phase 4: Frontend -- globe conjunction highlighting

8. **Add conjunction risk map and functions to `globe.js`** (`frontend/src/globe.js`)
   - Action: Add module-level `const conjunctionRiskMap = new Map()` mapping norad_id to `'first_order'|'second_order'`.
   - Also add module-level `let lastConjunctionMessage = null` to store the most recent conjunction_risk message (needed by the auto-clear logic in main.js to know which norad_ids to clear).
   - Export new function `applyConjunctionRisk(viewer, conjunctionMessage)`:
     - Clear previous entries from `conjunctionRiskMap`.
     - Store `conjunctionMessage` in `lastConjunctionMessage`.
     - For each first-order object: set `conjunctionRiskMap.set(norad_id, 'first_order')`, find entity in `entityMap`, set billboard color to `Cesium.Color.RED`.
     - For each second-order object: set `conjunctionRiskMap.set(norad_id, 'second_order')`, set billboard color to `Cesium.Color.YELLOW`.
   - Export new function `clearConjunctionRisk(viewer, noradIds)`:
     - For each norad_id in the provided array: remove from `conjunctionRiskMap`, restore billboard color to confidence-based color using `confidenceColor()` with the entity's current confidence (requires looking up the latest state -- accept the confidence from the last state_update stored in the entity's properties, or default to 0.5).
     - Set `lastConjunctionMessage = null`.
   - Export new function `getConjunctionRiskMap()` that returns the `conjunctionRiskMap` (read-only access for main.js auto-clear logic).
   - Export new function `getLastConjunctionMessage()` that returns `lastConjunctionMessage` (for main.js to extract object name and epoch for the toast).
   - Modify `updateSatellitePosition`: after setting the confidence-based color, check `conjunctionRiskMap`. If the norad_id is in the map, override the color with red (first_order) or yellow (second_order) instead. This ensures conjunction highlighting persists across state_update messages until explicitly cleared.
   - Why: Globe visualization of at-risk objects is a key demo visual.
   - Dependencies: None (frontend-only).
   - Risk: Low

### Phase 5: Frontend -- alert card enrichment

9. **Add `updateAlertConjunctions` to `alerts.js`** (`frontend/src/alerts.js`)
    - Action: Export new function `updateAlertConjunctions(panelState, noradId, conjunctionMessage)`.
    - Behavior:
      - Find the active (non-resolved) alert card for the given norad_id in `panelState.alerts`.
      - If found, create a new `<div class="conjunction-section">` and append it to the alert card element, after the existing metrics row. This follows the same in-place append pattern used by `_appendAnomalyHistorySection` in `main.js`.
      - Content:
        - Header: "Conjunction Risk"
        - First-order list: for each entry, show NORAD ID, name, miss distance (km, 1 decimal), TCA (HH:MM:SS UTC).
        - Second-order list: same, with "via [name]" attribution.
        - If both lists are empty: "No conjunctions within 5 km / 10 km in next 90 min".
      - If the alert card already has a `.conjunction-section`, replace it (handles re-screening).
      - If the alert card has scrolled out of view in the side panel, apply a brief CSS animation pulse (e.g., `conjunction-pulse` class with a 1-second border glow) to draw attention. The implementer should try this in-place approach first and flag if the visual cue is too easy to miss during demo.
    - Why: Alert card enrichment is specified in the feature requirements. In-place update on the existing card is preferred over creating a new card.
    - Dependencies: None (frontend-only).
    - Risk: Low

### Phase 6: Frontend -- message routing and auto-clear

10. **Add `conjunction_risk` routing and auto-clear in `main.js`** (`frontend/src/main.js`)
    - Action: In `routeMessage()`, add an `else if (type === 'conjunction_risk')` branch.
    - Behavior:
      - Import and call `applyConjunctionRisk(viewer, message)` from `globe.js`.
      - Import and call `updateAlertConjunctions(panelState, message.anomalous_norad_id, message)` from `alerts.js`.
      - If `selectedNoradId` equals `message.anomalous_norad_id` or is in the first_order/second_order lists, refresh the info panel by calling `_showObjectInfoPanel(selectedNoradId)`.
    - Auto-clear on `state_update` (resolved decision 3):
      - In the existing `state_update` handler in `routeMessage()`, after processing the update, check if the incoming `norad_id` matches the `anomalous_norad_id` from the last conjunction screening (use `getLastConjunctionMessage()` from globe.js).
      - If it matches: call `clearConjunctionRisk(viewer, allFlaggedNoradIds)` where `allFlaggedNoradIds` is all keys from `getConjunctionRiskMap()`.
      - Show a toast notification at the top of the side panel: "Conjunction risk cleared -- [object name] [epoch UTC]". The toast auto-dismisses after 8 seconds via `setTimeout`. Implementation: create a `<div class="conjunction-toast">` element, insert it at the top of the side panel container, and remove it after 8 seconds. Use CSS transition for fade-out.
      - The object name comes from the last conjunction message's context or from the catalog data available in panelState. The epoch UTC comes from the `state_update` message's `epoch_utc` field.
    - **Note:** The trigger is ANY `state_update` for the anomalous object, not just anomaly-free ones. This means the very next processing cycle for that object clears the conjunction highlighting regardless of the object's anomaly status at that point.
    - Why: Routes the new message type to the correct handlers. Auto-clear provides a clean lifecycle for conjunction risk indicators.
    - Dependencies: Steps 8, 9.
    - Risk: Low

### Phase 7: Frontend -- CSS

11. **Add conjunction-related CSS to `index.html`** (`frontend/index.html`)
    - Action: Add styles for:
      - `.conjunction-section` -- section container within the alert card
      - `.conjunction-header` -- "Conjunction Risk" header text
      - `.conjunction-entry` -- individual risk entry row
      - `.conjunction-via` -- "via [name]" attribution text styling
      - `.conjunction-pulse` -- brief border glow animation for attention when card is out of view (1-second CSS keyframe animation)
      - `.conjunction-toast` -- toast notification at top of side panel (dark background, monospace, auto-fade-out transition)
    - Styling: match the existing alert panel aesthetic (dark background, monospace, color-coded by risk order: red for first-order, amber for second-order).
    - Why: Visual consistency with existing UI.
    - Dependencies: None.
    - Risk: Low

### Phase 8: Tech debt register

12. **Add TD-027 and TD-028 to `docs/tech-debt.md`** (`docs/tech-debt.md`)
    - Action: Add two new entries:
      - **TD-027: Replace spherical miss-distance with RSW pizza-box screening**
        - Priority: P2
        - Description: POC uses a simple Euclidean distance threshold (5 km / 10 km spherical) for conjunction screening. The standard DoD/NASA approach uses an asymmetric screening volume in the RSW (Radial-Along-Cross) frame: typically 1 km radial x 25 km along-track x 25 km cross-track. This accounts for the elongated uncertainty distribution along the orbit track.
        - Resolution path: Compute the RSW frame from the relative velocity vector at TCA. Transform the miss vector into RSW components. Apply asymmetric thresholds.
      - **TD-028: Extend conjunction screening to debris cloud scenarios**
        - Priority: P3
        - Description: Current screening considers only existing catalog objects. A breakup event generates a debris cloud that is not yet in the catalog. POST-005 (debris cloud evolution) would feed into conjunction screening.
        - Resolution path: Integrate with the fragmentation model from POST-005. Screen the ensemble of modeled debris particles against all catalog objects.
    - Dependencies: None.
    - Risk: Low

### Phase 9: Tests

13. **Create `tests/test_conjunction.py`** (`tests/test_conjunction.py`)
    - Action: Unit tests for `conjunction.py`:
      - `test_generate_trajectory_eci_km_returns_correct_count`: verify 90 points for 5400s at 60s steps.
      - `test_generate_trajectory_eci_km_all_eci_j2000`: verify all positions are 3-element arrays with magnitudes in LEO range (6400-7400 km).
      - `test_compute_min_distance_km_identical_trajectories`: two identical trajectories should return distance ~0.
      - `test_compute_min_distance_km_different_objects`: two objects in different orbits should return a positive distance.
      - `test_screen_conjunctions_no_risks`: anomalous object with all others far away returns empty first_order and second_order lists.
      - `test_screen_conjunctions_first_order_detected`: mock two objects on near-collision course, verify first_order is populated.
      - `test_screen_conjunctions_second_order_detected`: set up three objects where A is close to B and B is close to C, verify C appears in second_order with via_norad_id = B.
      - `test_screen_conjunctions_result_schema`: verify all required keys are present in the result dict.
      - `test_screen_conjunctions_timestamps_utc`: verify all epoch strings end with 'Z' or contain '+00:00'.
    - Note: Tests for first/second order detection can use real TLEs from the catalog (ISS and COSMOS debris entries are in the catalog.json) but should not rely on actual conjunctions. Instead, use synthetic TLEs for objects in the same orbital plane with slightly different mean anomalies to force close approaches.
    - Dependencies: Phase 1.
    - Risk: Low

14. **Create `tests/test_conjunction_endpoint.py`** (`tests/test_conjunction_endpoint.py`)
    - Action: Integration tests for `GET /object/{norad_id}/conjunctions`:
      - `test_conjunctions_endpoint_404_unknown_norad`: verify 404 for unknown NORAD ID.
      - `test_conjunctions_endpoint_empty_results`: verify empty list for an object with no conjunction events.
      - `test_conjunctions_endpoint_returns_persisted_results`: manually insert a conjunction_event and risks, then verify the endpoint returns them correctly.
      - `test_conjunctions_endpoint_limit_5`: insert 10 events, verify only the 5 most recent are returned.
    - Uses the existing FastAPI test client pattern from the codebase.
    - Dependencies: Phase 3.
    - Risk: Low

## Test strategy

### Unit tests
- **`conjunction.py`**: Pure function tests for trajectory generation, distance computation, and the full screening algorithm. Test edge cases: empty catalog (only one object), all objects far apart, all objects very close, trajectories with skipped points (SGP4 failure on some steps).
- **Mock strategy**: Use real TLEs from `data/catalog/catalog.json` for trajectory generation tests. For conjunction detection tests, construct synthetic TLEs (or mock `propagator.propagate_tle` to return controlled positions) to guarantee specific miss distances.

### Integration tests
- **REST endpoint**: Use FastAPI `TestClient` to verify the `GET /object/{norad_id}/conjunctions` endpoint against a test SQLite database with pre-inserted data.
- **WebSocket broadcast**: Manually verify in the browser that a `conjunction_risk` message appears when a maneuver is injected via `scripts/seed_maneuver.py`. Not automated for POC.

### Manual demo verification
1. Run `scripts/seed_maneuver.py --object 25544 --delta-v 0.5`
2. Run `POST /admin/trigger-process`
3. Verify in browser: anomaly alert appears, then (after 9-18 seconds) conjunction risk section appears in the alert card, and at-risk objects turn red/amber on the globe.
4. Wait for next `state_update` for NORAD 25544 -- verify conjunction risk highlighting clears from globe and toast notification appears at top of side panel, auto-dismissing after 8 seconds.

## Risks and mitigations

- **Risk**: SGP4+astropy propagation for 20 objects x 90 steps (~1800 calls) takes 9-18 seconds. -- **Mitigation**: Run in a thread pool executor (`run_in_executor`) so it does not block the event loop. For demo, this delay is acceptable (results appear ~15 seconds after anomaly). If too slow, reduce to 120-second steps (45 points, halving time). Document this as a tuning knob.

- **Risk**: `run_in_executor` shares the default thread pool. Multiple simultaneous anomalies could queue up screening tasks. -- **Mitigation**: For POC with 20 objects and 30-minute polling, simultaneous anomalies are unlikely. If needed, add a dedicated `ThreadPoolExecutor(max_workers=1)` for conjunction screening.

- **Risk**: Conjunction results could be broadcast after the anomaly alert card has already been resolved (if screening takes longer than one polling cycle). -- **Mitigation**: The frontend should handle `conjunction_risk` messages for already-resolved alerts gracefully (append the section anyway -- the conjunction data is still useful).

- **Risk**: SQLite write from the async task could conflict with writes from the processing loop. -- **Mitigation**: SQLite WAL mode (already enabled) allows concurrent readers with one writer. The conjunction persistence write is small and fast. If a write lock contention occurs, SQLite will retry with its default 5-second timeout.

- **Risk**: Auto-clear on first `state_update` may fire before the user has had time to read the conjunction risk data (if the next processing cycle is fast). -- **Mitigation**: The conjunction data persists in the alert card even after globe highlighting is cleared. The toast notification serves as a visual record. The 8-second toast duration provides a window for the user to notice. If this proves too aggressive during demo rehearsal, the implementer can add a minimum display duration (e.g., 30 seconds after conjunction_risk arrival) before allowing auto-clear.

- **Risk**: In-place alert card update with conjunction section may be too subtle if the card is scrolled out of view. -- **Mitigation**: CSS pulse animation on the card border provides a visual cue. The implementer should try this first and flag if it is insufficient during demo testing. Fallback: scroll the panel to the updated card.

## Open questions

All open questions have been resolved:

1. **RESOLVED: Should `scripts/replay.py` also trigger conjunction screening?**
   Decision: No. replay.py is a synchronous offline tool; the 9-18 second screening cost per event makes it impractical. The `processing.py` return type is NOT changed -- it continues to return `list[dict]`. Instead, `main.py._process_single_object` detects anomaly messages in the returned list (`type == "anomaly"`) and extracts conjunction screening inputs from data already in scope: `tle_record` (fetched at main.py line 291), `filter_states` (function parameter), and `app.state.catalog_entries`. This is the least-invasive wiring point. `replay.py` requires zero changes.

2. **RESOLVED: Should the `conjunction_risk` WS message include the anomaly_type that triggered the screening?**
   Decision: Deferred -- not included in the initial schema. The anomaly_type is available in the anomaly WS message that precedes the conjunction_risk message, so the frontend already has this context. Adding it later is a backward-compatible schema addition if needed.

3. **RESOLVED: Should `clearConjunctionRisk` be triggered by a timer or by a new event?**
   Decision: Triggered by `state_update`. When the next `state_update` message arrives for the anomalous object (any `state_update`, not just anomaly-free ones), the frontend clears all conjunction risk highlighting on the globe AND displays a toast notification at the top of the side panel: "Conjunction risk cleared -- [object name] [epoch UTC]". The toast auto-dismisses after 8 seconds via `setTimeout` with a CSS fade-out transition. No timer-based clearing is used.
