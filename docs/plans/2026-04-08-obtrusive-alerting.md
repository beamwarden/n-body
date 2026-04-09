# Implementation Plan: Obtrusive Anomaly Alerting (Audio + Visual Flash)
Date: 2026-04-08
Status: Draft

## Summary

Add attention-grabbing audio and visual alerts when an anomaly WebSocket message arrives. A synthesized alarm tone (Web Audio API) and a fullscreen red overlay with object name and anomaly type will fire together, ensuring a presenter or operator cannot miss an anomaly event during a live demo. Both alerts are non-blocking and auto-dismiss.

## Requirements addressed

- **F-053 [DEMO]** — Affected object shall visually highlight within 2 seconds of backend detection event. The fullscreen flash augments the existing globe highlight.
- **F-055 [DEMO]** — Anomaly alert feed. The obtrusive alert is a supplemental attention mechanism layered on top of the existing alert panel.
- **NF-023** — Anomaly injection shall produce a visible response within 10 seconds. The flash fires within 1 frame of WebSocket message receipt.
- **NF-022** — Text legible at 3 meters on 1920x1080. The overlay text uses large font sizes (48px+ object name, 28px+ anomaly type).

## Files affected

- `frontend/src/alertsound.js` (NEW) — Web Audio API alarm tone module. Handles AudioContext creation, autoplay unlock, tone synthesis, and mute toggle state.
- `frontend/src/alertflash.js` (NEW) — Fullscreen overlay module. Creates/manages the overlay DOM element, triggers pulse animation, populates object name and anomaly type, auto-dismisses after 3 seconds.
- `frontend/src/main.js` — Import the two new modules; call them from the `type === 'anomaly'` branch in `routeMessage()`. Wire mute toggle. Pass AudioContext unlock to a one-time user interaction listener.
- `frontend/index.html` — Add the overlay DOM element (`#anomaly-flash-overlay`), its CSS styles, and the mute toggle button in the header bar. No new CDN dependencies.

## Data flow changes

**Before:** `routeMessage('anomaly')` calls `highlightAnomaly()` on the globe and `addAlert()` on the alert panel. Both are visual-only and confined to the globe/side-panel.

**After:** `routeMessage('anomaly')` additionally calls:
1. `playAlarmTone()` from `alertsound.js` — produces audio output.
2. `showAnomalyFlash(objectName, anomalyType)` from `alertflash.js` — shows and auto-dismisses the fullscreen overlay.

No backend changes. No new WebSocket message types. The anomaly message already contains `norad_id` and `anomaly_type`, which are the only fields needed.

## Implementation steps

### Phase 1: Audio alert module (`alertsound.js`)

1. **Create `frontend/src/alertsound.js`** (NEW)
   - Action: New ES module exporting three functions: `initAudioAlert()`, `playAlarmTone()`, `toggleMute()`.
   - Why: Isolates Web Audio API concerns from the main routing logic. Keeps the existing `alerts.js` focused on the panel DOM.
   - Dependencies: None.
   - Risk: Low.

2. **`initAudioAlert()` — AudioContext creation and autoplay unlock**
   - Action: Create an `AudioContext` in suspended state. Register a one-time `click` event listener on `document.body` that calls `audioCtx.resume()`. Store a module-level boolean `_muted` (default `false`) and the AudioContext reference. Return an object with `{ playAlarmTone, toggleMute, isMuted }`.
   - Why: Browsers (Chrome, Firefox, Safari) require a user gesture before an AudioContext can produce sound. By creating the context early and resuming on the first click anywhere on the page, the alarm is ready to fire by the time the demo reaches the anomaly injection step (the presenter will have clicked something by then).
   - Dependencies: None.
   - Risk: Medium — if no user interaction occurs before the first anomaly message, the tone will be silently skipped. This is acceptable: the visual flash still fires, and the demo flow (clicking objects on the globe) guarantees interaction well before the maneuver injection step.

3. **`playAlarmTone()` — Synthesized alarm**
   - Action: If `_muted` or AudioContext is suspended, return immediately (no-op). Otherwise, create a short beep sequence using `OscillatorNode` and `GainNode`:
     - **Waveform:** `"square"` — harsher, more alarm-like than sine.
     - **Beep pattern:** 3 beeps, each 120ms long, separated by 80ms silence.
     - **Frequencies:** Rising sequence: 660 Hz, 880 Hz, 1100 Hz. This creates an ascending urgency pattern without being shrill.
     - **Envelope per beep:** Attack 10ms (gain 0 to 0.25), sustain 80ms at 0.25, release 30ms (gain 0.25 to 0). The gain of 0.25 (not 1.0) keeps volume assertive but not startling in a quiet room.
     - **Implementation:** For each beep, create an OscillatorNode and a GainNode. Schedule start/stop times using `audioCtx.currentTime` offsets. Connect oscillator -> gain -> `audioCtx.destination`. Let the nodes auto-disconnect after stop.
   - Why: Web Audio API OscillatorNode requires no external audio files, no preloading, no CORS. The rising three-beep pattern is a standard alert idiom (recognizable, not obnoxious).
   - Dependencies: Step 2 (AudioContext must exist).
   - Risk: Low.

4. **`toggleMute()` and `isMuted()` — Mute control**
   - Action: `toggleMute()` flips the `_muted` boolean and returns the new value. `isMuted()` returns the current value. No AudioContext manipulation needed — the mute check is in `playAlarmTone()`.
   - Why: The presenter needs a quick way to silence audio without killing the browser tab or the visual flash. The mute toggle only affects the audio alarm; the visual flash always fires.
   - Dependencies: Step 2.
   - Risk: Low.

### Phase 2: Visual flash overlay (`alertflash.js`)

5. **Create `frontend/src/alertflash.js`** (NEW)
   - Action: New ES module exporting two functions: `initAnomalyFlash()`, `showAnomalyFlash(objectName, anomalyType)`.
   - Why: Separates overlay DOM management from `main.js` and `alerts.js`.
   - Dependencies: None.
   - Risk: Low.

6. **`initAnomalyFlash()` — Overlay element setup**
   - Action: Locate the `#anomaly-flash-overlay` element in the DOM (created in `index.html` — see step 9). Store a reference. Return a handle object.
   - Why: Separates DOM lookup from the hot path of `showAnomalyFlash()`.
   - Dependencies: Step 9 (DOM element must exist in `index.html`).
   - Risk: Low.

7. **`showAnomalyFlash(objectName, anomalyType)` — Trigger the flash**
   - Action:
     - Set the overlay's inner text: object name in large text (CSS class `.flash-object-name`, 48px), anomaly type below in medium text (CSS class `.flash-anomaly-type`, 28px, uppercase, badge-colored matching the alert panel's type colors).
     - Set `display: flex` and `opacity: 1` on the overlay.
     - After 2000ms, begin fade-out: transition `opacity` to 0 over 1000ms.
     - After the fade completes (3000ms total), set `display: none`.
     - If a new anomaly arrives while the overlay is still visible, reset the timer (extend the display, update the text to the newest anomaly). Do NOT stack overlays.
   - Why: 3 seconds total (2s visible + 1s fade) is long enough to read but short enough not to obscure the demo. The overlay is non-interactive (`pointer-events: none`) so the presenter can still click through it to the globe.
   - Dependencies: Step 6.
   - Risk: Low.

### Phase 3: DOM and CSS additions (`index.html`)

8. **Add mute toggle button to the header bar**
   - Action: Inside `#header`, add a `<button id="mute-toggle">` element. Style: positioned right-aligned in the header, monospace, small (font-size: 14px), toggles between text "SOUND: ON" and "SOUND: OFF". No icon — text-only for simplicity and legibility at distance (NF-022).
   - Why: Gives the presenter a visible, clickable control. Placed in the header so it is always accessible regardless of panel state.
   - Dependencies: None.
   - Risk: Low.

9. **Add the flash overlay DOM element**
   - Action: Add `<div id="anomaly-flash-overlay">` directly inside `<body>`, after `#main-layout`. It contains two child elements: `<div class="flash-object-name"></div>` and `<div class="flash-anomaly-type"></div>`.
   - Why: Must be a direct child of body (or at least outside the Cesium container) to avoid Cesium's stacking context.
   - Dependencies: None.
   - Risk: Low.

10. **Add CSS for the overlay and mute toggle**
    - Action: Add the following styles to the existing `<style>` block in `index.html`:
      - `#anomaly-flash-overlay`:
        - `display: none` (hidden by default)
        - `position: fixed; top: 0; left: 0; width: 100%; height: 100%`
        - `z-index: 9998` (below the noscript 9999, above everything else including Cesium and object-info-panel at z-index 100)
        - `background: radial-gradient(ellipse at center, rgba(180, 0, 0, 0.35) 0%, rgba(120, 0, 0, 0.55) 100%)` — red tint, translucent, allows globe to remain partially visible
        - `pointer-events: none` — click-through so presenter can interact with globe underneath
        - `display: flex; flex-direction: column; align-items: center; justify-content: center` (when shown)
        - `transition: opacity 1s ease-out`
      - `.flash-object-name`: `font-size: 48px; font-weight: bold; color: #ffffff; text-shadow: 0 0 20px #ff0000, 0 0 40px #ff0000; font-family: monospace; letter-spacing: 0.05em`
      - `.flash-anomaly-type`: `font-size: 28px; font-weight: bold; text-transform: uppercase; margin-top: 8px; font-family: monospace; letter-spacing: 0.08em`. Color varies by anomaly type (use same palette as `.alert-type-badge` — maneuver: `#ffcccc`, drag_anomaly: `#ffe0cc`, filter_divergence: `#ddaaff`).
      - `#mute-toggle`: `float: right; background: none; border: 1px solid #444; color: #888; font-family: monospace; font-size: 13px; padding: 2px 10px; cursor: pointer; border-radius: 3px`. On hover: `color: #ccc; border-color: #666`.
    - Why: z-index 9998 is above Cesium's rendering context and the object-info-panel (z-index 100) but below the noscript fallback (9999). `pointer-events: none` is critical — without it, the overlay would block all interaction during the 3-second display. The radial gradient provides visual urgency while keeping the globe partially visible for context.
    - Dependencies: None.
    - Risk: Low.

### Phase 4: Integration in `main.js`

11. **Import new modules**
    - Action: Add to the import block at the top of `main.js`:
      ```
      import { initAudioAlert } from './alertsound.js';
      import { initAnomalyFlash, showAnomalyFlash } from './alertflash.js';
      ```
    - Why: Standard ES module import pattern consistent with existing imports.
    - Dependencies: Steps 1, 5.
    - Risk: Low.

12. **Initialize in `initApp()`**
    - Action: After the existing `panelState = initAlertPanel('alert-panel')` call (line 675), add:
      - `const audioAlert = initAudioAlert();`
      - `const flashHandle = initAnomalyFlash();`
      - Store these as module-level variables (following the pattern of `viewer`, `chartState`, `panelState`).
      - Wire the mute toggle button: `document.getElementById('mute-toggle').addEventListener('click', () => { const muted = audioAlert.toggleMute(); muteBtn.textContent = muted ? 'SOUND: OFF' : 'SOUND: ON'; });`
    - Why: Initialization order matters — AudioContext must exist before any anomaly message arrives. The mute button wiring belongs in `initApp()` alongside other DOM setup.
    - Dependencies: Steps 1, 5, 8.
    - Risk: Low.

13. **Call from `routeMessage()` anomaly branch**
    - Action: In the `type === 'anomaly'` branch of `routeMessage()` (after the existing `addAlert()` call at line 134 and before the `addAnomalyMarker` call at line 145), add:
      - `audioAlert.playAlarmTone();`
      - `const objName = nameMap.get(norad_id) || String(norad_id);`
      - `showAnomalyFlash(objName, message.anomaly_type || 'unknown');`
    - Why: This is the single integration point. The anomaly branch already resolves the object name via `nameMap` (used by `addAlert`), so the same lookup pattern applies. Placing the calls after `addAlert` ensures the alert panel is populated before the flash fires (consistent ordering of side effects).
    - Dependencies: Steps 3, 7, 11, 12.
    - Risk: Low.

14. **Do NOT fire on recalibration messages**
    - Action: Explicitly do NOT add audio/visual alerts to the `type === 'recalibration'` branch. Recalibration is a recovery event, not a new alarm. Firing the alarm again during recovery would confuse the demo narrative ("the system is fixing the problem" should not look like "another problem").
    - Why: The demo script flow is: anomaly fires -> alarm sounds -> presenter explains -> recalibration begins -> alarm should NOT re-fire -> resolution appears in panel. Double-alarming undermines the confidence narrative.
    - Dependencies: None.
    - Risk: Low.

## Test strategy

### Manual testing (primary — no build step, vanilla JS)

- **Audio unlock:** Load the page, verify no console errors. Click anywhere on the page. Trigger an anomaly via `seed_maneuver.py`. Confirm 3 beeps play.
- **Audio mute:** Click "SOUND: ON" button. Verify text changes to "SOUND: OFF". Trigger another anomaly. Confirm no audio. Click again to re-enable. Trigger anomaly. Confirm audio returns.
- **Audio before interaction:** Load the page, do NOT click anything. Trigger an anomaly. Confirm no audio error in console (graceful skip). Confirm visual flash still fires.
- **Visual flash:** Trigger an anomaly. Confirm red overlay appears with object name and anomaly type in center. Confirm it fades after ~3 seconds. Confirm the globe is still partially visible through the overlay. Confirm clicking through the overlay to the globe still works (`pointer-events: none`).
- **Flash stacking:** Trigger two anomalies within 1 second (two different objects via rapid `seed_maneuver.py`). Confirm the overlay updates to the second anomaly's text and resets the timer (does not stack two overlays).
- **Recalibration no-fire:** After anomaly fires and recalibration message arrives, confirm no second flash or audio.
- **z-index:** Confirm the overlay renders above the Cesium globe, above the object-info-panel, and above the side panel.
- **NF-022 legibility:** View on a 1920x1080 display. Confirm object name (48px) and anomaly type (28px) are readable at 3 meters.

### Automated (if eslint is configured)

- Run `npx eslint frontend/src/alertsound.js frontend/src/alertflash.js` to verify no lint errors.

## Risks and mitigations

- **Risk:** Browser autoplay policy prevents audio on first anomaly if the user has not interacted with the page. **Mitigation:** The `initAudioAlert()` function registers a one-time click listener on `document.body` that resumes the AudioContext. In the demo flow, the presenter always clicks the globe to select objects before injecting a maneuver, so the AudioContext will be unlocked. The visual flash fires regardless of audio state, so even in the worst case the anomaly is not missed.

- **Risk:** z-index conflict with CesiumJS. Cesium creates its own stacking contexts with z-index values in the hundreds to low thousands. **Mitigation:** The overlay uses `position: fixed` on a DOM element outside the Cesium container, with `z-index: 9998`. Fixed positioning relative to the viewport escapes Cesium's stacking context entirely. Tested z-index values in the existing codebase: Cesium container has no explicit z-index; object-info-panel is at 100; noscript fallback is at 9999. The overlay at 9998 slots correctly.

- **Risk:** Rapid anomaly messages (e.g., batch processing of stale TLEs on reconnect) could produce a machine-gun effect of beeps and flashes. **Mitigation:** Add a debounce/cooldown in `playAlarmTone()` — if the last tone started less than 2 seconds ago, skip the new tone. The visual flash already handles this by resetting instead of stacking. For the reconnect seed path (`/alerts/active` on connect), do NOT play audio or show flash — those are historical alerts, not new detections. This requires a small guard: the `addAlert()` calls in the `socket.onopen` reconnect handler should NOT trigger obtrusive alerts. The simplest approach is to only call `playAlarmTone()` and `showAnomalyFlash()` inside `routeMessage()`, not inside the reconnect seed logic. Since the reconnect seed path calls `addAlert()` directly (not via `routeMessage()`), this separation already exists naturally.

- **Risk:** The 48px object name text could be excessively long for satellites with verbose names (e.g., "STARLINK-1234 [+]"). **Mitigation:** Apply `text-overflow: ellipsis; max-width: 80vw; overflow: hidden; white-space: nowrap` to `.flash-object-name`. Names in the current catalog are all under 30 characters, which fits comfortably at 48px on 1920px width.

## Open questions

1. **Should the flash also fire on conjunction_risk messages?** The conjunction_risk message indicates close-approach screening has found nearby objects. This is operationally significant but is a secondary alert (already handled by the conjunction section on alert cards and globe color changes). Recommendation: do NOT flash on conjunction_risk for the POC — it would dilute the anomaly alarm's urgency. If stakeholders want it, it can be added as a separate, visually distinct (amber, not red) flash in a follow-up plan.

2. **Should the alarm tone vary by anomaly type?** For example, maneuver could use the rising three-beep, drag_anomaly could use a two-tone warble, filter_divergence could use a single sustained tone. This would be useful operationally but adds complexity. Recommendation: use the same tone for all anomaly types in the POC. The visual overlay already differentiates by type via text and color. Audio differentiation is a post-POC enhancement.

3. **Volume control beyond mute?** A volume slider could be useful in presentation settings with varying room acoustics. For POC, the binary mute toggle is sufficient. The system volume on the presenter's laptop provides the fine-grained control. Flag for post-POC if needed.
