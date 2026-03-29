/**
 * @module alerts
 * @description Anomaly alert panel. Receives anomaly events via WebSocket
 * and renders a scrolling feed of alerts with status tracking. (F-055)
 *
 * Alert lifecycle: active → recalibrating → resolved.
 * Newest alerts appear at the top of the list.
 */

/** Maximum number of alerts retained in the panel. */
const MAX_ALERTS = 50;

// ---------------------------------------------------------------------------
// Step 21: initAlertPanel
// ---------------------------------------------------------------------------

/**
 * Initialize the alert panel in the given container. (F-055)
 *
 * @param {string} containerId - DOM element ID for the alert panel.
 * @returns {Object} panelState object for subsequent updates.
 */
export function initAlertPanel(containerId) {
    const containerEl = document.getElementById(containerId);
    if (!containerEl) {
        console.error('[alerts] Container not found:', containerId);
        return null;
    }

    // Header
    const header = document.createElement('div');
    header.className = 'alert-header';
    header.textContent = 'Anomaly Alerts';
    containerEl.appendChild(header);

    // Scrollable alert list
    const listEl = document.createElement('div');
    listEl.className = 'alert-list';
    containerEl.appendChild(listEl);

    return {
        containerEl,
        listEl,
        /** @type {Map<string, {el: HTMLElement, data: Object, status: string}>} */
        alerts: new Map(),
        /** @type {Map<number, string>} noradId -> object name */
        nameMap: new Map(),
    };
}

// ---------------------------------------------------------------------------
// seedFromCatalog: populate the name map from GET /catalog response
// ---------------------------------------------------------------------------

/**
 * Seed the alert panel's name map from the catalog. Called on connect/reconnect.
 * Used by addAlert to display object names instead of raw NORAD IDs.
 *
 * @param {Object} panelState - Panel state from initAlertPanel.
 * @param {Array<Object>} catalog - Array of catalog entries from GET /catalog.
 * @returns {void}
 */
export function seedFromCatalog(panelState, catalog) {
    if (!panelState) return;
    panelState.nameMap.clear();
    for (const entry of catalog) {
        if (entry.norad_id != null) {
            panelState.nameMap.set(entry.norad_id, entry.name || String(entry.norad_id));
        }
    }
}

// ---------------------------------------------------------------------------
// Step 22: addAlert
// ---------------------------------------------------------------------------

/**
 * Add a new anomaly alert to the panel. (F-055)
 *
 * Alert key: norad_id + '_' + epoch_utc (unique per detection event).
 * Newest alerts appear at the top. Panel is capped at MAX_ALERTS entries.
 *
 * @param {Object} panelState - Panel state from initAlertPanel.
 * @param {Object} anomalyEvent - Anomaly message from backend WebSocket.
 * @param {Map<number, string>} [nameMapOverride] - Optional name map from main.js.
 * @returns {void}
 */
export function addAlert(panelState, anomalyEvent, nameMapOverride) {
    if (!panelState) return;

    const { norad_id, epoch_utc, anomaly_type } = anomalyEvent;
    const key = `${norad_id}_${epoch_utc}`;

    // Avoid duplicate entries for the same event
    if (panelState.alerts.has(key)) return;

    // Resolve object name: prefer nameMapOverride, then panelState.nameMap, then NORAD ID string
    const nameMap = nameMapOverride || panelState.nameMap;
    const objectName = nameMap.get(norad_id) || String(norad_id);

    // Format time: HH:MM:SS UTC
    const epochDate = new Date(epoch_utc);
    const timeStr = epochDate.toISOString().substring(11, 19) + ' UTC';

    // Build alert item DOM element
    const alertEl = document.createElement('div');
    alertEl.className = 'alert-item active';
    alertEl.dataset.key = key;
    alertEl.dataset.noradId = String(norad_id);
    alertEl.dataset.epochUtc = epoch_utc;

    // Anomaly type badge
    const badge = document.createElement('span');
    badge.className = `alert-type-badge ${_sanitizeClass(anomaly_type || 'unknown')}`;
    badge.textContent = (anomaly_type || 'unknown').replace(/_/g, ' ');
    alertEl.appendChild(badge);

    // Object name + time
    const infoLine = document.createElement('div');
    infoLine.style.marginTop = '2px';
    infoLine.innerHTML =
        `<span style="color:#ddd">${_escapeHtml(objectName)}</span>` +
        ` <span style="color:#666;font-size:11px">${_escapeHtml(timeStr)}</span>`;
    alertEl.appendChild(infoLine);

    // Status indicator
    const statusEl = document.createElement('div');
    statusEl.className = 'alert-status active';
    statusEl.textContent = 'ACTIVE';
    alertEl.appendChild(statusEl);

    // Prepend (newest on top)
    panelState.listEl.insertBefore(alertEl, panelState.listEl.firstChild);

    // Store entry
    panelState.alerts.set(key, {
        el: alertEl,
        statusEl,
        data: anomalyEvent,
        status: 'active',
        detectedAt: epochDate,
    });

    // Enforce cap: remove oldest entries beyond MAX_ALERTS
    if (panelState.alerts.size > MAX_ALERTS) {
        // The Map retains insertion order; the oldest entry is the first one
        const firstKey = panelState.alerts.keys().next().value;
        const oldest = panelState.alerts.get(firstKey);
        if (oldest && oldest.el.parentNode) {
            oldest.el.parentNode.removeChild(oldest.el);
        }
        panelState.alerts.delete(firstKey);
    }
}

// ---------------------------------------------------------------------------
// Step 23: updateAlertStatus
// ---------------------------------------------------------------------------

/**
 * Update the status of existing alerts for a NORAD ID. (F-055)
 *
 * Finds all alerts for the given NORAD ID that are not yet 'resolved' and
 * updates their status. When resolved, displays time-to-resolution and dims
 * the alert.
 *
 * @param {Object} panelState - Panel state from initAlertPanel.
 * @param {number} noradId - NORAD catalog ID.
 * @param {string} newStatus - New status: 'active' | 'recalibrating' | 'resolved'.
 * @param {string|null} resolutionTime - ISO-8601 UTC time of resolution, or null.
 * @returns {void}
 */
export function updateAlertStatus(panelState, noradId, newStatus, resolutionTime) {
    if (!panelState) return;

    for (const [key, entry] of panelState.alerts) {
        if (parseInt(entry.data.norad_id, 10) !== noradId) continue;
        if (entry.status === 'resolved') continue;

        entry.status = newStatus;

        // Update item CSS class
        entry.el.className = `alert-item ${newStatus}`;

        // Update status indicator text and class
        entry.statusEl.className = `alert-status ${newStatus}`;
        entry.statusEl.textContent = newStatus.toUpperCase();

        if (newStatus === 'resolved' && resolutionTime && entry.detectedAt) {
            const resolutionDate = new Date(resolutionTime);
            const durationMs = resolutionDate - entry.detectedAt;
            const durationSec = Math.round(durationMs / 1000);
            const durationStr = durationSec >= 60
                ? `${Math.floor(durationSec / 60)}m ${durationSec % 60}s`
                : `${durationSec}s`;

            // Append resolution duration
            const resolutionEl = document.createElement('div');
            resolutionEl.style.color = '#66cc66';
            resolutionEl.style.fontSize = '11px';
            resolutionEl.textContent = `Resolved in ${durationStr}`;
            entry.el.appendChild(resolutionEl);

            // Dim the entry
            entry.el.style.opacity = '0.6';
        }
    }
}

// ---------------------------------------------------------------------------
// Step 24: clearResolved
// ---------------------------------------------------------------------------

/**
 * Remove all resolved alerts from the panel and internal map. (F-055, NF-004)
 *
 * @param {Object} panelState - Panel state from initAlertPanel.
 * @returns {void}
 */
export function clearResolved(panelState) {
    if (!panelState) return;

    for (const [key, entry] of panelState.alerts) {
        if (entry.status === 'resolved') {
            if (entry.el.parentNode) {
                entry.el.parentNode.removeChild(entry.el);
            }
            panelState.alerts.delete(key);
        }
    }
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/**
 * Sanitize a string for use as a CSS class name.
 * @param {string} str - Input string.
 * @returns {string} Safe CSS class name (alphanumeric and underscores only).
 */
function _sanitizeClass(str) {
    return str.replace(/[^a-zA-Z0-9_-]/g, '_');
}

/**
 * Escape HTML special characters to prevent XSS in innerHTML.
 * @param {string} str - Input string.
 * @returns {string} HTML-escaped string.
 */
function _escapeHtml(str) {
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}
