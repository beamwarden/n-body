# Implementation Plan: Technical Documentation Set
Date: 2026-03-29
Status: Draft

## Summary

This plan defines a complete documentation set for the n-body SSA platform, serving three audiences simultaneously: SBIR proposal reviewers (Space Force/NASA), Accenture Federal Services (AFS) acquisition evaluators, and the internal engineering team. Each document is scoped to what currently exists in the codebase, not aspirational features. The plan specifies 10 documents across four priority tiers, with a critical-path subset of 4 documents identified for a proposal deadline sprint.

## Requirements addressed

This plan is a documentation effort, not an implementation plan. It does not directly satisfy functional or non-functional requirements. It supports the following indirectly:

- NF-030 (type annotations and docstrings on public interfaces) — the API spec document will audit compliance
- NF-031 (70% test coverage on propagator/kalman) — the test report document will verify
- NF-032 (architecture document kept current) — the architecture reference will reconcile deviations
- NF-040 (no credentials in source) — the operational runbook will document credential handling
- NF-042 (audit trail) — the CONOPS will describe data provenance logging

## Files affected

This plan produces new files only. No existing source files are modified.

- `docs/reference/system-architecture.md` — new
- `docs/reference/algorithmic-foundation.md` — new
- `docs/specs/api-specification.md` — new
- `docs/specs/websocket-message-schema.md` — new
- `docs/guides/operational-runbook.md` — new
- `docs/guides/deployment-guide.md` — new
- `docs/reference/conops.md` — new
- `docs/reference/prior-art-comparison.md` — new
- `docs/reference/test-coverage-report.md` — new
- `docs/reference/tech-debt-roadmap.md` — new

## Data flow changes

None. This is a documentation-only plan.

---

## Document catalog

### Doc 1: System Architecture Reference
- **Type:** Reference
- **Priority:** P1
- **Audiences:** SBIR reviewers, AFS evaluators, engineering
- **Purpose:** What does this system do, how is it structured, and why was each architectural decision made?
- **Estimated pages:** 12-15
- **Key sections:**
  1. Executive summary (2 paragraphs — what the system does, why it matters)
  2. Architectural concept: continuous observe-predict-validate-recalibrate loop vs. static propagation
  3. Component diagram with data flow (reproduce and expand architecture.md Section 4 diagram)
  4. Component descriptions: ingest, propagator, kalman, anomaly, conjunction, processing, main/API, frontend
  5. State management: filter_states dict, SQLite tables (tle_catalog, state_history, alerts, conjunction_events, conjunction_risks), asyncio.Queue event bus
  6. Coordinate frame and units convention (ECI J2000, km, km/s, UTC — cite F-011, architecture Section 3.2)
  7. Key design decisions with rationale (expand architecture.md Section 7): UKF over EKF, TLE as observation proxy, SGP4 as process model limitation, monorepo, no message queue
  8. Scalability path: POC-to-production component replacement table (from architecture.md Section 6)
  9. Security and compliance posture (ITAR awareness, credential handling, data provenance)
  10. Known limitations and simulation fidelity boundary (ingest.py docstring lines 1-10, kalman.py docstring lines 7-12)
- **Source files the techwriter must read:**
  - `docs/architecture.md` (primary source, must be reconciled with implementation)
  - `docs/requirements.md` (cross-reference requirement IDs)
  - `docs/tech-debt.md` (known deviations)
  - `backend/main.py` (lifespan, app.state, endpoints, background tasks)
  - `backend/processing.py` (shared pipeline, data flow)
  - `backend/ingest.py` (module docstring, table schema)
  - `backend/propagator.py` (module docstring, coordinate frame)
  - `backend/kalman.py` (module docstring, simulation fidelity note)
  - `backend/anomaly.py` (module docstring, classification rules)
  - `backend/conjunction.py` (module docstring, screening approach)
  - `data/catalog/catalog.json` (catalog composition: 100 objects, object classes)
  - `frontend/src/main.js` (WebSocket consumption pattern, module imports)

---

### Doc 2: Algorithmic Foundation and Novel Contribution
- **Type:** Reference / White paper
- **Priority:** P1
- **Audiences:** SBIR reviewers (primary), AFS evaluators (secondary)
- **Purpose:** What is the mathematical basis of the system, and why is this approach a novel contribution relative to the state of the art? This is the "technical merit" document for SBIR reviewers.
- **Estimated pages:** 10-12
- **Key sections:**
  1. Problem statement: Lyapunov instability of orbital propagation and why long-horizon prediction fails
  2. Prior art limitations:
     - Static SGP4 propagation (standard 18th Space Defense Squadron catalog)
     - Commercial SSA platforms (LeoLabs radar-centric, Slingshot visualization-centric, ExoAnalytic optical) — characterize what they solve and what remains unsolved
     - Academic orbit determination (batch least squares, sequential filters) — why these exist but are not accessible as continuous monitoring tools
  3. n-body approach: reframing from prediction accuracy to detection latency
     - Control systems analogy: the orbit as a plant, TLE updates as sensor measurements, UKF as the estimator, anomaly detection as the alarm
  4. UKF formulation:
     - State vector definition: [x, y, z, vx, vy, vz] ECI J2000 km/km/s
     - Process model: SGP4 as deterministic trajectory model (cite TD-007: sigma point collapse, explain why this is acceptable for POC and what the production fix is)
     - Measurement model: identity (full-state observation via TLE-derived state vector)
     - Process noise Q: hand-tuned per object class (debris/active/rocket body), physical meaning of each variance term (cite kalman.py lines 27-56)
     - Measurement noise R: DEFAULT_R calibrated at 900 km^2 position variance / 0.002 (km/s)^2 velocity variance (cite kalman.py lines 58-66, explain the ISS empirical calibration)
     - NIS as filter consistency metric: chi-squared test with 6 DOF at p=0.05 (threshold 12.592)
  5. Anomaly detection and classification:
     - NIS threshold test (F-030)
     - Three-way classification: maneuver (consecutive NIS exceedance on active satellite), drag anomaly (along-track residual dominance), filter divergence (catch-all)
     - Recalibration strategy: state re-initialization with inflated covariance (cite kalman.recalibrate, inflation factors in anomaly.trigger_recalibration)
  6. Conjunction screening:
     - Trajectory-based spherical miss distance (current POC: 5 km first-order, 10 km second-order)
     - Cascade risk model: first-order and second-order conjunction chains
     - Limitations: spherical vs. RSW screening volume (cite TD-027)
  7. Confidence score mapping: heuristic blend of current NIS and NIS history (cite kalman.compute_confidence, TD-008)
  8. Validation evidence:
     - Real-world event: ISS Progress MS-33 reboost detection (2026-03-29 03:11 UTC, NIS=247, residual=383 km)
     - Synthetic maneuver injection (seed_maneuver.py with delta-v 5.0 m/s)
  9. Production path: adaptive Q (SAGE/Holt), numerical integrator replacing SGP4, multi-source sensor fusion
- **Source files the techwriter must read:**
  - `backend/kalman.py` (entire file — Q matrices, R matrix, UKF setup, predict, update, NIS, confidence, recalibrate)
  - `backend/propagator.py` (SGP4 usage, TEME-to-J2000 conversion, tle_epoch_utc)
  - `backend/anomaly.py` (classify_anomaly logic, trigger_recalibration inflation factors)
  - `backend/conjunction.py` (screening algorithm, thresholds, trajectory generation)
  - `docs/tech-debt.md` TD-006 through TD-012 (algorithmic limitations)
  - `docs/architecture.md` Sections 2, 3.2, 3.3, 3.4 (core concept, propagator, kalman, anomaly)
  - `docs/requirements.md` F-020 through F-035 (filter and anomaly requirements)
  - Memory file: `project_state.md` (real-world ISS event)

---

### Doc 3: REST and WebSocket API Specification
- **Type:** Spec
- **Priority:** P1
- **Audiences:** AFS evaluators (primary), engineering
- **Purpose:** What are the exact API contracts? An AFS delivery team needs this to build integrations, write client code, and plan a production API gateway.
- **Estimated pages:** 8-10
- **Key sections:**
  1. Base URL and transport (HTTP/1.1 REST, WebSocket upgrade on /ws/live)
  2. Authentication (currently: none — cite TD-019, production path: OAuth2/JWT)
  3. CORS configuration (localhost:3000 allowed, cite main.py line 636-641)
  4. REST endpoints — for each endpoint:
     - Method + path
     - Query parameters with types and defaults
     - Request body (if any)
     - Response schema (JSON, with field types and descriptions)
     - Error codes
     - Example request/response
  5. Endpoints to document:
     - `GET /config` — returns cesium_ion_token
     - `GET /catalog` — tracked objects with state summaries (F-040)
     - `GET /object/{norad_id}/history` — alert history (F-041, note TD-013: alerts only, not state_history)
     - `GET /object/{norad_id}/anomalies` — anomaly events with resolution data
     - `GET /object/{norad_id}/track` — historical and predictive track points with uncertainty
     - `GET /object/{norad_id}/conjunctions` — conjunction screening results
     - `POST /admin/trigger-process` — manual processing cycle trigger
  6. WebSocket protocol:
     - Connection lifecycle (connect, initial state dump per NF-012, keepalive loop, disconnect)
     - Connection cap (MAX_WS_CONNECTIONS=20, 1013 close code on rejection)
     - Message types: state_update, anomaly, recalibration, conjunction_risk
     - Full JSON schema for each message type (cite architecture Section 3.5 and conjunction.py return schema)
     - Message field descriptions with units and coordinate frame
  7. Coordinate frame and units in all responses: ECI J2000 km/km/s, UTC ISO-8601 with Z suffix
  8. Rate limiting: none implemented (cite production path)
- **Source files the techwriter must read:**
  - `backend/main.py` (all endpoint definitions, WebSocket handler, ConnectionManager, lifespan)
  - `backend/processing.py` (_build_ws_message function for WS message construction)
  - `backend/conjunction.py` (screen_conjunctions return dict schema, lines 186-210)
  - `docs/architecture.md` Section 3.5 (WebSocket message format)
  - `frontend/src/main.js` (fetchCatalog, WebSocket message routing — to verify frontend expectations match backend)

---

### Doc 4: Concept of Operations (CONOPS)
- **Type:** CONOPS
- **Priority:** P1
- **Audiences:** SBIR reviewers (primary), AFS evaluators (primary)
- **Purpose:** How would this system be used operationally by a Space Force SSA operator or a NASA conjunction assessment team? This bridges the gap between technical architecture and mission utility. SBIR reviewers need to see the operational relevance; AFS evaluators need to see a product they can deliver to a customer.
- **Estimated pages:** 6-8
- **Key sections:**
  1. Mission context: continuous space domain awareness for LEO catalog maintenance, maneuver detection, and conjunction warning
  2. System roles and users:
     - SSA operator (monitors catalog, responds to anomaly alerts)
     - Conjunction analyst (assesses collision risk for flagged objects)
     - System administrator (manages catalog configuration, credentials, demo setup)
  3. Operational loop:
     - Steady state: system polls Space-Track every 30 min, updates filter states, browser shows green confidence for all objects
     - Anomaly event: NIS spike detected, object turns amber/red, alert fires, conjunction screening runs automatically
     - Operator response: inspect anomaly type (maneuver/drag/divergence), assess conjunction risk, wait for recalibration
     - Resolution: filter recalibrates within 2-3 observation cycles (~1-1.5 hours), object returns to green
  4. Demo scenario walkthrough (translate CLAUDE.md demo script into operator-facing narrative):
     - Normal tracking (5-10 objects, residual flat)
     - Maneuver injection (ISS analog, delta-v 5.0 m/s)
     - Anomaly fires (<10 seconds per NF-023)
     - Recalibration completes
     - Contrast with static SGP4-only prediction (the "funding moment")
  5. Data provenance and ITAR compliance:
     - Sole data source: Space-Track.org (publicly available, unclassified)
     - All API calls logged with timestamp and response code (F-006)
     - No redistribution of raw TLE data
     - Credential management via environment variables
  6. Production deployment concept:
     - Multi-source sensor fusion (Space Fence, commercial radar, optical)
     - Adaptive process noise, high-fidelity propagator
     - Role-based access, audit logging, CUI handling pathway
     - Distributed architecture (Kafka, TimescaleDB, Kubernetes)
  7. Value proposition vs. current state of the art:
     - Detection latency advantage over static propagation
     - Automated anomaly classification vs. manual analyst review
     - Continuous monitoring vs. periodic catalog updates
- **Source files the techwriter must read:**
  - `CLAUDE.md` (demo script section, critical constraints, glossary)
  - `docs/architecture.md` Sections 2, 5, 6, 8 (core concept, deployment, scalability, security)
  - `docs/requirements.md` Section 2.7 (demo and replay capability), Section 5 (post-POC)
  - `scripts/seed_maneuver.py` (demo injection mechanism)
  - `scripts/replay.py` (offline replay capability)
  - Memory files: `project_state.md` (real-world ISS event for credibility)

---

### Doc 5: Operational Runbook
- **Type:** Guide
- **Priority:** P2
- **Audiences:** AFS evaluators, engineering
- **Purpose:** How do you install, configure, run, and troubleshoot the system? AFS evaluators need to see that a delivery team can pick this up without the original developer.
- **Estimated pages:** 6-8
- **Key sections:**
  1. Prerequisites: Python 3.11+, Space-Track.org account, Cesium Ion token
  2. Environment variable reference: SPACETRACK_USER, SPACETRACK_PASS, CESIUM_ION_TOKEN, NBODY_DB_PATH, NBODY_CATALOG_CONFIG
  3. Installation steps (backend pip install, frontend static serve)
  4. Catalog configuration: data/catalog/catalog.json format, object class values, 20-100 object limit
  5. Startup sequence: backend, frontend, initial TLE pull, demo setup
  6. Demo preparation checklist:
     - Pre-cache 72 hours of TLEs
     - Verify offline operation (disconnect network after cache)
     - Run trigger-process to initialize filter states
     - Inject maneuver and verify anomaly appears
  7. Troubleshooting guide:
     - Space-Track authentication failures (check env vars, account status)
     - Empty globe (check CESIUM_ION_TOKEN, browser console)
     - No objects appearing (check catalog.json, verify TLE cache has data)
     - Anomaly not firing on injection (check delta-v value — must be >= 5.0 m/s with current R=900)
     - WebSocket not connecting (check CORS origins, port 8000 availability)
  8. Log analysis: F-006 audit log format, where to find ingest/kalman/anomaly log entries
  9. Database inspection: SQLite tables (tle_catalog, state_history, alerts, conjunction_events, conjunction_risks), useful queries
- **Source files the techwriter must read:**
  - `CLAUDE.md` (running the system, environment variables, demo script, known issues)
  - `backend/requirements.txt` (dependencies)
  - `backend/main.py` (lifespan startup sequence, environment variable defaults)
  - `backend/ingest.py` (DB path defaults, catalog config loading, poll_once)
  - `data/catalog/catalog.json` (catalog format)
  - `scripts/replay.py` (CLI arguments)
  - `scripts/seed_maneuver.py` (CLI arguments, --trigger flag)
  - `scripts/seed_conjunction.py` (CLI arguments)
  - `.env` file pattern (if present)
  - Memory files: `open_threads.md` (known issues affecting demo, especially items 1-4)

---

### Doc 6: Deployment and Integration Guide
- **Type:** Guide
- **Priority:** P2
- **Audiences:** AFS evaluators, engineering
- **Purpose:** How would an AFS delivery team deploy this beyond a developer laptop? What are the integration points?
- **Estimated pages:** 5-6
- **Key sections:**
  1. Current deployment model: single-machine, two-process (uvicorn backend + static frontend)
  2. External dependencies: Space-Track.org (HTTPS), Cesium Ion (CDN + tile service)
  3. Network requirements: outbound HTTPS to space-track.org and cesium.com; inbound HTTP on ports 8000/3000
  4. Container packaging concept: Dockerfile for backend, nginx for frontend static serve
  5. Production architecture sketch: the POC-to-production table from architecture.md Section 6, with commentary on each transition
  6. Integration points for AFS systems:
     - REST API (GET /catalog, /object endpoints) — standard HTTP, JSON, no auth currently
     - WebSocket /ws/live — streaming integration for ops dashboards
     - SQLite database (portable file, schema documented) — migration path to TimescaleDB/PostgreSQL
     - Catalog configuration (JSON file, swappable for API-driven catalog management)
  7. DoD network considerations:
     - Air-gap demo: pre-cache TLEs, serve CesiumJS from local CDN mirror
     - Space-Track access from DoD networks (may require proxy configuration)
     - ITAR compliance: system uses only unclassified publicly available data
  8. Dependencies and supply chain: list all Python packages with versions, CDN-loaded JS libraries (CesiumJS, D3.js), license compatibility
- **Source files the techwriter must read:**
  - `backend/requirements.txt`
  - `backend/main.py` (CORSMiddleware config, lifespan, environment variables)
  - `docs/architecture.md` Sections 5, 6, 8 (deployment, scalability, security)
  - `frontend/index.html` (CDN imports, CesiumJS version)
  - `frontend/src/globe.js` (CesiumJS Ion token consumption, imagery provider)
  - `CLAUDE.md` (critical constraints, startup sequence)

---

### Doc 7: WebSocket Message Schema Reference
- **Type:** Spec
- **Priority:** P2
- **Audiences:** Engineering, AFS evaluators
- **Purpose:** Precise schema documentation for each WebSocket message type, suitable for generating client code or writing integration tests.
- **Estimated pages:** 4-5
- **Key sections:**
  1. Transport: WebSocket over ws:// (no TLS for POC), JSON text frames
  2. Connection protocol: accept, initial state dump, keepalive, close codes
  3. Message type: state_update — full schema with field types, units, ranges
  4. Message type: anomaly — schema, anomaly_type enum values
  5. Message type: recalibration — schema, inflation_factor semantics
  6. Message type: conjunction_risk — schema, first_order/second_order arrays
  7. Coordinate frame note: all eci_km fields are ECI J2000, all epoch_utc fields are ISO-8601 with Z suffix
  8. Frontend consumption example: code pattern from main.js _handleMessage
- **Source files the techwriter must read:**
  - `backend/processing.py` (_build_ws_message function — the canonical message constructor)
  - `backend/conjunction.py` (screen_conjunctions return schema, lines 186-210)
  - `backend/main.py` (websocket_live handler, initial state dump)
  - `frontend/src/main.js` (_handleMessage function — to verify frontend expectations)
  - `docs/architecture.md` Section 3.5

---

### Doc 8: Prior Art Comparison Matrix
- **Type:** Reference
- **Priority:** P2
- **Audiences:** SBIR reviewers
- **Purpose:** Side-by-side comparison of n-body's approach against existing SSA tools and methods. SBIR reviewers need to see that the applicant understands the competitive landscape and has a defensible differentiation.
- **Estimated pages:** 3-4
- **Key sections:**
  1. Comparison dimensions: detection latency, data sources, prediction method, anomaly detection, conjunction assessment, operator interface, deployment model, data classification level
  2. Comparison entries:
     - 18th SDS / Space-Track catalog (static TLE, batch orbit determination, manual analysis)
     - LeoLabs (proprietary radar network, high-accuracy tracking, API-centric, no continuous Kalman loop)
     - Slingshot Aerospace (visualization platform, commercial satellite imagery + SSA, Beacon product)
     - ExoAnalytic Solutions (optical network, deep-space focus, catalog augmentation)
     - AGI/Ansys STK (simulation and analysis tool, not a continuous monitoring platform)
     - Academic OD software (GMAT, Orekit) — general purpose, not integrated monitoring
  3. n-body differentiation: closed-loop continuous estimation, sub-hour anomaly detection, automated classification, browser-based visualization with real-time filter state
  4. Limitations of comparison: n-body is a POC using TLE as observation proxy; production systems use real sensor data. The comparison is against the *approach*, not the *data quality*.
- **Source files the techwriter must read:**
  - `docs/architecture.md` Section 2 (core architectural concept — the "why")
  - `docs/requirements.md` Section 5 (post-POC: multi-source, high-fidelity propagator)
  - `backend/kalman.py` module docstring (simulation fidelity boundary)
  - No specific source files beyond architecture docs — this document requires domain expertise and external research

---

### Doc 9: Test Coverage and Validation Report
- **Type:** Reference
- **Priority:** P2
- **Audiences:** AFS evaluators, engineering
- **Purpose:** What is tested, how well, and what is the validation evidence? AFS evaluators need confidence that the system can be maintained and extended. SBIR reviewers may look at this to assess engineering maturity.
- **Estimated pages:** 4-5
- **Key sections:**
  1. Test inventory: list all test files with description and count (159 tests total per project state)
  2. Coverage by module:
     - propagator.py (test_propagator.py)
     - kalman.py (test_kalman.py)
     - anomaly.py (test_anomaly.py)
     - ingest.py (test_ingest.py)
     - processing.py (test_processing.py)
     - main.py (test_main.py, test_anomaly_history_endpoint.py, test_track_endpoint.py, test_conjunction_endpoint.py)
     - conjunction.py (test_conjunction.py)
     - scripts (test_replay.py, test_seed_maneuver.py, test_seed_conjunction.py)
  3. Mathematically critical test cases: propagator accuracy bounds, Kalman filter convergence, NIS computation, anomaly classification logic, conjunction distance computation
  4. Integration test coverage: WebSocket lifecycle, REST endpoint responses, admin trigger-process flow
  5. Validation evidence:
     - Real-world ISS reboost detection (autonomous, not scripted)
     - Synthetic maneuver injection and anomaly detection loop
     - Conjunction screening with seed_conjunction.py
  6. Coverage gaps and NF-031 compliance status (70% target on propagator + kalman)
  7. How to run tests: pytest commands from CLAUDE.md
- **Source files the techwriter must read:**
  - All files in `tests/` directory
  - `CLAUDE.md` (validation commands section)
  - `docs/requirements.md` NF-031

---

### Doc 10: Technical Debt Roadmap
- **Type:** Reference
- **Priority:** P3
- **Audiences:** AFS evaluators, engineering
- **Purpose:** Comprehensive roadmap from POC to production, organized by priority and effort. AFS evaluators need this to estimate integration cost and timeline.
- **Estimated pages:** 4-5
- **Key sections:**
  1. Debt items by priority tier (P1/P2/P3), with estimated effort (days)
  2. Production blockers (P1): WebSocket auth (TD-014/TD-019), CesiumJS token handling (TD-018 — partially resolved via /config endpoint)
  3. Scaling blockers (P2): HTTP 429 backoff (TD-001), adaptive Q (TD-006), SGP4 sigma point collapse (TD-007), RSW conjunction screening (TD-027), async SQLite (TD-016), sequential processing (TD-017), unbounded state_history (TD-013)
  4. Polish items (P3): configurable propagation warning (TD-005), NIS history window (TD-010), confidence formula (TD-008), CZML DataSource (TD-024), track step UI (TD-025)
  5. Architecture evolution milestones:
     - Milestone 1: Production security (auth, HTTPS, audit logging)
     - Milestone 2: Sensor fusion (multi-source ingest, observation weighting)
     - Milestone 3: High-fidelity dynamics (numerical integrator, adaptive noise)
     - Milestone 4: Scale (Kafka, TimescaleDB, Kubernetes, 10k+ objects)
  6. Effort estimates and dependency graph
- **Source files the techwriter must read:**
  - `docs/tech-debt.md` (entire file — the canonical source)
  - `docs/architecture.md` Section 6 (scalability path)
  - `docs/requirements.md` Section 5 (post-POC requirements)
  - `CLAUDE.md` (known issues section)

---

## Recommended production order

The order optimizes for the SBIR/AFS deadline. Documents are numbered by their catalog entry above.

### Sprint 1 (P1 — proposal-critical, produce first)

1. **Doc 4: CONOPS** — Write first because it frames the "why" for all other documents. SBIR reviewers read this to understand operational relevance. Fastest to produce because it draws primarily from architecture.md and CLAUDE.md rather than source code.

2. **Doc 2: Algorithmic Foundation** — Write second because it is the technical merit argument. SBIR reviewers will evaluate novelty and rigor here. Requires careful reading of kalman.py and anomaly.py.

3. **Doc 1: System Architecture Reference** — Write third because it provides the structural context that the API spec and runbook depend on. AFS evaluators will use this as their primary assessment document.

4. **Doc 3: API Specification** — Write fourth because AFS evaluators need to assess integrability. This is mechanical to produce (read endpoints, document schemas) but must be precise.

### Sprint 2 (P2 — important, produce after P1 set)

5. **Doc 5: Operational Runbook** — AFS needs to see that handoff is possible.
6. **Doc 7: WebSocket Message Schema** — Companion to the API spec, may be merged into Doc 3 if time is short.
7. **Doc 8: Prior Art Comparison** — Strengthens the SBIR narrative.
8. **Doc 9: Test Coverage Report** — Demonstrates engineering maturity.
9. **Doc 6: Deployment Guide** — AFS planning document.

### Sprint 3 (P3 — nice to have)

10. **Doc 10: Tech Debt Roadmap** — Useful for AFS cost estimation but docs/tech-debt.md already exists in reasonable form.

---

## If you only have time for 3-4 documents

Produce these in order:

1. **Doc 4: CONOPS** (~6 pages, ~4 hours) — The "why this matters" document. SBIR reviewers need it.
2. **Doc 2: Algorithmic Foundation** (~10 pages, ~8 hours) — The "technical merit" document. Deepest technical content.
3. **Doc 1: System Architecture Reference** (~12 pages, ~6 hours) — The "how it works" document. AFS evaluators need it.
4. **Doc 3: API Specification** (~8 pages, ~5 hours) — The "how to integrate" document. AFS evaluators need it.

These four documents cover the critical concerns of both SBIR reviewers (CONOPS + Algorithmic Foundation) and AFS evaluators (Architecture + API Spec). Total estimated effort: ~23 hours of focused writing.

---

## Conflicts and gaps the techwriter will need to surface

### Conflicts

1. **Architecture.md Section 3.4 vs. implemented MANEUVER_CONSECUTIVE_CYCLES:**
   Architecture says ">3 consecutive update cycles." Implementation uses >= 2 (anomaly.py line 37). This is documented in TD-012 but the architecture document has not been updated. The techwriter must use the implemented value (2) and note the discrepancy.

2. **Architecture.md Section 3.1 says "20-50 objects"** but catalog.json contains approximately 100 objects (expanded per CLAUDE.md "20-50 originally, expanded for demo richness"). The techwriter must document 100 as the current catalog size.

3. **CLAUDE.md demo script says `--delta-v 0.5`** but the effective threshold requires `--delta-v 5.0` with the current R=900 measurement noise (per open_threads.md item 2). The techwriter must use the working value (5.0) in any operational documentation.

4. **Architecture.md Section 3.5 WebSocket schema does not include `innovation_eci_km`** but the actual implementation (processing.py _build_ws_message) includes it. The techwriter must document the implemented schema, not the architecture doc version.

5. **Architecture.md Section 3.5 WebSocket schema does not include `conjunction_risk` message type** which was added post-architecture-doc (conjunction.py). The techwriter must document all four message types.

### Gaps

1. **No formal OpenAPI/Swagger spec exists.** FastAPI generates one at /docs but it has not been exported or reviewed. The techwriter should note whether the auto-generated spec matches the documented behavior.

2. **GET /object/{norad_id}/history returns alerts, not state history** (TD-013). This is a significant gap between the requirement (F-041: "time-series of state updates and NIS values") and the implementation (last 100 alert records only). The techwriter must document what the endpoint actually returns and note the discrepancy with F-041.

3. **No formal performance benchmarks exist.** NF-001 (100ms Kalman update) and NF-003 (30 FPS) are not verified by automated tests. The techwriter should note these as unverified claims.

4. **Duplicate anomaly entries in the alerts table** (open_threads.md item 1). This is an unresolved bug that will affect any documentation of the anomaly history endpoints. The techwriter should note it as a known issue.

5. **CesiumJS Ion imagery banner** (open_threads.md item 3). Screenshots for documentation will show the "Upgrade for commercial use" banner unless resolved before documentation screenshots are taken.

6. **No authentication on any endpoint** (TD-019). Every API and WebSocket document must prominently note this as a POC limitation with the production path (OAuth2/JWT).

7. **The `GET /object/{norad_id}/track` endpoint returns ECI coordinates** but the frontend converts to ECEF in JavaScript. The API spec must clearly state the coordinate frame to avoid confusion for integrators.

## Open questions — RESOLVED 2026-03-30

1. **API spec format:** Yes — use OpenAPI 3.x format. Produce both: hand-written prose spec in `docs/specs/api-specification.md` and exported `docs/api/openapi.yaml`.

2. **Name competitors:** Yes — name LeoLabs, Slingshot Aerospace, ExoAnalytic, AGI/Ansys STK, and others by name in the Prior Art Comparison. Standard SBIR practice.

3. **Classified data CONOPS section:** Yes — include a production concept for CUI/ITAR-controlled data handling, but document no actual classified data or methods. Frame as the pathway the system would follow in a production deployment.

4. **Diagrams and screenshots:** Techwriter has latitude to capture screenshots and produce Mermaid/ASCII diagrams. User is the approver loop — techwriter should note any figures requiring review before publication.
