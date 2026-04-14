# Implementation Plan: N2YO Supplemental TLE Source
Date: 2026-04-09
Status: Draft

## Summary
Add N2YO (`https://api.n2yo.com/rest/v1/satellite/`) as a supplemental, fallback TLE
source alongside Space-Track.org. Space-Track remains the primary source; N2YO is
consulted only per-object when Space-Track returns no TLE for a catalog entry or
when the newest Space-Track TLE epoch is older than 7 days. All TLEs are tagged
with their source (`space_track` or `n2yo`) for provenance, and `ingest.py`
remains the only module permitted to speak to any external TLE API.

## Requirements addressed
- **F-001** — TLE ingestion for the configured catalog (extended with a fallback path).
- **F-003** — TLE checksum validation (reused for N2YO responses).
- **F-004** — Local persistent storage with fetch timestamp (extended with source column).
- **F-006** — API call audit logging (extended to cover N2YO calls with the same
  `F-006 <PROVIDER>_API_CALL/RESPONSE` log signature).
- **NF-010** — Recover from a failed external API call without crashing (N2YO
  failures must degrade gracefully without killing the ingest loop).
- **NF-040** — No credentials in source; `N2YO_API_KEY` read from environment only.
- **NF-042** — Data source access auditing (N2YO calls logged with timestamp and
  NORAD ID).

## Conflict — human decision required before implementation

> **Conflict:** This plan introduces a second external TLE source (N2YO), but
> **C-001** in `docs/requirements.md` states: "The POC shall use only publicly
> available, unclassified data sources. **Space-Track.org is the sole data
> source.**" Adding N2YO violates the "sole data source" clause of C-001 even
> though N2YO is publicly available and unclassified (so the first clause of
> C-001 is still satisfied).
>
> Additionally, `docs/architecture.md` section 3.1 describes `ingest.py` as the
> "Sole interface to Space-Track.org" and section 6 restates "Space-Track.org
> only" as a POC scope constraint. Those statements must be updated to reflect
> the dual-source model.
>
> **Resolution needed:** Either (a) amend C-001 and architecture doc section 3.1
> / section 6 to permit publicly-available supplemental TLE sources, with N2YO
> named explicitly, or (b) reject this plan. Implementation must not begin until
> the human updates the requirements and architecture documents.

I am not resolving this conflict. The remainder of this plan assumes resolution
(a) is chosen. If (b) is chosen, discard the plan.

Secondary note: **F-025** references "positional uncertainty class of Space-Track
TLEs" when describing the Kalman `R` matrix. If N2YO TLEs have a different
accuracy class, `R` may need per-source tuning. This plan does **not** modify
`R` — it only tags the source in the DB so a post-POC tuning plan can use it.
Flag in Open Questions below.

## Files affected

- `backend/ingest.py` — Add N2YO fetch function, schema migration, fallback
  selection logic in `poll_once`, new endpoint constants, and `source` field
  propagation through `cache_tles`.
- `.env.example` — Add `N2YO_API_KEY` line with placeholder.
- `docs/requirements.md` — (human-edited, not by implementer) update C-001 per
  conflict resolution above.
- `docs/architecture.md` — (human-edited, not by implementer) update section 3.1
  and section 6 per conflict resolution above.
- `tests/test_ingest.py` — Add N2YO fetch, fallback selection, and source-tagging
  tests. (Create file if it does not exist; otherwise append.)

**No changes to `backend/main.py`.** The lifespan/background task contract with
`ingest.run_ingest_loop(event_bus=…)` is preserved. The processing loop, Kalman
filter, propagator, and frontend remain untouched. N2YO integration is entirely
internal to `ingest.py`.

## Data flow changes

### Before
```
poll_once:
  authenticate() -> cookie
  fetch_tles(norad_ids, cookie) -> list[dict]  (Space-Track only)
  cache_tles(db, tles, fetched_at_utc)         (no source column)
  if inserted > 0: emit catalog_update event
```

### After
```
poll_once:
  authenticate() -> cookie
  st_tles = fetch_tles(norad_ids, cookie)         (Space-Track, tagged source='space_track')
  cache_tles(db, st_tles, fetched_at_utc, source='space_track')

  # Fallback selection — pure DB reads, no network
  gap_ids = _select_n2yo_fallback_ids(
      db, catalog_norad_ids, stale_threshold_days=7, max_ids=50
  )

  if N2YO_API_KEY set and gap_ids:
      n2yo_tles = []
      for norad_id in gap_ids:                    (sequential, rate-limited)
          tle = await fetch_tle_n2yo(norad_id, api_key, client)
          if tle: n2yo_tles.append(tle)
      cache_tles(db, n2yo_tles, fetched_at_utc, source='n2yo')

  if total_inserted > 0: emit catalog_update event (single event, same shape)
```

The event bus payload shape does not change. Downstream consumers
(`_processing_loop_task`, Kalman, broadcast) are unaffected; they read
`get_latest_tle()` which continues to return the newest-epoch TLE regardless of
source. The new `source` column is queryable but not required by any existing
consumer.

## Implementation steps

### Phase 1: Schema migration and source column

1. **Add schema migration to `init_catalog_db`** (`backend/ingest.py`)
   - Action: After the existing `CREATE TABLE IF NOT EXISTS tle_catalog (...)`
     statement, run a safe additive migration:
     ```
     # Pseudocode — do not copy-paste into code without review
     existing_cols = {row['name'] for row in
                      conn.execute("PRAGMA table_info(tle_catalog)").fetchall()}
     if 'source' not in existing_cols:
         conn.execute(
             "ALTER TABLE tle_catalog ADD COLUMN source TEXT NOT NULL DEFAULT 'space_track'"
         )
         conn.commit()
     ```
     This is idempotent and backwards-compatible: older rows get
     `source='space_track'`, which matches their actual provenance.
   - Why: F-004 plus the new provenance requirement. `ALTER TABLE ADD COLUMN`
     is safe in SQLite and does not require a table rebuild.
   - Dependencies: none.
   - Risk: Low. Additive, default-valued, idempotent.

2. **Update `CREATE TABLE` statement** (`backend/ingest.py::init_catalog_db`)
   - Action: Add `source TEXT NOT NULL DEFAULT 'space_track'` to the CREATE
     TABLE for fresh installs so new databases have the column natively without
     relying on the migration.
   - Why: Keep fresh-install and migrated schemas identical.
   - Dependencies: step 1.
   - Risk: Low.

3. **Extend `cache_tles` signature** (`backend/ingest.py`)
   - Action: Add a `source: str = 'space_track'` keyword argument. Update the
     `INSERT OR IGNORE INTO tle_catalog (…) VALUES (…)` statement to include
     the `source` column. Existing callers pass no `source` and continue to
     tag `space_track`.
   - Why: Provenance tagging at the write boundary is the cleanest seam.
   - Dependencies: steps 1 and 2.
   - Risk: Low. Default value preserves backward compatibility.

4. **Extend read helpers to return `source`** (`backend/ingest.py`)
   - Action: Update the SELECT lists in `get_cached_tles` and `get_latest_tle`
     to include `source`. The returned dicts gain a `source` key.
   - Why: Downstream code (audit logs, future tuning) can inspect provenance.
   - Dependencies: step 2.
   - Risk: Low. Adding a key to a dict is additive; existing consumers do not
     break. **Verify** `backend/main.py` lines ~507, ~709, ~1106, ~1242, ~1264
     do not use `**tle_record` unpacking into a strict schema — a dict-key
     addition there is a no-op.

### Phase 2: N2YO client

5. **Add N2YO constants** (`backend/ingest.py`)
   - Action: Add module-level constants near the existing `_SPACETRACK_*`
     constants:
     - `_N2YO_BASE_URL: str = "https://api.n2yo.com/rest/v1/satellite"`
     - `_N2YO_TLE_URL_TEMPLATE: str = f"{_N2YO_BASE_URL}/tle/{{norad_id}}"` (key appended per-call, not in format string — see step 6 on key safety)
     - `N2YO_MAX_REQUESTS_PER_CYCLE: int = 50`
     - `N2YO_STALE_THRESHOLD_S: int = 7 * 86400` (7 days in seconds)
   - Why: Centralize magic values; match the `_SPACETRACK_*` naming convention.
   - Dependencies: none.
   - Risk: Low.

6. **Add `fetch_tle_n2yo`** (`backend/ingest.py`)
   - Action: New `async def fetch_tle_n2yo(norad_id: int, api_key: str, client: httpx.AsyncClient) -> Optional[dict]:`
     Signature details:
     - Accepts an **injected** `httpx.AsyncClient` so the caller controls
       connection reuse and tests can mock via `httpx.MockTransport`.
     - Returns `None` on any failure (HTTP non-2xx, JSON decode error, empty
       `tle` field, checksum failure, missing `info` block) — **never raises**.
       This honors NF-010 (graceful per-object degradation).
     - Construct URL as `f"{_N2YO_BASE_URL}/tle/{norad_id}&apiKey={api_key}"`
       matching the N2YO format given in the task brief. Note: the N2YO URL
       uses `&apiKey=` as the first query separator (not `?apiKey=`) — this
       is unusual but documented in the task brief. Preserve it exactly.
     - Emit **F-006-style** call and response log lines with a
       `N2YO_API_CALL` / `N2YO_API_RESPONSE` tag and the **api key redacted**.
       The logged URL must strip the `apiKey` value (e.g. `&apiKey=***`).
     - Parse JSON body. Expect `{"info": {"satid": <int>, ...}, "tle": "<line1>\r\n<line2>"}`.
       Split the `tle` field on `\r\n` **or** `\n` (be tolerant to either).
       Require exactly 2 non-empty lines.
     - Validate with the existing `validate_tle(line1, line2)` (F-003).
     - Extract epoch via existing `_parse_tle_epoch_utc(line1)`.
     - Verify `info.satid == norad_id`. If mismatch, log a warning and return
       `None`. (Paranoia against wrong-satellite responses.)
     - Return dict:
       ```
       {
         "norad_id": norad_id,
         "epoch_utc": <iso8601 str>,
         "tle_line1": line1,
         "tle_line2": line2,
       }
       ```
       (Same shape as `fetch_tles` entries — source is applied at `cache_tles`
       time, not here, so the function is source-agnostic.)
   - Why: Isolates N2YO's quirky URL format, per-object rate semantics, and
     response shape behind a function that matches the existing
     `fetch_tles`-returned dict shape.
   - Dependencies: step 5.
   - Risk: Medium. N2YO's free-tier limits are per-hour across **all** calls,
     not per cycle — see Risks section for mitigation.

### Phase 3: Fallback selection and integration

7. **Add `_select_n2yo_fallback_ids` helper** (`backend/ingest.py`)
   - Action: New private sync function:
     `def _select_n2yo_fallback_ids(db, norad_ids: list[int], stale_threshold_s: int, max_ids: int, now_utc: datetime.datetime) -> list[int]:`
     For each NORAD ID in `norad_ids`:
     - Call `get_latest_tle(db, norad_id)`.
     - If it returns `None`: include in output (no TLE at all — gap).
     - If it returns a row whose `epoch_utc` parsed as UTC-aware datetime is
       older than `now_utc - stale_threshold_s`: include in output.
     - Otherwise skip.
     Truncate output to at most `max_ids` entries (deterministic order — sort
     by norad_id ascending, or preserve catalog order; pick catalog order for
     demo reproducibility).
     Return the list.
   - Why: Pure DB-only gap detection, unit-testable without any network
     mocking.
   - Dependencies: steps 1–4.
   - Risk: Low.

8. **Modify `poll_once`** (`backend/ingest.py`)
   - Action: After the existing `cache_tles(db, tles, fetched_at_utc)` call,
     add a fallback block:
     - Read `api_key = os.environ.get("N2YO_API_KEY")`.
     - If `api_key` is falsy, skip the fallback entirely. Log at INFO once:
       `"N2YO_API_KEY not set; skipping supplemental N2YO fallback."` This
       keeps the existing Space-Track-only deployment working unchanged.
     - Otherwise compute `gap_ids = _select_n2yo_fallback_ids(db, norad_ids, N2YO_STALE_THRESHOLD_S, N2YO_MAX_REQUESTS_PER_CYCLE, fetched_at_utc)`.
     - If `gap_ids` is non-empty, create a fresh `httpx.AsyncClient()` (context
       manager) and loop sequentially over `gap_ids`, calling
       `await fetch_tle_n2yo(...)`. Collect successful returns into a
       `n2yo_tles` list. **Do not** run concurrently — sequential keeps us
       well under the 1,000/hour free-tier limit and makes rate-limit
       debugging simpler. Add a small `await asyncio.sleep(0.1)` between
       calls (polite pacing).
     - After the loop, call
       `cache_tles(db, n2yo_tles, fetched_at_utc, source='n2yo')`.
     - Add returned count into the existing `inserted` variable so the
       catalog_update event reflects both sources as one total.
     - Wrap the N2YO block in a broad `try/except Exception` that logs and
       continues — an N2YO failure must never break the Space-Track pipeline
       (NF-010).
   - Why: Preserves the existing poll flow; N2YO is additive and defensible.
   - Dependencies: steps 3, 6, 7.
   - Risk: Medium. See Risks section.

9. **Update `poll_once` docstring and logs** (`backend/ingest.py`)
   - Action: Note in the docstring that `poll_once` now supplements
     Space-Track with N2YO when `N2YO_API_KEY` is set. Add an INFO log line
     at end of cycle: `"poll_once complete: %d new TLEs inserted (space_track=%d, n2yo=%d), ..."`.
   - Why: Operators need to see source mix in the logs.
   - Dependencies: step 8.
   - Risk: Low.

### Phase 4: Environment variable and docs

10. **Update `.env.example`**
    - Action: Append a new line:
      ```
      N2YO_API_KEY=your_n2yo_api_key
      ```
      The key is optional; if unset the fallback is simply skipped.
    - Why: NF-040 — operators must know the env var name.
    - Dependencies: none.
    - Risk: None.

### Phase 5: Tests

11. **Add `tests/test_ingest.py` cases** (create file if absent; otherwise
    append a new `class TestN2YOFallback`)
    - `test_fetch_tle_n2yo_happy_path` — mock an `httpx.AsyncClient` with
      `MockTransport` returning the sample JSON from the task brief (ISS,
      NORAD 25544). Assert the returned dict has `norad_id=25544`, both TLE
      lines split correctly, and a valid `epoch_utc`.
    - `test_fetch_tle_n2yo_accepts_unix_newlines` — same, but `tle` body uses
      `\n` only (no `\r`). Must still succeed.
    - `test_fetch_tle_n2yo_returns_none_on_http_error` — mock a 500 response;
      assert return value is `None` and no exception propagates.
    - `test_fetch_tle_n2yo_returns_none_on_checksum_fail` — mock a response
      whose TLE lines have bad checksums; assert `None`.
    - `test_fetch_tle_n2yo_returns_none_on_satid_mismatch` — mock response
      whose `info.satid` differs from the requested NORAD ID; assert `None`.
    - `test_fetch_tle_n2yo_redacts_api_key_in_logs` — capture log output,
      assert literal api key string does **not** appear in any log record.
    - `test_select_fallback_ids_includes_missing` — seed DB with no rows for
      some NORAD IDs; assert those IDs are selected.
    - `test_select_fallback_ids_includes_stale` — seed DB with a TLE whose
      `epoch_utc` is 8 days before `now_utc`; assert selection.
    - `test_select_fallback_ids_excludes_fresh` — seed DB with a TLE 1 day
      old; assert not selected.
    - `test_select_fallback_ids_caps_at_max` — seed DB with 80 missing IDs;
      assert only 50 returned.
    - `test_cache_tles_records_source_tag` — call `cache_tles(..., source='n2yo')`
      and assert the row's `source` column is `'n2yo'`; default path is
      `'space_track'`.
    - `test_init_catalog_db_migrates_missing_source_column` — create a DB
      with the pre-migration schema manually, call `init_catalog_db` on it,
      assert the `source` column now exists and existing rows are tagged
      `'space_track'`.
    - `test_poll_once_falls_back_to_n2yo_for_gap_only` — full integration
      test with mocked Space-Track returning TLEs for only half the catalog
      and mocked N2YO returning TLEs for the other half. Assert: total
      inserted == catalog size, source distribution matches expectation,
      single `catalog_update` event emitted, event count reflects both
      sources.
    - `test_poll_once_skips_n2yo_when_api_key_unset` — unset
      `N2YO_API_KEY`, assert no N2YO HTTP calls are attempted and the log
      contains the skip message.
    - `test_poll_once_survives_n2yo_total_failure` — mock N2YO to raise on
      every call; assert `poll_once` still returns successfully with the
      Space-Track inserts intact.

12. **Integration smoke test (manual, documented only)**
    - Action: Document in the plan that the implementer should, after unit
      tests pass, run `uvicorn backend.main:app --reload` with a real
      `N2YO_API_KEY` and a synthetic catalog containing one known-missing
      NORAD ID (e.g. an object intentionally omitted from the Space-Track
      query via the catalog config), and verify a row appears in SQLite with
      `source='n2yo'`. Do not commit the real key.
    - Why: End-to-end confidence before demo.
    - Dependencies: all prior steps.
    - Risk: Low (manual, optional).

## Test strategy

- **Unit**: Items 11.1–11.14 above cover `fetch_tle_n2yo`, the fallback
  selector, the schema migration, and `poll_once` composition. All HTTP is
  mocked via `httpx.MockTransport` — no network in CI.
- **Integration**: Item 11.13 (`test_poll_once_falls_back_to_n2yo_for_gap_only`)
  is the end-to-end ingest-layer test. Item 12 is an optional manual smoke
  test requiring a real key.
- **Regression**: Existing `fetch_tles` / `cache_tles` tests must continue to
  pass unchanged. `cache_tles` without a `source` argument must still
  produce `space_track`-tagged rows.
- **Coverage goal**: every new branch in `poll_once` (API key present/absent,
  gap list empty/non-empty, N2YO exception path) must have at least one test.

## Risks and mitigations

- **Risk — C-001 violation**: This is the primary blocker. See the Conflict
  section above. **Mitigation**: human updates C-001 and architecture §3.1/§6
  before implementation begins.

- **Risk — N2YO rate-limit mismatch**: Free tier is 1,000 requests/hour
  **globally** across your account, not per cycle. Our cap is 50/cycle, but
  ingest runs every 30 minutes, so worst case 100/hour — well under limit.
  However, other clients using the same key (dev testing, other machines)
  could collide. **Mitigation**: (a) document in the plan that the key should
  be ingest-dedicated, (b) on HTTP 429 from N2YO, `fetch_tle_n2yo` returns
  `None` (normal failure path), (c) a future plan can add an in-process
  token-bucket if needed. Do not implement backoff in this plan.

- **Risk — N2YO TLE accuracy class differs from Space-Track**: F-025 assumes
  TLE uncertainty is Space-Track-class. If N2YO republishes Space-Track data
  unchanged (likely — N2YO is a republisher for many sats), uncertainty is
  identical. If N2YO uses a different source for some objects, Kalman `R` may
  need retuning per source. **Mitigation**: the `source` column lets a
  post-POC plan correlate residuals by source; no `R` changes in this plan.
  Flagged in Open Questions.

- **Risk — N2YO epoch format drift**: N2YO returns TLEs in the standard
  two-line format, so `_parse_tle_epoch_utc` should work unchanged. If N2YO
  ever returns a non-standard epoch field, `_parse_tle_epoch_utc` will raise
  `ValueError`, which `fetch_tle_n2yo` catches and returns `None`.
  **Mitigation**: the `None`-on-any-failure discipline in step 6. Also, the
  `test_fetch_tle_n2yo_happy_path` test uses the exact sample from the task
  brief as a canary.

- **Risk — API key leakage in logs**: N2YO appends the key to the URL as a
  query parameter. If we log raw URLs we leak the key. **Mitigation**: step 6
  requires redaction; step 11.6 tests it.

- **Risk — `\r\n` vs `\n` in TLE body**: N2YO docs say `\r\n`, but HTTP
  middleware sometimes normalizes. **Mitigation**: step 6 accepts either
  separator; step 11.2 tests the `\n`-only case.

- **Risk — Concurrent ingest loops**: None in POC (single background task),
  but N2YO's per-cycle cap must not be shared-state across loops in future
  multi-worker deployments. **Mitigation**: out of scope for POC; note in
  architecture post-POC section (not done in this plan).

- **Risk — Migration rollback**: Adding a column is irreversible in SQLite
  without a table rebuild. **Mitigation**: the default value means
  rolling-back the code (reverting `ingest.py`) still works with the newer
  schema — the old code simply ignores the column. Document this in the
  commit message.

## Open questions

1. **C-001 / architecture alignment** — Human must explicitly approve the
   deviation from "Space-Track.org is the sole data source" before the
   implementer runs. If approved, which exact wording replaces C-001? Suggested:
   *"The POC shall use only publicly available, unclassified data sources.
   Space-Track.org is the primary data source; supplemental public TLE
   republishers (e.g. N2YO) may be used as fallback for catalog entries where
   Space-Track returns no TLE or a stale TLE."* Implementer does **not** edit
   the requirements doc — human does.

2. **Stale threshold value** — 7 days is specified in the task brief. Is that
   the right value for **all** object classes? LEO objects degrade faster than
   GEO. For POC this is acceptable; flag if a domain expert wants per-class
   thresholds.

3. **Per-source measurement noise (F-025)** — Should N2YO-sourced TLEs get a
   different `R` matrix in the Kalman filter? For POC, no — same `R`. But the
   `source` column is now queryable so a future tuning plan can diverge them.
   Confirm this decision with the Kalman owner.

4. **N2YO key dedication** — Is the N2YO key ingest-dedicated, or shared with
   other tooling? If shared, consider adding token-bucket rate-limit tracking.
   Out of scope for this plan; flag if other tooling exists.

5. **Persistence of N2YO skip log** — Should the "N2YO_API_KEY not set" INFO
   log fire every cycle, or only once at startup? Recommendation: once per
   ingest-loop process (use a module-level boolean). Decide before
   implementation.

6. **Catalog order vs sort order for fallback selection** — Step 7 picks
   catalog order for determinism. Confirm this is acceptable; alternative is
   to prioritize by staleness (oldest-first) so the most-stale objects get
   refreshed first when the 50-per-cycle cap bites. Recommendation: **oldest
   first** is better for the demo; the implementer should choose
   oldest-first and note it in the function docstring. Confirm.
