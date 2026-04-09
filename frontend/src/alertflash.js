/**
 * @module alertflash
 * @description Fullscreen red overlay for anomaly alerting.
 *
 * Exports:
 *   initAlertFlash()                          — call once on app init; looks up the
 *                                               #anomaly-flash-overlay DOM element.
 *   triggerAlertFlash(objectName, anomalyType) — show the overlay for 3s then fade.
 *
 * The overlay is pointer-events: none (set in CSS) so it never blocks globe interaction.
 * If a second anomaly arrives while the overlay is visible, the timer resets and the
 * text updates to the newest anomaly — overlays never stack.
 *
 * The flash must only fire from routeMessage() for live WebSocket anomaly messages.
 * It must NOT fire for historical alerts seeded on reconnect (the reconnect path calls
 * addAlert() directly, never triggerAlertFlash(), so this separation is structural).
 */

/** @type {HTMLElement|null} */
let _overlayEl = null;

/** @type {HTMLElement|null} */
let _nameEl = null;

/** @type {HTMLElement|null} */
let _typeEl = null;

/** @type {number|null} Timer ID for the hide-after-hold timeout. */
let _holdTimer = null;

/** @type {number|null} Timer ID for the remove-after-fade timeout. */
let _fadeTimer = null;

/**
 * Initialize the flash overlay system.
 *
 * Looks up the #anomaly-flash-overlay element (created in index.html). Must be
 * called after the DOM is ready (e.g., from initApp()).
 *
 * @returns {void}
 */
export function initAlertFlash() {
    _overlayEl = document.getElementById('anomaly-flash-overlay');
    if (!_overlayEl) {
        console.warn('[alertflash] #anomaly-flash-overlay not found in DOM.');
        return;
    }
    _nameEl = _overlayEl.querySelector('.flash-object-name');
    _typeEl = _overlayEl.querySelector('.flash-anomaly-type');

    if (!_nameEl || !_typeEl) {
        console.warn('[alertflash] .flash-object-name or .flash-anomaly-type not found inside overlay.');
    }
}

/**
 * Show the anomaly flash overlay.
 *
 * Displays a fullscreen red overlay with the object name and anomaly type for
 * approximately 3 seconds (2.4s hold + 0.3s fade-in + 0.3s fade-out, totaling 3s).
 * Per the plan: flash in opacity 0→1 over 0.3s, hold 2.4s, fade out 1→0 over 0.3s,
 * total 3s then remove (display: none).
 *
 * If called while the overlay is already visible, any pending timers are cancelled
 * and the animation restarts with the new anomaly's data.
 *
 * @param {string} objectName - Human-readable satellite name (or NORAD ID string).
 * @param {string} anomalyType - Anomaly type string (e.g. 'maneuver', 'drag_anomaly').
 * @returns {void}
 */
export function triggerAlertFlash(objectName, anomalyType) {
    if (!_overlayEl || !_nameEl || !_typeEl) return;

    // Cancel any in-progress timers so we can restart cleanly.
    if (_holdTimer !== null) {
        clearTimeout(_holdTimer);
        _holdTimer = null;
    }
    if (_fadeTimer !== null) {
        clearTimeout(_fadeTimer);
        _fadeTimer = null;
    }

    // Populate text content.
    _nameEl.textContent = String(objectName);

    // Normalize anomaly type for display and CSS class.
    const anomalyStr = String(anomalyType || 'unknown');
    _typeEl.textContent = anomalyStr.replace(/_/g, ' ').toUpperCase();

    // Apply anomaly-type-specific color class to .flash-anomaly-type.
    // Matches the palette used by .alert-type-badge in index.html.
    _typeEl.className = 'flash-anomaly-type ' + _sanitizeClass(anomalyStr);

    // Reset to fully opaque, make visible.
    _overlayEl.style.transition = 'none';
    _overlayEl.style.opacity = '0';
    _overlayEl.style.display = 'flex';

    // Force a reflow so the opacity:0 takes effect before we transition.
    // eslint-disable-next-line no-unused-expressions
    _overlayEl.offsetHeight;

    // Fade in over 0.3s.
    _overlayEl.style.transition = 'opacity 0.3s ease-in';
    _overlayEl.style.opacity = '1';

    // After 2.7s (0.3s fade-in + 2.4s hold), begin fade-out.
    _holdTimer = setTimeout(() => {
        _holdTimer = null;
        _overlayEl.style.transition = 'opacity 0.3s ease-out';
        _overlayEl.style.opacity = '0';

        // After fade-out completes (0.3s), hide the element.
        _fadeTimer = setTimeout(() => {
            _fadeTimer = null;
            _overlayEl.style.display = 'none';
        }, 300);
    }, 2700);
}

/**
 * Sanitize a string for safe use as a CSS class name suffix.
 * @param {string} str - Input string.
 * @returns {string} Alphanumeric-and-underscore-only string.
 */
function _sanitizeClass(str) {
    return str.replace(/[^a-zA-Z0-9_-]/g, '_');
}
