# n-body — Technical Debt Register
Last updated: 2026-03-28

This document tracks all known technical debt, POC simplifications, and deferred
post-POC work. Items are grouped by component and tagged with priority and
the requirement or architecture section they relate to.

## How to use this document
- Before starting any new implementation plan, check this register for relevant items.
- When a debt item is resolved, mark it [RESOLVED] with the date and PR/commit.
- Priority: **P1** = must fix before production; **P2** = fix before scaling; **P3** = nice to have.

---

## Backend: ingest.py

### TD-001: HTTP 429 backoff not implemented
- **Priority:** P2
- **Source:** `CLAUDE.md` Known issues section
- **Relates to:** NF-010 (error recovery), Space-Track rate limit compliance
- **Description:** The `run_ingest_loop` catches `httpx.HTTPStatusError` and retries after
  `POLL_INTERVAL_S` seconds, but does not implement exponential backoff or respect the
  `Retry-After` header on HTTP 429 responses from Space-Track.org. Under heavy polling or
  transient rate-limit events, this may lead to repeated 429s.
- **Resolution path:** Inspect the `httpx.HTTPStatusError` response status in
  `run_ingest_loop`. If status is 429, read the `Retry-After` response header (or default
  to a configurable backoff, e.g. 300s) and sleep accordingly before the next attempt.
  Use exponential backoff with jitter for repeated 429s.
- **Status:** Open

### TD-002: DEVIATION — load_catalog_config return type changed from list[int] to list[dict]
- **Priority:** P3
- **Source:** `backend/ingest.py` line 145 (DEVIATION comment)
- **Relates to:** F-005 (catalog configuration)
- **Description:** The initial scaffold plan stub declared `load_catalog_config() -> list[int]`.
  The resolved open question in the scaffold plan superseded this: the function must return
  `list[dict]` containing at minimum `norad_id`, `name`, and `object_class` per entry. This
  was a deliberate plan correction, not a runtime bug.
- **Resolution path:** No code change needed. The current implementation is correct. This
  entry documents the deviation for audit purposes.
- **Status:** Open (documentation only)

---

## Backend: propagator.py

### TD-003: GCRS used as J2000 equivalent (sub-meter approximation)
- **Priority:** P2
- **Source:** `backend/propagator.py` line 53 (POC comment in `_teme_to_eci_j2000`)
- **Relates to:** F-011 (ECI J2000 output), architecture section 3.2
- **Description:** GCRS (Geocentric Celestial Reference System) and FK5-based J2000 differ
  by up to ~20 milliarcseconds due to the ICRS/FK5 frame-tie rotation. For LEO objects this
  translates to sub-meter position differences — negligible for POC filter accuracy. The
  current implementation uses `astropy`'s TEME→GCRS transform and treats GCRS as equivalent
  to J2000.
- **Resolution path:** Apply the small constant ICRS-to-FK5 frame-tie rotation matrix after
  the GCRS transform. The rotation matrix is a fixed 3×3 matrix available in the IERS
  conventions. Alternatively, use astropy's `FK5` frame directly as the target.
- **Status:** Open

### TD-004: astropy is a large dependency (~150 MB) justified only by frame conversion
- **Priority:** P3
- **Source:** `docs/plans/2026-03-28-propagator.md` Risks section; `backend/propagator.py` module docstring
- **Relates to:** C-005 (minimize dependencies)
- **Description:** `astropy>=6.0` was added solely to perform the TEME→GCRS frame rotation
  and the ECI→geodetic API boundary conversion. The full astropy installation is ~150 MB.
  For POC on a developer machine this is acceptable, but it is a significant dependency for
  a production container image.
- **Resolution path:** Post-POC, extract the specific IAU-76/FK5 precession-nutation rotation
  matrices into a self-contained utility module using numpy only. Remove the astropy
  dependency from `requirements.txt`. The `eci_to_geodetic` function can be replaced with
  a standalone WGS84 iterative geodetic inversion.
- **Status:** Open

### TD-005: Propagation warning threshold of 7 days is not configurable
- **Priority:** P3
- **Source:** `backend/propagator.py` line 39 (`_PROPAGATION_WARN_DAYS = 7.0`)
- **Relates to:** F-010 (propagation accuracy)
- **Description:** The warning emitted when propagating more than 7 days from the TLE epoch
  uses a hard-coded constant `_PROPAGATION_WARN_DAYS`. This cannot be overridden by the
  caller or by environment variable. For demonstration scenarios with stale TLE caches this
  may produce spurious warnings.
- **Resolution path:** Expose `max_propagation_days_warn` as an optional parameter to
  `propagate_tle`, defaulting to `_PROPAGATION_WARN_DAYS`. Alternatively, make the constant
  read from an environment variable `NBODY_PROPAGATION_WARN_DAYS`.
- **Status:** Open

---

## Backend: kalman.py

### TD-006: UKF process noise matrix Q is hand-tuned (POST-002)
- **Priority:** P2
- **Source:** `backend/kalman.py` lines 29–31 (POST-002 comment); `CLAUDE.md` Known issues section; `docs/plans/2026-03-28-kalman.md` Risks section
- **Relates to:** F-024 (Q configurable per object class), architecture section 3.3
- **Description:** The process noise matrices `_Q_DEBRIS`, `_Q_ACTIVE_SATELLITE`, and
  `_Q_ROCKET_BODY` are manually tuned based on physical intuition (unmodeled acceleration
  estimates). They are not derived from empirical filter residuals or an adaptive algorithm.
  The comment `# Hand-tuned for POC; see POST-002 for adaptive noise estimation.` marks this
  explicitly. The values work for the demo but may over- or under-tune for specific objects.
- **Resolution path:** Implement an adaptive process noise estimator such as the SAGE-Holt
  method or an innovation-based covariance estimation algorithm. The algorithm monitors
  recent NIS history and adjusts Q to keep the filter consistent (NIS near the expected
  chi-squared mean = 6 for 6 DOF). The architecture section 6 scalability table lists
  "Adaptive process noise (SAGE/Holt estimator)" as the production replacement.
- **Status:** Open

### TD-007: SGP4 used as UKF process model causes all sigma points to collapse to same state (POST-002)
- **Priority:** P2
- **Source:** `backend/kalman.py` lines 207–222 (POST-002 comment block in `predict`); `docs/plans/2026-03-28-kalman.md` step 4 design note and Risks section
- **Relates to:** F-020 (UKF state estimate), F-021 (predict+update cycle), architecture section 3.3
- **Description:** SGP4 is a deterministic trajectory model parameterised by TLE elements,
  not a force-model ODE. All 13 UKF sigma points map to the same SGP4-propagated state
  because SGP4 does not accept an arbitrary initial state as input. The covariance growth
  in the predict step is therefore dominated entirely by Q rather than by sigma-point
  divergence through dynamics. This is a known POC simplification of the UKF process model.
- **Resolution path:** Replace SGP4 with a numerical integrator (RK4 or RK45) with a full
  force model (J2/J3 geopotential, atmospheric drag via NRLMSISE-00, solar radiation
  pressure). Each sigma point would be integrated independently from its perturbed initial
  condition, restoring proper sigma-point covariance propagation. The architecture section 6
  scalability table identifies this as "High-fidelity numerical integrator (RK4 + full force
  model)" for production.
- **Status:** Open

### TD-008: compute_confidence formula is heuristic, not chi-squared CDF based
- **Priority:** P3
- **Source:** `docs/plans/2026-03-28-kalman.md` step 9 and Risks section
- **Relates to:** F-022 (NIS statistic), frontend F-051 (confidence colour thresholds)
- **Description:** The `compute_confidence` function uses a weighted linear blend of current
  NIS score and recent-history fraction below threshold. This is calibrated by hand against
  the three frontend colour thresholds. A more principled approach would use the chi-squared
  CDF: `confidence = 1 - scipy.stats.chi2.cdf(nis, df=6)`, which maps NIS directly to its
  p-value.
- **Resolution path:** Replace the current formula with `1 - scipy.stats.chi2.cdf(nis, df=6)`
  and verify that the resulting values still align with the frontend green/amber/red
  thresholds (F-051). Adjust threshold boundaries in the frontend if needed.
- **Status:** Open

### TD-009: pinv fallback in compute_nis silently masks near-singular innovation covariance
- **Priority:** P2
- **Source:** `backend/kalman.py` lines 301–306 (DEVIATION note in `compute_nis`)
- **Relates to:** F-022 (NIS computation)
- **Description:** `compute_nis` uses `np.linalg.pinv` as a fallback when the innovation
  covariance S is rank-deficient. A rank-deficient S indicates a bug — S = H*P*H^T + R
  must be positive definite if Q and R are positive definite. The pinv fallback prevents
  a crash but returns a numerically incorrect NIS value without alerting the operator.
- **Resolution path:** Add a structured log warning with the matrix rank and condition
  number when this branch is taken. In production, treat a rank-deficient S as a filter
  health failure and trigger recalibration rather than silently continuing. Consider adding
  a Prometheus/metric counter for this event.
- **Status:** Open

### TD-010: NIS history capped at 20 entries without configurable window
- **Priority:** P3
- **Source:** `backend/kalman.py` lines 275–276 (in `update`); `docs/plans/2026-03-28-kalman.md` step 6g
- **Relates to:** F-022 (NIS statistic)
- **Description:** The NIS history list is capped at the most recent 20 values with a
  hard-coded limit. This affects the `compute_confidence` calculation (which uses the
  full history) and any anomaly classification that examines history length. The window
  cannot be adjusted without a code change.
- **Resolution path:** Make the NIS history window configurable via a constant
  `NIS_HISTORY_MAX_LEN` at module level, or as a parameter to `init_filter`. 20 updates
  at 30-minute intervals covers 10 hours of history, which is reasonable for POC.
- **Status:** Open

---

## Backend: anomaly.py

### TD-011: Drag anomaly classification uses velocity residual as along-track proxy (ECI simplification)
- **Priority:** P2
- **Source:** `backend/anomaly.py` lines 154 and 188–192 (TECH DEBT comments in `classify_anomaly`)
- **Relates to:** F-031 (three-way anomaly classification), F-032 (drag anomaly)
- **Description:** The drag anomaly heuristic uses the velocity residual direction
  (`innovation_eci_km[3:6]`) as a proxy for the along-track direction. This is an ECI
  simplification — a proper along-track/cross-track decomposition requires the object's
  actual velocity vector (from the filter state) to define the RSW (radial-along-cross)
  frame. The residual velocity direction and the actual orbital velocity direction can
  differ significantly, leading to potential misclassification.
- **Resolution path:** Replace the drag anomaly heuristic with a proper RSW frame
  decomposition. Obtain the object's ECI velocity vector from the filter state
  (`filter_state["state_eci_km"][3:6]`). Construct the along-track unit vector as
  `v_hat = velocity / |velocity|`. Decompose `pos_residual_km` into along-track and
  cross-track components using `v_hat` as the along-track axis.
- **Status:** Open

### TD-012: MANEUVER_CONSECUTIVE_CYCLES threshold of 2 differs from architecture.md section 3.4
- **Priority:** P3
- **Source:** `backend/anomaly.py` lines 33–36 (resolution comment); `docs/plans/2026-03-28-anomaly.md` Open questions item 1
- **Relates to:** F-032 (maneuver classification), architecture section 3.4
- **Description:** Architecture section 3.4 states maneuver detection requires ">3 consecutive
  update cycles above threshold." The anomaly plan resolved this to >=2 consecutive cycles
  (the more conservative threshold) and implemented `MANEUVER_CONSECUTIVE_CYCLES = 2`.
  The architecture document has not been updated to reflect this decision.
- **Resolution path:** Update `docs/architecture.md` section 3.4 to read "at least 2
  consecutive update cycles" to match the implemented constant and the F-032 requirement.
- **Status:** Open

---

## Backend: main.py

### TD-013: state_history table growth is unbounded (no retention policy)
- **Priority:** P2
- **Source:** `docs/plans/2026-03-28-main.md` Open questions item 1
- **Relates to:** F-041 (history endpoint), NF-004 (4-hour demo stability)
- **Description:** The `state_history` table accumulates one row per tracked object per
  polling cycle (~50 rows every 30 minutes = 2,400 rows/day). For the 4-hour demo this is
  trivial (~400 rows). For extended operation the table will grow without bound, eventually
  degrading SQLite query performance. No retention limit or pruning is implemented.
- **Resolution path:** Add a background cleanup task that deletes `state_history` rows older
  than a configurable retention window (e.g., `NBODY_HISTORY_RETENTION_HOURS=72`). Run the
  cleanup once per poll cycle. In production, migrate to TimescaleDB (architecture section 6)
  which handles time-series retention natively.
- **Resolution path (demo short-term):** For `GET /object/{norad_id}/history`, the plan
  resolves this to return the last 100 records from the `alerts` table only for demo (per
  approved resolution in this document's companion plan). See TD-013 note below.
- **Status:** Open

### TD-014: No WebSocket authentication or session management
- **Priority:** P1
- **Source:** `CLAUDE.md` Known issues section; `docs/plans/2026-03-28-main.md`
- **Relates to:** F-044 (WebSocket connections), architecture section 8 (security)
- **Description:** The `/ws/live` WebSocket endpoint accepts all connections without any
  authentication or authorization check. A `MAX_WS_CONNECTIONS` cap of 20 (per the approved
  plan resolution) prevents resource exhaustion, but any client on the network can connect
  and receive orbital state data. CLAUDE.md notes this is "acceptable for local demo, not
  for production."
- **Resolution path:** Add OAuth2 token validation on WebSocket upgrade request. Validate
  the `Authorization: Bearer <token>` header before calling `websocket.accept()`. In
  production, integrate with the OAuth2/RBAC system described in architecture section 6.
- **Status:** Open

### TD-015: asyncio.Queue event bus has no backpressure handling beyond maxsize block
- **Priority:** P2
- **Source:** `docs/plans/2026-03-28-main.md` Open questions item 3
- **Relates to:** NF-010 (resilience), architecture section 7 (no message queue for POC)
- **Description:** The event bus between `ingest.py` and the processing loop uses
  `asyncio.Queue(maxsize=10)`. When the queue is full, `poll_once`'s `event_bus.put()`
  call will block until space is available, throttling the ingest loop. This is acceptable
  for POC but provides no visibility into queue depth, no dead-letter handling, and no
  ability to drop stale events. The architecture section 7 explicitly notes the in-process
  queue is a named post-POC replacement target.
- **Resolution path:** Post-POC, replace the in-process `asyncio.Queue` with an external
  message broker (Kafka or NATS as mentioned in architecture section 7). For intermediate
  improvement, add a queue depth metric log and implement `asyncio.Queue.put_nowait()` with
  explicit drop-and-log semantics for stale events instead of blocking.
  Add `# POST-POC: parallelize UKF updates with ThreadPoolExecutor for large catalogs`
  comment in the processing loop.
- **Status:** Open

### TD-016: SQLite concurrent access not fully async-safe
- **Priority:** P2
- **Source:** `docs/plans/2026-03-28-main.md` Risks section
- **Relates to:** NF-010 (resilience under load)
- **Description:** The `sqlite3` stdlib module is not async-safe. All DB writes are
  serialized through `_processing_loop_task` (safe), but REST endpoint reads run in the
  same process without `asyncio.to_thread()` wrapping. Under high read load or contention,
  a slow SQLite query in a REST handler can block the event loop. WAL mode mitigates
  reader/writer conflicts but does not solve the event-loop blocking issue.
- **Resolution path:** Wrap all SQLite calls in `asyncio.to_thread()` to offload blocking
  I/O to a thread pool. In production, migrate to an async-capable database driver
  (e.g., `aiosqlite` for SQLite, or TimescaleDB with `asyncpg`).
- **Status:** Open

### TD-017: _processing_loop_task processes all catalog objects sequentially
- **Priority:** P2
- **Source:** `docs/plans/2026-03-28-main.md` Risks section
- **Relates to:** NF-001 (100ms per update), NF-002 (500ms broadcast latency)
- **Description:** For a catalog of 50 objects, if each Kalman update takes up to 100ms
  (NF-001 ceiling), the total per-cycle processing time is up to 5 seconds. Broadcast
  happens per-object within the loop, so individual object latency is within NF-002, but
  the last object in the loop receives its update 5 seconds after the first. This is
  acceptable for the 30-minute polling interval demo but becomes a bottleneck if polling
  frequency increases or catalog size grows.
- **Resolution path:** Parallelize UKF updates using `asyncio.gather()` with per-object
  coroutines, or use `concurrent.futures.ThreadPoolExecutor` for CPU-bound filter updates.
  Add `# POST-POC: parallelize UKF updates with ThreadPoolExecutor for large catalogs`
  comment at the sequential loop.
- **Status:** Open

---

## Frontend

### TD-018: CesiumJS Ion token hardcoded in globe.js
- **Priority:** P1
- **Source:** `CLAUDE.md` Known issues section
- **Relates to:** Architecture section 8 (security and compliance)
- **Description:** The CesiumJS Ion token is hardcoded directly in `frontend/src/globe.js`.
  This means the token is committed to the repository and visible to anyone with repository
  access. The token must be rotated before any public deployment or public repository
  exposure. CLAUDE.md explicitly notes "must be moved to env var before any public
  deployment."
- **Resolution path:** Remove the hardcoded token from `globe.js`. Read it from a
  `window.CESIUM_ION_TOKEN` global that is injected at page load by a server-rendered
  template or a `/config` API endpoint that reads `CESIUM_ION_TOKEN` from the server
  environment. The backend `main.py` can expose a `GET /config` endpoint that returns
  non-secret frontend configuration.
- **Status:** Open

---

## Cross-cutting

### TD-019: No authentication on any API endpoint (REST or WebSocket)
- **Priority:** P1
- **Source:** `CLAUDE.md` Known issues section; architecture section 8
- **Relates to:** Architecture section 8 (security), F-044
- **Description:** Neither the REST endpoints (`GET /catalog`, `GET /object/{norad_id}/history`)
  nor the WebSocket endpoint (`/ws/live`) have any authentication or authorization. Any
  client with network access to port 8000 can read orbital state data and anomaly alerts.
  CLAUDE.md states this is "acceptable for local demo, not for production."
- **Resolution path:** Add OAuth2/JWT bearer token validation to all FastAPI endpoints
  using FastAPI's `Depends()` security dependency. For WebSocket, validate the token on
  the upgrade request before `websocket.accept()`. Implement at minimum three roles:
  viewer (read-only), analyst (read + alert acknowledgement), operator (full access).
  See architecture section 6 scalability table.
- **Status:** Open

### TD-020: No message queue between ingest and processing (in-process asyncio.Queue only)
- **Priority:** P2
- **Source:** `docs/architecture.md` section 7; `docs/plans/2026-03-28-main.md`
- **Relates to:** Architecture section 7 (no message queue for POC)
- **Description:** The event bus between the ingest loop and the Kalman processing loop is
  an in-process `asyncio.Queue`. This means ingest and processing run in the same OS
  process. A crash in the processing loop loses all queued events. There is no replay
  capability, no dead-letter queue, and no ability to scale ingest and processing
  independently. Architecture section 7 explicitly calls this out as a named post-POC
  replacement.
- **Resolution path:** Replace the in-process queue with Kafka or NATS (as named in
  architecture section 7). The `poll_once` function in `ingest.py` would publish to a
  Kafka topic instead of calling `event_bus.put()`. The processing loop would consume from
  the same topic, enabling independent scaling and replay.
- **Status:** Open

### TD-021: SQLite used for all persistence; not suitable for production scale
- **Priority:** P2
- **Source:** `docs/architecture.md` section 6 (scalability table); `CLAUDE.md`
- **Relates to:** Architecture section 6, F-041 (history endpoint performance)
- **Description:** SQLite is used for the TLE catalog cache, state history, and alerts table.
  It is single-writer, file-based, and does not support horizontal scaling or efficient
  time-series queries at production catalog sizes (10k+ objects). The architecture section 6
  scalability table identifies TimescaleDB or InfluxDB as the production replacement.
- **Resolution path:** Migrate to TimescaleDB for time-series state history (automatic
  chunking and retention) and PostgreSQL for catalog and alerts. Use `asyncpg` for
  async-compatible database access. The existing SQLite schema is migration-ready — all
  table columns map directly to a relational schema.
- **Status:** Open

### TD-022: No structured logging or metrics export
- **Priority:** P2
- **Source:** General codebase observation; `docs/plans/2026-03-28-main.md` Risks section
- **Relates to:** NF-010 (resilience), production operational requirements
- **Description:** All logging uses Python's stdlib `logging` module with unstructured text
  messages. There is no metrics export (Prometheus, OpenTelemetry), no distributed tracing,
  and no alerting on backend health. For the demo this is sufficient, but production
  operations require structured logs (JSON) for ingestion into log aggregators and metrics
  for SLA monitoring.
- **Resolution path:** Add `structlog` for structured JSON logging. Add `prometheus-client`
  for metrics export on a `/metrics` endpoint: track TLE fetch counts, Kalman update
  latency histogram, NIS distribution, anomaly event rate, WebSocket connection count,
  and queue depth. Add OpenTelemetry tracing spans around the ingest→process→broadcast
  pipeline.
- **Status:** Open

### TD-024: Replace CesiumJS Entity API with CZML DataSource
- **Priority:** P3
- **Source:** `frontend/src/globe.js` (POC implementation)
- **Relates to:** architecture.md section 3.6.1
- **Description:** POC uses CesiumJS Entity API for simplicity. Architecture.md specifies CZML DataSource for interpolation between updates. At large catalog scale (1000+ objects), Entity API becomes a performance bottleneck.
- **Resolution path:** Rewrite globe.js to maintain a CesiumJS CzmlDataSource, patching it with CZML packets per WebSocket message. Enables time-interpolated playback and is the correct architecture for the production renderer.
- **Status:** Open

---

### TD-027: Replace spherical miss-distance with RSW pizza-box screening
- **Priority:** P2
- **Source:** `backend/conjunction.py` (POC implementation); plan docs/plans/2026-03-29-conjunction-risk.md
- **Relates to:** F-030 (anomaly triggers conjunction screening), POST-004 (full Pc-based conjunction assessment)
- **Description:** The POC uses a simple Euclidean distance threshold (5 km / 10 km spherical)
  for conjunction screening. The standard DoD/NASA approach uses an asymmetric screening volume
  in the RSW (Radial-Along-Cross) frame: typically 1 km radial x 25 km along-track x 25 km
  cross-track. This accounts for the elongated uncertainty distribution along the orbit track
  and produces far fewer false positives for objects in similar orbital planes.
- **Resolution path:** Compute the RSW frame from the relative velocity vector at TCA.
  Transform the miss vector into RSW components. Apply asymmetric thresholds (r_km <= 1.0,
  s_km <= 25.0, w_km <= 25.0) instead of the spherical 5 km threshold. See Vallado
  (Fundamentals of Astrodynamics) Chapter 9 for the RSW frame definition.
- **Status:** Open

### TD-028: Extend conjunction screening to debris cloud scenarios
- **Priority:** P3
- **Source:** plan docs/plans/2026-03-29-conjunction-risk.md; architecture POST-005
- **Relates to:** F-030 (conjunction screening), POST-005 (debris cloud evolution)
- **Description:** Current screening considers only existing catalog objects with initialized
  filters. A breakup event generates a debris cloud that is not yet in the catalog. The
  screening is blind to uncatalogued fragments for the first hours after a fragmentation
  event — precisely when conjunction risk is highest.
- **Resolution path:** Integrate with the fragmentation model from POST-005. When a breakup
  event is detected, generate an ensemble of modeled debris particles using a statistical
  fragmentation model (NASA SBM or similar). Screen the ensemble against all catalog objects.
  Probability of collision (Pc) should replace the deterministic miss-distance threshold for
  this application.
- **Status:** Open

---

### TD-025: Track endpoint step_s is not user-configurable via UI (post-POC)
- **Priority:** P3
- **Source:** `docs/plans/2026-03-29-history-tracks-cones.md` Decision 1
- **Relates to:** F-041 (history endpoint), F-050 (3D globe visualization)
- **Description:** The `GET /object/{norad_id}/track` endpoint accepts `step_s` as a query
  parameter, but the frontend always requests with the hard-coded default (60 s). Operators
  cannot adjust the track density without modifying source code. For high-inclination or
  rapidly maneuvering objects, finer steps (e.g., 30 s) would improve track accuracy; for
  routine monitoring, coarser steps (e.g., 120 s) would reduce latency.
- **Resolution path:** Add a small UI control (slider or dropdown: 30 / 60 / 120 s) in the
  object info panel or a settings overlay. Pass the selected value as the `step_s` query
  parameter when calling `_fetchAndDrawTrack()`. Store the preference in `localStorage`.
- **Status:** Open

---

### TD-023: scripts/replay.py and scripts/seed_maneuver.py are not implemented
- **Priority:** P1
- **Source:** `scripts/replay.py` line 21; `scripts/seed_maneuver.py` line 29
- **Relates to:** Demo script step 2 and 3 (CLAUDE.md demo script), F-033
- **Description:** Both `replay_tles()` in `replay.py` and `inject_maneuver()` in
  `seed_maneuver.py` raise `NotImplementedError`. These scripts are required for the
  "funding moment" demo (CLAUDE.md demo script steps 2–5). Without them, the maneuver
  injection and historical replay demonstration cannot be executed.
- **Resolution path:** Implement `replay_tles()` to load cached TLEs from SQLite within
  the specified time window and POST them to the backend API (or directly insert into
  the event bus). Implement `inject_maneuver()` to construct a synthetic TLE with an
  applied delta-V offset in the specified direction (along-track/cross-track/radial) and
  insert it into the TLE cache with a future epoch.
- **Status:** [RESOLVED] 2026-03-28 — scripts implemented per plan docs/plans/2026-03-28-scripts.md.
  `backend/processing.py` extracted shared pipeline. `scripts/replay.py` and
  `scripts/seed_maneuver.py` fully implemented with tests in `tests/test_processing.py`,
  `tests/test_replay.py`, `tests/test_seed_maneuver.py`. Admin endpoint
  `POST /admin/trigger-process` added to `main.py` for NF-023 compliance.
