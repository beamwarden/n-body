# Implementation Plan: Frontend Improvements (Tooltips, Anomaly Markers, Alert Enrichment, Object Info Panel)
Date: 2026-03-29
Status: Draft

## Summary

Four frontend enhancements that improve the demo experience and close gaps between the architecture spec and current implementation. In priority order: (1) hover tooltips on chart data points, (2) anomaly event vertical markers on charts, (3) enriched alert cards with peak NIS/residual and accurate resolution time, and (4) an object info panel triggered by globe click or alert card click. All changes are vanilla JS, no new dependencies, no build step.

## Requirements addressed

- **F-054 [DEMO]**: Residual magnitude and NIS time-series chart -- tooltips add interactivity specified in architecture section 3.6.2 ("Interaction: Clicking an object on the globe cross-filters the residual charts to that object").
- **F-055 [DEMO]**: Anomaly alert feed with "object name, time, type, and resolution status" -- enrichment adds peak NIS, peak residual, and accurate resolution duration (currently hardcoded to 0s for some paths).
- **F-034**: Record time between anomaly detection and recalibration completion -- the frontend must display this accurately, not estimate it.
- **F-035**: Store anomaly events with NIS value -- the frontend must surface this to the user.
- **F-056**: Click-to-select on globe updates per-object panels -- the object info panel is a new per-object panel wired to the same selection mechanism.

## WS message fields relied on (cross-reference `backend/processing.py` `_build_ws_message`)

All four features consume the existing WS message schema. No backend changes required.

### state_update message fields used:
| Field | Type | Used by |
|---|---|---|
| `type` | `"state_update"` | Routing |
| `norad_id` | int | All features |
| `epoch_utc` | ISO-8601 string | Tooltip, info panel |
| `eci_km` | [x, y, z] float | Info panel |
| `eci_km_s` | [vx, vy, vz] float | Info panel |
| `covariance_diagonal_km2` | [sx2, sy2, sz2] float | Existing chart |
| `nis` | float | Tooltip |
| `innovation_eci_km` | [dx,dy,dz,dvx,dvy,dvz] float | Tooltip (residual magnitude = norm of [0:3]) |
| `confidence` | float | Info panel |
| `anomaly_type` | null | Routing |

### anomaly message fields used:
| Field | Type | Used by |
|---|---|---|
| `type` | `"anomaly"` | Routing |
| `norad_id` | int | Alert enrichment, anomaly marker |
| `epoch_utc` | ISO-8601 string | Anomaly marker x-position, alert card |
| `nis` | float | Alert enrichment (peak NIS) |
| `innovation_eci_km` | [6 floats] | Alert enrichment (peak residual = norm of [0:3]) |
| `anomaly_type` | string | Anomaly marker label |

### recalibration message fields used:
| Field | Type | Used by |
|---|---|---|
| `type` | `"recalibration"` | Routing |
| `norad_id` | int | Alert resolution |
| `epoch_utc` | ISO-8601 string | Alert resolution time (resolution epoch) |

### GET /catalog response fields used (object info panel):
| Field | Type | Notes |
|---|---|---|
| `norad_id` | int | Display |
| `name` | string | Display |
| `object_class` | **NOT CURRENTLY RETURNED** | See conflict below |
| `eci_km` | [3 floats] | Display |
| `eci_km_s` | [3 floats] | Display |
| `confidence` | float | Display |
| `last_update_epoch_utc` | string | Display |

> **Conflict:** Feature 4 (object info panel) requires `object_class` to be displayed per object. The `GET /catalog` endpoint in `backend/main.py` (line 482-495) does not include `object_class` in the response dict, even though it is available in `entry["object_class"]`. Resolution needed: either add `object_class` to the catalog response (trivial backend change -- one line), or omit object_class from the info panel. Recommend adding it to the catalog response. This is a 1-line backend change to `main.py` line ~485: add `"object_class": entry.get("object_class", "unknown")`.

## Files affected

- **`frontend/src/residuals.js`** -- Add tooltip div creation in `initResidualChart`, add mouseover/mouseout handlers on data point circles in `_redrawChart`, add anomaly marker rendering (vertical dashed lines with labels), export new function `addAnomalyMarker` for main.js to call on anomaly messages.
- **`frontend/src/alerts.js`** -- Enrich `addAlert` to display peak NIS and peak residual magnitude. Fix `updateAlertStatus` to compute accurate resolution duration from the recalibration message epoch instead of relying only on `entry.detectedAt` (already correct but the display currently shows "0s" when detection and recalibration arrive in the same processing cycle). Add click handler on alert cards to dispatch object selection. Export a callback registration function.
- **`frontend/src/globe.js`** -- No changes required.
- **`frontend/src/main.js`** -- Route anomaly messages to `addAnomalyMarker` in residuals.js. Pass peak NIS and innovation data to alert enrichment. Wire alert card clicks to the same selection handler as globe clicks. Create and manage the object info panel. Store latest state_update per object for info panel display.
- **`frontend/index.html`** -- Add object info panel container div. Add CSS for tooltip, anomaly markers, info panel, and enriched alert cards.
- **`backend/main.py`** -- (Minimal, 1 line) Add `object_class` to `GET /catalog` response dict. **Only if conflict above is approved.**

## Data flow changes

### Before
```
anomaly WS message --> main.js routeMessage --> alerts.js addAlert (name, epoch, type, status only)
                                            --> residuals.js appendResidualDataPoint (data point only)
                                            --> globe.js highlightAnomaly

state_update WS message --> main.js routeMessage --> globe.js updateSatellitePosition
                                                 --> residuals.js appendResidualDataPoint (selected object only)

Globe click --> main.js onSelect --> residuals.js selectObject
```

### After
```
anomaly WS message --> main.js routeMessage --> alerts.js addAlert (+ peak NIS, peak residual)
                                            --> residuals.js appendResidualDataPoint (data point)
                                            --> residuals.js addAnomalyMarker (vertical line at epoch)
                                            --> globe.js highlightAnomaly

recalibration WS message --> main.js routeMessage --> alerts.js updateAlertStatus (+ resolution epoch for accurate duration)
                                                  --> (existing globe + residual updates)

state_update WS message --> main.js routeMessage --> globe.js updateSatellitePosition
                                                 --> residuals.js appendResidualDataPoint (selected object only)
                                                 --> main.js latestStateMap.set(norad_id, message) [for info panel]

Globe click --> main.js onSelect --> residuals.js selectObject
                                 --> main.js showObjectInfoPanel(norad_id)

Alert card click --> main.js onSelect --> (same as globe click)
```

New data stored in main.js:
- `latestStateMap: Map<number, Object>` -- latest state_update message per NORAD ID, used by info panel to display current ECI position/velocity/confidence without re-fetching.

## Implementation steps

### Phase 1: Hover Tooltips on Chart Data Points

1. **Create shared tooltip div** (`frontend/src/residuals.js`)
   - Action: In `initResidualChart`, after creating the SVG, create a single `<div>` element with `position: absolute; pointer-events: none; display: none` appended to `containerEl` (which must have `position: relative`). Store as `chartState.tooltipEl`.
   - D3 approach: Use invisible enlarged circles (r=6, fill-opacity=0) overlaid on the visible circles (r=3) as hover targets. On `mouseenter`, show tooltip div positioned at `event.offsetX`, `event.offsetY`. On `mouseleave`, hide tooltip div. This avoids D3 `d3-tip` plugin (not in CDN imports) and uses pure DOM positioning.
   - Tooltip content: `<div>Epoch: 2026-03-28 19:00:00 UTC<br>Residual: 0.42 km<br>NIS: 3.7</div>` (top chart) or `<div>Epoch: ...<br>NIS: 3.7<br>Residual: 0.42 km</div>` (bottom chart). Both show all three values regardless of which chart is hovered.
   - Why: Users need to read exact values, not estimate from axis position. Critical for demo narration.
   - Dependencies: None
   - Risk: Low. Pure DOM manipulation, no external library.

2. **Add hover targets to top chart circles** (`frontend/src/residuals.js`)
   - Action: In `_redrawChart`, after the existing `topCircles` enter/merge/exit block, add a second circle selection (`topHoverG`) with `r=8, fill-opacity=0, cursor=pointer`. Bind same data. Attach `mouseenter` and `mouseleave` events that show/hide `chartState.tooltipEl` with formatted data from the bound datum.
   - D3 pattern: `topHoverG.selectAll('circle').data(data, d => d.epoch_utc.getTime())` with enter/merge/exit matching the visible circles.
   - Why: Larger invisible hit target prevents tooltip flicker on small 3px circles.
   - Dependencies: Step 1
   - Risk: Low

3. **Add hover targets to bottom chart circles** (`frontend/src/residuals.js`)
   - Action: Same pattern as step 2 but for `bottomHoverG` in the NIS chart group. Tooltip content is identical (shows epoch, residual, NIS).
   - Dependencies: Step 1
   - Risk: Low

4. **Add tooltip CSS** (`frontend/index.html`)
   - Action: Add CSS for `.chart-tooltip` class: `position: absolute; background: #1a1a2e; border: 1px solid #444; padding: 6px 10px; font-size: 12px; font-family: monospace; color: #e0e0e0; pointer-events: none; z-index: 10; border-radius: 3px; white-space: nowrap;`
   - Dependencies: None
   - Risk: Low

### Phase 2: Anomaly Event Markers on Charts

5. **Add anomaly marker store to residuals module** (`frontend/src/residuals.js`)
   - Action: Add a module-level `Map<number, Array<{epoch_utc: Date, anomaly_type: string}>>` called `anomalyMarkerStore`, keyed by NORAD ID. Capped at 20 markers per object (sliding window).
   - Why: Markers must persist across redraws and survive object switching.
   - Dependencies: None
   - Risk: Low

6. **Export `addAnomalyMarker` function** (`frontend/src/residuals.js`)
   - Action: New exported function `addAnomalyMarker(chartState, noradId, epochUtcStr, anomalyType)`. Parses epoch, stores in `anomalyMarkerStore`, triggers redraw if `noradId === chartState.selectedNoradId`.
   - Why: main.js calls this on anomaly messages to inject markers.
   - Dependencies: Step 5
   - Risk: Low

7. **Render anomaly markers in `_redrawChart`** (`frontend/src/residuals.js`)
   - Action: At the end of `_redrawChart`, after drawing lines and circles, read `anomalyMarkerStore.get(noradId)`. For each marker, draw a vertical dashed line from y=0 to y=chartHeight at `xScale(marker.epoch_utc)` in both the top and bottom chart groups. Use `stroke: #ff4444; stroke-dasharray: 4,3; stroke-width: 1`. Add a small text label at the top of each line showing the anomaly type (abbreviated: "MNV", "DRG", "DIV").
   - D3 pattern: Use `topGroup.selectAll('.anomaly-marker')` with data join on `anomalyMarkerStore` entries. Enter/update/exit pattern. Same for `bottomGroup`.
   - Why: Visual correlation between NIS spike and residual spike is critical for the demo funding moment (CLAUDE.md demo script step 3).
   - Dependencies: Steps 5, 6
   - Risk: Low

8. **Wire anomaly marker in main.js** (`frontend/src/main.js`)
   - Action: In `routeMessage` anomaly branch, add call: `addAnomalyMarker(chartState, norad_id, message.epoch_utc, message.anomaly_type)`. Import `addAnomalyMarker` from residuals.js.
   - Dependencies: Step 6
   - Risk: Low

### Phase 3: Alert Card Enrichment

9. **Enrich `addAlert` with peak NIS and residual** (`frontend/src/alerts.js`)
   - Action: Modify `addAlert` to accept and display two additional fields from the anomaly message:
     - `nis` (float) -- display as "Peak NIS: 14.2"
     - `innovation_eci_km` (array) -- compute `Math.sqrt(v[0]**2 + v[1]**2 + v[2]**2)` for position-only residual magnitude, display as "Peak residual: 1.23 km"
   - Add these as a new `<div>` inside the alert card, after the info line and before the status indicator. Style: `color: #888; font-size: 11px;`
   - The anomaly WS message already contains both fields (see `_build_ws_message` in processing.py lines 346-357: `anomaly_ws_message["nis"] = nis_val` and `anomaly_ws_message["innovation_eci_km"] = innovation_eci_km_list`).
   - Why: F-035 requires anomaly events to surface NIS value. Demo reviewers need quantitative values, not just visual spikes.
   - Dependencies: None
   - Risk: Low

10. **Fix resolution time accuracy** (`frontend/src/alerts.js`)
    - Action: In `updateAlertStatus`, the `resolutionTime` parameter is already the recalibration message's `epoch_utc` (passed from main.js line 116: `updateAlertStatus(panelState, norad_id, 'resolved', message.epoch_utc)`). The duration calculation `resolutionDate - entry.detectedAt` is correct in principle. However, when anomaly and recalibration messages arrive in the same processing cycle (same `epoch_utc`), the delta is 0ms, displayed as "0s". This is **accurate behavior** -- the recalibration happened at the same epoch as detection. No code change needed here; the display is correct.
    - **However**, there is a subtlety: `entry.detectedAt` is set from `new Date(epoch_utc)` of the anomaly message, and the recalibration message in the same cycle also uses the same `epoch_utc`. The real "resolution" happens on the **next** NIS-normal cycle (see processing.py lines 382-409), which sends a `state_update`, not a `recalibration`. The current main.js calls `updateAlertStatus(... 'resolved', message.epoch_utc)` only on `recalibration` messages, but the actual resolution confirmation comes later.
    - **Plan**: Store `_anomaly_detection_epoch_utc` on the alert entry (already stored as `detectedAt`). Add a new intermediate status update: when a `recalibration` message arrives, set status to `"recalibrating"` (not `"resolved"`). When the **next** `state_update` for that NORAD ID arrives with `anomaly_type === null` (no anomaly), **then** set status to `"resolved"` with `epoch_utc` from that state_update as the resolution time. This gives the true time-to-resolution per F-034.
    - Implementation detail: In main.js `routeMessage`, for `recalibration` type, change from `updateAlertStatus(... 'resolved', ...)` to `updateAlertStatus(... 'recalibrating', null)`. For `state_update` type, add: if panelState has any `recalibrating` alerts for this `norad_id`, call `updateAlertStatus(... 'resolved', message.epoch_utc)`.
    - Why: F-034 requires accurate time between detection and recalibration completion. The current code shows 0s for same-epoch events, which is technically correct for the recalibration step but not for the full resolution cycle.
    - Dependencies: None
    - Risk: Medium. Changes the alert lifecycle from 2-state (active -> resolved) to 3-state (active -> recalibrating -> resolved). The CSS classes already support all three states (see index.html lines 53-59). Must verify the `recalibrating` intermediate state renders correctly.

11. **Add CSS for enriched alert content** (`frontend/index.html`)
    - Action: Add `.alert-metrics { color: #888; font-size: 11px; margin-top: 2px; }` class.
    - Dependencies: None
    - Risk: Low

### Phase 4: Object Info Panel

12. **Add latest-state map to main.js** (`frontend/src/main.js`)
    - Action: Add module-level `const latestStateMap = new Map();`. In `routeMessage` for `state_update` and `recalibration`, call `latestStateMap.set(norad_id, message)`. This gives O(1) lookup of the latest state for any object without re-fetching.
    - Why: The info panel needs current ECI position, velocity, confidence, and epoch. These are already in every state_update message.
    - Dependencies: None
    - Risk: Low

13. **Add info panel container and CSS** (`frontend/index.html`)
    - Action: Add a `<div id="object-info-panel">` inside `#side-panel`, positioned between `#residual-chart` and `#alert-panel`. Default state: `display: none`. When visible, shows a compact card with object metadata.
    - CSS: Fixed height ~120px, `background: #12121a; border-bottom: 1px solid #222; padding: 8px; font-size: 13px; font-family: monospace; overflow: hidden;`
    - Reduce `#residual-chart` flex to accommodate the new panel. Use `flex: 1` on residual-chart (already set) and fixed heights on info panel and alert panel.
    - Why: Must fit in the existing side panel without breaking the layout.
    - Dependencies: None
    - Risk: Medium. Layout changes could affect residual chart sizing and trigger ResizeObserver, but the chart already handles resize.

14. **Implement `showObjectInfoPanel` in main.js** (`frontend/src/main.js`)
    - Action: New function `_showObjectInfoPanel(noradId)`. Reads from `latestStateMap.get(noradId)` and `nameMap.get(noradId)`. Populates the info panel div with:
      - NORAD ID and object name (from nameMap)
      - Object class (from catalog -- see conflict note above; if not approved, omit this field)
      - ECI Position: `[x, y, z] km` formatted to 2 decimal places
      - ECI Velocity: `[vx, vy, vz] km/s` formatted to 4 decimal places
      - Confidence: percentage with color class (green/amber/red)
      - Last update: epoch_utc formatted as `YYYY-MM-DD HH:MM:SS UTC`
    - If `noradId` is null, hide the panel (`display: none`).
    - Uses `innerHTML` with `_escapeHtml` (import or duplicate the helper from alerts.js -- plan recommends extracting to a shared util or duplicating the 4-line function in main.js).
    - Why: Requirement 4 from the task. Operators need to see quantitative state data, not just visual representations.
    - Dependencies: Steps 12, 13
    - Risk: Low

15. **Wire info panel to globe selection** (`frontend/src/main.js`)
    - Action: In the `setupSelectionHandler` callback (line 279-284), add call to `_showObjectInfoPanel(noradId)` after `selectObject(chartState, noradId)`.
    - Dependencies: Steps 13, 14
    - Risk: Low

16. **Wire alert card click to object selection** (`frontend/src/alerts.js`, `frontend/src/main.js`)
    - Action: In `addAlert`, add a `click` event listener on the alert card element. On click, extract `norad_id` from `alertEl.dataset.noradId` and call a registered callback. Add a new exported function `onAlertClick(panelState, callback)` that stores the callback. The callback in main.js will be the same selection handler: set `selectedNoradId`, call `selectObject`, call `_showObjectInfoPanel`.
    - Alternative (simpler): Instead of a callback registration pattern, have `addAlert` accept an `onClickCallback` parameter. main.js passes `(noradId) => { selectedNoradId = noradId; selectObject(chartState, noradId); _showObjectInfoPanel(noradId); }`.
    - Plan recommends the simpler approach (callback parameter to `addAlert`).
    - Why: The task specifies "clicking an alert card should show the info panel." This also cross-filters the residual chart to the clicked object.
    - Dependencies: Steps 14, 15
    - Risk: Low

17. **Store catalog entries for object_class lookup** (`frontend/src/main.js`)
    - Action: Add module-level `const catalogMap = new Map();`. In `_seedFromCatalog`, populate: `catalogMap.set(entry.norad_id, entry)`. Use in `_showObjectInfoPanel` to read `object_class`.
    - **Conditional on conflict resolution**: If `object_class` is not added to the backend response, this step only provides `name` and `norad_id` (already available from `nameMap`). The info panel would omit the object class row.
    - Dependencies: Step 14
    - Risk: Low

## Test strategy

All testing is manual browser verification (per CLAUDE.md frontend validation).

### Phase 1: Tooltips
- **Manual test 1.1**: Load page, select an object, wait for data points to appear. Hover over a cyan dot on the top chart. Verify tooltip shows epoch (UTC), residual (km), and NIS value. Verify tooltip disappears on mouseout.
- **Manual test 1.2**: Hover over an orange dot on the bottom (NIS) chart. Verify same tooltip content appears.
- **Manual test 1.3**: Rapidly move mouse across multiple data points. Verify no tooltip flicker, no stale content, no DOM leaks (check Elements panel).
- **Manual test 1.4**: Resize the browser window. Verify tooltips still position correctly after resize.

### Phase 2: Anomaly Markers
- **Manual test 2.1**: Run `scripts/seed_maneuver.py --object 25544 --delta-v 0.5`. Select the affected object. Verify a vertical red dashed line appears on both charts at the anomaly epoch with a label (e.g., "MNV").
- **Manual test 2.2**: Inject multiple anomalies. Verify multiple markers render without overlapping or breaking chart layout.
- **Manual test 2.3**: Switch to a different object and back. Verify anomaly markers persist for the original object.

### Phase 3: Alert Enrichment
- **Manual test 3.1**: Trigger an anomaly. Verify the alert card shows "Peak NIS: X.X" and "Peak residual: X.XX km" in addition to existing fields.
- **Manual test 3.2**: Wait for recalibration to complete (next NIS-normal state_update after recalibration message). Verify alert transitions from ACTIVE -> RECALIBRATING -> RESOLVED with accurate time delta (not "0s" unless truly instantaneous).
- **Manual test 3.3**: Verify the peak NIS value shown in the alert card matches the NIS value at the anomaly marker on the chart.

### Phase 4: Object Info Panel
- **Manual test 4.1**: Click a satellite on the globe. Verify the info panel appears showing NORAD ID, name, ECI position (3 components), ECI velocity (3 components), confidence with color, and last update epoch.
- **Manual test 4.2**: Click a different satellite. Verify the info panel updates to the new object.
- **Manual test 4.3**: Click empty space on the globe (deselect). Verify the info panel hides.
- **Manual test 4.4**: Click an alert card. Verify the info panel shows for that object, and the residual chart switches to that object.
- **Manual test 4.5**: Verify ECI position values update in real time as new state_update messages arrive for the selected object.

## Risks and mitigations

- **Risk (Low)**: Tooltip positioning may overflow the chart container on edge data points near the right or bottom margins.
  Mitigation: Clamp tooltip position to container bounds. If `offsetX + tooltipWidth > containerWidth`, position tooltip to the left of the cursor.

- **Risk (Medium)**: The alert lifecycle change (Phase 3, step 10) from 2-state to 3-state could cause alerts to remain stuck in "RECALIBRATING" if the next state_update never arrives (e.g., object drops from catalog).
  Mitigation: Add a 5-minute timeout in `updateAlertStatus`. If an alert has been in "recalibrating" state for more than 5 minutes without a resolving state_update, auto-resolve it with the text "Resolved (timeout)".

- **Risk (Low)**: The object info panel adds a fixed-height element to the side panel, reducing space for the residual chart. On small screens (< 800px height), the chart may become too small to be useful.
  Mitigation: Set `min-height: 160px` on `#residual-chart`. The info panel is hidden by default and only appears on selection, so the chart gets full space when no object is selected.

- **Risk (Low)**: Storing `latestStateMap` in main.js for all 50 objects consumes trivial memory (50 small JSON objects).
  Mitigation: None needed.

- **Risk (Medium)**: The `addAlert` function signature change (adding `onClickCallback` parameter) is a breaking change if any other code calls `addAlert`.
  Mitigation: Make the callback parameter optional with a default of `null`. Existing callers (main.js line 99) will be updated in step 16.

## Open questions

1. **Object class in catalog response**: Should `backend/main.py` `GET /catalog` be updated to include `object_class` in the response? This is a 1-line change (`"object_class": entry.get("object_class", "unknown")`). If not approved, the info panel will omit the object class field. **Recommend: approve.**

2. **Alert card click behavior -- select on globe too?**: When the user clicks an alert card, should the globe camera fly to the selected object? This would require adding a `viewer.flyTo(entity)` call. The current plan does not include camera fly-to, only data panel updates. If fly-to is desired, it is a 3-line addition to the click handler. **Recommend: defer to post-POC to avoid disorienting camera movements during demo narration.**

3. **Tooltip on touch devices**: The hover tooltip pattern does not work on touch screens. For the POC demo (projected laptop display), this is acceptable. If tablet support is needed, a tap-to-show-tooltip pattern would be required. **Recommend: defer.**
