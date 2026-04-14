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

    // The header lives in the HTML as #alert-panel-header (always visible).
    const headerEl = document.getElementById('alert-panel-header');
    const toggleSpan = document.getElementById('alert-panel-toggle');

    // containerEl (#alert-panel) holds only the scrollable alert list.
    const listEl = document.createElement('div');
    listEl.className = 'alert-list';
    containerEl.appendChild(listEl);

    const panelState = {
        containerEl,
        listEl,
        toggleSpan,
        collapsed: false,
        /** @type {Map<string, {el: HTMLElement, data: Object, status: string}>} */
        alerts: new Map(),
        /** @type {Map<number, string>} noradId -> object name */
        nameMap: new Map(),
    };

    if (headerEl) {
        headerEl.addEventListener('click', () => _toggleCollapse(panelState));
    }

    return panelState;
}

/**
 * Expand the alert panel if it is currently collapsed.
 * @param {Object} panelState - Panel state from initAlertPanel.
 */
export function expandAlertPanel(panelState) {
    if (!panelState || !panelState.collapsed) return;
    panelState.collapsed = false;
    panelState.containerEl.style.display = '';
    if (panelState.toggleSpan) panelState.toggleSpan.textContent = '\u25b2';
}

function _toggleCollapse(panelState) {
    if (panelState.collapsed) {
        expandAlertPanel(panelState);
    } else {
        panelState.collapsed = true;
        panelState.containerEl.style.display = 'none';
        if (panelState.toggleSpan) panelState.toggleSpan.textContent = '\u25bc';
    }
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
 * Step 9: Displays peak NIS and peak residual magnitude from the anomaly message.
 * Step 16: Accepts optional onClickCallback(noradId) for alert card click wiring.
 *
 * @param {Object} panelState - Panel state from initAlertPanel.
 * @param {Object} anomalyEvent - Anomaly message from backend WebSocket.
 * @param {Map<number, string>} [nameMapOverride] - Optional name map from main.js.
 * @param {function(number): void|null} [onClickCallback] - Optional callback for card clicks.
 * @returns {void}
 */
export function addAlert(panelState, anomalyEvent, nameMapOverride, onClickCallback = null) {
    if (!panelState) return;

    const { norad_id, epoch_utc, anomaly_type, nis, innovation_eci_km } = anomalyEvent;
    const key = `${norad_id}_${epoch_utc}`;

    // Avoid duplicate entries for the same event
    if (panelState.alerts.has(key)) return;

    // Resolve object name: prefer nameMapOverride, then panelState.nameMap, then NORAD ID string
    const nameMap = nameMapOverride || panelState.nameMap;
    const objectName = nameMap.get(norad_id) || String(norad_id);

    // Format time: HH:MM:SS UTC
    const epochDate = new Date(epoch_utc);
    const timeStr = epochDate.toISOString().substring(11, 19) + ' UTC';

    // Step 9: Compute peak residual magnitude from innovation position components [0:3].
    let peakResidualStr = '';
    if (Array.isArray(innovation_eci_km) && innovation_eci_km.length >= 3) {
        const mag = Math.sqrt(
            innovation_eci_km[0] ** 2 +
            innovation_eci_km[1] ** 2 +
            innovation_eci_km[2] ** 2
        );
        peakResidualStr = mag.toFixed(3) + ' km';
    }
    const peakNisStr = (nis != null) ? Number(nis).toFixed(2) : '';

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

    // Step 9: Peak NIS and residual metrics row.
    if (peakNisStr || peakResidualStr) {
        const metricsEl = document.createElement('div');
        metricsEl.className = 'alert-metrics';
        const parts = [];
        if (peakNisStr) parts.push(`Peak NIS: ${peakNisStr}`);
        if (peakResidualStr) parts.push(`Peak residual: ${peakResidualStr}`);
        metricsEl.textContent = parts.join('  \u00b7  ');
        alertEl.appendChild(metricsEl);
    }

    // Status indicator
    const statusEl = document.createElement('div');
    statusEl.className = 'alert-status active';
    statusEl.textContent = 'ACTIVE';
    alertEl.appendChild(statusEl);

    // Dismiss button — top-right of card, removes this entry only.
    const dismissBtn = document.createElement('button');
    dismissBtn.className = 'alert-dismiss-btn';
    dismissBtn.textContent = '×';
    dismissBtn.title = 'Dismiss';
    dismissBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        if (alertEl.parentNode) alertEl.parentNode.removeChild(alertEl);
        panelState.alerts.delete(key);
    });
    alertEl.appendChild(dismissBtn);

    // Step 16: Wire alert card click to object selection callback.
    if (onClickCallback !== null) {
        alertEl.style.cursor = 'pointer';
        alertEl.addEventListener('click', () => {
            onClickCallback(norad_id);
        });
    }

    // Auto-expand the panel when a new anomaly arrives.
    expandAlertPanel(panelState);

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

        // Step 10 risk mitigation: if stuck in 'recalibrating' for > 5 minutes,
        // auto-resolve (handles objects that drop from catalog before next state_update).
        if (newStatus === 'recalibrating') {
            const capturedKey = key;
            setTimeout(() => {
                if (!panelState.alerts.has(capturedKey)) return;
                const e = panelState.alerts.get(capturedKey);
                if (e.status === 'recalibrating') {
                    e.status = 'resolved';
                    e.el.className = 'alert-item resolved';
                    e.statusEl.className = 'alert-status resolved';
                    e.statusEl.textContent = 'RESOLVED';
                    const timeoutEl = document.createElement('div');
                    timeoutEl.style.color = '#66cc66';
                    timeoutEl.style.fontSize = '11px';
                    timeoutEl.textContent = 'Resolved (timeout)';
                    e.el.appendChild(timeoutEl);
                    e.el.style.opacity = '0.6';
                }
            }, 5 * 60 * 1000); // 5-minute timeout
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
// Step 9 (conjunction plan): updateAlertConjunctions
// ---------------------------------------------------------------------------

/**
 * Append or replace the conjunction risk section on an active alert card.
 *
 * Finds the active (non-resolved) alert card for noradId in panelState.alerts.
 * If found, creates a .conjunction-section and appends it (or replaces an
 * existing one) to the alert card element, following the in-place append
 * pattern used by _appendAnomalyHistorySection in main.js.
 *
 * If the alert card is scrolled out of view in the panel, applies the
 * .conjunction-pulse CSS animation for 1 second to draw attention.
 *
 * @param {Object} panelState - Panel state from initAlertPanel.
 * @param {number} noradId - NORAD catalog ID.
 * @param {Object} conjunctionMessage - conjunction_risk message from backend.
 * @returns {void}
 */
export function updateAlertConjunctions(panelState, noradId, conjunctionMessage) {
    if (!panelState) return;

    // Find the most recent active (non-resolved) alert card for this noradId.
    let targetEntry = null;
    for (const [, entry] of panelState.alerts) {
        if (
            parseInt(entry.data.norad_id, 10) === noradId &&
            entry.status !== 'resolved'
        ) {
            targetEntry = entry;
        }
    }

    if (!targetEntry) return;

    const alertEl = targetEntry.el;

    // Remove existing conjunction section if present (handles re-screening).
    const existing = alertEl.querySelector('.conjunction-section');
    if (existing) {
        alertEl.removeChild(existing);
    }

    const section = document.createElement('div');
    section.className = 'conjunction-section';

    const header = document.createElement('div');
    header.className = 'conjunction-header';
    header.textContent = 'Conjunction Risk';
    section.appendChild(header);

    const firstOrder = conjunctionMessage.first_order || [];
    const secondOrder = conjunctionMessage.second_order || [];

    if (firstOrder.length === 0 && secondOrder.length === 0) {
        const noRisk = document.createElement('div');
        noRisk.className = 'conjunction-entry';
        noRisk.style.color = '#666';
        noRisk.textContent = 'No conjunctions within 5 km / 10 km in next 90 min';
        section.appendChild(noRisk);
    } else {
        // First-order entries.
        for (const entry of firstOrder) {
            const row = document.createElement('div');
            row.className = 'conjunction-entry first-order';
            const tca = _formatTca(entry.time_of_closest_approach_utc);
            row.innerHTML =
                `<span class="conjunction-norad">${_escapeHtml(String(entry.norad_id))}</span>` +
                ` <span class="conjunction-name">${_escapeHtml(entry.name || '')}</span>` +
                ` <span class="conjunction-dist">${Number(entry.min_distance_km).toFixed(1)} km</span>` +
                ` <span class="conjunction-tca">${_escapeHtml(tca)}</span>`;
            section.appendChild(row);
        }
        // Second-order entries.
        for (const entry of secondOrder) {
            const row = document.createElement('div');
            row.className = 'conjunction-entry second-order';
            const tca = _formatTca(entry.time_of_closest_approach_utc);
            const viaName = conjunctionMessage.first_order
                ? (conjunctionMessage.first_order.find((f) => f.norad_id === entry.via_norad_id) || {}).name || String(entry.via_norad_id)
                : String(entry.via_norad_id);
            row.innerHTML =
                `<span class="conjunction-norad">${_escapeHtml(String(entry.norad_id))}</span>` +
                ` <span class="conjunction-name">${_escapeHtml(entry.name || '')}</span>` +
                ` <span class="conjunction-dist">${Number(entry.min_distance_km).toFixed(1)} km</span>` +
                ` <span class="conjunction-tca">${_escapeHtml(tca)}</span>` +
                ` <span class="conjunction-via">via ${_escapeHtml(viaName)}</span>`;
            section.appendChild(row);
        }
    }

    alertEl.appendChild(section);

    // Pulse the card border if it is scrolled out of the panel's visible area.
    if (panelState.listEl) {
        const listRect = panelState.listEl.getBoundingClientRect();
        const cardRect = alertEl.getBoundingClientRect();
        const isOutOfView = cardRect.bottom < listRect.top || cardRect.top > listRect.bottom;
        if (isOutOfView) {
            alertEl.classList.add('conjunction-pulse');
            setTimeout(() => alertEl.classList.remove('conjunction-pulse'), 1200);
        }
    }
}

/**
 * Format a TCA ISO-8601 UTC string as HH:MM:SS UTC.
 * @param {string} tcaStr - ISO-8601 UTC string.
 * @returns {string} Formatted time string.
 */
function _formatTca(tcaStr) {
    if (!tcaStr) return '';
    try {
        const d = new Date(tcaStr);
        return d.toISOString().substring(11, 19) + ' UTC';
    } catch (_) {
        return tcaStr;
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
