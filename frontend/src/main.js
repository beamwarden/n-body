/**
 * @module main
 * @description Application entry point. Establishes WebSocket connection
 * to the backend and routes incoming messages to globe, residuals, and
 * alerts modules.
 */

import { initGlobe, updateSatellitePosition, updateUncertaintyEllipsoid, highlightAnomaly, setupSelectionHandler } from './globe.js';
import { initResidualChart, appendResidualDataPoint, selectObject } from './residuals.js';
import { initAlertPanel, addAlert, updateAlertStatus, seedFromCatalog as alertSeedFromCatalog } from './alerts.js';

// ---------------------------------------------------------------------------
// Module-level state (step 7)
// ---------------------------------------------------------------------------

/** @type {Object|null} Cesium.Viewer instance */
let viewer = null;

/** @type {Object|null} Chart state from initResidualChart */
let chartState = null;

/** @type {Object|null} Panel state from initAlertPanel */
let panelState = null;

/** @type {number|null} Currently selected NORAD ID */
let selectedNoradId = null;

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

// Reconnection state
let _reconnectDelay_s = 1;
const _MAX_RECONNECT_DELAY_S = 30;
let _reconnecting = false;

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
export function routeMessage(message) {
    if (!viewer) return;

    const { type, norad_id } = message;

    if (type === 'state_update') {
        updateSatellitePosition(viewer, message);
        updateUncertaintyEllipsoid(
            viewer,
            norad_id,
            message.covariance_diagonal_km2,
            message.eci_km,
            message.epoch_utc
        );
        if (norad_id === selectedNoradId && chartState) {
            appendResidualDataPoint(chartState, message);
        }

    } else if (type === 'anomaly') {
        highlightAnomaly(viewer, norad_id, message.anomaly_type);
        if (panelState) {
            addAlert(panelState, message, nameMap);
        }
        if (norad_id === selectedNoradId && chartState) {
            appendResidualDataPoint(chartState, message);
        }

    } else if (type === 'recalibration') {
        // recalibration includes an updated state; update position and ellipsoid.
        updateSatellitePosition(viewer, message);
        updateUncertaintyEllipsoid(
            viewer,
            norad_id,
            message.covariance_diagonal_km2,
            message.eci_km,
            message.epoch_utc
        );
        if (panelState) {
            updateAlertStatus(panelState, norad_id, 'resolved', message.epoch_utc);
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

        // NF-012: On reconnect, fetch catalog to re-seed globe and charts (step 27).
        const catalog = await fetchCatalog(backendBaseUrl);
        _seedFromCatalog(catalog);
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
 * Schedule a WebSocket reconnection with exponential backoff and jitter.
 * @param {string} url - WebSocket URL to reconnect to.
 * @returns {void}
 */
function _scheduleReconnect(url) {
    if (_reconnecting) return;
    _reconnecting = true;

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
// Step 26+27: Seed globe, charts, and name map from catalog data
// ---------------------------------------------------------------------------

/**
 * Seed the globe, name map, and alerts module from a /catalog response.
 * Used on initial load (step 26) and reconnection (step 27).
 * @param {Array<Object>} catalog - Array of catalog entries from GET /catalog.
 * @returns {void}
 */
function _seedFromCatalog(catalog) {
    if (!catalog || catalog.length === 0) return;

    // Build name map (open question 2 resolution)
    nameMap.clear();
    for (const entry of catalog) {
        if (entry.norad_id != null) {
            nameMap.set(entry.norad_id, entry.name || String(entry.norad_id));
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
            entry.epoch_utc != null
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
    backendBaseUrl = window.location.protocol + '//' + window.location.hostname + ':8000';

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

    // 6. Wire globe click selection to residuals and alerts (F-056).
    setupSelectionHandler(viewer, (noradId) => {
        selectedNoradId = noradId;
        if (chartState) {
            selectObject(chartState, noradId);
        }
    });

    // 7. Fetch initial catalog; seed globe and name map; auto-select first object.
    const catalog = await fetchCatalog(backendBaseUrl);
    _seedFromCatalog(catalog);

    // Step 26: auto-select first catalog object so chart is not empty on demo start.
    if (catalog.length > 0 && chartState) {
        const firstId = catalog[0].norad_id;
        selectedNoradId = firstId;
        selectObject(chartState, firstId);
    }

    // 8. Open the WebSocket connection.
    connectWebSocket('ws://' + window.location.hostname + ':8000/ws/live');
}

// ---------------------------------------------------------------------------
// Top-level entry call
// ---------------------------------------------------------------------------
initApp().catch((err) => {
    console.error('[main] initApp failed:', err);
});
