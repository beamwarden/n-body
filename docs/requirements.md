# ne-body: Requirements Document
**Version 0.1 — POC**
**Project:** Continuous Monitoring & Prediction Platform for Space Situational Awareness
**Status:** Draft — for funding review

---

## 1. Document purpose

This document defines the functional and non-functional requirements for the ne-body proof-of-concept. Requirements are scoped to the funding demo milestone. Post-POC requirements are listed separately in Section 5 and are out of scope for initial implementation.

**Requirement IDs** follow the pattern `F-NNN` (functional) and `NF-NNN` (non-functional). Requirements marked `[DEMO]` are specifically required to be demonstrable in a live funding presentation.

---

## 2. Functional requirements

### 2.1 Data ingestion

**F-001** The system shall connect to Space-Track.org using authenticated HTTPS and retrieve TLE data for a configured catalog of objects.

**F-002** The ingest service shall poll Space-Track.org no more than once per 30 minutes to comply with API rate limits.

**F-003** The system shall validate each retrieved TLE for checksum integrity and reject malformed records without crashing.

**F-004** The system shall cache all retrieved TLEs in local persistent storage with fetch timestamp, so the demo can run offline after an initial data pull.

**F-005** The system shall support a catalog configuration file listing the NORAD IDs of objects to track (minimum 20, maximum 100 for POC).

**F-006** The system shall log all Space-Track API calls with timestamp, response code, and object count for audit purposes.

---

### 2.2 Orbital propagation

**F-010** The system shall propagate orbital state for each tracked object using the SGP4 algorithm from the current TLE to an arbitrary future epoch.

**F-011** All propagated state vectors shall be expressed in the ECI J2000 coordinate frame in units of kilometers and km/s.

**F-012** The propagator shall accept a TLE and a target UTC epoch as inputs and return a 6-element state vector `[x, y, z, vx, vy, vz]`.

**F-013** The propagator shall be stateless — it shall hold no memory between calls and be independently testable.

---

### 2.3 State estimation (Kalman filter loop)

**F-020 [DEMO]** The system shall maintain an Unscented Kalman Filter state estimate for each tracked object, updated with each new TLE observation.

**F-021** Each filter update cycle shall consist of: (1) predict step — propagate state from last epoch to current observation epoch; (2) update step — incorporate new TLE-derived observation.

**F-022** The system shall compute the Normalized Innovation Squared (NIS) statistic for each filter update as the primary consistency metric.

**F-023** The system shall maintain a covariance matrix for each object's state estimate, representing current uncertainty.

**F-024** The process noise matrix `Q` shall be configurable per object class (debris, active satellite, rocket body).

**F-025** The measurement noise matrix `R` shall be configurable and shall reflect the known positional uncertainty class of Space-Track TLEs.

---

### 2.4 Anomaly detection

**F-030 [DEMO]** The system shall flag an anomaly when the NIS statistic exceeds the chi-squared critical value (p=0.05, 6 degrees of freedom) for a given object.

**F-031 [DEMO]** The system shall classify detected anomalies into at least the following types: `maneuver`, `drag_anomaly`, `filter_divergence`.

**F-032** Maneuver classification shall require NIS elevation on at least 2 consecutive update cycles for an object in the active satellite catalog.

**F-033 [DEMO]** The system shall trigger filter recalibration when an anomaly is detected, re-initializing the state estimate from the new observation with inflated covariance.

**F-034** The system shall record the time between anomaly detection and the completion of recalibration (return to normal NIS range) for each event.

**F-035** The system shall store all anomaly events with: NORAD ID, detection epoch, anomaly type, NIS value, recalibration duration.

---

### 2.5 API and streaming

**F-040** The backend shall expose a REST endpoint `GET /catalog` returning the list of tracked objects with their current state summary (NORAD ID, name, last update epoch, current confidence score).

**F-041** The backend shall expose a REST endpoint `GET /object/{norad_id}/history` returning the time-series of state updates and NIS values for a single object.

**F-042 [DEMO]** The backend shall expose a WebSocket endpoint `/ws/live` that pushes real-time updates to connected browser clients whenever a state update or anomaly event occurs.

**F-043** WebSocket messages shall conform to the JSON schema defined in the architecture document (Section 3.5), including: message type, NORAD ID, epoch, ECI state, covariance diagonal, NIS, confidence, anomaly type.

**F-044** The WebSocket endpoint shall support at least 5 simultaneous connections without degradation (sufficient for demo room scenario).

---

### 2.6 Browser visualization

**F-050 [DEMO]** The frontend shall render a 3D globe with the positions of all tracked objects updated in real time as WebSocket messages arrive.

**F-051 [DEMO]** Each satellite position marker on the globe shall be color-coded by current confidence score: green (confidence > 0.85), amber (0.60–0.85), red (< 0.60).

**F-052 [DEMO]** The system shall render the uncertainty ellipsoid for each tracked object as a translucent 3D shape scaled to 3σ from the current covariance diagonal.

**F-053 [DEMO]** When an anomaly is detected, the affected object's marker and ellipsoid shall visually highlight (distinct color, increased size) within 2 seconds of the backend detection event.

**F-054 [DEMO]** The frontend shall display a time-series chart of residual magnitude and NIS score for a selected object, with the expected noise band (±2σ) shown as a reference envelope.

**F-055 [DEMO]** The frontend shall display an anomaly alert feed showing recent anomaly events with: object name, time, type, and resolution status.

**F-056** Clicking an object on the 3D globe shall select that object and update the residual chart and any other per-object panels to show that object's data.

---

### 2.7 Demo and replay capability

**F-060 [DEMO]** The system shall include a replay script that can simulate the observe-predict-validate loop using a pre-cached sequence of historical TLEs, without requiring live Space-Track connectivity.

**F-061 [DEMO]** The system shall include a maneuver injection script that artificially introduces a delta-V event into a selected object's state, causing a detectable anomaly to propagate through the filter and visualization.

**F-062** The maneuver injection script shall accept: NORAD ID, delta-V magnitude (m/s), direction (along-track / cross-track / radial), and epoch offset from current time.

**F-063** The replay and injection scripts shall be runnable from a single terminal command to support rapid demo setup.

---

## 3. Non-functional requirements

### 3.1 Performance

**NF-001** The Kalman filter update for a single object shall complete within 100 milliseconds on commodity hardware (2 vCPU, 4 GB RAM).

**NF-002** The WebSocket broadcast from a state update event to the browser receiving the message shall complete within 500 milliseconds end-to-end on a local network.

**NF-003** The 3D globe shall maintain a frame rate of at least 30 FPS on a modern laptop GPU (integrated graphics acceptable) when rendering 50 objects.

**NF-004** The system shall remain stable during a continuous 4-hour demo session without memory leak or frame rate degradation.

---

### 3.2 Reliability

**NF-010** The backend shall recover from a failed Space-Track API call without crashing. It shall log the failure and retry on the next scheduled poll.

**NF-011** The system shall not lose filter state for any object due to a temporary network interruption to Space-Track.org.

**NF-012** If a WebSocket client disconnects and reconnects, it shall receive the current state of all tracked objects within 5 seconds of reconnection.

---

### 3.3 Usability (demo context)

**NF-020** The demo environment shall be launchable from a single shell script with no interactive prompts after initial credential setup.

**NF-021** The frontend shall be viewable in Google Chrome and Firefox without any browser extensions or local server configuration beyond a static file server.

**NF-022** All text in the visualization shall be legible on a 1920×1080 projected display at 3 meters viewing distance.

**NF-023** The anomaly injection (maneuver simulation) shall produce a visible response in the browser visualization within 10 seconds of the script being run.

---

### 3.4 Maintainability

**NF-030** All Python functions with a public interface shall have type annotations and a docstring.

**NF-031** The backend shall achieve at least 70% unit test coverage on `propagator.py` and `kalman.py` (the mathematically critical modules).

**NF-032** The architecture document shall be kept current with any implementation that deviates from the documented design. The implementer agent is responsible for flagging deviations.

---

### 3.5 Security and compliance

**NF-040** No credentials (Space-Track, Cesium Ion) shall appear in any committed source file. All credentials shall be loaded from environment variables.

**NF-041** The system shall not redistribute raw TLE data to external parties or expose it via a public API endpoint.

**NF-042** The system shall log all data source accesses with timestamps for audit trail purposes.

---

## 4. Constraints

**C-001** The POC shall use only publicly available, unclassified data sources. Space-Track.org is the sole data source.

**C-002** The POC shall be operable on a single developer machine without cloud infrastructure dependencies (except for the initial Space-Track data pull).

**C-003** The frontend shall not require a JavaScript build step. All dependencies shall be loaded from CDN.

**C-004** The backend shall be implemented in Python 3.11 or later.

**C-005** Total third-party library dependencies shall be minimized. New dependencies require explicit justification in the planner's implementation plan.

---

## 5. Post-POC requirements (out of scope for v0.1)

These requirements are documented here to guide architecture decisions in the POC but will not be implemented until post-funding.

**POST-001** Multi-source sensor fusion: incorporate Space Fence, commercial radar, and optical observation feeds alongside TLE data.

**POST-002** Adaptive process noise: replace hand-tuned `Q` matrix with an online estimator (SAGE algorithm or similar) that adapts to observed dynamics.

**POST-003** High-fidelity propagator: replace SGP4 with a full numerical integrator including J2–J6 zonal harmonics, atmospheric drag with NRLMSISE-00, and solar radiation pressure.

**POST-004** Conjunction assessment: compute probability of collision (Pc) for all object pairs within configurable threshold, using Monte Carlo sampling of covariance distributions.

**POST-005** Debris cloud evolution: post-breakup event mode that initializes a population of new objects from a fragmentation model and tracks ensemble divergence.

**POST-006** Multi-node backend: replace single-process FastAPI with a distributed architecture (Kafka for event streaming, per-object worker pods, TimescaleDB for state history).

**POST-007** ITAR/CUI handling: classified sensor integration pathway, role-based access control, audit logging to DoD-compliant standard.

**POST-008** Mobile and large-display support: responsive frontend suitable for mission operations center wall display (4K, multi-panel).

**POST-009** Crowdsourced observation ingestion: pipeline for ingesting astrometric observations from amateur astronomer networks with per-source quality weighting.

---

## 6. Acceptance criteria for POC milestone

The POC is considered complete and demo-ready when all of the following are verified:

- [ ] `F-001` through `F-006`: System polls Space-Track and caches TLEs correctly
- [ ] `F-020`, `F-022`, `F-023`: UKF updates running and NIS computed for all catalog objects
- [ ] `F-030`, `F-031`, `F-033` [DEMO]: Anomaly detection fires correctly on maneuver injection
- [ ] `F-050` through `F-055` [DEMO]: Full visualization renders in browser with live updates
- [ ] `F-060`, `F-061` [DEMO]: Replay and injection scripts run without errors
- [ ] `NF-001`: Kalman update < 100ms verified by profiling run
- [ ] `NF-004`: 4-hour stability test passes
- [ ] `NF-020`: Demo launch script verified by a person who did not write it
- [ ] `NF-031`: Test coverage ≥ 70% on propagator and kalman modules
- [ ] `NF-040`: Credential audit — no secrets in git history
