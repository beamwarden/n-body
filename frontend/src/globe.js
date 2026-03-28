/**
 * @module globe
 * @description CesiumJS 3D orbital view. Renders Earth globe with satellite
 * positions, ground tracks, uncertainty ellipsoids, and anomaly highlights.
 * All position data received in ECI J2000 km; conversion to Cesium's
 * Cartesian3 (ECEF meters) happens in this module.
 */

/**
 * Initialize the CesiumJS viewer in the given container.
 * @param {string} containerId - DOM element ID for the Cesium viewer.
 * @param {string} ionToken - Cesium Ion access token.
 * @returns {Object} Cesium.Viewer instance.
 */
export function initGlobe(containerId, ionToken) {
    throw new Error('not implemented');
}

/**
 * Update or add a satellite entity on the globe.
 * @param {Object} viewer - Cesium.Viewer instance.
 * @param {Object} stateUpdate - State update message from backend.
 * @returns {void}
 */
export function updateSatellitePosition(viewer, stateUpdate) {
    throw new Error('not implemented');
}

/**
 * Render or update the uncertainty ellipsoid for a tracked object.
 * @param {Object} viewer - Cesium.Viewer instance.
 * @param {number} noradId - NORAD catalog ID.
 * @param {Array<number>} covarianceDiagonalKm2 - [sigma_x^2, sigma_y^2, sigma_z^2].
 * @param {Array<number>} positionEciKm - [x, y, z] in ECI km.
 * @returns {void}
 */
export function updateUncertaintyEllipsoid(viewer, noradId, covarianceDiagonalKm2, positionEciKm) {
    throw new Error('not implemented');
}

/**
 * Highlight an object due to anomaly detection.
 * @param {Object} viewer - Cesium.Viewer instance.
 * @param {number} noradId - NORAD catalog ID.
 * @param {string} anomalyType - Type of anomaly.
 * @returns {void}
 */
export function highlightAnomaly(viewer, noradId, anomalyType) {
    throw new Error('not implemented');
}

/**
 * Get the color for a confidence level.
 * Green > 0.85, amber 0.60-0.85, red < 0.60.
 * @param {number} confidence - Confidence score 0-1.
 * @returns {Object} Cesium.Color instance.
 */
export function confidenceColor(confidence) {
    throw new Error('not implemented');
}

/**
 * Handle click selection of an object on the globe.
 * @param {Object} viewer - Cesium.Viewer instance.
 * @param {function} onSelect - Callback receiving the selected NORAD ID.
 * @returns {void}
 */
export function setupSelectionHandler(viewer, onSelect) {
    throw new Error('not implemented');
}
