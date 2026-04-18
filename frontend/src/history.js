/**
 * @module history
 * @description History/Events page module for the ne-body SSA Platform.
 * Fetches paginated anomaly event data from GET /events/history and renders
 * a sortable, filterable table with pagination controls.
 */

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
    q: '',
    type: '',
    status: '',
    since_utc: '',
    until_utc: '',
    sort_by: 'detection_epoch_utc',
    sort_dir: 'desc',
    page: 1,
    page_size: 25,
    total: 0,
};

/** Backend base URL — same derivation as main.js. */
let backendBaseUrl = '';

// ---------------------------------------------------------------------------
// Date/time utilities
// ---------------------------------------------------------------------------

/**
 * Parse a MM/DD/YYYY HH:MM string (24-hour, local input treated as UTC) into
 * an ISO-8601 UTC string (YYYY-MM-DDTHH:MM:00Z). Returns null if invalid.
 *
 * @param {string} str - Input string in MM/DD/YYYY HH:MM format.
 * @returns {string|null} ISO-8601 UTC string or null if unparseable.
 */
function _parseDateTime(str) {
    if (!str || !str.trim()) return null;
    const match = str.trim().match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})\s+(\d{1,2}):(\d{2})$/);
    if (!match) return null;
    const [, mm, dd, yyyy, hh, min] = match;
    const month = parseInt(mm, 10);
    const day = parseInt(dd, 10);
    const year = parseInt(yyyy, 10);
    const hour = parseInt(hh, 10);
    const minute = parseInt(min, 10);
    if (month < 1 || month > 12) return null;
    if (day < 1 || day > 31) return null;
    if (hour < 0 || hour > 23) return null;
    if (minute < 0 || minute > 59) return null;
    const mmStr = String(month).padStart(2, '0');
    const ddStr = String(day).padStart(2, '0');
    const hhStr = String(hour).padStart(2, '0');
    const minStr = String(minute).padStart(2, '0');
    return `${yyyy}-${mmStr}-${ddStr}T${hhStr}:${minStr}:00Z`;
}

/**
 * Format an ISO-8601 string to MM/DD/YYYY HH:MM UTC for display.
 *
 * @param {string|null} isoStr - ISO-8601 UTC string.
 * @returns {string} Formatted string or empty string if null/invalid.
 */
function _formatDateTime(isoStr) {
    if (!isoStr) return '';
    // Parse as UTC. Accept both +00:00 and Z suffixes.
    const d = new Date(isoStr);
    if (isNaN(d.getTime())) return isoStr;
    const mm = String(d.getUTCMonth() + 1).padStart(2, '0');
    const dd = String(d.getUTCDate()).padStart(2, '0');
    const yyyy = d.getUTCFullYear();
    const hh = String(d.getUTCHours()).padStart(2, '0');
    const min = String(d.getUTCMinutes()).padStart(2, '0');
    return `${mm}/${dd}/${yyyy} ${hh}:${min} UTC`;
}

/**
 * Format a duration in seconds to "Xm Ys" or "—" if null.
 *
 * @param {number|null} seconds - Duration in seconds.
 * @returns {string} Formatted duration string.
 */
function _formatDuration(seconds) {
    if (seconds == null) return '\u2014';
    const totalS = Math.round(seconds);
    const m = Math.floor(totalS / 60);
    const s = totalS % 60;
    if (m === 0) return `${s}s`;
    return `${m}m ${s}s`;
}

/**
 * Escape HTML special characters to prevent XSS in innerHTML.
 *
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

// ---------------------------------------------------------------------------
// Fetch and render
// ---------------------------------------------------------------------------

/**
 * Build the request URL from current state, fetch GET /events/history,
 * then render the table and pagination.
 *
 * @returns {Promise<void>}
 */
async function fetchAndRender() {
    const params = new URLSearchParams();
    if (state.q)         params.set('q', state.q);
    if (state.type)      params.set('type', state.type);
    if (state.status)    params.set('status', state.status);
    if (state.since_utc) params.set('since_utc', state.since_utc);
    if (state.until_utc) params.set('until_utc', state.until_utc);
    params.set('sort_by',   state.sort_by);
    params.set('sort_dir',  state.sort_dir);
    params.set('page',      String(state.page));
    params.set('page_size', String(state.page_size));

    const url = `${backendBaseUrl}/events/history?${params.toString()}`;

    let data;
    try {
        const resp = await fetch(url);
        if (!resp.ok) {
            console.error('[history] GET /events/history returned', resp.status);
            renderTable([]);
            renderPagination(0, state.page, state.page_size);
            return;
        }
        data = await resp.json();
    } catch (err) {
        console.error('[history] fetch error:', err);
        renderTable([]);
        renderPagination(0, state.page, state.page_size);
        return;
    }

    state.total = data.total;
    renderTable(data.results);
    renderPagination(data.total, data.page, data.page_size);

    const countEl = document.getElementById('results-count');
    if (countEl) countEl.textContent = `${data.total} event${data.total !== 1 ? 's' : ''}`;
}

/**
 * Render table rows from the results array.
 *
 * @param {Array<Object>} results - Array of event objects from the API.
 * @returns {void}
 */
function renderTable(results) {
    const tbody = document.getElementById('events-tbody');
    if (!tbody) return;

    if (results.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7"><div class="empty-state">No events found.</div></td></tr>`;
        return;
    }

    const rows = results.map((ev) => {
        const timeStr = _formatDateTime(ev.detection_epoch_utc);
        const objectStr = _escapeHtml(`${ev.name} [${ev.norad_id}]`);

        const typeClass = _escapeHtml(ev.anomaly_type || '');
        const typeLabel = _escapeHtml((ev.anomaly_type || 'unknown').replace('_', ' '));
        const typeBadge = `<span class="type-badge ${typeClass}">${typeLabel}</span>`;

        const statusClass = _escapeHtml(ev.status || '');
        const statusLabel = _escapeHtml(ev.status || 'unknown');
        const statusBadge = `<span class="status-badge ${statusClass}">${statusLabel}</span>`;

        const nisStr = ev.nis_value != null ? Number(ev.nis_value).toFixed(2) : '\u2014';
        const durStr = _escapeHtml(_formatDuration(ev.recalibration_duration_s));
        const viewUrl = `index.html?select=${encodeURIComponent(ev.norad_id)}`;

        return `<tr>
            <td>${_escapeHtml(timeStr)}</td>
            <td>${objectStr}</td>
            <td>${typeBadge}</td>
            <td>${statusBadge}</td>
            <td>${_escapeHtml(nisStr)}</td>
            <td>${durStr}</td>
            <td><a class="view-link" href="${_escapeHtml(viewUrl)}">View &rarr;</a></td>
        </tr>`;
    });

    tbody.innerHTML = rows.join('');
}

/**
 * Update pagination controls.
 *
 * @param {number} total - Total number of matching records.
 * @param {number} page - Current 1-indexed page.
 * @param {number} page_size - Records per page.
 * @returns {void}
 */
function renderPagination(total, page, page_size) {
    const totalPages = Math.max(1, Math.ceil(total / page_size));

    const indicator = document.getElementById('page-indicator');
    if (indicator) indicator.textContent = `Page ${page} of ${totalPages}`;

    const prevBtn = document.getElementById('prev-btn');
    const nextBtn = document.getElementById('next-btn');
    if (prevBtn) prevBtn.disabled = page <= 1;
    if (nextBtn) nextBtn.disabled = page >= totalPages;
}

// ---------------------------------------------------------------------------
// Sort header helpers
// ---------------------------------------------------------------------------

/**
 * Update sort indicator classes on all sortable th elements.
 *
 * @returns {void}
 */
function _updateSortHeaders() {
    const headers = document.querySelectorAll('th.sortable');
    for (const th of headers) {
        th.classList.remove('sort-asc', 'sort-desc');
        if (th.dataset.col === state.sort_by) {
            th.classList.add(state.sort_dir === 'asc' ? 'sort-asc' : 'sort-desc');
        }
    }
}

// ---------------------------------------------------------------------------
// Debounce utility
// ---------------------------------------------------------------------------

/**
 * Return a debounced version of fn with the given delay in ms.
 *
 * @param {Function} fn - Function to debounce.
 * @param {number} delay_ms - Debounce delay in milliseconds.
 * @returns {Function} Debounced function.
 */
function _debounce(fn, delay_ms) {
    let timer = null;
    return function (...args) {
        clearTimeout(timer);
        timer = setTimeout(() => fn.apply(this, args), delay_ms);
    };
}

// ---------------------------------------------------------------------------
// init
// ---------------------------------------------------------------------------

/**
 * Initialize the history page: derive backendBaseUrl, bind all event handlers,
 * and perform the initial fetch.
 *
 * @returns {void}
 */
function init() {
    // Derive backend base URL — same logic as main.js.
    backendBaseUrl = window.location.protocol + '//' + window.location.hostname + ':8001';

    // Apply initial sort indicator.
    _updateSortHeaders();

    // --- Search input: debounced 400ms ---
    const searchInput = document.getElementById('search-input');
    if (searchInput) {
        const debouncedSearch = _debounce(() => {
            state.q = searchInput.value;
            state.page = 1;
            fetchAndRender();
        }, 400);
        searchInput.addEventListener('input', debouncedSearch);
    }

    // --- Type dropdown: immediate ---
    const typeSelect = document.getElementById('type-select');
    if (typeSelect) {
        typeSelect.addEventListener('change', () => {
            state.type = typeSelect.value;
            state.page = 1;
            fetchAndRender();
        });
    }

    // --- Status dropdown: immediate ---
    const statusSelect = document.getElementById('status-select');
    if (statusSelect) {
        statusSelect.addEventListener('change', () => {
            state.status = statusSelect.value;
            state.page = 1;
            fetchAndRender();
        });
    }

    // --- Since date input: on blur ---
    const sinceInput = document.getElementById('since-input');
    if (sinceInput) {
        sinceInput.addEventListener('blur', () => {
            const val = sinceInput.value.trim();
            if (val === '') {
                sinceInput.classList.remove('invalid');
                state.since_utc = '';
                state.page = 1;
                fetchAndRender();
                return;
            }
            const parsed = _parseDateTime(val);
            if (parsed === null) {
                sinceInput.classList.add('invalid');
                // Do not update state or fetch on invalid input.
                return;
            }
            sinceInput.classList.remove('invalid');
            state.since_utc = parsed;
            state.page = 1;
            fetchAndRender();
        });
    }

    // --- Until date input: on blur ---
    const untilInput = document.getElementById('until-input');
    if (untilInput) {
        untilInput.addEventListener('blur', () => {
            const val = untilInput.value.trim();
            if (val === '') {
                untilInput.classList.remove('invalid');
                state.until_utc = '';
                state.page = 1;
                fetchAndRender();
                return;
            }
            const parsed = _parseDateTime(val);
            if (parsed === null) {
                untilInput.classList.add('invalid');
                return;
            }
            untilInput.classList.remove('invalid');
            state.until_utc = parsed;
            state.page = 1;
            fetchAndRender();
        });
    }

    // --- Sortable column headers ---
    const sortHeaders = document.querySelectorAll('th.sortable');
    for (const th of sortHeaders) {
        th.addEventListener('click', () => {
            const col = th.dataset.col;
            if (state.sort_by === col) {
                // Toggle direction if same column.
                state.sort_dir = state.sort_dir === 'desc' ? 'asc' : 'desc';
            } else {
                state.sort_by = col;
                state.sort_dir = 'desc';
            }
            state.page = 1;
            _updateSortHeaders();
            fetchAndRender();
        });
    }

    // --- Pagination: prev button ---
    const prevBtn = document.getElementById('prev-btn');
    if (prevBtn) {
        prevBtn.addEventListener('click', () => {
            if (state.page > 1) {
                state.page -= 1;
                fetchAndRender();
            }
        });
    }

    // --- Pagination: next button ---
    const nextBtn = document.getElementById('next-btn');
    if (nextBtn) {
        nextBtn.addEventListener('click', () => {
            const totalPages = Math.max(1, Math.ceil(state.total / state.page_size));
            if (state.page < totalPages) {
                state.page += 1;
                fetchAndRender();
            }
        });
    }

    // --- Page size selector ---
    const pageSizeSelect = document.getElementById('page-size-select');
    if (pageSizeSelect) {
        pageSizeSelect.addEventListener('change', () => {
            state.page_size = parseInt(pageSizeSelect.value, 10);
            state.page = 1;
            fetchAndRender();
        });
    }

    // Initial fetch.
    fetchAndRender();
}

document.addEventListener('DOMContentLoaded', init);
