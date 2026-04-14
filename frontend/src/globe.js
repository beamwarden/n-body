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
// Conjunction risk state (plan 2026-03-29-conjunction-risk.md step 8)
// ---------------------------------------------------------------------------

/**
 * Maps NORAD ID to conjunction risk order string for active conjunction risks.
 * 'first_order' | 'second_order'
 * @type {Map<number, string>}
 */
const conjunctionRiskMap = new Map();

/**
 * Most recent conjunction_risk message received. Stored so main.js can extract
 * the object name and epoch for the auto-clear toast notification.
 * @type {Object|null}
 */
let lastConjunctionMessage = null;

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
        // Use Ion default imagery (requires valid Ion token in CESIUM_ION_TOKEN env var).
        // To use offline, set imageryProvider to a SingleTileImageryProvider with a
        // local texture. For now, defer to Ion default for demo use.
        // TD-026: add globe imagery selection to UI or config (post-POC).
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
    // Compute confidence-based color as the base color.
    const baseColor = confidenceColor(confidence);

    // Apply conjunction risk color override if this object is flagged.
    // First-order: RED; second-order: YELLOW. Override persists across state_update
    // messages until clearConjunctionRisk() is called by main.js auto-clear logic.
    let effectiveColor = baseColor;
    const riskOrder = conjunctionRiskMap.get(norad_id);
    if (riskOrder === 'first_order') {
        effectiveColor = Cesium.Color.RED;
    } else if (riskOrder === 'second_order') {
        effectiveColor = Cesium.Color.YELLOW;
    }

    if (entityMap.has(norad_id)) {
        const entity = entityMap.get(norad_id);
        entity.position = new Cesium.ConstantPositionProperty(cartesian3);
        entity.billboard.color = new Cesium.ConstantProperty(effectiveColor);
        // Store confidence for use by clearConjunctionRisk color restoration.
        entity.properties._lastConfidence = new Cesium.ConstantProperty(confidence);
    } else {
        const entity = viewer.entities.add({
            id: 'sat-' + norad_id,
            position: cartesian3,
            billboard: {
                image: _createSatelliteDot(),
                color: effectiveColor,
                scale: 0.6,
                verticalOrigin: Cesium.VerticalOrigin.CENTER,
                horizontalOrigin: Cesium.HorizontalOrigin.CENTER,
            },
            label: {
                text: String(norad_id),
                font: '14px monospace',
                fillColor: Cesium.Color.WHITE,
                style: Cesium.LabelStyle.FILL,
                verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
                pixelOffset: new Cesium.Cartesian2(0, -12),
                show: false,
            },
            properties: {
                norad_id: norad_id,
                _lastConfidence: confidence,
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

/**
 * Remove a satellite entity from the globe entirely (e.g. stale TLE cleanup).
 * Also removes the associated uncertainty ellipsoid if present.
 * @param {Object} viewer - Cesium.Viewer instance.
 * @param {number} noradId - NORAD ID to remove.
 */
export function removeSatelliteEntity(viewer, noradId) {
    if (entityMap.has(noradId)) {
        viewer.entities.remove(entityMap.get(noradId));
        entityMap.delete(noradId);
    }
    if (ellipsoidMap.has(noradId)) {
        viewer.entities.remove(ellipsoidMap.get(noradId));
        ellipsoidMap.delete(noradId);
    }
}

/**
 * Fly the camera to the current position of a tracked satellite.
 * No-ops silently if the entity is not in the map (stale or unprocessed).
 *
 * @param {Cesium.Viewer} viewer
 * @param {number} noradId
 */
export function flyToObject(viewer, noradId) {
    const entity = entityMap.get(noradId);
    if (!entity) return;
    const position = entity.position.getValue(Cesium.JulianDate.now());
    if (!position) return;
    // Build an explicit BoundingSphere at the satellite position so the
    // 800 km range is respected. viewer.flyTo() on a billboard entity
    // computes a near-zero bounding sphere and ignores the range offset;
    // flyToBoundingSphere with an explicit sphere is the correct API.
    const sphere = new Cesium.BoundingSphere(position, 1000);
    viewer.camera.flyToBoundingSphere(sphere, {
        offset: new Cesium.HeadingPitchRange(
            0,
            Cesium.Math.toRadians(-30),
            4500000,  // 4,500 km from satellite (~5,000 km above surface) — continental scale
        ),
        duration: 1.5,
    });
}

// ---------------------------------------------------------------------------
// Conjunction risk highlighting (plan 2026-03-29-conjunction-risk.md step 8)
// ---------------------------------------------------------------------------

/**
 * Apply conjunction risk color overrides to globe entities.
 *
 * Clears any previous conjunction risk state, stores the new message in
 * lastConjunctionMessage, and sets billboard colors: RED for first-order,
 * YELLOW for second-order. The conjunctionRiskMap is updated so that
 * updateSatellitePosition will continue to enforce the override across
 * subsequent state_update messages until clearConjunctionRisk is called.
 *
 * @param {Object} viewer - Cesium.Viewer instance.
 * @param {Object} conjunctionMessage - conjunction_risk message from backend WebSocket.
 * @returns {void}
 */
export function applyConjunctionRisk(viewer, conjunctionMessage) {
    // Clear previous conjunction risk state before applying new one.
    conjunctionRiskMap.clear();
    lastConjunctionMessage = conjunctionMessage;

    for (const entry of (conjunctionMessage.first_order || [])) {
        conjunctionRiskMap.set(entry.norad_id, 'first_order');
        const entity = entityMap.get(entry.norad_id);
        if (entity) {
            entity.billboard.color = new Cesium.ConstantProperty(Cesium.Color.RED);
        }
    }

    for (const entry of (conjunctionMessage.second_order || [])) {
        // Do not override a first-order entry with a second-order color.
        if (!conjunctionRiskMap.has(entry.norad_id)) {
            conjunctionRiskMap.set(entry.norad_id, 'second_order');
            const entity = entityMap.get(entry.norad_id);
            if (entity) {
                entity.billboard.color = new Cesium.ConstantProperty(Cesium.Color.YELLOW);
            }
        }
    }
}

/**
 * Clear conjunction risk highlighting and restore confidence-based colors.
 *
 * For each NORAD ID in the provided array, removes it from conjunctionRiskMap and
 * restores the billboard color to its confidence-based color using the confidence
 * value stored in the entity's last known state (or 0.5 if unknown).
 * Resets lastConjunctionMessage to null.
 *
 * @param {Object} viewer - Cesium.Viewer instance.
 * @param {Array<number>} noradIds - Array of NORAD IDs to clear.
 * @returns {void}
 */
export function clearConjunctionRisk(viewer, noradIds) {
    for (const noradId of noradIds) {
        conjunctionRiskMap.delete(noradId);
        const entity = entityMap.get(noradId);
        if (entity) {
            // Restore confidence-based color. Read confidence from entity properties
            // if stored; default to 0.5 (amber range) if not available.
            const storedConfidence = entity.properties && entity.properties._lastConfidence
                ? entity.properties._lastConfidence.getValue()
                : 0.5;
            entity.billboard.color = new Cesium.ConstantProperty(
                confidenceColor(storedConfidence)
            );
        }
    }
    lastConjunctionMessage = null;
}

/**
 * Return a read-only reference to the conjunction risk map.
 * Used by main.js to determine which NORAD IDs to pass to clearConjunctionRisk.
 *
 * @returns {Map<number, string>} conjunctionRiskMap (live reference, do not mutate).
 */
export function getConjunctionRiskMap() {
    return conjunctionRiskMap;
}

/**
 * Return the number of satellite entities currently rendered on the globe.
 * Reflects only objects that have been successfully placed via updateSatellitePosition.
 *
 * @returns {number} Count of active globe entities.
 */
export function getRenderedEntityCount() {
    return entityMap.size;
}

/**
 * Return the last conjunction_risk message received, or null if none.
 * Used by main.js to extract object name and epoch for the auto-clear toast.
 *
 * @returns {Object|null} lastConjunctionMessage.
 */
export function getLastConjunctionMessage() {
    return lastConjunctionMessage;
}

// ---------------------------------------------------------------------------
// Track and uncertainty cone drawing (plan 2026-03-29-history-tracks-cones.md)
// ---------------------------------------------------------------------------

/**
 * Module-level reference to the current historical track entity, for cleanup.
 * @type {Object|null}
 */
let _currentTrackEntity = null;

/**
 * Module-level reference to the current forward track entity, for cleanup.
 * @type {Object|null}
 */
let _currentForwardTrackEntity = null;

/**
 * Module-level array of corridor segment entities for the uncertainty cone.
 * Stored as an array because Option A creates multiple short corridor segments.
 * @type {Array<Object>}
 */
let _currentConeEntities = [];

/**
 * Draw a historical ground track polyline on the globe for the selected object.
 *
 * Converts each ECI J2000 track point to ECEF Cartesian3 using the per-point
 * epoch for correct GMST rotation. Adds a single cyan polyline entity.
 *
 * @param {Object} viewer - Cesium.Viewer instance.
 * @param {Array<{epoch_utc: string, eci_km: Array<number>}>} trackPoints -
 *   Historical track points from GET /object/{norad_id}/track backward_track.
 * @returns {void}
 */
export function drawHistoricalTrack(viewer, trackPoints) {
    if (!trackPoints || trackPoints.length === 0) return;

    const positions = trackPoints.map((pt) =>
        eciToEcefCartesian3(pt.eci_km, pt.epoch_utc)
    );

    _currentTrackEntity = viewer.entities.add({
        id: 'track-historical',
        polyline: {
            positions: positions,
            width: 2,
            material: Cesium.Color.CYAN.withAlpha(0.6),
            clampToGround: false,
        },
    });
}

/**
 * Draw the predictive forward track and widening uncertainty corridor.
 *
 * Nominal forward track: orange dashed polyline.
 * Uncertainty corridor: segmented corridors (Option A from plan decision 2).
 *   Multiple short corridor segments with increasing width approximate the
 *   widening cone. Each segment spans 5 forward points and uses the uncertainty
 *   radius at its midpoint as the half-width.
 *
 * IMPLEMENTATION NOTE: Cesium.CorridorGraphics accepts a single scalar width,
 * not a per-vertex width array. Option A (segmented corridors) is used here.
 * Each segment has a constant width equal to twice the uncertainty_radius_km
 * at the segment midpoint, converted to metres. This produces a stepped but
 * clearly widening corridor visual, acceptable for the POC demo.
 * See plan docs/plans/2026-03-29-history-tracks-cones.md step 2.3 for rationale.
 *
 * @param {Object} viewer - Cesium.Viewer instance.
 * @param {Array<{epoch_utc: string, eci_km: Array<number>, uncertainty_radius_km: number}>} forwardTrackPoints -
 *   Forward track points from GET /object/{norad_id}/track forward_track.
 * @returns {void}
 */
export function drawPredictiveTrackWithCone(viewer, forwardTrackPoints) {
    if (!forwardTrackPoints || forwardTrackPoints.length === 0) return;

    const positions = forwardTrackPoints.map((pt) =>
        eciToEcefCartesian3(pt.eci_km, pt.epoch_utc)
    );

    // Nominal forward track — orange dashed polyline.
    _currentForwardTrackEntity = viewer.entities.add({
        id: 'track-forward',
        polyline: {
            positions: positions,
            width: 2,
            material: new Cesium.PolylineDashMaterialProperty({
                color: Cesium.Color.ORANGE.withAlpha(0.7),
                dashLength: 16,
            }),
            clampToGround: false,
        },
    });

    // Uncertainty corridor: Option A — segmented corridors (plan decision 2).
    // Group forward points into segments of 2 points each (one per step) for a
    // smooth cone. Each segment gets a constant width = 2 * uncertainty_radius_km
    // at the midpoint (in metres).
    const SEGMENT_SIZE = 2;
    const numSegments = Math.ceil(forwardTrackPoints.length / SEGMENT_SIZE);

    for (let seg = 0; seg < numSegments; seg++) {
        const startIdx = seg * SEGMENT_SIZE;
        const endIdx = Math.min(startIdx + SEGMENT_SIZE + 1, forwardTrackPoints.length);
        const segPositions = positions.slice(startIdx, endIdx);
        if (segPositions.length < 2) continue;

        // Use the midpoint of the segment for the width value.
        // Apply a 10x display scale factor so the cone is visible at full-Earth
        // zoom (~35,000 km altitude). Raw uncertainty radii (~30-100 km) are
        // imperceptible at that scale without scaling.
        // Floor at 20 km (display) so the near-satellite cone tip remains visible.
        // Clamp ceiling at 2000 km.
        const midIdx = Math.floor((startIdx + endIdx - 1) / 2);
        const midPt = forwardTrackPoints[Math.min(midIdx, forwardTrackPoints.length - 1)];
        const rawRadius_km = midPt.uncertainty_radius_km * 10;
        const clampedRadius_km = Math.max(20, Math.min(2000, rawRadius_km));
        const corridorWidth_m = clampedRadius_km * 2 * 1000;

        const coneSegEntity = viewer.entities.add({
            id: 'track-cone-seg-' + seg,
            corridor: {
                positions: segPositions,
                width: corridorWidth_m,
                material: Cesium.Color.ORANGE.withAlpha(0.3),
                cornerType: Cesium.CornerType.ROUNDED,
                height: 1000,
                extrudedHeight: 10000,
            },
        });
        _currentConeEntities.push(coneSegEntity);
    }
}

/**
 * Remove all track and uncertainty cone entities from the viewer.
 *
 * Removes entities whose IDs start with 'track-'. Resets module-level
 * references to null/empty.
 *
 * @param {Object} viewer - Cesium.Viewer instance.
 * @returns {void}
 */
export function clearTrackAndCone(viewer) {
    if (_currentTrackEntity) {
        viewer.entities.remove(_currentTrackEntity);
        _currentTrackEntity = null;
    }
    if (_currentForwardTrackEntity) {
        viewer.entities.remove(_currentForwardTrackEntity);
        _currentForwardTrackEntity = null;
    }
    for (const entity of _currentConeEntities) {
        viewer.entities.remove(entity);
    }
    _currentConeEntities = [];
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
        // Handle both satellite billboard ('sat-NNNNN') and uncertainty ellipsoid
        // ('ell-NNNNN') entities — ellipsoids are larger click targets and should
        // also select the underlying satellite.
        if (typeof entity.id === 'string') {
            let noradId = null;
            if (entity.id.startsWith('sat-')) {
                noradId = parseInt(entity.id.slice(4), 10);
            } else if (entity.id.startsWith('ell-')) {
                noradId = parseInt(entity.id.slice(4), 10);
            }
            if (noradId !== null && !isNaN(noradId)) onSelect(noradId);
        }
    });
}
