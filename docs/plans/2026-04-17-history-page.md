# Implementation Plan: History/Events Page
Date: 2026-04-17
Status: Approved — ready for implementation

## Summary

Add a dedicated history page (`history.html`) to the ne-body SSA platform that displays all past anomaly events in a searchable, sortable, filterable table. A new `GET /events/history` backend endpoint provides paginated, filtered queries against the existing SQLite `alerts` table, joining object names from the in-memory catalog. The main page gains a `?select=NORAD_ID` deep-link parameter so the history page can link back to a specific object on the globe.

## Requirements addressed

- **F-035** — Store all anomaly events with NORAD ID, detection epoch, anomaly type, NIS value, recalibration duration. (Already implemented; this plan surfaces the data in a dedicated UI.)
- **F-055** — Anomaly alert feed with object name, time, type, resolution status. (Extends the existing real-time panel with a persistent, queryable historical view.)
- **F-041** — `GET /object/{norad_id}/history` returns time-series for a single object. (The new `GET /events/history` endpoint extends this to cross-catalog queries with filtering.)

## Decision: SQLite vs PostgreSQL

**Recommendation: Stay with SQLite for the POC.**

Reasoning:
1. **Row count.** The `alerts` table will contain hundreds to low thousands of rows for this POC (76 objects, polling every 30 min, anomalies are infrequent). SQLite handles this trivially.
2. **Full-text search on object names.** Object names live in `catalog.json` (loaded into `app.state.catalog_entries` at startup), not in the `alerts` table. The search endpoint will resolve names in Python by building a NORAD-ID-to-name map and filtering by name match before or after the SQL query. No FTS extension needed.
3. **Multi-column sort/filter/pagination.** SQLite supports `ORDER BY`, `LIMIT/OFFSET`, and `WHERE` clauses — all that is needed here.
4. **No new dependency.** Adding PostgreSQL would require `asyncpg` or `psycopg`, a running Postgres server, and migration tooling. This violates C-002 (operable on a single developer machine without infrastructure dependencies) and C-005 (minimize dependencies).
5. **Scalability path.** The architecture document Section 6 already names TimescaleDB as the production replacement. PostgreSQL is the right choice post-POC, not during it.

If the user later needs full-text search across free-form notes attached to events, or if the alert volume grows past ~50,000 rows, PostgreSQL should be revisited. For now, SQLite + Python-side name filtering is sufficient and simpler.

## Files affected

- `backend/main.py` — Add `GET /events/history` endpoint
- `frontend/history.html` — New file: history page with table, search, filters, pagination
- `frontend/src/history.js` — New file: JS module for fetching, rendering, and interacting with the history table
- `frontend/index.html` — Add "HISTORY" nav link in the header bar
- `frontend/src/main.js` — Read `?select=NORAD_ID` from URL on load, auto-select and fly to object

## Data flow changes

**Before:** Anomaly events are stored in `alerts` table and displayed only in the real-time alert panel (`alerts.js`) during the current browser session. No way to review past events after page reload (except the 20-item `GET /object/{norad_id}/anomalies` endpoint per object).

**After:**
```
Browser (history.html)
      |
      | GET /events/history?q=ISS&type=maneuver&page=1&page_size=25
      v
backend/main.py -> GET /events/history
      |
      | 1. Build norad_id->name map from app.state.catalog_entries
      | 2. If q param: find matching NORAD IDs by name substring or numeric match
      | 3. Query alerts table with WHERE/ORDER BY/LIMIT/OFFSET
      | 4. Join object name from catalog map
      | 5. Return paginated response
      v
Browser renders table
      |
      | User clicks "View on Globe" for NORAD 25544
      v
Navigates to index.html?select=25544
      |
      | main.js reads URLSearchParams, calls flyToObject(25544)
      v
Globe auto-selects and flies to object
```

## Implementation steps

### Phase 1: Backend endpoint

1. **Add `GET /events/history` endpoint** (`backend/main.py`)
   - Action: Add a new route handler with query parameters as defined below.
   - Why: The existing per-object endpoints (`/object/{norad_id}/history`, `/object/{norad_id}/anomalies`) do not support cross-catalog search, filtering by type/status, time range, or pagination.
   - Dependencies: None
   - Risk: Low

   **API schema:**
   ```
   GET /events/history

   Query parameters:
     q           string   optional  Free-text search. Matches NORAD ID (exact numeric)
                                    or object name (case-insensitive substring).
     type        string   optional  Filter by anomaly_type. One of: maneuver,
                                    drag_anomaly, filter_divergence.
     status      string   optional  Filter by status. One of: active, resolved,
                                    dismissed, recalibrating.
     since_utc   string   optional  ISO-8601 UTC. Include only events with
                                    detection_epoch_utc >= this value.
     until_utc   string   optional  ISO-8601 UTC. Include only events with
                                    detection_epoch_utc <= this value.
     sort_by     string   optional  Column to sort by. One of: detection_epoch_utc,
                                    anomaly_type, status, nis_value,
                                    recalibration_duration_s. Default: detection_epoch_utc.
     sort_dir    string   optional  asc or desc. Default: desc (newest first).
     page        int      optional  1-indexed page number. Default: 1.
     page_size   int      optional  Results per page. Default: 25. Max: 100.

   Response (200):
   {
     "total": 142,
     "page": 1,
     "page_size": 25,
     "results": [
       {
         "id": 87,
         "norad_id": 25544,
         "name": "ISS (ZARYA)",
         "object_class": "active_satellite",
         "detection_epoch_utc": "2026-04-15T14:30:00+00:00",
         "anomaly_type": "maneuver",
         "nis_value": 18.42,
         "status": "resolved",
         "resolution_epoch_utc": "2026-04-15T15:00:00+00:00",
         "recalibration_duration_s": 1800.0
       },
       ...
     ]
   }
   ```

   **Implementation logic (pseudocode):**
   ```
   1. Build catalog_name_map: {norad_id: {name, object_class}} from app.state.catalog_entries
   2. If q is provided:
      a. Try parsing q as int -> match_norad_ids = {int(q)} if in catalog
      b. Also: for each catalog entry, if q.lower() in entry.name.lower(), add norad_id
      c. Result: set of matching NORAD IDs (union of a and b)
   3. Build SQL WHERE clause dynamically:
      - If q matched NORAD IDs: WHERE norad_id IN (...)
      - If q matched nothing: return {total: 0, page: 1, results: []}
      - If type: AND anomaly_type = ?
      - If status: AND status = ?
      - If since_utc: AND detection_epoch_utc >= ?
      - If until_utc: AND detection_epoch_utc <= ?
   4. Validate sort_by against allowlist, default to detection_epoch_utc
   5. Validate sort_dir against {asc, desc}, default to desc
   6. Run COUNT(*) query with WHERE clause -> total
   7. Run SELECT with WHERE + ORDER BY + LIMIT/OFFSET -> rows
   8. For each row, join name and object_class from catalog_name_map
   9. Return {total, page, page_size, results}
   ```

   - Allowlisted sort columns prevent SQL injection via sort_by. Use parameterized queries for all WHERE values.
   - The `detection_epoch_utc` column stores ISO-8601 strings. SQLite string comparison works correctly for ISO-8601 date ordering, so `>=` / `<=` comparisons on the string column are valid for time range filtering.

### Phase 2: Frontend — history page

2. **Create `frontend/history.html`** (new file)
   - Action: New HTML page with the same dark/monospace theme as `index.html`. Contains:
     - Header bar matching `index.html` style, with "ne-body SSA Platform" title and a "LIVE VIEW" link back to `index.html`
     - Search input field (debounced 300ms)
     - Filter row: Type dropdown (All / Maneuver / Drag Anomaly / Filter Divergence), Status dropdown (All / Active / Resolved / Dismissed / Recalibrating), Date range inputs (since/until, HTML date inputs or text fields accepting ISO-8601)
     - Results table with columns:
       - **Time** (detection_epoch_utc, formatted as `YYYY-MM-DD HH:MM:SS UTC`)
       - **Object** (name + NORAD ID in parentheses, e.g., "ISS (ZARYA) [25544]")
       - **Type** (badge with anomaly_type, same color scheme as alert-type-badge in index.html)
       - **Status** (badge, same color scheme as alert-status in index.html)
       - **NIS** (numeric, 2 decimal places)
       - **Duration** (recalibration_duration_s formatted as "Xm Ys" or "--" if null)
       - **Actions** ("View on Globe" link -> `index.html?select=NORAD_ID`)
     - Pagination controls: Previous / Next buttons, "Page X of Y" display, page size selector (10/25/50/100)
     - Empty state: "No events found" message when results array is empty
   - Why: Separate page keeps the globe view uncluttered (important for DoD/Space Force demo where the globe is the centerpiece). Full screen width gives the table proper room.
   - Dependencies: None
   - Risk: Low

3. **Create `frontend/src/history.js`** (new file)
   - Action: ES2022 module loaded by `history.html`. Manages:
     - State object: `{ q, type, status, since_utc, until_utc, sort_by, sort_dir, page, page_size, total }`
     - `fetchHistory(state)` — builds URL from state, calls `GET /events/history`, returns parsed JSON
     - `renderTable(results)` — clears table body, creates `<tr>` for each result with formatted cells
     - `renderPagination(total, page, page_size)` — updates page controls
     - `bindSearchInput()` — attaches debounced (300ms) input handler that resets page to 1 and fetches
     - `bindFilterDropdowns()` — attaches change handlers to type, status, date inputs; resets page to 1 and fetches
     - `bindSortHeaders()` — attaches click handlers to `<th>` elements; toggles sort_dir if same column, sets sort_by, fetches
     - `bindPagination()` — prev/next buttons adjust page and fetch
     - `init()` — called on DOMContentLoaded; reads `backendBaseUrl` from same origin logic as main.js; binds all handlers; performs initial fetch
   - Why: Keeps JS modular and consistent with existing codebase pattern.
   - Dependencies: Step 2 (history.html must exist)
   - Risk: Low

   **Backend base URL derivation** (same pattern as `main.js`):
   ```
   Frontend served on port 8080 -> backend on port 8001
   Use: const backendBaseUrl = window.location.origin.replace(':8080', ':8001')
   Or: read from a shared config if one exists.
   ```
   Note: Check how `main.js` derives `backendBaseUrl` and replicate exactly.

### Phase 3: Navigation and deep linking

4. **Add nav link to `frontend/index.html`** (`frontend/index.html`)
   - Action: In the `#header` div, add a link element after the title text:
     ```
     <a href="history.html" style="...">HISTORY</a>
     ```
     Style: monospace, same font-size as header (18px), color #aaa, no underline, hover color #fff, margin-left 20px. Float right, positioned before the mute button.
   - Why: Users need a way to navigate to the history page from the main view.
   - Dependencies: Step 2 (history.html must exist)
   - Risk: Low

5. **Add `?select=NORAD_ID` deep link support** (`frontend/src/main.js`)
   - Action: In the initialization flow (after viewer and catalog are loaded), add:
     ```
     1. Read URLSearchParams from window.location.search
     2. If 'select' param exists:
        a. Parse as integer -> targetNoradId
        b. Validate: check nameMap.has(targetNoradId)
        c. If valid: set selectedNoradId = targetNoradId
        d. Call flyToObject(viewer, targetNoradId)
        e. Call selectObject(chartState, targetNoradId) to update residual chart
        f. Trigger info panel update for this object
     ```
   - Why: Enables the history page "View on Globe" links to deep-link directly to a specific object.
   - Dependencies: None (can be implemented independently)
   - Risk: Low

6. **Add "LIVE VIEW" link to `frontend/history.html`** (part of step 2)
   - Action: Header includes a link back to `index.html` labeled "LIVE VIEW".
   - Why: Bidirectional navigation between the two pages.
   - Dependencies: Part of step 2
   - Risk: Low

## Test strategy

### Unit tests (backend)
- **`tests/test_events_history.py`** (new file):
  - Test `GET /events/history` with no params returns paginated results (default sort desc by detection_epoch_utc)
  - Test `q` param: numeric match (exact NORAD ID), name substring match (case-insensitive), no match returns empty
  - Test `type` filter: returns only matching anomaly_type
  - Test `status` filter: returns only matching status
  - Test `since_utc` and `until_utc` time range filtering
  - Test `sort_by` with each allowed column + `sort_dir` asc/desc
  - Test pagination: page=1 page_size=2 with 5 total rows -> correct total, 2 results, correct offset
  - Test invalid `sort_by` value is rejected or defaults gracefully
  - Test `page_size` clamped to max 100
  - Test response includes `name` and `object_class` joined from catalog
  - Test object not in catalog still returns (with name as string of NORAD ID)

### Integration tests
- Seed the alerts table with 10+ rows spanning different anomaly types and statuses
- Verify full round-trip: seed -> `GET /events/history` -> parse response -> assert correct filtering

### Frontend manual tests
- Open `history.html`, verify table loads with data
- Type in search box, verify debounced fetch and table update
- Click column headers, verify sort toggling
- Click filter dropdowns, verify filtering
- Click "View on Globe", verify navigation to `index.html?select=NORAD_ID` and object auto-selection
- Verify pagination controls work (next/prev, page size change)
- Verify empty state message when no results match

## Risks and mitigations

- **Risk: ISO-8601 string comparison in SQLite.** The `detection_epoch_utc` column stores timezone-aware ISO-8601 strings (e.g., `2026-04-15T14:30:00+00:00`). SQLite string comparison works correctly for UTC-only timestamps because lexicographic order matches chronological order for the same timezone offset. However, if timestamps with different offsets are stored, comparison would be incorrect. **Mitigation:** All timestamps in the alerts table are UTC (enforced by `anomaly.py` requiring UTC-aware datetimes). Verify this assumption holds. — Risk: Low

- **Risk: Name search performance at scale.** The Python-side name filtering iterates all ~76 catalog entries for every search request. **Mitigation:** 76 entries is negligible. If the catalog grows to 10,000+ (post-POC), add a name column to the alerts table or use SQLite FTS5. — Risk: Low

- **Risk: CORS.** The history page is served from the same origin as `index.html` (same static file server). The existing CORS middleware allows `localhost:8080`. If the history page is served from a different port, CORS will block requests. **Mitigation:** Serve both pages from the same static server. No change needed. — Risk: Low

- **Risk: `LIMIT/OFFSET` pagination becomes slow on very large tables.** **Mitigation:** For POC-scale data (hundreds to low thousands of rows), OFFSET-based pagination is fine. Post-POC, switch to keyset pagination (WHERE id > last_seen_id). — Risk: Low

## Open questions — RESOLVED

1. **Date input format:** Two `<input type="text">` fields with `MM/DD/YYYY HH:MM` placeholder (24-hour). Parse on the frontend to ISO-8601 UTC before sending to the backend. Invalid input is ignored (field outlined red, request not sent).

2. **Auto-refresh:** Filter changes (search input debounced 400ms, dropdowns immediate, date inputs on blur) trigger an automatic reload. No manual refresh button needed.

3. **Dismissed alerts in default view:** Show all statuses by default (no pre-filter). The status dropdown includes "Dismissed" as an option for explicit filtering.
