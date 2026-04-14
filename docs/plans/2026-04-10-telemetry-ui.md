# Implementation Plan: Telemetry Dashboard UI Refactor
Date: 2026-04-10
Status: Approved

## Summary
Refactor the frontend layout from a demo-app feel into a real-time telemetry operations dashboard. Three changes: (1) an always-visible tracked-object counter in the header, updated live from WebSocket state_update messages; (2) event-driven residual/NIS charts that are hidden by default and slide in only when the user selects an object with an active anomaly; (3) tighter, denser visual framing with ops-dashboard styling. No new backend endpoints. No build step. No breaking changes to alert sound, alert flash, object info panel, or demo script hooks.

## Requirements addressed
- **F-050 [DEMO]** — globe with real-time position updates (preserved, globe remains dominant)
- **F-054 [DEMO]** — residual/NIS charts for selected object (preserved, now conditionally shown)
- **F-055 [DEMO]** — anomaly alert feed (preserved, gains more vertical space when charts are hidden)
- **F-056** — click-to-select cross-filtering (extended: selection now also controls chart visibility)
- **NF-003** — 30 FPS globe rendering (preserved, globe gains screen area when charts are hidden)
- **NF-004** — 4-hour stability (counter uses Map.size, no unbounded growth)
- **NF-022** — text legibility at 3m on 1080p (counter font size meets minimum)

## Files affected

- `frontend/index.html` — add tracked-object counter element to header; restructure `#side-panel` to wrap charts in a collapsible container; add new CSS for counter, collapse animation, and dashboard density
- `frontend/src/main.js` — maintain a `trackedObjectCount` derived from `latestStateMap.size`; update counter DOM on each `state_update`; control chart panel visibility based on anomaly state of selected object; modify `selectObject` / `routeMessage` flow to show/hide charts
- `frontend/src/residuals.js` — no logic changes; add an exported `resizeChart(chartState)` function that triggers a manual resize (the ResizeObserver already handles this, but an explicit call ensures correct dimensions after the container transitions from `display:none` / `height:0` to visible)

## Data flow changes

### Object count tracking

**Before:** `latestStateMap` accumulates entries but its size is never displayed.

**After:** On every `state_update` message processed by `routeMessage()`, after `latestStateMap.set(norad_id, message)`, update the counter DOM element:
```
counterEl.textContent = latestStateMap.size + ' TRACKED';
```
This counts every NORAD ID that has received at least one fresh state_update. On `removeSatelliteEntity` calls (stale TLE cleanup in `routeMessage`), also delete the entry from `latestStateMap` and update the counter. This is a new side-effect in the `state_update` stale branch.

### Chart show/hide flow

**Before:** `#residual-chart` is always visible in the side panel, initialized at app start, first catalog object auto-selected.

**After:**
1. On app init, `#residual-chart` container starts with CSS `max-height: 0; overflow: hidden; opacity: 0`. Charts are still initialized (D3 SVG created), but the container is visually collapsed.
2. When user selects an object (globe click or alert card click), `main.js` checks whether that object has an active (non-resolved) anomaly in `panelState.alerts`.
3. If active anomaly exists: add CSS class `.chart-expanded` to `#residual-chart` which transitions `max-height` to `400px` and `opacity` to `1` over 300ms. After transition ends, call `resizeChart(chartState)` to recalculate D3 dimensions.
4. If no active anomaly: remove `.chart-expanded` class, collapsing the chart panel.
5. When an anomaly resolves (status changes to `resolved` via `_resolveRecalibratingAlerts`), if the resolved object is the currently selected object, remove `.chart-expanded`.
6. When a new anomaly fires for the currently selected object, add `.chart-expanded`.
7. Auto-select on init is removed — no object is pre-selected, charts start hidden. The globe loads with all objects visible but no chart panel.

## Layout

```
+--------------------------------------------------------------+
| ne-body SSA Platform — Continuous Monitoring & Prediction    |
|                           [LIVE] [42 TRACKED] [MUTE]        |
+--------------------------------------------------------------+
|                                          |                    |
|                                          |   ALERT PANEL      |
|                                          |   (scrollable,     |
|                                          |    fills full      |
|          CESIUM GLOBE                    |    side-panel      |
|          (fills remaining space)         |    height when     |
|                                          |    charts hidden)  |
|                                          |                    |
|   [Object Info Panel]                    |                    |
|   (overlay, upper-left)                  |------- - - - ------|
|                                          |   RESIDUALS / NIS  |
|                                          |   (slides in from  |
|                                          |    bottom when     |
|                                          |    anomaly active  |
|                                          |    on selected obj)|
+--------------------------------------------------------------+
```

Key layout properties:
- Header height unchanged (padding 8px, font 18px)
- Object counter is a `<span>` right-aligned in the header, to the left of the mute button
- WebSocket status indicator ("LIVE" / "RECONNECTING") is right-aligned, to the left of the counter
- Side panel width stays 380px
- Alert panel: changes from fixed `height: 240px` to `flex: 1; min-height: 200px` so it expands to fill when charts are hidden
- Residual chart: changes from `flex: 1` to a fixed-max-height collapsible section at the bottom of the side panel, initially collapsed

## Implementation steps

### Phase 1: Object count counter

**Step 1 — Add counter element to header** (`frontend/index.html`)
- Add `<span id="tracked-count" class="tracked-count">0 TRACKED</span>` inside `#header`, to the left of the mute button.
- CSS: `.tracked-count { float: right; color: #66ff66; font-size: 14px; font-weight: bold; letter-spacing: 0.05em; padding: 2px 12px; border: 1px solid #333; border-radius: 3px; margin-top: 2px; margin-right: 10px; }`
- Risk: Low

**Step 2 — Update counter from routeMessage** (`frontend/src/main.js`)
- Declare `let trackedCountEl = null;` at module level. Assign in `initApp`.
- In `routeMessage` `state_update` branch, after `latestStateMap.set(...)`, call `_updateTrackedCount()`.
- In stale-TLE branch, add `latestStateMap.delete(norad_id)` before `removeSatelliteEntity`, then call `_updateTrackedCount()`.
- Helper: `function _updateTrackedCount() { if (trackedCountEl) trackedCountEl.textContent = latestStateMap.size + ' TRACKED'; }`
- Risk: Low

**Step 3 — Update counter on catalog seed** (`frontend/src/main.js`)
- At end of `_seedFromCatalog`, call `_updateTrackedCount()`.
- Risk: Low

### Phase 2: Event-driven chart visibility

**Step 4 — Collapse residual-chart by default** (`frontend/index.html`)
```css
#residual-chart {
    max-height: 0;
    overflow: hidden;
    opacity: 0;
    transition: max-height 0.3s ease-in-out, opacity 0.3s ease-in-out;
    padding: 0 8px;
    font-size: 14px;
}
#residual-chart.chart-expanded {
    max-height: 400px;
    opacity: 1;
    padding: 8px;
}
```
- Change `#alert-panel` from `height: 240px` to `flex: 1; min-height: 200px`.
- Risk: Medium — D3 chart initializes at zero height. Mitigated by resizeChart call after transitionend.

**Step 5 — Add `_hasActiveAnomaly(noradId)` helper** (`frontend/src/main.js`)
```js
function _hasActiveAnomaly(noradId) {
    if (!panelState) return false;
    for (const [, entry] of panelState.alerts) {
        if (parseInt(entry.data.norad_id, 10) === noradId &&
            (entry.status === 'active' || entry.status === 'recalibrating')) {
            return true;
        }
    }
    return false;
}
```
- Risk: Low

**Step 6 — Add `_setChartVisible(visible)` controller** (`frontend/src/main.js`)
```js
let _chartVisible = false;

function _setChartVisible(visible) {
    const chartEl = document.getElementById('residual-chart');
    if (!chartEl || visible === _chartVisible) return;
    _chartVisible = visible;
    if (visible) {
        chartEl.classList.add('chart-expanded');
        chartEl.addEventListener('transitionend', function _onExpand(e) {
            if (e.propertyName !== 'max-height') return;
            chartEl.removeEventListener('transitionend', _onExpand);
            if (chartState) resizeChart(chartState);
        });
    } else {
        chartEl.classList.remove('chart-expanded');
    }
}
```
- Risk: Medium — see Open Question 3 regarding resolved alert click behavior.

**Step 7 — Wire chart visibility to object selection** (`frontend/src/main.js`)
- In globe selection handler, after `selectObject(chartState, noradId)`:
  `_setChartVisible(noradId !== null && _hasActiveAnomaly(noradId));`
- In alert card click handlers: `_setChartVisible(true)` (clicking alert implies active anomaly).
- When selection cleared: `_setChartVisible(false)`.
- Risk: Low

**Step 8 — Show charts when anomaly fires on selected object** (`frontend/src/main.js`)
- In `routeMessage` anomaly branch, after `addAlert()`:
  `if (norad_id === selectedNoradId) { _setChartVisible(true); }`
- Risk: Low

**Step 9 — Hide charts when anomaly resolves for selected object** (`frontend/src/main.js`)
- In `_resolveRecalibratingAlerts`, after `updateAlertStatus(... 'resolved' ...)`:
  `if (noradId === selectedNoradId) { _setChartVisible(false); }`
- Risk: Low

**Step 10 — Remove auto-select of first catalog object** (`frontend/src/main.js`)
- Remove the `if (catalog.length > 0 && chartState) { selectObject(...) }` block at end of init.
- Risk: Low. Demo narrative: presenter sees overview, clicks anomalous object to drill in.

### Phase 3: Dashboard density and styling

**Step 11 — Tighten header** (`frontend/index.html`)
- Reduce padding to `6px 16px`. Add `box-shadow: 0 1px 4px rgba(0, 150, 255, 0.15)`. Background `#0d0d14`.
- Risk: Low

**Step 12 — WebSocket status indicator** (`frontend/index.html` + `frontend/src/main.js`)
- Add `<span id="ws-status" class="ws-status ws-live">LIVE</span>` in header.
- CSS:
  ```css
  .ws-status { float: right; font-size: 11px; padding: 3px 8px; border-radius: 3px; margin-top: 3px; margin-right: 10px; }
  .ws-live { color: #66ff66; border: 1px solid #2a4a2a; }
  .ws-reconnecting { color: #ffaa00; border: 1px solid #4a3a1a; }
  ```
- In `main.js`: update on `socket.onopen` (→ LIVE) and `_scheduleReconnect` (→ RECONNECTING).
- Risk: Low

**Step 13 — Residual chart section header** (`frontend/index.html`)
- Add `<div class="alert-header" id="chart-section-header">RESIDUALS / NIS</div>` as first child of `#residual-chart`.
- Risk: Low

### Phase 4: resizeChart export

**Step 14 — Export resizeChart** (`frontend/src/residuals.js`)
```js
export function resizeChart(chartState) {
    if (!chartState || chartState.selectedNoradId === null) return;
    _redrawChart(chartState, chartState.selectedNoradId);
}
```
- Risk: Low

**Step 15 — Import resizeChart in main.js** (`frontend/src/main.js`)
- Add `resizeChart` to the import from `./residuals.js`.
- Risk: Low

## Decisions (approved 2026-04-10)

1. **`latestStateMap.delete()` on stale TLE removal** → **YES.** Delete entry and update counter.
2. **Auto-select on startup** → **REMOVE.** Globe starts in overview mode, no selection.
3. **Clicking a resolved alert card** → **SHOW CHARTS.** Any alert card click (active, recalibrating, or resolved) shows charts so the user can review historical event data. Update `_hasActiveAnomaly` to `_hasAnyAnomaly` and check all statuses, or simply call `_setChartVisible(true)` unconditionally on any alert card click.

## Risks and mitigations

| Risk | Mitigation |
|------|-----------|
| D3 initializes at zero height | `resizeChart` call after `transitionend` forces redraw with correct dimensions |
| `max-height: 400px` may clip content | 400px is generous for dual-chart layout; tunable post-implementation |
| Removing auto-select changes demo feel | Demo narrative stronger without it — presenter drills in on demand |
| `latestStateMap.delete()` new side-effect | All `latestStateMap` readers handle missing entries gracefully |
| `transitionend` fires per-property | Filter on `e.propertyName === 'max-height'` to prevent double resize |

## Test strategy

1. **Counter accuracy:** Verify counter matches live object count; verify decrement on stale removal.
2. **Chart visibility — anomaly active:** Click objects with no anomaly, charts hidden. Inject maneuver, wait for anomaly, click object, charts slide in with data.
3. **Chart visibility — anomaly resolves:** Charts visible → recalibration completes → charts collapse.
4. **Non-anomalous selection:** Charts visible → click non-anomalous object → charts collapse.
5. **Alert sound/flash unbroken:** Inject maneuver, verify sound + flash + mute toggle work.
6. **Object info panel unbroken:** Click, update, hide on empty click.
7. **WebSocket status:** LIVE / RECONNECTING / LIVE cycle.
8. **Full demo script:** All 5 acts run without regression.
