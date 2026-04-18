/**
 * @module main
 * @description Application entry point. Establishes WebSocket connection
 * to the backend and routes incoming messages to globe, residuals, and
 * alerts modules.
 */

import { initGlobe, updateSatellitePosition, updateUncertaintyEllipsoid, highlightAnomaly, setupSelectionHandler, drawHistoricalTrack, drawPredictiveTrackWithCone, clearTrackAndCone, applyConjunctionRisk, clearConjunctionRisk, getConjunctionRiskMap, getLastConjunctionMessage, removeSatelliteEntity, flyToObject, getRenderedEntityCount, applySampledTrack } from './globe.js';
import { initResidualChart, appendResidualDataPoint, selectObject, addAnomalyMarker, resizeChart } from './residuals.js';
import { initAlertPanel, addAlert, updateAlertStatus, updateAlertConjunctions, seedFromCatalog as alertSeedFromCatalog } from './alerts.js';
import { initAlertSound, triggerAlertSound, setAlertSoundMuted } from './alertsound.js';
import { initAlertFlash, triggerAlertFlash } from './alertflash.js';

// ---------------------------------------------------------------------------
// Module-level state (step 7)
// ---------------------------------------------------------------------------

/** @type {Object|null} Cesium.Viewer instance */
let viewer = null;

/** @type {Object|null} Chart state from initResidualChart */
let chartState = null;

/** @type {Object|null} Panel state from initAlertPanel */
let panelState = null;

/** @type {boolean} Current mute state for the alarm sound. */
let _soundMuted = false;

/** @type {number|null} Currently selected NORAD ID */
let selectedNoradId = null;

/** @type {HTMLElement|null} Tracked object counter element */
let trackedCountEl = null;

/** @type {boolean} Current chart panel visibility state */
let _chartVisible = false;

/** @type {WebSocket|null} Active WebSocket instance */
let ws = null;

/** @type {string} Backend base URL (derived from window.location) */
let backendBaseUrl = '';

/**
 * Lookup map from NORAD ID to object name.
 * Populated from GET /catalog on connect/reconnect.
 * @type {Map<number, string>}
 */
const nameMap = new Map();

/**
 * Step 12: Latest state_update (or recalibration) message per NORAD ID.
 * Used by the object info panel for O(1) access to current state without re-fetching.
 * @type {Map<number, Object>}
 */
const latestStateMap = new Map();

/**
 * Step 17: Catalog entries map for object_class lookup.
 * Populated from GET /catalog on connect/reconnect.
 * @type {Map<number, Object>}
 */
const catalogMap = new Map();

// Reconnection state
let _reconnectDelay_s = 1;
const _MAX_RECONNECT_DELAY_S = 30;
let _reconnecting = false;

// Deep-link: NORAD ID to fly to once its first state_update entity is created.
let _pendingDeepLinkNoradId = null;

// ---------------------------------------------------------------------------
// Step 5: fetchCatalog
// ---------------------------------------------------------------------------

/**
 * Fetch the current catalog from the REST endpoint.
 * @param {string} baseUrl - Backend base URL (e.g., 'http://localhost:8000').
 * @returns {Promise<Array<Object>>} List of tracked objects, or empty array on error.
 */
export async function fetchCatalog(baseUrl) {
    try {
        const response = await fetch(baseUrl + '/catalog');
        if (!response.ok) {
            console.error('[main] GET /catalog returned', response.status);
            return [];
        }
        return await response.json();
    } catch (err) {
        console.error('[main] fetchCatalog error:', err);
        return [];
    }
}

// ---------------------------------------------------------------------------
// Step 4: routeMessage
// ---------------------------------------------------------------------------

/**
 * Route an incoming WebSocket message to the appropriate handler.
 * @param {Object} message - Parsed JSON message from backend.
 * @returns {void}
 */
/** Maximum TLE age in milliseconds before an object is suppressed from the globe. */
const MAX_TLE_AGE_MS = 28 * 24 * 60 * 60 * 1000;

/**
 * Returns true if the epoch string is within the 28-day staleness window.
 * @param {string|null} epochUtc - ISO-8601 UTC string.
 * @returns {boolean}
 */
function _isFreshEpoch(epochUtc) {
    if (!epochUtc) return false;
    return (Date.now() - new Date(epochUtc).getTime()) <= MAX_TLE_AGE_MS;
}

export function routeMessage(message) {
    if (!viewer) return;

    const { type, norad_id } = message;

    if (type === 'state_update') {
        if (!_isFreshEpoch(message.epoch_utc)) {
            latestStateMap.delete(norad_id);
            removeSatelliteEntity(viewer, norad_id);
            _updateTrackedCount();
            return;
        }
        updateSatellitePosition(viewer, message);
        updateUncertaintyEllipsoid(
            viewer,
            norad_id,
            message.covariance_diagonal_km2,
            message.eci_km,
            message.epoch_utc
        );
        // Color diverged/anomalous objects red immediately on state_update so
        // objects flagged during a previous session are red on page load/reconnect.
        if (message.anomaly_type !== null) {
            highlightAnomaly(viewer, norad_id, message.anomaly_type);
        }
        // Deep-link fly-to: deferred until the entity exists in entityMap.
        if (_pendingDeepLinkNoradId !== null && norad_id === _pendingDeepLinkNoradId) {
            flyToObject(viewer, norad_id);
            if (chartState) selectObject(chartState, norad_id);
            _pendingDeepLinkNoradId = null;
        }
        if (norad_id === selectedNoradId && chartState) {
            appendResidualDataPoint(chartState, message);
        }
        // Step 12: Store latest state for info panel.
        latestStateMap.set(norad_id, message);
        _updateTrackedCount();
        // Step 10: Resolve any recalibrating alerts when filter returns to normal
        // (anomaly_type === null confirms recalibration cycle is complete — F-034).
        if (message.anomaly_type === null && panelState) {
            _resolveRecalibratingAlerts(norad_id, message.epoch_utc);
        }

        // Conjunction auto-clear (plan step 10): on ANY state_update for the
        // anomalous object, clear conjunction risk highlighting and show a toast.
        // The trigger is the next processing cycle for that object regardless of
        // anomaly status.
        const lastConjMsg = getLastConjunctionMessage();
        if (lastConjMsg && lastConjMsg.anomalous_norad_id === norad_id) {
            const allFlaggedIds = Array.from(getConjunctionRiskMap().keys());
            // Include the anomalous object itself in the clear set so its color
            // is restored if it was inadvertently in the risk map.
            if (!allFlaggedIds.includes(norad_id)) allFlaggedIds.push(norad_id);
            clearConjunctionRisk(viewer, allFlaggedIds);
            _showConjunctionClearedToast(norad_id, message.epoch_utc);
        }

    } else if (type === 'anomaly') {
        highlightAnomaly(viewer, norad_id, message.anomaly_type);
        if (panelState) {
            addAlert(panelState, message, nameMap, (clickedId) => {
                selectedNoradId = clickedId;
                if (chartState) selectObject(chartState, clickedId);
                _showObjectInfoPanel(clickedId);
                _setChartVisible(true);
                flyToObject(viewer, clickedId);
                _fetchAndDrawTrack(clickedId).catch((err) => {
                    console.warn('[main] _fetchAndDrawTrack (alert click) error:', err);
                });
            }, _dismissAlert);
        }
        if (norad_id === selectedNoradId) { _setChartVisible(true); }
        // Obtrusive alerting: audio alarm + fullscreen flash for live anomaly messages.
        // triggerAlertSound() is debounced internally (2s cooldown).
        // triggerAlertFlash() resets if the overlay is already visible.
        // These are ONLY called here in routeMessage(), never in the reconnect seed
        // path (which calls addAlert() directly), so historical alerts do not fire.
        triggerAlertSound();
        const _flashObjName = nameMap.get(norad_id) || String(norad_id);
        triggerAlertFlash(_flashObjName, message.anomaly_type || 'unknown');

        // Step 8: Add anomaly marker to residual chart at the anomaly epoch.
        if (chartState) {
            addAnomalyMarker(chartState, norad_id, message.epoch_utc, message.anomaly_type);
        }
        if (norad_id === selectedNoradId && chartState) {
            appendResidualDataPoint(chartState, message);
        }

    } else if (type === 'recalibration') {
        // recalibration includes an updated state; update position and ellipsoid.
        if (!_isFreshEpoch(message.epoch_utc)) {
            removeSatelliteEntity(viewer, norad_id);
            return;
        }
        updateSatellitePosition(viewer, message);
        updateUncertaintyEllipsoid(
            viewer,
            norad_id,
            message.covariance_diagonal_km2,
            message.eci_km,
            message.epoch_utc
        );
        // Step 10: Transition to 'recalibrating' (not 'resolved') — true resolution
        // is confirmed by the next state_update with anomaly_type === null.
        if (panelState) {
            updateAlertStatus(panelState, norad_id, 'recalibrating', null);
        }
        latestStateMap.set(norad_id, message);

    } else if (type === 'track_update') {
        applySampledTrack(viewer, message);
    } else if (type === 'conjunction_risk') {
        // Conjunction risk (plan step 10): apply globe highlighting and enrich alert card.
        applyConjunctionRisk(viewer, message);
        if (panelState) {
            updateAlertConjunctions(panelState, message.anomalous_norad_id, message);
        }
        // Refresh info panel if the selected object is the anomalous one or is at risk.
        const isRelevant =
            selectedNoradId === message.anomalous_norad_id ||
            (message.first_order || []).some((e) => e.norad_id === selectedNoradId) ||
            (message.second_order || []).some((e) => e.norad_id === selectedNoradId);
        if (isRelevant) {
            _showObjectInfoPanel(selectedNoradId);
        }
    }
}

// ---------------------------------------------------------------------------
// Step 3: connectWebSocket (with exponential backoff reconnection)
// ---------------------------------------------------------------------------

/**
 * Connect to the backend WebSocket endpoint with automatic reconnection.
 * @param {string} url - WebSocket URL (e.g., 'ws://localhost:8000/ws/live').
 * @returns {WebSocket} The WebSocket instance.
 */
export function connectWebSocket(url) {
    console.info('[main] WebSocket connecting to', url);
    const socket = new WebSocket(url);
    ws = socket;

    socket.onopen = async () => {
        console.info('[main] WebSocket connected.');
        _reconnectDelay_s = 1;
        _reconnecting = false;
        const wsStatusEl = document.getElementById('ws-status');
        if (wsStatusEl) {
            wsStatusEl.textContent = 'LIVE';
            wsStatusEl.className = 'ws-status ws-live';
        }

        // NF-012: On reconnect, fetch catalog to re-seed globe and charts (step 27).
        const catalog = await fetchCatalog(backendBaseUrl);
        _seedFromCatalog(catalog);

        // Seed alert panel with any active anomalies that fired while disconnected.
        try {
            const resp = await fetch(`${backendBaseUrl}/alerts/active`);
            if (resp.ok) {
                const activeAlerts = await resp.json();
                for (const alert of activeAlerts) {
                    if (panelState) {
                        addAlert(panelState, alert, nameMap, (clickedId) => {
                            selectedNoradId = clickedId;
                            if (chartState) selectObject(chartState, clickedId);
                            _showObjectInfoPanel(clickedId);
                            _setChartVisible(true);
                            flyToObject(viewer, clickedId);
                            _fetchAndDrawTrack(clickedId).catch((err) => {
                                console.warn('[main] _fetchAndDrawTrack (alert seed) error:', err);
                            });
                        }, _dismissAlert);
                    }
                }
            }
        } catch (err) {
            console.warn('[main] Failed to seed active alerts on connect:', err);
        }
    };

    socket.onmessage = (event) => {
        let message;
        try {
            message = JSON.parse(event.data);
        } catch (err) {
            console.error('[main] Failed to parse WebSocket message:', err);
            return;
        }
        routeMessage(message);
    };

    socket.onclose = (event) => {
        console.info('[main] WebSocket closed (code=%d). Scheduling reconnect.', event.code);
        _scheduleReconnect(url);
    };

    socket.onerror = (err) => {
        console.error('[main] WebSocket error:', err);
        // onclose will fire after onerror; reconnect is handled there.
    };

    return socket;
}

/**
 * Update the tracked object counter in the header from the number of
 * satellite entities currently rendered on the globe.
 * @returns {void}
 */
function _updateTrackedCount() {
    if (trackedCountEl) trackedCountEl.textContent = getRenderedEntityCount() + ' TRACKED';
}

/**
 * Return true if the given NORAD ID has any alert in panelState (active, recalibrating, or resolved).
 * @param {number} noradId - NORAD catalog ID.
 * @returns {boolean}
 */
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

/**
 * Show or hide the residual chart panel with a CSS transition.
 * After expansion, calls resizeChart so D3 recalculates dimensions.
 * @param {boolean} visible - True to expand, false to collapse.
 * @returns {void}
 */
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

/**
 * Schedule a WebSocket reconnection with exponential backoff and jitter.
 * @param {string} url - WebSocket URL to reconnect to.
 * @returns {void}
 */
function _scheduleReconnect(url) {
    if (_reconnecting) return;
    _reconnecting = true;
    const wsStatusEl = document.getElementById('ws-status');
    if (wsStatusEl) {
        wsStatusEl.textContent = 'RECONNECTING';
        wsStatusEl.className = 'ws-status ws-reconnecting';
    }

    // Jitter: ±25% of current delay
    const jitter = (_reconnectDelay_s * 0.25) * (Math.random() * 2 - 1);
    const delay_s = Math.min(_reconnectDelay_s + jitter, _MAX_RECONNECT_DELAY_S);
    console.info('[main] Reconnecting in %.1fs...', delay_s);

    setTimeout(() => {
        _reconnecting = false;
        // Double delay for next attempt, capped at max
        _reconnectDelay_s = Math.min(_reconnectDelay_s * 2, _MAX_RECONNECT_DELAY_S);
        connectWebSocket(url);
    }, delay_s * 1000);
}

// ---------------------------------------------------------------------------
// Steps 14, 17: Object info panel helpers
// ---------------------------------------------------------------------------

/**
 * Escape HTML special characters to prevent XSS in innerHTML.
 * Duplicated from alerts.js per plan recommendation (no shared util module).
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

/**
 * Fetch anomaly history for a tracked object from GET /object/{norad_id}/anomalies.
 * Returns the JSON array on success, or an empty array on error.
 *
 * Called by _showObjectInfoPanel after the static info is rendered, so
 * a slow fetch does not block the core panel display.
 *
 * @param {string} baseUrl - Backend base URL (e.g., 'http://localhost:8000').
 * @param {number} noradId - NORAD catalog ID.
 * @returns {Promise<Array<Object>>} Anomaly event array, or [] on error.
 */
async function _fetchAnomalyHistory(baseUrl, noradId) {
    try {
        const response = await fetch(`${baseUrl}/object/${noradId}/anomalies`);
        if (!response.ok) {
            console.warn('[main] GET /object/' + noradId + '/anomalies returned', response.status);
            return [];
        }
        return await response.json();
    } catch (err) {
        console.warn('[main] _fetchAnomalyHistory error:', err);
        return [];
    }
}

/**
 * Render the anomaly history section and append it to the info panel.
 * Called asynchronously after the static info rows are already displayed,
 * so the panel is visible immediately without waiting for the fetch.
 *
 * @param {HTMLElement} panelEl - The #object-info-panel DOM element.
 * @param {number} noradId - NORAD catalog ID being displayed.
 * @returns {Promise<void>}
 */
async function _appendAnomalyHistorySection(panelEl, noradId) {
    const events = await _fetchAnomalyHistory(backendBaseUrl, noradId);

    // Guard: if the panel was replaced (user clicked away), do not append stale data.
    if (!panelEl.isConnected || panelEl.style.display === 'none') return;
    // Guard: check that the panel still shows the same NORAD ID.
    if (selectedNoradId !== noradId) return;

    const section = document.createElement('div');
    section.className = 'anomaly-history-section';

    const title = document.createElement('div');
    title.className = 'anomaly-history-title';
    title.textContent = 'Anomaly History';
    section.appendChild(title);

    if (events.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'anomaly-history-empty';
        empty.textContent = 'No anomaly history';
        section.appendChild(empty);
    } else {
        for (const ev of events) {
            const entry = document.createElement('div');
            entry.className = 'anomaly-history-entry';

            // Anomaly type badge — reuses CSS class from alert panel.
            const badge = document.createElement('span');
            badge.className = 'alert-type-badge ' + _escapeHtml(ev.anomaly_type || '');
            badge.textContent = (ev.anomaly_type || 'unknown').replace('_', ' ');

            // Detection epoch formatted as YYYY-MM-DD HH:MM UTC.
            const epochText = document.createTextNode(
                ' ' + String(ev.detection_epoch_utc || '').replace('T', ' ').substring(0, 16) + ' UTC'
            );

            // NIS value.
            const nisText = document.createTextNode(
                ' NIS: ' + (ev.nis_value != null ? Number(ev.nis_value).toFixed(1) : 'N/A')
            );

            // Resolved/unresolved indicator.
            const statusSpan = document.createElement('span');
            if (ev.status === 'resolved' && ev.resolution_epoch_utc) {
                statusSpan.className = 'anomaly-resolved';
                const resolvedTime = String(ev.resolution_epoch_utc).replace('T', ' ').substring(0, 16);
                const durText = ev.recalibration_duration_s != null
                    ? ' (' + Math.round(ev.recalibration_duration_s) + 's)'
                    : '';
                statusSpan.textContent = ' resolved ' + resolvedTime + durText;
            } else {
                statusSpan.className = 'anomaly-unresolved';
                statusSpan.textContent = ' ' + (ev.status || 'active');
            }

            entry.appendChild(badge);
            entry.appendChild(epochText);
            entry.appendChild(nisText);
            entry.appendChild(document.createElement('br'));
            entry.appendChild(statusSpan);
            section.appendChild(entry);
        }
    }

    panelEl.appendChild(section);
}

/**
 * Fetch the track for a NORAD ID, then draw historical and forward tracks on the globe.
 * Clears any existing track first. Called on object selection.
 *
 * @param {number} noradId - NORAD catalog ID.
 * @returns {Promise<void>}
 */
async function _fetchAndDrawTrack(noradId) {
    // Clear existing track immediately for instant visual feedback.
    if (viewer) clearTrackAndCone(viewer);

    try {
        const url = `${backendBaseUrl}/object/${noradId}/track?seconds_back=1500&seconds_forward=1500`;
        const response = await fetch(url);
        if (!response.ok) {
            console.warn('[main] GET track returned', response.status, 'for NORAD', noradId);
            return;
        }
        const data = await response.json();

        // Guard: if selection changed while fetching, discard stale track data.
        if (selectedNoradId !== noradId) return;
        if (!viewer) return;

        if (data.backward_track && data.backward_track.length > 0) {
            drawHistoricalTrack(viewer, data.backward_track);
        }
        if (data.forward_track && data.forward_track.length > 0) {
            drawPredictiveTrackWithCone(viewer, data.forward_track);
        }
    } catch (err) {
        console.warn('[main] _fetchAndDrawTrack error for NORAD', noradId, ':', err);
    }
}

/**
 * Show or hide the object info panel for the given NORAD ID. (Step 14, F-056)
 *
 * Reads from latestStateMap (current ECI position/velocity/confidence) and
 * catalogMap (object_class). If noradId is null, hides the panel.
 *
 * The static info rows are rendered synchronously for immediate display.
 * The anomaly history section is fetched async and appended when the
 * response arrives, so the panel is never blocked by a slow fetch.
 *
 * @param {number|null} noradId - NORAD catalog ID to display, or null to hide.
 * @returns {void}
 */
function _showObjectInfoPanel(noradId) {
    const panelEl = document.getElementById('object-info-panel');
    if (!panelEl) return;

    if (noradId === null) {
        panelEl.style.display = 'none';
        return;
    }

    const state = latestStateMap.get(noradId);
    const objName = nameMap.get(noradId) || String(noradId);
    const catalogEntry = catalogMap.get(noradId);
    const objectClass = (catalogEntry && catalogEntry.object_class) ? catalogEntry.object_class : null;

    const confScore = (state && state.confidence != null) ? state.confidence : null;
    let confClass = 'conf-red';
    let confPct = 'N/A';
    if (confScore !== null) {
        confPct = (confScore * 100).toFixed(1) + '%';
        if (confScore > 0.85) confClass = 'conf-green';
        else if (confScore >= 0.60) confClass = 'conf-amber';
        else confClass = 'conf-red';
    }

    let posStr = 'N/A';
    let velStr = 'N/A';
    let sgp4PosStr = 'N/A';
    let tleEpochStr = 'N/A';
    let tleAgeStr = '';
    let tleAgeClass = 'age-ok';
    let deltaKm = null;

    if (state) {
        if (state.eci_km && state.eci_km.length === 3) {
            posStr = state.eci_km.map((v) => v.toFixed(2)).join(', ') + ' km';
        }
        if (state.eci_km_s && state.eci_km_s.length === 3) {
            velStr = state.eci_km_s.map((v) => v.toFixed(4)).join(', ') + ' km/s';
        }
        if (state.sgp4_eci_km && state.sgp4_eci_km.length === 3) {
            sgp4PosStr = state.sgp4_eci_km.map((v) => v.toFixed(2)).join(', ') + ' km';
        }
        if (state.tle_epoch_utc) {
            const tleDate = new Date(state.tle_epoch_utc);
            tleEpochStr = state.tle_epoch_utc.replace('T', ' ').substring(0, 19) + ' UTC';
            const ageMs = Date.now() - tleDate.getTime();
            const ageDays = ageMs / 86400000;
            if (ageDays < 1) {
                tleAgeStr = `${(ageDays * 24).toFixed(1)} hr ago`;
                tleAgeClass = 'age-ok';
            } else if (ageDays < 3) {
                tleAgeStr = `${ageDays.toFixed(1)} days ago`;
                tleAgeClass = 'age-warn';
            } else {
                tleAgeStr = `${ageDays.toFixed(1)} days ago`;
                tleAgeClass = 'age-old';
            }
        }
        // Delta: magnitude of position innovation (prediction vs observation).
        if (state.innovation_eci_km && state.innovation_eci_km.length >= 3) {
            const inn = state.innovation_eci_km;
            deltaKm = Math.sqrt(inn[0] ** 2 + inn[1] ** 2 + inn[2] ** 2);
        }
    }

    let deltaHtml = '';

    let rows = `
        <div class="info-row" title="NORAD catalog number — the unique international identifier for this space object."><span class="info-label">NORAD ID: </span><a class="norad-link" href="https://www.satcat.com/sats/${noradId}" target="_blank" rel="noopener">${_escapeHtml(String(noradId))}</a></div>
        <div class="info-row" title="Common name from the Space-Track catalog."><span class="info-label">Name: </span>${_escapeHtml(objName)}</div>`;
    if (objectClass) {
        rows += `<div class="info-row" title="Object classification: active_satellite, rocket_body, or debris."><span class="info-label">Class: </span>${_escapeHtml(objectClass)}</div>`;
    }
    rows += `
        <div class="info-row" title="ECI J2000 position [x, y, z] from the Kalman filter state estimate. Origin at Earth center, x toward vernal equinox, z toward north pole."><span class="info-label">Filter Pos: </span>${_escapeHtml(posStr)}</div>
        <div class="info-row" title="ECI J2000 position predicted by raw SGP4 propagation of the latest TLE, without Kalman filter correction. Divergence from Filter Pos indicates orbital maneuver or TLE error."><span class="info-label">SGP4 Pos: </span>${_escapeHtml(sgp4PosStr)}</div>
        <div class="info-row" title="ECI J2000 velocity [vx, vy, vz] from the Kalman filter state estimate. Typical LEO magnitude is ~7.5 km/s."><span class="info-label">ECI Vel: </span>${_escapeHtml(velStr)}</div>
        <div class="info-row" title="Filter confidence score (0–100%). Green &gt; 85%: well-converged. Amber 60–85%: moderate uncertainty. Red &lt; 60%: poor fit or diverging filter."><span class="info-label">Confidence: </span><span class="${confClass}">${_escapeHtml(confPct)}</span></div>
        <div class="info-row" title="The timestamp at which this TLE was measured — the starting point SGP4 propagates forward from. The older this is, the less accurate the position prediction, especially for maneuvering objects."><span class="info-label">TLE Epoch: </span>${_escapeHtml(tleEpochStr)}${tleAgeStr ? ` <span class="info-tle-age ${tleAgeClass}">(${_escapeHtml(tleAgeStr)})</span>` : ''}</div>`;
    if (deltaKm !== null) {
        let dClass = 'delta-ok';
        let deltaTip = 'Magnitude of the innovation vector (SGP4 prediction minus filter state). Small values (&lt;5 km) indicate good TLE accuracy. Large values indicate a maneuver or stale TLE.';
        if (deltaKm > 50) { dClass = 'delta-alert'; deltaTip = 'Large delta (&gt;50 km) — likely maneuver or significantly stale TLE. Filter is actively diverging.'; }
        else if (deltaKm > 5) { dClass = 'delta-warn'; deltaTip = 'Moderate delta (5–50 km) — TLE may be aging or object performed a small maneuver.'; }
        deltaHtml = `<div class="info-delta ${dClass}" title="${deltaTip}"><span class="info-delta-label">SGP4 vs Filter Delta</span>${deltaKm.toFixed(1)} km</div>`;
    }
    rows += deltaHtml;

    panelEl.innerHTML = `<div class="info-title">Object Info</div>${rows}`;
    panelEl.style.display = 'block';

    // Append anomaly history async — panel is already visible with static info.
    // _appendAnomalyHistorySection guards against stale data if selection changes.
    _appendAnomalyHistorySection(panelEl, noradId).catch((err) => {
        console.warn('[main] _appendAnomalyHistorySection error:', err);
    });
}

/**
 * Show a dismissible toast notification at the top of the side panel to inform
 * the operator that conjunction risk has been cleared for an object.
 *
 * The toast is inserted at the top of #side-panel and auto-dismisses after 8
 * seconds with a CSS fade-out transition. Uses the nameMap for the object name.
 *
 * @param {number} noradId - NORAD catalog ID of the object whose risk was cleared.
 * @param {string} epochUtc - ISO-8601 UTC epoch from the clearing state_update.
 * @returns {void}
 */
function _showConjunctionClearedToast(noradId, epochUtc) {
    const sidePanelEl = document.getElementById('side-panel');
    if (!sidePanelEl) return;

    const objName = nameMap.get(noradId) || String(noradId);
    const timeStr = epochUtc
        ? String(epochUtc).replace('T', ' ').substring(0, 19) + ' UTC'
        : '';

    const toastEl = document.createElement('div');
    toastEl.className = 'conjunction-toast';
    toastEl.textContent =
        'Conjunction risk cleared \u2014 ' + objName + (timeStr ? '  ' + timeStr : '');

    sidePanelEl.insertBefore(toastEl, sidePanelEl.firstChild);

    // Auto-dismiss after 8 seconds with CSS fade-out.
    setTimeout(() => {
        toastEl.style.opacity = '0';
        setTimeout(() => {
            if (toastEl.parentNode) {
                toastEl.parentNode.removeChild(toastEl);
            }
        }, 600); // allow transition to complete
    }, 8000);
}

/**
 * Resolve any 'recalibrating' alerts for a NORAD ID when the filter returns to normal.
 * Called from routeMessage on state_update with anomaly_type === null. (Step 10, F-034)
 *
 * @param {number} noradId - NORAD catalog ID.
 * @param {string} epochUtc - ISO-8601 UTC epoch of the resolving state_update.
 * @returns {void}
 */
function _resolveRecalibratingAlerts(noradId, epochUtc) {
    if (!panelState) return;
    let hasRecalibrating = false;
    for (const [, entry] of panelState.alerts) {
        if (parseInt(entry.data.norad_id, 10) === noradId && entry.status === 'recalibrating') {
            hasRecalibrating = true;
            break;
        }
    }
    if (hasRecalibrating) {
        updateAlertStatus(panelState, noradId, 'resolved', epochUtc);
        if (noradId === selectedNoradId) { _setChartVisible(false); }
    }
}

// ---------------------------------------------------------------------------
// Step 26+27: Seed globe, charts, and name map from catalog data
// ---------------------------------------------------------------------------

/**
 * Seed the globe, name map, and alerts module from a /catalog response.
 * Used on initial load (step 26) and reconnection (step 27).
 * @param {Array<Object>} catalog - Array of catalog entries from GET /catalog.
 * @returns {void}
 */
/**
 * POST to /alerts/dismiss to persist a card dismissal across page reloads.
 * Fire-and-forget: failures are logged but do not affect the UI.
 *
 * @param {number} noradId - NORAD catalog ID.
 * @param {string} epochUtc - ISO-8601 detection epoch matching the alerts table.
 */
function _dismissAlert(noradId, epochUtc) {
    fetch(`${backendBaseUrl}/alerts/dismiss`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ norad_id: noradId, epoch_utc: epochUtc }),
    }).catch((err) => {
        console.warn('[main] Failed to persist alert dismissal:', err);
    });
}

function _seedFromCatalog(catalog) {
    if (!catalog || catalog.length === 0) return;

    // Build name map (open question 2 resolution)
    nameMap.clear();
    for (const entry of catalog) {
        if (entry.norad_id != null) {
            nameMap.set(entry.norad_id, entry.name || String(entry.norad_id));
        }
    }

    // Step 17: Populate catalogMap for object_class lookup in info panel.
    // Also seed latestStateMap with catalog data so the info panel shows
    // name/class/state immediately on click, even before any WS messages arrive.
    catalogMap.clear();
    for (const entry of catalog) {
        if (entry.norad_id != null) {
            catalogMap.set(entry.norad_id, entry);
            // Pre-populate latestStateMap from catalog so info panel works on
            // first click regardless of whether trigger-process has been run.
            if (!latestStateMap.has(entry.norad_id)) {
                latestStateMap.set(entry.norad_id, {
                    type: 'state_update',
                    norad_id: entry.norad_id,
                    epoch_utc: entry.last_update_epoch_utc ?? null,
                    tle_epoch_utc: entry.last_update_epoch_utc ?? null,
                    eci_km: entry.eci_km ?? null,
                    eci_km_s: entry.eci_km_s ?? null,
                    sgp4_eci_km: null,
                    confidence: entry.confidence ?? null,
                    nis: entry.nis ?? 0,
                    innovation_eci_km: entry.innovation_eci_km ?? null,
                    anomaly_type: null,
                });
            }
        }
    }

    // Seed alerts module with catalog (for name resolution)
    if (panelState) {
        alertSeedFromCatalog(panelState, catalog);
    }

    // For each catalog entry with a full state, synthesize a state_update message
    // and route it through the normal path to populate the globe and chart buffers.
    for (const entry of catalog) {
        if (
            entry.eci_km != null &&
            entry.covariance_diagonal_km2 != null &&
            entry.last_update_epoch_utc != null &&
            _isFreshEpoch(entry.last_update_epoch_utc)
        ) {
            const syntheticMsg = {
                type: 'state_update',
                norad_id: entry.norad_id,
                epoch_utc: entry.last_update_epoch_utc,
                eci_km: entry.eci_km,
                eci_km_s: entry.eci_km_s,
                covariance_diagonal_km2: entry.covariance_diagonal_km2,
                nis: entry.nis ?? 0,
                innovation_eci_km: entry.innovation_eci_km ?? [0, 0, 0, 0, 0, 0],
                confidence: entry.confidence ?? 0,
                anomaly_type: null,
            };
            routeMessage(syntheticMsg);
        }
    }
    _updateTrackedCount();

    // Deep-link: ?select=NORAD_ID. Two paths depending on timing:
    // (a) WS connect burst arrived before catalog fetch — entity already exists,
    //     flyToObject works immediately and we clear the pending flag.
    // (b) Catalog fetch returns before WS burst — entity not yet in entityMap,
    //     store pending ID so the state_update handler fires the fly-to later.
    // Both paths are tried: immediate call is a no-op if entity is absent.
    if (_pendingDeepLinkNoradId === null) {
        const _dlParams = new URLSearchParams(window.location.search);
        const _dlSelect = _dlParams.get('select');
        if (_dlSelect) {
            const targetNoradId = parseInt(_dlSelect, 10);
            if (!isNaN(targetNoradId) && nameMap.has(targetNoradId)) {
                _pendingDeepLinkNoradId = targetNoradId;
                selectedNoradId = targetNoradId;
                _showObjectInfoPanel(targetNoradId);
                // Try immediately in case WS burst already created the entity.
                flyToObject(viewer, targetNoradId);
                if (chartState) selectObject(chartState, targetNoradId);
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Step 6: initApp — application entry point
// ---------------------------------------------------------------------------

/**
 * Initialize the application: fetch config, initialize modules, connect WebSocket.
 * Called at module load via the top-level call at the bottom of this file.
 * @returns {Promise<void>}
 */
export async function initApp() {
    // 1. Derive backend base URL from window.location (same host, port 8000).
    backendBaseUrl = window.location.protocol + '//' + window.location.hostname + ':8001';

    // 2. Fetch /config for the Cesium Ion token (resolves TD-018).
    let cesiumIonToken = '';
    try {
        const configResp = await fetch(backendBaseUrl + '/config');
        if (configResp.ok) {
            const config = await configResp.json();
            cesiumIonToken = config.cesium_ion_token || '';
        } else {
            console.error('[main] GET /config returned', configResp.status, '— globe may show grey.');
        }
    } catch (err) {
        console.error('[main] Failed to fetch /config:', err, '— globe may show grey.');
    }

    // 3. Initialize CesiumJS globe.
    viewer = initGlobe('cesium-container', cesiumIonToken);

    // 4. Initialize D3 residual chart.
    chartState = initResidualChart('residual-chart');

    // 5. Initialize anomaly alert panel.
    panelState = initAlertPanel('alert-panel');

    // 5a. Initialize audio alarm and visual flash overlay.
    initAlertSound();
    initAlertFlash();

    // Step 2: Assign tracked count element reference.
    trackedCountEl = document.getElementById('tracked-count');

    // 5b. Wire mute toggle button.
    const muteBtn = document.getElementById('mute-toggle');
    if (muteBtn) {
        muteBtn.addEventListener('click', () => {
            _soundMuted = !_soundMuted;
            setAlertSoundMuted(_soundMuted);
            muteBtn.textContent = _soundMuted ? '\uD83D\uDD07 UNMUTE' : '\uD83D\uDD14 MUTE';
        });
        // Sound starts ON; button shows action to take (mute it).
        muteBtn.textContent = '\uD83D\uDD14 MUTE';
    }

    // 6. Wire globe click selection to residuals, alerts, and info panel (F-056).
    // Step 15: _showObjectInfoPanel called here alongside selectObject.
    setupSelectionHandler(viewer, (noradId) => {
        selectedNoradId = noradId;
        if (chartState) {
            selectObject(chartState, noradId);
        }
        _showObjectInfoPanel(noradId);
        _setChartVisible(noradId !== null && _hasActiveAnomaly(noradId));
        if (noradId !== null) {
            _fetchAndDrawTrack(noradId).catch((err) => {
                console.warn('[main] _fetchAndDrawTrack (globe click) error:', err);
            });
        } else {
            // Selection cleared — remove track from globe.
            if (viewer) clearTrackAndCone(viewer);
        }
    });

    // 7. Fetch initial catalog; seed globe and name map; auto-select first object.
    const catalog = await fetchCatalog(backendBaseUrl);
    _seedFromCatalog(catalog);

    // 8. Open the WebSocket connection.
    connectWebSocket('ws://' + window.location.hostname + ':8001/ws/live');
}

// ---------------------------------------------------------------------------
// Top-level entry call
// ---------------------------------------------------------------------------
initApp().catch((err) => {
    console.error('[main] initApp failed:', err);
});
