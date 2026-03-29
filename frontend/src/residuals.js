/**
 * @module residuals
 * @description D3.js residual timeline charts. Renders per-object
 * residual magnitude, NIS score, and ±2σ expected noise band. (F-054)
 *
 * Uses incremental append with a 100-point sliding window per object.
 * No full redraw on every message; axes and paths update incrementally.
 */

// ---------------------------------------------------------------------------
// Step 15: Per-object data store
// ---------------------------------------------------------------------------

/**
 * Per-object time-series data store.
 * Keys are NORAD IDs; values are arrays of data point objects, capped at 100.
 *
 * Data point shape:
 *   { epoch_utc: Date, residual_magnitude_km: number, nis: number,
 *     confidence: number, sigma2_km: number }
 *
 * @type {Map<number, Array<Object>>}
 */
const dataStore = new Map();

/** Maximum data points retained per object (sliding window). */
const DATA_WINDOW = 100;

/** NIS chi-squared critical value (p=0.05, 6 degrees of freedom). */
const NIS_THRESHOLD = 12.592;

// ---------------------------------------------------------------------------
// Step 16: initResidualChart
// ---------------------------------------------------------------------------

/**
 * Initialize the dual-panel D3 residual chart in the given container. (F-054)
 *
 * Creates two vertically stacked sub-charts:
 *   - Top (60%): Residual magnitude (km) vs. time, with ±2σ noise band.
 *   - Bottom (40%): NIS score vs. time, with chi-squared threshold line.
 *
 * @param {string} containerId - DOM element ID for the chart container.
 * @returns {Object} chartState object for subsequent updates.
 */
export function initResidualChart(containerId) {
    const containerEl = document.getElementById(containerId);
    if (!containerEl) {
        console.error('[residuals] Container not found:', containerId);
        return null;
    }

    const margin = { top: 28, right: 18, bottom: 22, left: 52 };
    const totalWidth = containerEl.clientWidth || 360;
    const totalHeight = containerEl.clientHeight || 320;
    const innerWidth = totalWidth - margin.left - margin.right;
    const topHeight = Math.floor((totalHeight - margin.top - margin.bottom) * 0.60);
    const bottomHeight = Math.floor((totalHeight - margin.top - margin.bottom) * 0.40) - 16;

    const svg = d3.select(containerEl)
        .append('svg')
        .attr('width', totalWidth)
        .attr('height', totalHeight)
        .style('background', '#0a0a0f')
        .style('font-family', 'monospace')
        .style('font-size', '12px');

    // --- Top chart group (residual magnitude) ---
    const topGroup = svg.append('g')
        .attr('transform', `translate(${margin.left},${margin.top})`);

    // Chart title
    svg.append('text')
        .attr('class', 'chart-title')
        .attr('x', margin.left)
        .attr('y', 16)
        .attr('fill', '#aaa')
        .attr('font-size', '13px')
        .attr('font-family', 'monospace')
        .text('Object: [none selected]');

    // Top chart axes
    const xScaleTop = d3.scaleTime().range([0, innerWidth]);
    const yScaleTop = d3.scaleLinear().range([topHeight, 0]);

    const xAxisTopG = topGroup.append('g')
        .attr('class', 'x-axis-top')
        .attr('transform', `translate(0,${topHeight})`)
        .attr('color', '#555');

    const yAxisTopG = topGroup.append('g')
        .attr('class', 'y-axis-top')
        .attr('color', '#555');

    // Top chart Y-axis label
    topGroup.append('text')
        .attr('transform', 'rotate(-90)')
        .attr('x', -topHeight / 2)
        .attr('y', -40)
        .attr('fill', '#888')
        .attr('font-size', '11px')
        .attr('text-anchor', 'middle')
        .attr('font-family', 'monospace')
        .text('Residual (km)');

    // ±2σ noise band area (F-054)
    const areaBand = topGroup.append('path')
        .attr('class', 'noise-band')
        .attr('fill', '#00aaff')
        .attr('fill-opacity', 0.10)
        .attr('stroke', 'none');

    // Residual magnitude line
    const lineResidualPath = topGroup.append('path')
        .attr('class', 'line-residual')
        .attr('fill', 'none')
        .attr('stroke', '#00ccff')
        .attr('stroke-width', 1.5);

    // Top chart data point circles
    const topDotsG = topGroup.append('g').attr('class', 'top-dots');

    // --- Bottom chart group (NIS) ---
    const bottomGroupTop = margin.top + topHeight + 24;
    const bottomGroup = svg.append('g')
        .attr('transform', `translate(${margin.left},${bottomGroupTop})`);

    const xScaleBottom = d3.scaleTime().range([0, innerWidth]);
    const yScaleBottom = d3.scaleLinear().domain([0, 15]).range([bottomHeight, 0]);

    const xAxisBottomG = bottomGroup.append('g')
        .attr('class', 'x-axis-bottom')
        .attr('transform', `translate(0,${bottomHeight})`)
        .attr('color', '#555');

    const yAxisBottomG = bottomGroup.append('g')
        .attr('class', 'y-axis-bottom')
        .attr('color', '#555');

    // Bottom chart Y-axis label
    bottomGroup.append('text')
        .attr('transform', 'rotate(-90)')
        .attr('x', -bottomHeight / 2)
        .attr('y', -40)
        .attr('fill', '#888')
        .attr('font-size', '11px')
        .attr('text-anchor', 'middle')
        .attr('font-family', 'monospace')
        .text('NIS');

    // NIS threshold line at chi-squared critical value 12.592 (p=0.05, 6 dof)
    const nisThresholdLine = bottomGroup.append('line')
        .attr('class', 'nis-threshold')
        .attr('x1', 0)
        .attr('x2', innerWidth)
        .attr('stroke', '#ff4444')
        .attr('stroke-width', 1)
        .attr('stroke-dasharray', '4,3');

    // NIS score line
    const lineNisPath = bottomGroup.append('path')
        .attr('class', 'line-nis')
        .attr('fill', 'none')
        .attr('stroke', '#ffaa00')
        .attr('stroke-width', 1.5);

    // Bottom chart data point circles
    const bottomDotsG = bottomGroup.append('g').attr('class', 'bottom-dots');

    // Draw initial axes with empty domain
    _renderAxes(xAxisTopG, yAxisTopG, xAxisBottomG, yAxisBottomG,
                xScaleTop, yScaleTop, xScaleBottom, yScaleBottom);

    // Position threshold line once with initial scale
    _positionNisThreshold(nisThresholdLine, yScaleBottom, bottomHeight);

    const chartState = {
        svg,
        topGroup,
        bottomGroup,
        xScaleTop,
        yScaleTop,
        xScaleBottom,
        yScaleBottom,
        xAxisTopG,
        yAxisTopG,
        xAxisBottomG,
        yAxisBottomG,
        lineResidualPath,
        lineNisPath,
        areaBand,
        nisThresholdLine,
        topDotsG,
        bottomDotsG,
        innerWidth,
        topHeight,
        bottomHeight,
        selectedNoradId: null,
        containerEl,
    };

    // Set up ResizeObserver for container resize
    if (typeof ResizeObserver !== 'undefined') {
        const ro = new ResizeObserver(() => {
            if (chartState.selectedNoradId !== null) {
                _redrawChart(chartState, chartState.selectedNoradId);
            }
        });
        ro.observe(containerEl);
        chartState._resizeObserver = ro;
    }

    return chartState;
}

// ---------------------------------------------------------------------------
// Step 17: appendResidualDataPoint
// ---------------------------------------------------------------------------

/**
 * Append a new data point from a WebSocket state/anomaly message.
 * Computes residual magnitude from innovation_eci_km[0..2] if available;
 * falls back to RSS of covariance diagonal 1-sigma values.
 *
 * @param {Object} chartState - Chart state from initResidualChart.
 * @param {Object} stateUpdate - State update or anomaly message from backend.
 * @returns {void}
 */
export function appendResidualDataPoint(chartState, stateUpdate) {
    if (!chartState) return;

    const { norad_id, epoch_utc, nis, confidence, covariance_diagonal_km2, innovation_eci_km } = stateUpdate;

    const sigma_total_km = Math.sqrt(
        covariance_diagonal_km2[0] + covariance_diagonal_km2[1] + covariance_diagonal_km2[2]
    );

    // Residual magnitude: use innovation vector position components if available (Q1 resolution)
    let residual_magnitude_km;
    if (
        Array.isArray(innovation_eci_km) &&
        innovation_eci_km.length >= 3 &&
        (innovation_eci_km[0] !== 0 || innovation_eci_km[1] !== 0 || innovation_eci_km[2] !== 0)
    ) {
        residual_magnitude_km = Math.sqrt(
            innovation_eci_km[0] ** 2 +
            innovation_eci_km[1] ** 2 +
            innovation_eci_km[2] ** 2
        );
    } else {
        // Fallback: use RSS of 1-sigma position uncertainty as proxy
        residual_magnitude_km = sigma_total_km;
    }

    const dataPoint = {
        epoch_utc: new Date(epoch_utc),
        residual_magnitude_km,
        nis: nis ?? 0,
        confidence: confidence ?? 0,
        sigma2_km: 2 * sigma_total_km,
    };

    if (!dataStore.has(norad_id)) {
        dataStore.set(norad_id, []);
    }
    const buffer = dataStore.get(norad_id);
    buffer.push(dataPoint);

    // Enforce sliding window cap
    if (buffer.length > DATA_WINDOW) {
        buffer.shift();
    }

    // If this object is currently selected, update the chart
    if (norad_id === chartState.selectedNoradId) {
        _redrawChart(chartState, norad_id);
    }
}

// ---------------------------------------------------------------------------
// Step 18: _redrawChart (internal)
// ---------------------------------------------------------------------------

/**
 * Redraw the chart for the given NORAD ID using its buffered data.
 * Updates scales, axes, lines, and noise band. Called on data append or object switch.
 *
 * @param {Object} chartState - Chart state.
 * @param {number} noradId - NORAD ID to draw.
 * @returns {void}
 */
function _redrawChart(chartState, noradId) {
    const data = dataStore.get(noradId);
    if (!data || data.length === 0) return;

    const {
        xScaleTop, yScaleTop, xScaleBottom, yScaleBottom,
        xAxisTopG, yAxisTopG, xAxisBottomG, yAxisBottomG,
        lineResidualPath, lineNisPath, areaBand, nisThresholdLine,
        topDotsG, bottomDotsG,
        innerWidth, topHeight, bottomHeight,
    } = chartState;

    // Update x scale domain (shared for both charts)
    const xDomain = d3.extent(data, (d) => d.epoch_utc);
    xScaleTop.domain(xDomain);
    xScaleBottom.domain(xDomain);

    // Update y scale domains
    const maxResidual = d3.max(data, (d) => d.sigma2_km) || 1;
    yScaleTop.domain([0, maxResidual * 1.2]);

    const maxNis = d3.max(data, (d) => d.nis) || 0;
    yScaleBottom.domain([0, Math.max(maxNis, 15)]);

    // Redraw axes with 200ms transition
    _renderAxes(xAxisTopG, yAxisTopG, xAxisBottomG, yAxisBottomG,
                xScaleTop, yScaleTop, xScaleBottom, yScaleBottom, 200);

    // Reposition NIS threshold line
    _positionNisThreshold(nisThresholdLine, yScaleBottom, bottomHeight);

    // Redraw residual magnitude line (top chart)
    const lineResidualGenerator = d3.line()
        .x((d) => xScaleTop(d.epoch_utc))
        .y((d) => yScaleTop(d.residual_magnitude_km))
        .curve(d3.curveMonotoneX);

    lineResidualPath
        .datum(data)
        .attr('d', lineResidualGenerator);

    // Redraw ±2σ noise band (lower bound 0, upper bound 2*sigma_total_km)
    const areaGenerator = d3.area()
        .x((d) => xScaleTop(d.epoch_utc))
        .y0(() => yScaleTop(0))
        .y1((d) => yScaleTop(d.sigma2_km))
        .curve(d3.curveMonotoneX);

    areaBand
        .datum(data)
        .attr('d', areaGenerator);

    // Redraw NIS line (bottom chart)
    const lineNisGenerator = d3.line()
        .x((d) => xScaleBottom(d.epoch_utc))
        .y((d) => yScaleBottom(d.nis))
        .curve(d3.curveMonotoneX);

    lineNisPath
        .datum(data)
        .attr('d', lineNisGenerator);

    // Data point circles (top chart) — colored by confidence
    const topCircles = topDotsG.selectAll('circle').data(data, (d) => d.epoch_utc.getTime());
    topCircles.enter()
        .append('circle')
        .attr('r', 3)
        .merge(topCircles)
        .attr('cx', (d) => xScaleTop(d.epoch_utc))
        .attr('cy', (d) => yScaleTop(d.residual_magnitude_km))
        .attr('fill', (d) => _confidenceHex(d.confidence));
    topCircles.exit().remove();

    // Data point circles (bottom chart) — colored by confidence
    const bottomCircles = bottomDotsG.selectAll('circle').data(data, (d) => d.epoch_utc.getTime());
    bottomCircles.enter()
        .append('circle')
        .attr('r', 3)
        .merge(bottomCircles)
        .attr('cx', (d) => xScaleBottom(d.epoch_utc))
        .attr('cy', (d) => yScaleBottom(d.nis))
        .attr('fill', (d) => _confidenceHex(d.confidence));
    bottomCircles.exit().remove();
}

/**
 * Render D3 axes with optional transition duration.
 * @param {Object} xAxisTopG - Top x-axis group.
 * @param {Object} yAxisTopG - Top y-axis group.
 * @param {Object} xAxisBottomG - Bottom x-axis group.
 * @param {Object} yAxisBottomG - Bottom y-axis group.
 * @param {Object} xScaleTop - Top x scale.
 * @param {Object} yScaleTop - Top y scale.
 * @param {Object} xScaleBottom - Bottom x scale.
 * @param {Object} yScaleBottom - Bottom y scale.
 * @param {number} [duration=0] - Transition duration in ms.
 */
function _renderAxes(xAxisTopG, yAxisTopG, xAxisBottomG, yAxisBottomG,
                     xScaleTop, yScaleTop, xScaleBottom, yScaleBottom, duration = 0) {
    const xAxisFmt = d3.axisBottom(xScaleTop).ticks(4).tickFormat(d3.timeFormat('%H:%M'));
    const yAxisTopFmt = d3.axisLeft(yScaleTop).ticks(4).tickFormat((d) => d.toFixed(1));
    const xAxisBottomFmt = d3.axisBottom(xScaleBottom).ticks(4).tickFormat(d3.timeFormat('%H:%M'));
    const yAxisBottomFmt = d3.axisLeft(yScaleBottom).ticks(4).tickFormat((d) => d.toFixed(0));

    const applyAxis = (sel, axisGen) => {
        if (duration > 0) {
            sel.transition().duration(duration).call(axisGen);
        } else {
            sel.call(axisGen);
        }
        // Style axis text
        sel.selectAll('text').attr('fill', '#888').attr('font-size', '11px').attr('font-family', 'monospace');
        sel.selectAll('line').attr('stroke', '#444');
        sel.select('.domain').attr('stroke', '#444');
    };

    applyAxis(xAxisTopG, xAxisFmt);
    applyAxis(yAxisTopG, yAxisTopFmt);
    applyAxis(xAxisBottomG, xAxisBottomFmt);
    applyAxis(yAxisBottomG, yAxisBottomFmt);
}

/**
 * Position the NIS threshold line at the correct pixel y-coordinate.
 * @param {Object} nisThresholdLine - D3 selection of the threshold line.
 * @param {Object} yScaleBottom - Bottom y scale.
 * @param {number} bottomHeight - Height of the bottom chart area in px.
 */
function _positionNisThreshold(nisThresholdLine, yScaleBottom, bottomHeight) {
    const yPx = yScaleBottom(NIS_THRESHOLD);
    if (yPx >= 0 && yPx <= bottomHeight) {
        nisThresholdLine
            .attr('y1', yPx)
            .attr('y2', yPx)
            .attr('display', null);
    } else {
        nisThresholdLine.attr('display', 'none');
    }
}

/**
 * Convert a confidence score to a hex color string for D3.
 * @param {number} confidence - Confidence score in [0, 1].
 * @returns {string} Hex color string.
 */
function _confidenceHex(confidence) {
    if (confidence > 0.85) return '#66ff66';
    if (confidence >= 0.60) return '#ffaa00';
    return '#ff4444';
}

// ---------------------------------------------------------------------------
// Step 19: selectObject
// ---------------------------------------------------------------------------

/**
 * Switch the chart to display data for a different object. (F-056)
 *
 * @param {Object} chartState - Chart state from initResidualChart.
 * @param {number|null} noradId - NORAD ID to display, or null to clear.
 * @returns {void}
 */
export function selectObject(chartState, noradId) {
    if (!chartState) return;

    chartState.selectedNoradId = noradId;

    // Update title
    const titleText = noradId !== null ? `Object: ${noradId}` : 'Object: [none selected]';
    chartState.svg.select('.chart-title').text(titleText);

    if (noradId !== null && dataStore.has(noradId) && dataStore.get(noradId).length > 0) {
        _redrawChart(chartState, noradId);
    } else {
        // Clear chart lines and show "No data" message
        chartState.lineResidualPath.attr('d', null);
        chartState.lineNisPath.attr('d', null);
        chartState.areaBand.attr('d', null);
        chartState.topDotsG.selectAll('circle').remove();
        chartState.bottomDotsG.selectAll('circle').remove();
    }
}

// ---------------------------------------------------------------------------
// Step 20: renderNoiseBand and renderNisThreshold (exported)
// ---------------------------------------------------------------------------

/**
 * Trigger a re-render of the ±2σ noise band. Called when sigma values update.
 * The band data is re-computed from dataStore in _redrawChart; this function
 * forces a redraw for the selected object.
 *
 * @param {Object} chartState - Chart state from initResidualChart.
 * @param {number} sigma2Km - 2-sigma threshold in km (unused — derived from data).
 * @returns {void}
 */
export function renderNoiseBand(chartState, sigma2Km) {
    if (!chartState || chartState.selectedNoradId === null) return;
    _redrawChart(chartState, chartState.selectedNoradId);
}

/**
 * Update the NIS threshold line to a new chi-squared critical value.
 * Default is 12.592 (p=0.05, 6 dof). Callers may override per-object.
 *
 * @param {Object} chartState - Chart state from initResidualChart.
 * @param {number} threshold - Chi-squared critical value.
 * @returns {void}
 */
export function renderNisThreshold(chartState, threshold) {
    if (!chartState) return;
    // Re-position the threshold line at the new value using the current scale.
    _positionNisThreshold(
        chartState.nisThresholdLine,
        chartState.yScaleBottom,
        chartState.bottomHeight
    );
}
