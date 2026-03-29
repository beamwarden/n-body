/**
 * @module globe
 * @description CesiumJS 3D orbital view. Renders Earth globe with satellite
 * positions, confidence color-coding, uncertainty ellipsoids, and anomaly
 * highlights. All position data received in ECI J2000 km; conversion to
 * Cesium's Cartesian3 (ECEF meters) happens in this module only.
 *
 * TECH DEBT TD-024: replace with CzmlDataSource for production.
 * This module uses the CesiumJS Entity API directly rather than CZML DataSource
 * (deviation from architecture section 3.6.1). Rationale: simpler code, no
 * serialization overhead, identical visual result at POC scale (50 objects).
 * See docs/tech-debt.md TD-024.
 *
 * TECH DEBT TD-018: Cesium Ion token resolved.
 * Token is read from window.CESIUM_ION_TOKEN set by main.js via GET /config.
 * Never hardcoded. See docs/tech-debt.md TD-018.
 */

// ---------------------------------------------------------------------------
// Module-level entity maps (step 9)
// ---------------------------------------------------------------------------

/**
 * Map from NORAD ID to the satellite billboard Cesium.Entity.
 * @type {Map<number, Object>}
 */
const entityMap = new Map();

/**
 * Map from NORAD ID to the uncertainty ellipsoid Cesium.Entity.
 * @type {Map<number, Object>}
 */
const ellipsoidMap = new Map();

// ---------------------------------------------------------------------------
// Step 8: ECI-to-ECEF conversion helper
// ---------------------------------------------------------------------------

/**
 * Convert an ECI J2000 position vector to a CesiumJS ECEF Cartesian3.
 *
 * Uses a simplified GMST rotation (Vallado IAU-1982 formula, POC accuracy).
 * The ECI vectors from the backend are technically GCRS (see TD-003), which
 * differs from J2000 by ~20 milliarcseconds — sub-meter error, negligible for
 * POC visualization.
 *
 * @param {Array<number>} eci_km - [x, y, z] in ECI J2000, kilometres.
 * @param {string} epoch_utc_str - ISO-8601 UTC epoch string (e.g. '2026-03-28T19:00:00Z').
 * @returns {Object} Cesium.Cartesian3 in ECEF metres.
 */
function eciToEcefCartesian3(eci_km, epoch_utc_str) {
    const epochMs = Date.parse(epoch_utc_str);

    // Days since J2000.0 (2000-01-01T12:00:00Z = 946728000000 ms)
    const daysSinceJ2000 = (epochMs - 946728000000) / 86400000;

    // GMST in degrees using Vallado simplified formula, then convert to radians
    const gmst_deg = (280.46061837 + 360.98564736629 * daysSinceJ2000) % 360;
    const gmst_rad = gmst_deg * Math.PI / 180;

    const cos_gmst = Math.cos(gmst_rad);
    const sin_gmst = Math.sin(gmst_rad);

    const x_eci_km = eci_km[0];
    const y_eci_km = eci_km[1];
    const z_eci_km = eci_km[2];

    // Apply Z-rotation (ECI → ECEF):
    //   x_ecef =  x_eci * cos(θ) + y_eci * sin(θ)
    //   y_ecef = -x_eci * sin(θ) + y_eci * cos(θ)
    //   z_ecef =  z_eci
    const x_ecef_km =  x_eci_km * cos_gmst + y_eci_km * sin_gmst;
    const y_ecef_km = -x_eci_km * sin_gmst + y_eci_km * cos_gmst;
    const z_ecef_km =  z_eci_km;

    // Convert km → m for CesiumJS
    return new Cesium.Cartesian3(x_ecef_km * 1000, y_ecef_km * 1000, z_ecef_km * 1000);
}

// ---------------------------------------------------------------------------
// Step 10: confidenceColor
// ---------------------------------------------------------------------------

/**
 * Return the Cesium.Color corresponding to a confidence score.
 * Green (> 0.85), amber (0.60–0.85), red (< 0.60). (F-051)
 *
 * @param {number} confidence - Confidence score in [0, 1].
 * @returns {Object} Cesium.Color instance.
 */
export function confidenceColor(confidence) {
    if (confidence > 0.85) {
        return Cesium.Color.LIME;
    } else if (confidence >= 0.60) {
        return Cesium.Color.ORANGE;
    } else {
        return Cesium.Color.RED;
    }
}

// ---------------------------------------------------------------------------
// Step 9: initGlobe
// ---------------------------------------------------------------------------

/**
 * Initialize the CesiumJS viewer in the given container.
 *
 * TECH DEBT TD-018: Ion token is passed in by caller (main.js reads it from
 * GET /config). Never hardcoded here.
 *
 * @param {string} containerId - DOM element ID for the Cesium viewer.
 * @param {string} ionToken - Cesium Ion access token.
 * @returns {Object} Cesium.Viewer instance.
 */
export function initGlobe(containerId, ionToken) {
    // TECH DEBT TD-018: token sourced from environment via GET /config, never hardcoded.
    Cesium.Ion.defaultAccessToken = ionToken;

    const viewer = new Cesium.Viewer(containerId, {
        timeline: false,
        animation: false,
        baseLayerPicker: false,
        geocoder: false,
        homeButton: false,
        sceneModePicker: false,
        navigationHelpButton: false,
        infoBox: false,
        selectionIndicator: true,
    });

    // Set initial camera to full-Earth view
    viewer.camera.flyTo({
        destination: Cesium.Cartesian3.fromDegrees(0, 0, 35000000),
    });

    return viewer;
}

// ---------------------------------------------------------------------------
// Step 11: updateSatellitePosition
// ---------------------------------------------------------------------------

/**
 * Update or add a satellite billboard entity on the globe. (F-050, F-051)
 *
 * Creates the entity on first call for a given NORAD ID; updates position and
 * color on subsequent calls.
 *
 * TECH DEBT TD-024: replace with CzmlDataSource for production.
 *
 * @param {Object} viewer - Cesium.Viewer instance.
 * @param {Object} stateUpdate - State update message from backend WebSocket.
 * @returns {void}
 */
export function updateSatellitePosition(viewer, stateUpdate) {
    const { norad_id, eci_km, epoch_utc, confidence } = stateUpdate;
    const cartesian3 = eciToEcefCartesian3(eci_km, epoch_utc);
    const color = confidenceColor(confidence);

    if (entityMap.has(norad_id)) {
        const entity = entityMap.get(norad_id);
        entity.position = new Cesium.ConstantPositionProperty(cartesian3);
        entity.billboard.color = new Cesium.ConstantProperty(color);
    } else {
        const entity = viewer.entities.add({
            id: 'sat-' + norad_id,
            position: cartesian3,
            billboard: {
                image: _createSatelliteDot(),
                color: color,
                scale: 0.6,
                verticalOrigin: Cesium.VerticalOrigin.CENTER,
                horizontalOrigin: Cesium.HorizontalOrigin.CENTER,
                disableDepthTestDistance: Number.POSITIVE_INFINITY,
            },
            label: {
                text: String(norad_id),
                font: '14px monospace',
                fillColor: Cesium.Color.WHITE,
                style: Cesium.LabelStyle.FILL,
                verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
                pixelOffset: new Cesium.Cartesian2(0, -12),
                disableDepthTestDistance: Number.POSITIVE_INFINITY,
                show: false,
            },
            properties: {
                norad_id: norad_id,
            },
        });
        entityMap.set(norad_id, entity);
    }
}

/**
 * Create a small circular canvas data URL for the satellite billboard icon.
 * @returns {string} Data URL of an 8x8 white circle on transparent background.
 */
function _createSatelliteDot() {
    const canvas = document.createElement('canvas');
    canvas.width = 16;
    canvas.height = 16;
    const ctx = canvas.getContext('2d');
    ctx.beginPath();
    ctx.arc(8, 8, 6, 0, 2 * Math.PI);
    ctx.fillStyle = 'white';
    ctx.fill();
    return canvas.toDataURL();
}

// ---------------------------------------------------------------------------
// Step 12: updateUncertaintyEllipsoid
// ---------------------------------------------------------------------------

/**
 * Render or update the 3-sigma uncertainty ellipsoid for a tracked object. (F-052)
 *
 * Known approximation: ellipsoid axes are aligned with the ECEF frame, not the
 * object's ECI orbital frame. The shape (semi-axis lengths) is correct but the
 * orientation relative to the orbit is not preserved. Correcting this requires
 * the full 3×3 covariance matrix rotated into ECEF — out of scope for POC.
 * Minimum radii clamped to 5 km to ensure visibility at globe zoom levels.
 *
 * TECH DEBT TD-024: replace with CzmlDataSource for production.
 *
 * DEVIATION from plan docs/plans/2026-03-28-frontend.md step 4 routing call:
 * Plan step 4 specifies updateUncertaintyEllipsoid(viewer, message.norad_id,
 * message.covariance_diagonal_km2, message.eci_km) with 4 parameters. However,
 * eciToEcefCartesian3 (step 8) requires epoch_utc for the GMST rotation. Without
 * it, the ellipsoid position cannot be computed. Added epochUtcStr as a 5th
 * parameter. The caller in main.js passes message.epoch_utc. Flagged for planner
 * review.
 *
 * @param {Object} viewer - Cesium.Viewer instance.
 * @param {number} noradId - NORAD catalog ID.
 * @param {Array<number>} covarianceDiagonalKm2 - [σx², σy², σz²] in km².
 * @param {Array<number>} positionEciKm - [x, y, z] in ECI km.
 * @param {string} epochUtcStr - ISO-8601 UTC epoch string for GMST rotation.
 * @returns {void}
 */
export function updateUncertaintyEllipsoid(viewer, noradId, covarianceDiagonalKm2, positionEciKm, epochUtcStr) {
    const MIN_RADIUS_M = 5000; // 5 km minimum for globe visibility

    // 3-sigma radii: sqrt(σ²) * 3, converted km → m
    const radii_m = covarianceDiagonalKm2.map(
        (variance_km2) => Math.max(Math.sqrt(variance_km2) * 3 * 1000, MIN_RADIUS_M)
    );

    const cartesian3 = eciToEcefCartesian3(positionEciKm, epochUtcStr);

    if (ellipsoidMap.has(noradId)) {
        const entity = ellipsoidMap.get(noradId);
        entity.position = new Cesium.ConstantPositionProperty(cartesian3);
        entity.ellipsoid.radii = new Cesium.ConstantProperty(
            new Cesium.Cartesian3(radii_m[0], radii_m[1], radii_m[2])
        );
    } else {
        const entity = viewer.entities.add({
            id: 'ell-' + noradId,
            position: cartesian3,
            ellipsoid: {
                radii: new Cesium.Cartesian3(radii_m[0], radii_m[1], radii_m[2]),
                material: Cesium.Color.CYAN.withAlpha(0.15),
                outline: true,
                outlineColor: Cesium.Color.CYAN.withAlpha(0.4),
                // Ellipsoid is not selectable — only the billboard entity is
                show: true,
            },
        });
        ellipsoidMap.set(noradId, entity);
    }
}

// ---------------------------------------------------------------------------
// Step 13: highlightAnomaly
// ---------------------------------------------------------------------------

/**
 * Highlight a satellite and its ellipsoid due to anomaly detection. (F-053)
 *
 * Changes billboard color to YELLOW and increases scale. Reverts to the
 * confidence-based color after 10 seconds (entity will be re-colored on the
 * next state_update anyway).
 *
 * @param {Object} viewer - Cesium.Viewer instance.
 * @param {number} noradId - NORAD catalog ID.
 * @param {string} anomalyType - Type of anomaly ('maneuver' | 'drag_anomaly' | 'filter_divergence').
 * @returns {void}
 */
export function highlightAnomaly(viewer, noradId, anomalyType) {
    const satEntity = entityMap.get(noradId);
    const ellEntity = ellipsoidMap.get(noradId);

    if (satEntity) {
        satEntity.billboard.color = new Cesium.ConstantProperty(Cesium.Color.YELLOW);
        satEntity.billboard.scale = new Cesium.ConstantProperty(1.2);
    }

    if (ellEntity) {
        ellEntity.ellipsoid.material = Cesium.Color.YELLOW.withAlpha(0.3);
    }

    // Revert after 10 seconds. The next state_update will re-apply confidence color.
    setTimeout(() => {
        if (satEntity) {
            satEntity.billboard.scale = new Cesium.ConstantProperty(0.6);
            // Color will be updated by the next state_update message.
        }
        if (ellEntity) {
            ellEntity.ellipsoid.material = Cesium.Color.CYAN.withAlpha(0.15);
        }
    }, 10000);
}

// ---------------------------------------------------------------------------
// Step 14: setupSelectionHandler
// ---------------------------------------------------------------------------

/**
 * Register a handler for globe object selection. (F-056)
 *
 * When the user clicks a satellite entity, extracts the NORAD ID and calls
 * the provided onSelect callback. Calls onSelect(null) when selection is cleared.
 *
 * @param {Object} viewer - Cesium.Viewer instance.
 * @param {function(number|null): void} onSelect - Callback receiving NORAD ID or null.
 * @returns {void}
 */
export function setupSelectionHandler(viewer, onSelect) {
    viewer.selectedEntityChanged.addEventListener((entity) => {
        if (entity === undefined || entity === null) {
            onSelect(null);
            return;
        }
        // Only handle satellite entities (IDs starting with 'sat-')
        if (typeof entity.id === 'string' && entity.id.startsWith('sat-')) {
            const noradId = entity.properties.norad_id.getValue();
            onSelect(noradId);
        }
    });
}
