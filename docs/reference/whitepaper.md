# ne-body (Near Earth Body): Closed-Loop Continuous Space Situational Awareness

*Technical Whitepaper — DARPA BAA Register*

Version: 0.1.1  |  Status: Draft  |  Last Updated: 2026-04-17

Distribution Statement: Unclassified — Public Release

Technology Readiness Level: TRL 3–4 (Analytical and Experimental Proof of Concept)

---

# Overview

This document presents a technical capability description of the ne-body (Near Earth Body) Space Situational Awareness (SSA) proof-of-concept (POC) implementation. It is written for subject matter experts in orbital mechanics, recursive state estimation, and SSA operations. The intended audience includes program managers, principal investigators, and technical review panels evaluating the feasibility of closed-loop, continuous orbit determination and anomaly detection architectures.

The POC implements a closed-loop recursive estimation pipeline for a curated Very Low Earth Orbit (VLEO) catalog of 75 objects, using publicly available Two-Line Element sets (TLEs) as synthetic observations. The system operates at Technology Readiness Level (TRL) 3–4: analytical proof of concept has been established through implementation and limited testing, but no formal verification and validation (V&V) campaign has been conducted, and no operational deployment has been attempted.

> **Scope Boundaries**
>
> The following scope constraints apply to all claims in this document:
>
> - Catalog size: 75 curated VLEO objects. Scaling to the full operational catalog (30,000+ trackable objects) is a non-trivial engineering transition, not a configuration change.
> - Observation source: Publicly available TLEs from Space-Track.org and N2YO.com. TLEs are orbit determination products with correlated batch-fit errors, not independent sensor measurements. The Kalman filter's Markov assumption is structurally violated by this input type.
> - Update cadence: 30-minute polling interval imposed by Space-Track rate limits. Detection latency is bounded below by this external constraint.
> - Processing architecture: Single-process Python backend. This is a POC simplification and does not represent the proposed production topology.
> - Validation: No ground-truth maneuver data has been used for false-positive/false-negative rate analysis. No formal V&V campaign has been conducted.

All quantitative claims in this document are drawn directly from the implemented code, configuration files, and engineering log. Where the POC makes simplifications relative to a production deployment, those simplifications are identified explicitly with their associated technical debt tracking numbers (TD-xxx) or post-POC activity identifiers (POST-xxx). Every claim is bounded to the specific test conditions under which it was observed.

## Context

The authoritative architecture reference is `docs/reference/system-architecture.md` (version 0.2.0). The mathematical foundation is documented in `docs/reference/algorithmic-foundation.md`. The operational concept is in `docs/reference/conops.md`. This whitepaper consolidates the technically load-bearing elements of those documents for a subject matter expert reader who does not require the full architecture pack.

---

# 1. Thesis: Closed-Loop Recursive Estimation Offers Bounded Detection Latency Advantages over Static Propagation under Stated Assumptions

Standard SSA workflows propagate a TLE forward using the Simplified General Perturbations 4 (SGP4) algorithm and treat the result as the object's predicted state until a new TLE arrives. This procedure is mathematically limited by the Lyapunov instability of orbital dynamics. Low Earth orbit (LEO) constitutes a nonlinear dynamical system with a positive Lyapunov exponent: small perturbations in initial conditions — atmospheric drag variability, unmodeled solar radiation pressure, unannounced delta-V events — grow exponentially in time. A TLE accurate to 100 m at epoch can degrade to kilometer-level position error within hours for an object in an active drag regime or following an unannounced maneuver.

The standard response has been to publish updated TLEs more frequently and to improve propagator fidelity. Both approaches exhibit diminishing returns. More frequent TLEs reduce the integration horizon but do not eliminate the instability. Higher-fidelity propagators reduce systematic modeling error but cannot model events that are, by definition, absent from the model.

The proposed system reframes the problem. Instead of optimizing prediction accuracy over extended horizons, the effort asks: how quickly can the system detect that its current prediction is inconsistent with new observations, and initiate corrective action? This is a control-systems reframing: the orbit is the plant, TLE updates serve as noisy sensor measurements, an Unscented Kalman Filter (UKF) is the estimator, and anomaly detection is the alarm. The figure of merit becomes detection latency rather than prediction accuracy.

Under the conditions tested (75 VLEO objects, 30-minute TLE update cadence from Space-Track, ISS-calibrated R matrix), detection latency is bounded above by one observation interval (30 minutes at Space-Track rate limits) plus filter processing time (measured at approximately 100 ms per object on a single-threaded Python 3.11 process; hardware specification: testing was conducted on commodity hardware and the specific CPU/memory configuration should be documented for reproducibility). Static propagation, by contrast, provides no intrinsic detection latency bound — it fails silently until an analyst manually compares ephemerides or a new TLE is published.

> **Caveat: Detection Latency Is Externally Bounded**
>
> The 30-minute detection latency is determined by the Space-Track polling interval — an external constraint imposed by the data source, not a system property. With higher-cadence observation sources (e.g., dedicated sensor feeds at 1–10 minute intervals), the architectural topology is expected to accommodate proportionally lower detection latency, but this claim requires validation at scale with production sensor data.

The system's architectural contribution is not the UKF or the Normalized Innovation Squared (NIS) test, both of which are well established in the orbit determination literature. The contribution is the operational integration: a per-object UKF maintained continuously across a heterogeneous catalog, tested on every new TLE, classified automatically into three anomaly types, coupled to an automatic conjunction screening cascade, and surfaced to an operator through a telemetry interface — all within a single processing cycle. This integration has been demonstrated at POC scale; the claim that this integration extends to production scale without fundamental redesign is an architectural assertion that requires validation through scaled testing.

---

# 2. System Architecture

The processing pipeline is structured as follows:

```
Space-Track.org  +  N2YO.com  →  ingest.py  →  tle_catalog (SQLite)
                                    │
                                    ▼
                               processing.py
                                    │
                     ┌──────────────┼──────────────┐
                     ▼              ▼              ▼
                propagator.py   kalman.py    anomaly.py
                 (SGP4 +         (UKF         (classifier +
                 TEME→GCRS)      predict/     recalibration)
                                 update)
                                    │
                                    ▼
                                 main.py
                       (FastAPI REST + WebSocket)
                                    │
                                    ▼
                             Browser dashboard
                  (CesiumJS globe, D3 timeline, alerts)
```

**Backend.** Python 3.11+, FastAPI, sgp4 (Vallado implementation), filterpy (UKF), astropy (TEME→GCRS frame rotation), SQLite (WAL mode). The POC uses a single-process architecture with two asyncio background tasks: the ingest loop (30-minute Space-Track poll interval plus N2YO fallback) and the processing loop (consumes `catalog_update` events and runs the predict-update-anomaly-recalibrate cycle per object). This single-process architecture is a POC simplification and does not represent the proposed production topology, which would require a parallel-processing pipeline to accommodate full-catalog throughput.

**Frontend.** Vanilla ES2022 modules, no build step. CesiumJS 1.114 for the 3D globe, D3.js v7 for the residual / NIS timeline. Web Audio API for the audio alarm. All CDN-loaded.

**Storage.** Five SQLite tables: `tle_catalog` (TLE cache with `source` provenance column), `state_history` (per-cycle filter state and NIS), `alerts` (anomaly records), `conjunction_events`, `conjunction_risks`.

**Frame discipline.** SGP4 produces True Equator Mean Equinox (TEME) output, which is rotated to the Geocentric Celestial Reference System (GCRS) via astropy before entering the filter. GCRS is used as the Earth-Centered Inertial (ECI) J2000 equivalent; the frame-tie difference (~20 milliarcseconds) translates to sub-meter position error for LEO — below filter accuracy in the POC. All filter state, all database storage, and all WebSocket messages are in ECI J2000 km and km/s. Earth-Centered Earth-Fixed (ECEF) conversion occurs only in the frontend for rendering.

## 2.1 IDEF0 System Boundary

The two figures below use IDEF0 notation: inputs enter from the left, controls from the top, mechanisms from the bottom, and outputs exit to the right.

**Figure 1 — A-0 Context: Full Pipeline as a Single Activity**

```
  Controls
  C1 ITAR compliance rules
  C2 Space-Track rate limit (1 poll / 30 min)
  C3 UKF noise matrices Q, R
  C4 NIS chi-squared threshold (χ²₆, p=0.05)
  C5 Anomaly debounce (2 consecutive cycles)
                           │
   ┌───────────────────────▼──────────────────────────────────────────────┐
   │                                                                      │
Space-Track TLEs ─────────▶│   ne-body: Continuous Orbital Monitoring     ├──▶ Operator alerts
N2YO supplemental TLEs ───▶│   (observe → propagate → validate →          ├──▶ Conjunction risk messages
Catalog NORAD IDs ─────────▶│    recalibrate)                              ├──▶ Filter state history (SQLite)
   │                                                                      │
   └───────────────────────┬──────────────────────────────────────────────┘
                           │
  Mechanisms
  M1 httpx (async HTTP) + SQLite (WAL mode)
  M2 sgp4 (SGP4/SDP4)  + astropy (TEME→GCRS frame rotation)
  M3 filterpy (UKF)    + scipy (chi-squared test)
  M4 FastAPI + WebSocket server + Web Audio API
```

**Figure 2 — A0 Decomposition: Ingest → Process → Alert**

```
         C1, C2                      C3, C4                       C5
            │                           │                           │
  ┌─────────▼──────────┐    ┌───────────▼──────────┐    ┌──────────▼─────────┐
  │                    │    │                      │    │                    │──▶ audio alarm
Space-Track ──▶         │    │                      │    │                    │──▶ visual flash
N2YO        ──▶  A1     ├───▶   A2                 ├───▶   A3                │──▶ conjunction
Catalog     ──▶  INGEST │ TLE│   PROCESS            │anom│   ALERT            │    risk msgs
             │  Fetch,  │cache│   UKF predict-update │flag│   Broadcast anomaly│
             │  validate│    │   NIS consistency    │+   │   Screen conjunc-  │
             │  epoch-  │    │   Classify & recal.  │state   tions           │
             │  check,  │    │                      │    │   Cue operator     │
             │  cache   │    │                      │    │                    │
  └──────────┬──────────┘    └──────────┬───────────┘    └──────────┬─────────┘
             │                          │                           │
      M1 httpx                   M2 sgp4 + astropy           M4 FastAPI
      SQLite WAL                 M3 filterpy + scipy          WebSocket
                                                              Web Audio API
```

---

# 3. Kalman Filter Formulation

## 3.1 State Vector

```
x = [x_km, y_km, z_km, vx_km_s, vy_km_s, vz_km_s]
```

Six-dimensional position and velocity in ECI J2000. Units: km and km/s.

## 3.2 Process and Measurement Models

**Process model `fx`.** SGP4 applied to the previous TLE (stored in the filter state as `last_tle_line1`/`last_tle_line2`), propagated from the prior observation epoch to the current observation epoch. Using the previous TLE, not the new TLE, is essential: using the new TLE as the process model would make the predicted state identical to the observation, yielding zero innovation.

**Measurement model `hx`.** Identity. The observation is the full six-element ECI state vector derived from the new TLE via SGP4 + TEME→GCRS rotation. This is a deliberate simplification for the POC.

> **Limitation: Identity Measurement Function**
>
> The identity `hx` conflates mean and osculating elements. SGP4 operates on mean Keplerian elements (Brouwer theory), whereas the physical state is osculating. The short-period oscillations introduced by J₂ (and higher zonal harmonics) are on the order of several kilometers in position for LEO objects. The inflated R matrix (Section 3.4) partially absorbs this error, but this conflation means the filter cannot distinguish mean-element TLE publication artifacts from physical state changes at amplitudes below the R matrix floor. Remediation requires an explicit mean-to-osculating element transformation in the measurement model (see Section 9.6, Layer 2).

**Sigma points.** Merwe Scaled Sigma Points via FilterPy's `MerweScaledSigmaPoints`, with n=6, α=1e-3, β=2.0, κ=0.0, producing 2n+1 = 13 sigma points per cycle.

> **Limitation: Sigma-Point Collapse under SGP4**
>
> The SGP4 propagator is an analytical theory, not a numerical integrator. For the sigma-point perturbation magnitudes produced by the UKF (governed by α=1e-3 and the state covariance), SGP4 responses are effectively linear — the sigma points do not explore nonlinear regime behavior. This means the UKF degenerates to a linear estimator with additive noise characteristics similar to an Extended Kalman Filter (EKF). This is a known limitation that bounds the POC's ability to demonstrate UKF-specific advantages (nonlinear covariance propagation) over a computationally less expensive EKF. The UKF formulation is retained because it provides the correct architectural framework for production use with a numerical integrator, where nonlinear effects become material.

## 3.3 Process Noise Q (Per Object Class)

| Object Class | Q Position Diagonal (km²) | Q Velocity Diagonal ((km/s)²) |
|---|---|---|
| `active_satellite` | 0.25 | 25e-4 |
| `debris` | 1.00 | 1e-4 |
| `rocket_body` | 0.5625 | 4e-4 |

The active-satellite matrix has elevated velocity variance specifically to accommodate maneuver probability: an unmodeled delta-V primarily appears as a velocity residual before propagating into a position residual. Debris has higher position variance to accommodate drag uncertainty with no maneuver capability modeled.

Q matrices are hand-tuned based on engineering judgment during POC development (POST-002 open). No adaptive estimation has been implemented. The sensitivity of anomaly detection performance (false-positive rate, detection latency) to Q perturbation has not been characterized through formal Monte Carlo analysis. Small changes in Q values can shift the operating point on the receiver operating characteristic curve in ways that are not currently quantified.

## 3.4 Measurement Noise R

```
R = diag([900.0, 900.0, 900.0, 2e-3, 2e-3, 2e-3])   # km² and (km/s)²
```

Position variance 900 km² corresponds to a 1-sigma of 30 km. Velocity variance 2e-3 corresponds to approximately 0.045 km/s 1-sigma. These values were empirically calibrated against observed International Space Station (ISS) TLE-to-TLE prediction error over 30-minute intervals. Tighter R values (e.g., 1 km² position variance) produced perpetual spurious recalibration because normal Space-Track TLE updates for high-drag or maneuvering LEO objects can differ by tens of kilometers from the prior prediction. The 30 km figure is consistent with published Space-Track TLE accuracy studies (Vallado, Crawford 2008).

> **Limitation: R Matrix Calibration Scope**
>
> R is calibrated against ISS TLE-to-TLE error only. ISS is a high-drag, frequently maneuvering platform with an atypical TLE update cadence (multiple updates per day due to its crewed status). The generalization of this R matrix to debris, rocket bodies, and commercial smallsats with different drag profiles, TLE update frequencies, and orbit determination accuracy characteristics is unvalidated. The uniform R matrix applied across all objects and both TLE sources (Space-Track and N2YO) does not account for inter-object or inter-source accuracy differences. Per-source and per-object-class R calibration is identified as a post-POC activity.

## 3.5 NIS Consistency Test

The Normalized Innovation Squared is computed on every update:

```
y = z_observed − z_predicted
NIS = yᵀ S⁻¹ y
```

where S is the 6×6 innovation covariance output by the UKF. Under a consistent filter, NIS is chi-squared distributed with 6 degrees of freedom. The anomaly threshold is the chi-squared critical value at p=0.05:

```
CHI2_THRESHOLD_6DOF = 12.592
```

NIS values exceeding this threshold indicate the filter's model no longer explains the observation. NIS history is retained (last 20 values) to support the two-cycle maneuver confirmation protocol.

> **Caveat: NIS Independence Assumption**
>
> The chi-squared distributional assumption for NIS requires that innovations be independent and identically distributed. With temporally correlated TLE errors (arising from the batch-fit smoothing process inherent in TLE generation), the effective degrees of freedom of the NIS test statistic may differ from the nominal 6. No autocorrelation analysis of the NIS time series has been performed in the POC. This means the p=0.05 threshold may not correspond to the intended 5% false-alarm rate. Formal characterization of the NIS autocorrelation structure under TLE inputs is required to establish calibrated detection thresholds.

## 3.6 Recalibration

On anomaly detection, the filter is re-initialized from the current observation with an inflated covariance (factor 20.0 for maneuvers, 10.0 for drag anomalies and filter divergences). This avoids the pathological case where the filter retains an inconsistent prior that prevents convergence to a valid new state.

The inflation factors (20.0 and 10.0) are heuristic values selected during POC development. The rationale for the 2:1 ratio between maneuver and drag inflation factors is that maneuvers introduce larger state discontinuities requiring broader initial uncertainty. The sensitivity of post-recalibration convergence time to these specific values has not been formally characterized.

---

# 4. Anomaly Classification

`anomaly.classify_anomaly` applies three rules in priority order when NIS exceeds threshold:

1. **`maneuver`** — Active-satellite object class AND NIS exceeds threshold for at least `MANEUVER_CONSECUTIVE_CYCLES = 2` consecutive cycles. A two-cycle deferred confirmation protocol is applied: the first exceedance is stored as pending anomaly state; if the next cycle also exceeds, the classification is upgraded (the first cycle's `state_history` row is retroactively corrected via `UPDATE`).
2. **`drag_anomaly`** — A single NIS exceedance with along-track velocity residual dominating cross-track by a 3:1 ratio and cross-track residual below 1 km. The along-track determination uses the ECI velocity residual direction as a proxy.
3. **`filter_divergence`** — Any remaining NIS exceedance. Catch-all category.

Each detection writes a row to the `alerts` table with status `active`, broadcasts an `anomaly` WebSocket message, triggers conjunction screening asynchronously, and triggers filter recalibration.

> **Caveats on Classification Heuristics**
>
> - **Drag classifier ECI-frame proxy:** The 3:1 ECI velocity ratio heuristic is an approximation of the RSW (Radial, Along-Track, Cross-Track) decomposition. For objects at high inclinations, ECI-frame velocity residual directions diverge from the RSW frame, potentially misclassifying drag-induced anomalies as filter divergences (or vice versa). Proper decomposition requires the RSW frame, which is a named post-POC activity (TD-017).
> - **Two-cycle debounce:** The requirement for two consecutive NIS exceedances before maneuver classification is a design choice that trades detection latency (adds one observation interval) for reduced false-positive rate. This is not a validated optimal threshold; it is a heuristic selected during POC development.
> - **No formal detection performance analysis:** No Receiver Operating Characteristic (ROC) analysis or confusion matrix has been produced against ground-truth maneuver data. Without such analysis, the false-positive and false-negative rates of the classifier are unknown. Obtaining ground-truth maneuver data for the tracked catalog is a prerequisite for formal performance characterization.

---

# 5. Data Ingestion

## 5.1 Space-Track Primary Path

The Space-Track.org REST API is polled every 30 minutes (`POLL_INTERVAL_S = 1800`) via authenticated HTTPS. The query returns the most recent TLE per requested NORAD ID. The response is validated against the standard modulo-10 TLE checksum on both lines. Valid TLEs are written to the `tle_catalog` SQLite table via `INSERT OR IGNORE` on the unique key `(norad_id, epoch_utc)`. `ingest.py` is the only module in the system permitted to make external network calls for TLE data.

Credentials (`SPACETRACK_USER`, `SPACETRACK_PASS`) are read from the process environment at startup. Every API call is audit-logged with timestamp, response code, and object count.

## 5.2 N2YO Supplemental Fallback

N2YO.com is an approved supplemental public TLE source under the amended C-001 requirement. After each Space-Track fetch, `ingest.py` computes the set of catalog objects for which Space-Track returned no TLE or whose most recent cached TLE epoch is older than `N2YO_STALE_THRESHOLD_S = 7` days. This set is capped at `N2YO_MAX_REQUESTS_PER_CYCLE = 50` objects per cycle (ordered oldest-first) and queried per-object from the N2YO REST API. Calls are paced at 100 ms between requests, yielding a worst case of approximately 100 N2YO requests per hour — within the N2YO free-tier 1,000-requests-per-hour account limit.

Each row in `tle_catalog` carries a `source` column (`space_track` or `n2yo`) recording its provenance. The filter is currently source-agnostic (uniform R matrix regardless of source), but the tagging enables per-source measurement noise calibration as a post-POC activity. If `N2YO_API_KEY` is unset in the environment, the fallback is skipped silently and the system operates Space-Track-only. N2YO failures (HTTP errors, checksum failures, NORAD ID mismatch) produce per-object `None` returns and do not propagate out of `ingest.py`.

> **Note: Dual-Source TLE Consistency**
>
> The N2YO fallback introduces a second TLE source with potentially different orbit determination methodology, observation sets, and accuracy characteristics. The uniform R matrix applied to both sources does not account for inter-source accuracy differences. The practical impact is that filter consistency (as measured by NIS statistics) may differ systematically between Space-Track-sourced and N2YO-sourced updates. The provenance tagging provides the data infrastructure for characterizing and correcting this difference, but that analysis has not been performed in the POC.

## 5.3 ITAR Awareness

Both Space-Track and N2YO publish unclassified, publicly releasable TLE data. The Space-Track account is registered under acknowledgment of export-control terms at account creation. No classified or Controlled Unclassified Information (CUI) sources are ingested. The system does not redistribute raw TLE data — `GET /catalog` returns processed state summaries, not TLE strings. Raw TLE data remains in the local SQLite cache only.

---

# 6. Tracked Catalog (VLEO Scope)

The tracked catalog contains 75 verified objects, all at or below 600 km altitude. Altitude verification is documented in `data/catalog/altitude_verification_report.txt`. The catalog targets Very Low Earth Orbit because this regime concentrates the highest-activity SSA population: crewed platforms (ISS, CSS), the densest commercial smallsat deployments, active commercial radar-imaging constellations, and the fragment populations with the highest operational conjunction risk.

> **Limitation: Catalog Scope and Scaling**
>
> The 75-object catalog is a curated subset that does not test scaling behavior, catalog management at density, or cross-correlation challenges present in a full operational catalog. At full catalog scale (30,000+ objects), the system would encounter additional challenges including: computational throughput for per-object UKF maintenance, I/O bottlenecks for concurrent database writes, memory management for concurrent filter state, and catalog maintenance operations (object correlation, new-object ingestion, decay/reentry removal) that are absent from the POC.

Catalog composition by category:

- **Crewed platforms.** ISS (ZARYA) NORAD 25544, CSS (TIANHE) NORAD 48274. These are the highest-priority maneuver detection targets; ISS executes regular Progress-delivered reboost sequences.
- **Legacy active.** Hubble Space Telescope (HST) NORAD 20580.
- **Starlink VLEO subset.** STARLINK-24, -25, -26, -1095, -1306, -1571, -1706, -1800, -1965, -1990, and other Starlink entries at or below 600 km. Dense constellation with regular station-keeping.
- **Fragmentation monitoring.** STARLINK-34343 NORAD 64157, fragmented 2026-03-29 at approximately 560 km.
- **Commercial imaging.** BlackSky GLOBAL-1 through -5; CAPELLA-2 (SEQUOIA) and CAPELLA-5 through -8; UMBRA-04, -05, -06; ICEYE-X1, -X6, -X7, -X9, -X11, -X14 and additional ICEYE platforms.
- **Radio-frequency geolocation.** HawkEye 360 HAWK-A (43765), HAWK-B (43794), HAWK-C (43799), HAWK-8A (59443), HAWK-8B (59445), HAWK-8C (59449).
- **Planet and Swarm smallsats.** VLEO subset of FLOCK and SpaceBEE entries.
- **Rocket bodies.** CZ-5B and Falcon 9 upper stages. Non-maneuvering, drag-sensitive.
- **Debris.** Cosmos 1408 fragment cloud members from the November 2021 anti-satellite event.

STARLINK-34343 is retained as a worked example of fragmentation-event response. When Space-Track and N2YO both cease publishing TLEs for the fragmented parent, the frontend's 28-day staleness filter removes it from the live globe view while the alert panel retains its historical anomaly records. When fragment NORAD IDs are publicly assigned, they can be appended to `data/catalog/catalog.json` and tracked through the standard pipeline with no code changes.

---

# 7. Operator Interface: Telemetry Dashboard

The operator interface implementation demonstrates the following design principles for SSA telemetry presentation. The interface is implemented as a browser-based dashboard; it is a POC operator interface, not a production-ready multi-seat operations center display.

**System health visibility.** The header displays a live tracked-object counter (`N TRACKED`) driven by the WebSocket state-update stream and a WebSocket status indicator (`LIVE` / `RECONNECTING`). This provides immediate operator awareness of data pipeline health and catalog coverage.

**Event-driven chart activation.** The residual / NIS chart panel is collapsed by default. It expands when the operator selects an object with an active, recalibrating, or historical anomaly — via click on the globe, click on an alert card, or automatically when a new anomaly fires on the currently selected object. This design decision reduces cognitive load during nominal operations and focuses attention on objects requiring operator assessment.

**Anomaly alerting.** On every live `anomaly` WebSocket message, the interface activates two subsystems simultaneously: a Web Audio API three-beep rising tone (660 Hz, 880 Hz, 1100 Hz) via `alertsound.js`, and a fullscreen red flash overlay bearing the object name and anomaly type via `alertflash.js`. A mute toggle in the header silences audio only; the visual flash is persistent. The alerting mechanism is designed to be difficult to ignore during sustained monitoring operations.

**28-day TLE staleness filter.** `main.js` rejects any `state_update` or `recalibration` message whose epoch is older than 28 days, removing the corresponding entity from the viewer and decrementing the tracked-object counter. This prevents decayed or deorbited objects from persisting in the display.

---

# 8. Conjunction Screening Coupling

Standard conjunction assessment is a scheduled batch process (typically daily). The proposed system couples conjunction screening to anomaly detection: whenever the filter flags an anomaly, `conjunction.screen_conjunctions` runs in `loop.run_in_executor` (non-blocking on the main event loop), propagating the anomalous object and all other catalog objects at 60-second steps over a 5400-second horizon (90 minutes, approximately one LEO orbital period). First-order risks are catalog objects with minimum separation < 5 km; second-order risks are objects within 10 km of any first-order risk. The results are broadcast as a `conjunction_risk` WebSocket message and enrich the corresponding alert card.

The rationale is operationally motivated: a maneuver is precisely the condition under which conjunction risk may have changed, and re-screening at the moment of detection provides earlier situational awareness than waiting for the next scheduled batch.

> **Limitations of Current Conjunction Screening**
>
> - **Spherical miss-distance threshold:** The POC uses a spherical miss-distance threshold, which produces a higher false-positive rate than RSW-decomposed screening (the "pizza-box" approach using 1 km radial, 25 km along-track, 25 km cross-track). The DoD/NASA standard RSW screening methodology is a named post-POC activity (TD-027).
> - **90-minute propagation horizon:** The 90-minute horizon (approximately one LEO orbital period) is adequate for immediate risk identification but is insufficient for the 72–96-hour conjunction assessment windows used in operational conjunction assessment by the 18th Space Defense Squadron (18th SDS). Extended propagation horizons require higher-fidelity propagators to maintain meaningful accuracy.
> - **Screening scope:** The conjunction screening is performed only against the 75-object internal catalog. Operational conjunction assessment requires screening against the full public catalog (30,000+ objects) and, where accessible, owner/operator ephemeris data.

---

# 9. Technical Growth Path: Toward Full-Spectrum Space Domain Awareness

> **Framing Note**
>
> This section describes proposed development directions and aspirational capabilities. All items described below as production extensions, research extensions, or growth path elements are proposed, not planned or committed. Each requires independent technical validation, and the engineering effort, cost, and timeline for each is subject to the caveats identified herein. The distinction between demonstrated POC capability and proposed future capability is maintained throughout.

## 9.1 Architectural Claim at Scale

The POC demonstrates a specific architectural claim: a closed-loop observe-propagate-validate-recalibrate cycle, realized as a per-object Unscented Kalman Filter (UKF) driven by periodic TLE updates, detects orbital anomalies within a single observation cycle and couples detection to conjunction screening. The POC is intentionally constrained — 75 objects, public TLEs, single-process backend, browser dashboard.

The architectural assertion is that the closed-loop control structure does not require fundamental redesign as the system scales — that the filter topology is extensible to larger catalogs, higher-fidelity process models, and heterogeneous sensor inputs. This assertion is based on the modular separation of the process model, measurement model, and noise parameterization from the filter topology itself. However, this claim requires validation at scale; the engineering effort required to realize the transition from POC to production is material and is characterized in Section 9.6.

## 9.2 The Kessler Risk as Motivation for Continued Development

In 1978, Donald Kessler and Burton Cour-Palais characterized the critical debris density threshold above which fragment-generating collisions become self-sustaining — each collision produces fragments that increase the probability of further collisions, eventually rendering entire orbital shells unusable on human-civilization timescales. Three events have moved the observed trajectory materially toward the Kessler threshold:

1. Iridium 33 / Cosmos 2251 collision (2009, 789 km).
2. PLA ASAT test (2007, 863 km), NUDOL test (2021, 485 km).
3. Commercial constellation growth (ongoing).

A critical scope distinction applies to trackable versus lethal non-trackable debris:

| Size | Population (est.) | Threat Profile | Trackable? |
|---|---|---|---|
| ≥ 10 cm | ~30,000 cataloged objects | Catastrophic collision; generates fragment clouds | Yes — ground radar |
| 1–10 cm | ~1,000,000 estimated objects | Kill vehicle for active satellites; below radar sensitivity | No — gap in current architecture |
| < 1 cm | Hundreds of millions | Cumulative surface erosion | No — statistical flux models only |

The ne-body POC operates exclusively in the ≥ 10 cm cataloged domain. The 1–10 cm population is the most operationally hazardous size class in terms of probability-weighted lethality. The POC does not address this gap. The platform's leverage point is upstream in the Kessler causal chain: by detecting anomalous maneuvers, unexpected drag profiles, and conjunction risks in the cataloged (≥ 10 cm) population within one observation cycle, it enables intervention before parent-body collisions generate the sub-trackable fragment spray.

## 9.3 Proposed Transition: TLE Observation to Multi-Sensor Fusion

The current measurement pipeline treats a Space-Track or N2YO TLE publication as the system's observation. This is a deliberate POC approximation: a TLE is itself the output of an external orbit determination process, not a direct sensor measurement. The measurement noise R matrix absorbs this conflation pragmatically but not rigorously.

The proposed production architecture would replace TLE-derived state vectors with observations from purpose-built sensors:

| Sensor Type | Observable | Typical 1σ | Object Visibility | Availability |
|---|---|---|---|---|
| Phased-array radar | Range, range-rate, azimuth, elevation | ~10 m range, ~0.1 m/s Doppler | Non-cooperative, all sizes ≥ ~10 cm | DoD-controlled |
| Optical telescope network | Right ascension, declination | ~1 arcsecond | Sun-lit objects, clear skies | Commercial |
| Laser ranging (SLR) | Two-way range | ~1 cm | Cooperative targets with retroreflectors | ILRS network |
| RF emission monitoring | Doppler shift, TDOA | Depends on frequency | Active RF-emitting objects only | Commercial SIGINT |
| SAR imaging | Resolved target position | ~1 m | Non-cooperative; all-weather | Commercial |

> **Integration Complexity for Multi-Sensor Fusion**
>
> Each sensor type introduces unique integration challenges that are not addressed in the current POC: security classification boundaries (DoD radar data requires air-gapped processing enclaves), heterogeneous data formats and observation models (each sensor requires a dedicated `hx` measurement function), asynchronous observation timelines (sensors observe at different cadences and with different latencies), cross-calibration requirements (sensor biases must be estimated and removed), and coverage geometry (no single sensor type provides full-catalog visibility). Multi-sensor fusion represents a bounded but material systems integration effort, estimated at multiple person-years of engineering beyond the current POC.

## 9.4 Proposed Object Classification Extension

The current catalog uses three `object_class` values (`active_satellite`, `debris`, `rocket_body`). The proposed production extension would classify across two axes:

**Origin axis:** Allied, Neutral, Adversarial, Unknown. An unannounced maneuver by an adversarial object near a high-value allied asset generates a different alert escalation path than the same maneuver by a neutral commercial satellite. The same NIS exceedance drives different response chains depending on attribution.

**Type axis:** Payload, rocket body, debris, natural objects (near-Earth asteroids, meteoroids). Natural objects require a different propagator (heliocentric orbital elements, n-body gravitational model during close approach) but the same closed-loop detect-classify-alert architecture.

## 9.5 Proposed Detection Chain for Kessler Mitigation

1. **Anomaly detection** (demonstrated in POC).
2. **Immediate conjunction screening** (demonstrated in POC, with limitations noted in Section 8).
3. **Extended forward projection** (proposed production extension — requires higher-fidelity propagators with J₂–J₆ geopotential, NRLMSISE-00 atmospheric drag, solar radiation pressure over a 72–96-hour horizon).
4. **Sensor tasking generation** (proposed production extension — requires sensor network integration; system generates structured tasking requests specifying target NORAD ID, required observation type, and time window).
5. **Mitigation coupling** (proposed production extension — decision support computing delta-V required to exit conjunction corridor for debris objects approaching Kessler-contributing thresholds; not autonomous command).
6. **Fragment cascade modeling** (proposed research extension — requires validated breakup models; STARLINK-34343 is the current worked example with fragment NORAD IDs appended to `catalog.json` when published).

## 9.6 Architectural Continuity — and the Limits of That Claim

The claim of architectural continuity from POC to production must be qualified by three layers of rework that are required:

**Layer 1 — Process model.** SGP4 must be replaced with a numerical integrator (e.g., RK4/5 or RK7/8 with configurable force models including J₂–J₆ geopotential, atmospheric drag with density model, solar radiation pressure, third-body perturbations). This is a well-understood engineering task but affects the computational budget per object per cycle by orders of magnitude. It is also required to restore the UKF's mathematical validity: SGP4 is a deterministic analytic trajectory parameterized by mean elements, so all 13 sigma points map to effectively identical output states. A numerical integrator propagates each sigma point independently through nonlinear dynamics, recovering the UKF's designed covariance propagation behavior.

**Layer 2 — Measurement model and state definition.** The identity `hx` must be replaced with per-sensor partial-observation measurement functions. A mean-to-osculating element transformation must be incorporated. SGP4 operates on mean elements; raw sensors (radar, optical, lidar) produce osculating-state measurements. These are fundamentally different quantities — the mean-to-osculating transformation (Brouwer/Kozai theory) introduces position oscillations with amplitudes of tens to hundreds of kilometers depending on inclination and altitude. This requires re-derivation of the measurement Jacobian structure and is the most significant structural change in the production transition.

**Layer 3 — Noise tuning.** Q and R must be rederived from scratch using sensor calibration data and physically motivated process noise models rather than TLE empirics. Q absorbs unmodeled dynamics in the mean-element space in the current POC; R absorbs TLE-to-TLE fit residuals. When the measurement model changes to raw sensor observations, both matrices must be derived against measured sensor noise characteristics. The current parameter values do not transfer.

The correct claim is: the filter topology (predict-update-test-recalibrate cycle, per-object state maintenance, anomaly-triggered conjunction screening) is expected to accommodate production requirements without architectural redesign. The filter parameterization and measurement model require well-characterized, bounded rework. The magnitude of that rework is non-trivial, and the preceding three layers represent the dominant engineering investment in a production transition.

| POC (Current) | Production (Proposed) |
|---|---|
| TLE ingest (Space-Track, N2YO) | Sensor adapters: radar, optical, lidar, SIGINT |
| SGP4 mean-element process model | Numerical integrator (RK45 + force models), osculating state |
| Identity measurement function `hx` | Per-sensor partial-observation `hx` with mean→osculating correction |
| Hand-tuned Q and R (TLE empirics) | Physically derived Q and R from sensor calibration data |
| 75-object VLEO catalog | Full LEO/MEO/GEO/HEO/cislunar catalog |
| 90-minute conjunction horizon | 72–96-hour probabilistic conjunction timeline |
| NIS anomaly detection → alert | NIS detection → sensor tasking → mitigation recommendation |
| Public TLE sources | Classified sensor data (CUI/S+SI, air-gapped deployment) |
| Local SQLite | Distributed time-series database |
| Single-process backend | Parallel-processing pipeline |
| Browser dashboard | Multi-seat ops center display |

## 9.7 Proposed Research Direction: Bioinformatics Pathway for Correlated Observation Noise

> **Research Direction — Not Demonstrated Capability**
>
> The following describes a proposed research direction. The analogy between Hidden Markov Models (HMMs) in bioinformatics and the TLE-as-observation filtering problem is structurally sound at the mathematical level, but it has not been implemented or validated against orbital mechanics ground truth. The feasibility and performance advantage of this approach relative to the current threshold-based classifier remain to be demonstrated.

The Markov assumption at the heart of the Kalman filter states that the current state fully encapsulates all information relevant to predicting the next state. TLEs violate this assumption structurally. A TLE is the output of a batch orbit determination process that smooths observations over a multi-day span into a single mean-element set. The resulting "noise" is therefore temporally correlated — successive TLE errors are not independent draws from a stationary distribution. The inflated R matrix used in the POC is a pragmatic workaround that absorbs this correlation into the measurement noise budget, but it is not a principled noise model. It does not capture the temporal structure of TLE-to-TLE error correlation.

The proposed bioinformatics HMM analogy:

| Bioinformatics HMM | Orbital Mechanics Equivalent |
|---|---|
| Hidden state sequence | True osculating orbital state trajectory |
| Markov process over hidden states | Physical dynamics (integrator + force model) |
| Emission probability | TLE generation process: mean-element smoothing + fit residuals |
| Observed sequence | Sequence of TLE publications |
| Profile HMM | Per-object-class nominal behavior model |
| Log-likelihood anomaly score | NIS test (chi-squared under Gaussian emission assumption) |
| Viterbi decoding | Rauch-Tung-Striebel (RTS) smoother |
| Baum-Welch parameter learning | EM for online Q and R estimation |

Three-part near-term upgrade proposed (each requires implementation and validation before deployment):

1. **Explicit emission model for TLE generation.** Model the TLE publication process as a stochastic mapping from osculating state to mean-element observation, with temporally correlated noise structure. The Brouwer short-period corrections are analytic and implementable as part of `hx` using existing TLE inputs, improving POC fidelity without requiring sensor architecture changes.
2. **EM-based adaptive noise estimation** (addressing POST-002). Replace hand-tuned Q and R with expectation-maximization estimation from the NIS time series. The existing NIS history buffer (last 20 values) is the seed data structure for this computation. Requires sufficient observation history for convergence and may be sensitive to non-stationarity in the TLE error distribution.
3. **Competing HMMs for anomaly classification.** Replace the threshold-based classifier with competing generative models for nominal, maneuvering, and drag-perturbed behavior. Requires training data for each anomaly class — availability and quality of ground-truth maneuver data is a limiting factor. This approach replaces the brittle 3:1 ECI ratio heuristic (TD-017) with a probabilistic discriminator that generalizes across inclinations, altitudes, and object classes.

## 9.8 Temporal Urgency

The Kessler-mitigating ISR mission that motivates continued development of closed-loop SSA capabilities is time-limited. The debris environment in the most congested orbital shells (700–900 km) is approaching density thresholds where cascading collisions become statistically likely within the next five to ten years, based on current trajectory models. This provides operational motivation for accelerated development of SSA architectures capable of detecting anomalies and generating actionable conjunction warnings at the cadences required for active collision avoidance.

---

# 10. Current Limitations and Roadmap (POC Scope)

> **V&V Status**
>
> No formal verification and validation (V&V) campaign has been conducted on any component of the POC. All performance characterizations described in this document are based on engineering observation during development, not on structured test protocols with defined acceptance criteria. Formal V&V is a prerequisite for any advancement beyond TRL 4.

The following limitations are organized by technical debt tracking number and post-POC activity identifier. Each limitation includes the current impact and the remediation path.

**TD-007: SGP4 process model fidelity.** SGP4 is an analytical mean-element propagator that does not model atmospheric drag variability, solar radiation pressure, or third-body perturbations with physical fidelity. For the POC, this is acceptable because the TLE observations themselves are products of the same SGP4 theory. In a production system with direct sensor observations (osculating state), SGP4 must be replaced with a numerical integrator. This is the single largest rework item on the production transition path.

**POST-003: Filter state persistence.** The UKF state (covariance matrix, last TLE, NIS history) is held in memory only. A process restart re-initializes all filters from the most recent TLE with the default prior covariance. This means post-restart convergence requires multiple observation cycles (typically 2–4 updates, or 1–2 hours at the current 30-minute cadence). The remediation is serialization of filter state to the SQLite database with recovery on restart.

**POST-002: Adaptive Q estimation.** Process noise matrices are hand-tuned and static. The filter cannot adapt to changing drag conditions (e.g., solar maximum vs. solar minimum) or to objects with atypical maneuver profiles. EM-based adaptive estimation from the NIS time series is the proposed remediation (see Section 9.7).

**TD-011 / TD-017: Mean-to-osculating element transformation and RSW-decomposed anomaly classification.** The identity measurement function `hx` conflates mean and osculating elements (error on the order of several kilometers for LEO objects, partially absorbed by the inflated R matrix). The drag anomaly classifier uses an ECI-frame velocity residual proxy for what should be an RSW-frame decomposition; this limits drag/maneuver discrimination accuracy at high inclinations. Both are addressed by the Layer 2 measurement model rework.

**TD-027: RSW-decomposed conjunction screening.** The spherical miss-distance threshold produces a higher false-positive rate than the operational RSW pizza-box standard (1 km radial, 25 km along-track, 25 km cross-track). Implementation of RSW-decomposed screening is required for conjunction assessment results to be comparable to 18th SDS products.

**TD-019: Authentication and access control.** The REST and WebSocket endpoints are unauthenticated. Any network-accessible deployment requires authentication (e.g., OAuth2 or API key) and role-based access control (RBAC) before the system may be connected to any network beyond localhost.

**Additional limitations:**

- **Single-process reliability:** No watchdog monitoring, automatic restart, or graceful degradation on process failure. A crash results in total service interruption until manual restart.
- **No per-source R calibration:** Space-Track and N2YO TLEs are treated with identical measurement noise, despite potentially different accuracy characteristics.
- **No multi-source sensor integration:** The system ingests TLE-derived state vectors only. Direct sensor observation processing requires the Layer 1–3 rework described in Section 9.6.
- **No formal detection performance characterization:** False-positive rate, false-negative rate, and detection latency have not been characterized against ground-truth maneuver data.
- **No load testing:** The system has not been tested under sustained high-throughput conditions (e.g., catalog update storms, concurrent conjunction screening for multiple objects).

**Roadmap priorities** (ordered by criticality for production transition):

1. Numerical integrator replacement for SGP4 process model (TD-007).
2. Adaptive Q estimation via EM or innovation-based methods (POST-002).
3. Persistent filter state with database serialization and restart recovery (POST-003).
4. RSW-decomposed classifier and conjunction screening (TD-017, TD-027).
5. Per-source R calibration (Space-Track vs. N2YO, and preparation for direct sensor R).
6. Authentication and RBAC (TD-019).
7. Multi-source sensor integration (Layers 1–3 rework per Section 9.6).

---

# 11. Summary

The ne-body POC demonstrates the following capabilities under the stated test conditions (75 VLEO objects, 30-minute TLE update cadence, ISS-calibrated R matrix, single-process Python backend):

1. **Continuous closed-loop orbit estimation.** A per-object Unscented Kalman Filter maintains recursive state estimates across a heterogeneous VLEO catalog, using TLE-derived state vectors as observations.
2. **Automated anomaly detection.** The Normalized Innovation Squared (NIS) consistency test, applied at the chi-squared 6-DOF p=0.05 threshold, identifies filter-observation inconsistencies within a single observation cycle (bounded by the 30-minute polling interval).
3. **Three-class anomaly classification.** Detected anomalies are classified as maneuver, drag anomaly, or filter divergence using heuristic rules. Classification accuracy has not been formally validated against ground-truth data.
4. **Anomaly-triggered conjunction screening.** Conjunction risk assessment is coupled to anomaly detection, providing immediate re-screening at the moment an orbital state change is detected. Screening uses spherical miss-distance thresholds over a 90-minute horizon against the internal 75-object catalog.
5. **Operator telemetry interface.** A browser-based dashboard provides real-time state visualization, anomaly alerting (audio and visual), and NIS/residual time-series display.

The POC does not demonstrate: scaling to full catalog size, multi-sensor fusion, operationally standard conjunction assessment (RSW-decomposed, 72–96-hour horizon), adaptive noise estimation, persistent filter state, authenticated access, or formal detection performance characterization.

**Technology Readiness Level assessment:** TRL 3–4. The analytical proof of concept (TRL 3) has been established through implementation and testing against live TLE data. Experimental proof of concept (TRL 4) has been partially demonstrated through sustained operation of the closed-loop pipeline, but the absence of formal V&V, ground-truth performance characterization, and testing under operationally representative conditions (catalog scale, sensor diversity, sustained uptime requirements) prevents full TRL 4 assertion.

**Specific next steps required to advance readiness:**

1. Conduct formal V&V campaign with defined test protocols and acceptance criteria.
2. Obtain ground-truth maneuver data and characterize detection performance (ROC analysis, confusion matrix).
3. Implement and validate numerical integrator process model (TD-007) to establish TRL 4 for the production-representative architecture.
4. Conduct scaling tests at 1,000- and 10,000-object catalog sizes to characterize computational and I/O bottlenecks.
5. Implement adaptive Q estimation (POST-002) and characterize its impact on detection performance.
6. Engage with sensor data providers to establish data access agreements for multi-sensor fusion development.

---

# Cross-References

| Document | Path | Version |
|---|---|---|
| System Architecture Reference | `docs/reference/system-architecture.md` | 0.2.0 |
| Algorithmic Foundation | `docs/reference/algorithmic-foundation.md` | — |
| Concept of Operations | `docs/reference/conops.md` | — |
| Altitude Verification Report | `data/catalog/altitude_verification_report.txt` | — |

---

# Appendix A — Reviewer Concerns and Responses

The following addresses anticipated technical concerns from a review panel evaluating this effort for feasibility, technical rigor, and readiness for continued development investment. Responses are direct and acknowledge limitations where they exist.

---

### Concern 1: UKF vs. EKF Justification

*The whitepaper acknowledges that sigma-point collapse under SGP4 causes the UKF to degenerate to a linear estimator. If the UKF provides no nonlinear covariance propagation advantage in the current implementation, why not use a computationally cheaper EKF? What measurable advantage does the UKF provide over an EKF in the POC?*

**Response:**

This is a valid concern. In the current POC implementation, where SGP4 serves as the process model, the UKF does not provide a measurable advantage over an EKF. The sigma-point perturbations (governed by α=1e-3 and the state covariance) produce SGP4 responses that are effectively linear, meaning the UKF's nonlinear covariance propagation capability is not exercised.

The UKF was selected as an architectural decision, not a POC performance decision. The production transition (Section 9.6, Layer 1) replaces SGP4 with a numerical integrator, under which the state transition function is genuinely nonlinear — atmospheric drag is exponentially dependent on altitude, and gravitational perturbations introduce nonlinear coupling between state elements. In that regime, the UKF's sigma-point sampling provides meaningful covariance propagation advantages over the EKF's first-order Jacobian linearization.

The computational cost difference between UKF and EKF in the POC is negligible: 13 sigma-point SGP4 propagations per object per cycle versus one propagation plus one Jacobian evaluation. At 75 objects with a 30-minute cycle, both are sub-second total. At full catalog scale with a numerical integrator, the UKF's 2n+1 propagation cost becomes material and may motivate investigation of reduced sigma-point strategies or square-root UKF formulations.

A formal comparative study (EKF vs. UKF detection performance under identical conditions) has not been conducted and is acknowledged as a gap. Such a study would be straightforward to execute and would provide empirical evidence for the filter selection.

---

### Concern 2: TLE-as-Observation Validity

*The filter treats TLE-derived state vectors as direct observations with an independent-measurement noise model. TLEs are orbit determination products with correlated batch-fit errors. How does this violation of the independent-measurement assumption affect filter consistency? What is the quantified impact on NIS reliability?*

**Response:**

This concern identifies the most fundamental theoretical limitation of the POC. TLEs are the output of a batch least-squares orbit determination process that fits a mean-element model to sensor observations accumulated over a multi-day span. The resulting errors are temporally correlated: successive TLEs share overlapping observation arcs, and the batch-fit smoothing introduces inter-epoch error correlation that violates the Kalman filter's Markov assumption.

The quantified impact on NIS reliability has not been formally characterized. What can be stated is: (a) the inflated R matrix (30 km 1-sigma position) serves as a pragmatic workaround that broadens the filter's acceptance window to accommodate the non-Gaussian, correlated error structure of TLE inputs; (b) under this inflated R, the filter does not exhibit systematic divergence over the observation periods tested (weeks of continuous operation against the 75-object catalog); (c) the NIS chi-squared threshold at p=0.05 is calibrated under the assumption of independent innovations, and the true false-alarm rate under correlated inputs is unknown.

Formal characterization would require: (1) autocorrelation analysis of the NIS time series across the catalog to quantify the effective degrees of freedom, (2) comparison of the empirical NIS distribution against the theoretical chi-squared distribution to identify systematic bias, and (3) validation against ground-truth maneuver data to determine whether the threshold produces the intended detection sensitivity. This analysis is tractable with the existing data (the `state_history` table retains NIS values) and is recommended as an immediate post-POC activity.

The proposed HMM-based approach (Section 9.7) provides a principled path to modeling the TLE generation process as an emission probability, which would replace the independent-measurement assumption with a structured noise model. This remains a proposed research direction.

---

### Concern 3: R Matrix Generalization

*R is calibrated against ISS TLE-to-TLE error only. ISS is a high-drag, frequently maneuvering platform with an atypical TLE update cadence. How does this calibration generalize to debris, rocket bodies, and commercial smallsats with different drag profiles and TLE update frequencies?*

**Response:**

The generalization is unvalidated. ISS was selected as the calibration target because it has the highest TLE update cadence in the catalog (multiple updates per day), providing the largest sample of TLE-to-TLE prediction errors for empirical R estimation. However, ISS is atypical in several respects: (a) its high drag profile (large cross-sectional area, ~420 km altitude) produces rapid orbital decay between maneuvers, (b) it executes frequent reboost maneuvers that introduce state discontinuities, and (c) its crewed status drives a higher TLE publication priority than most cataloged objects.

For debris objects and rocket bodies, TLE update cadences are typically lower (daily to weekly), and the prediction error over one update interval may be systematically different from the ISS calibration — potentially larger for high-drag debris (Cosmos 1408 fragments) or smaller for stable-orbit objects. The uniform R matrix may therefore be over-conservative for some objects (suppressing valid detections) and under-conservative for others (producing false alarms).

The data infrastructure for per-object-class R calibration exists: the `tle_catalog` table retains historical TLEs with epoch timestamps, and the `state_history` table retains prediction-observation residuals. A per-class R calibration study (computing empirical residual covariance grouped by `object_class`) is tractable with the existing data and is identified as a roadmap item.

---

### Concern 4: Q Matrix Hand-Tuning and Sensitivity

*Process noise matrices are hand-tuned with no adaptive estimation. What is the sensitivity of anomaly detection performance to Q perturbation? Has a Monte Carlo analysis been conducted?*

**Response:**

No Monte Carlo analysis has been conducted. This is acknowledged as a gap. The Q matrices were selected through iterative manual tuning during development, observing filter behavior (convergence time, NIS stability, anomaly detection responsiveness) across the 75-object catalog over a period of weeks. The tuning criteria were qualitative: avoid persistent NIS exceedances during nominal operations, detect known ISS reboost events within one observation cycle, and avoid filter divergence during extended periods without TLE updates.

The sensitivity of detection performance to Q perturbation is expected to be material. Q directly controls the filter's prediction uncertainty growth rate, which determines the NIS threshold's effective sensitivity. Increasing Q broadens the filter's acceptance window (reducing false positives but increasing detection latency and potentially missing low-amplitude anomalies). Decreasing Q narrows the window (increasing sensitivity but raising the false-positive rate). The specific operating point on this tradeoff curve has not been characterized.

A structured sensitivity analysis would involve: (1) defining a grid or Latin hypercube sample over the Q parameter space, (2) running the filter against historical TLE data for each Q configuration, (3) computing NIS statistics and anomaly detection counts, and (4) characterizing the sensitivity surface. This analysis is computationally feasible (the replay infrastructure exists) and is recommended as a prerequisite for any claims about detection performance robustness. Adaptive Q estimation via EM (POST-002) is the proposed long-term remediation.

---

### Concern 5: Drag Classifier Heuristic Brittleness

*The 3:1 ECI velocity ratio heuristic for drag anomaly classification is an approximation. What is the misclassification rate for high-inclination orbits where ECI-frame decomposition diverges significantly from RSW?*

**Response:**

The misclassification rate has not been quantified. The 3:1 ECI velocity ratio is a proxy for the RSW along-track/cross-track decomposition. The proxy is geometrically accurate when the ECI velocity direction is closely aligned with the along-track direction — which is true near the equator and at low inclinations. For near-polar orbits (inclination ~90°), the ECI velocity vector rotates relative to the orbital frame over each orbit, and the ECI-frame ratio can diverge substantially from the RSW ratio.

The practical impact in the current catalog is bounded: the 75-object VLEO catalog includes objects at a range of inclinations (ISS at ~51.6°, Starlink at ~53°, SSO objects at ~97°), but no systematic study of misclassification versus inclination has been performed. The classified anomalies in the POC have been reviewed manually during development for qualitative consistency, but this does not constitute formal validation.

The remediation is RSW-decomposed classification (TD-017), which computes residuals in the orbital reference frame. This requires the orbital velocity direction (available from the filter state) and is a straightforward coordinate transformation. It is prioritized on the roadmap.

---

### Concern 6: Absence of Formal Detection Performance Characterization

*No ROC analysis or confusion matrix has been presented. Without ground-truth maneuver data, how can detection performance be assessed?*

**Response:**

It cannot be rigorously assessed without ground-truth data. This is the most significant validation gap in the POC. The absence of formal performance characterization means the false-positive rate, false-negative rate, and detection latency distribution are all unknown in a statistical sense.

Partial mitigation approaches available without ground-truth data include: (a) synthetic injection — simulating maneuvers by applying known delta-V perturbations to TLE-propagated states and measuring detection performance against the injected ground truth; (b) historical correlation — comparing detected anomalies against publicly available operator announcements (e.g., SpaceX Starlink maneuver schedules, ISS reboost announcements) to assess true-positive rate for a subset of the catalog; (c) NIS distribution analysis — comparing the empirical NIS distribution against the theoretical chi-squared distribution to assess filter consistency in aggregate.

None of these fully substitutes for ground-truth data. Obtaining ground-truth maneuver data requires cooperation from operators (SpaceX, NASA, ESA, commercial imaging operators) or access to high-cadence sensor data that enables independent orbit determination. This is identified as a prerequisite for any detection performance claims beyond qualitative characterization.

---

### Concern 7: Scalability from 75 to 30,000+ Objects

*75 objects to 30,000+ is a 400x increase. What are the computational, memory, and I/O bottlenecks? Has any profiling been done beyond the approximately 100 ms/object figure?*

**Response:**

Limited profiling has been conducted. The approximately 100 ms per object per cycle figure is an engineering observation from the POC (single-threaded Python 3.11, commodity hardware), not a formal benchmark. It includes SGP4 propagation (13 sigma points), UKF predict-update, NIS computation, anomaly classification, and SQLite writes.

At 30,000 objects with a 30-minute (1800-second) cycle, single-threaded processing would require approximately 3,000 seconds — exceeding the cycle time. This confirms that the single-process architecture does not scale to full catalog size. The identified bottlenecks are:

- **Compute:** SGP4 propagation of 13 sigma points per object per cycle. With a numerical integrator, this cost increases by 1–2 orders of magnitude per object.
- **Memory:** Each UKF maintains a 6×6 covariance matrix plus state vector and NIS history. At 30,000 objects, the aggregate memory footprint is modest (~100 MB), but concurrent in-flight processing state may be larger.
- **I/O:** SQLite write throughput for `state_history` inserts (one per object per cycle). WAL mode provides adequate concurrency for the POC, but 30,000 writes per cycle may require batched transactions or a higher-throughput database.
- **Conjunction screening:** Anomaly-triggered conjunction screening against a 30,000-object catalog is an O(N²) pairwise distance computation per anomalous object. Pre-filtering (spatial indexing, coarse epoch binning) is required at scale.

The proposed production architecture addresses these through: (a) parallel processing across multiple workers (object-level parallelism), (b) distributed time-series database replacing SQLite, and (c) spatial indexing for conjunction screening. The engineering effort to implement and validate these is non-trivial but well-characterized in the systems engineering literature.

---

### Concern 8: Mean vs. Osculating Element Conflation

*The identity `hx` conflates mean and osculating elements. What is the magnitude of the short-period oscillation error introduced, and how does the inflated R matrix compensate (or fail to compensate)?*

**Response:**

The short-period oscillations due to J₂ (the dominant zonal harmonic) produce position variations on the order of several kilometers for LEO objects. Specifically, the J₂ short-period radial oscillation amplitude is approximately a·e·J₂·(RE/a)² ≈ 1–5 km for typical LEO orbits, with corresponding along-track oscillations of similar or larger magnitude. These oscillations occur at twice the orbital frequency (period ~45 minutes for a 90-minute orbit).

The inflated R matrix (30 km 1-sigma position) is substantially larger than the mean-to-osculating oscillation amplitude, which means the filter absorbs this error as part of the measurement noise budget. The filter remains consistent (NIS does not systematically exceed threshold during nominal operations), but at the cost of sensitivity: physical state changes smaller than the R matrix floor (tens of kilometers in position) cannot be distinguished from mean-osculating publication artifacts.

This means the POC's anomaly detection is fundamentally limited to detecting events that produce state changes larger than the TLE accuracy floor — primarily large maneuvers (delta-V > ~1 m/s) and major drag perturbations. Subtle maneuvers and gradual orbit adjustments fall within the noise floor and are not detectable with the current formulation.

The remediation is an explicit mean-to-osculating element transformation in the measurement model (TD-011, Section 9.6, Layer 2). This would reduce the minimum detectable anomaly amplitude and allow the R matrix to be calibrated against actual sensor noise rather than TLE publication artifacts.

---

### Concern 9: Conjunction Screening False-Positive Rate

*The spherical miss-distance threshold (5 km first-order, 10 km second-order) is known to produce higher false-positive rates than RSW-decomposed screening. What is the estimated false-positive inflation factor relative to the operational standard?*

**Response:**

The false-positive inflation factor has not been quantified against the operational RSW standard for this catalog. In general, spherical screening produces false positives proportional to the volume ratio between a sphere of radius r and the RSW pizza-box volume. For the operational standard (1 km radial × 25 km along-track × 25 km cross-track), the pizza-box volume is approximately 2,500 km³ (approximating as a rectangular parallelepiped). A sphere of radius 5 km has volume approximately 524 km³ — smaller than the pizza-box. However, the sphere and the pizza-box have different geometric orientations relative to the orbit: the pizza-box is elongated along-track, capturing the dominant uncertainty direction, while the sphere wastes screening volume in directions where collision probability is negligible (radial and cross-track at large separations).

The practical effect is that the spherical threshold will flag conjunctions where the along-track separation is large but the radial/cross-track separation happens to be small — cases that the RSW screening would correctly dismiss. For the 75-object catalog with anomaly-triggered screening, the absolute number of false positives is manageable (the conjunction screening runs infrequently — only on anomaly detection — and the catalog is small). At full catalog scale with more frequent screening, the false-positive rate under spherical thresholds would become operationally burdensome.

RSW-decomposed screening (TD-027) is on the roadmap and requires only the RSW frame transformation (computable from the orbital state) and asymmetric threshold comparison. The implementation is straightforward; it has been deferred in favor of higher-priority items.

---

### Concern 10: Security Classification Boundaries for Multi-Sensor Fusion

*The production vision includes classified sensor data (CUI/S+SI). How does the proposed architecture handle security classification boundaries? Has any analysis of air-gapped deployment requirements been conducted?*

**Response:**

No formal security architecture analysis has been conducted. The POC operates exclusively with unclassified, publicly available data. The production vision's reference to classified sensor data acknowledges that operational SSA systems require access to DoD-controlled sensor feeds (Space Fence, SBSS, partner-nation radar) that carry classification markings.

The architectural implications of classification boundaries include: (a) the processing enclave must be accredited at the classification level of the highest-sensitivity input, (b) cross-domain solutions (CDS) are required if products derived from classified inputs are to be shared at lower classification levels, (c) the software supply chain (Python runtime, third-party libraries) must be evaluated for deployment in accredited enclaves, and (d) the browser-based dashboard may require replacement with a thick-client application approved for classified networks.

These are well-understood systems engineering challenges in the DoD SSA community. The POC's value in this context is demonstrating the algorithmic pipeline in an unclassified environment, establishing TRL 3–4 for the filter topology before investing in the accreditation and infrastructure costs of a classified deployment. A formal security architecture study is a prerequisite for any classified deployment and would be conducted as part of a Phase II or production transition effort.

---

### Concern 11: Recalibration Inflation Factor Selection

*The covariance inflation factors on recalibration (20.0 for maneuvers, 10.0 for drag anomalies) appear to be heuristic. What is the justification for the 2:1 ratio? What is the convergence time post-recalibration, and how sensitive is it to the inflation factor?*

**Response:**

The 2:1 ratio is a heuristic based on engineering judgment: maneuvers introduce larger instantaneous state discontinuities (delta-V events can shift velocity by meters per second, propagating to kilometers of position error within minutes) than drag anomalies (which produce gradual along-track acceleration changes). The larger inflation factor for maneuvers reflects the expectation that the filter's prior state is more thoroughly invalidated by a maneuver than by a drag perturbation.

Post-recalibration convergence time has been observed qualitatively during development but not formally measured. Typical convergence (defined as NIS returning to below threshold for two consecutive cycles) occurs within 2–4 observation cycles (1–2 hours at 30-minute cadence) for maneuvers and 1–2 cycles (30–60 minutes) for drag anomalies. These figures are engineering observations, not statistical characterizations.

Sensitivity analysis of convergence time versus inflation factor has not been conducted. The expected relationship is: larger inflation factors produce faster convergence (the filter gives more weight to new observations relative to the inflated prior) but with larger transient state uncertainty during the convergence period. Excessively large inflation factors effectively discard the prior entirely, reducing to a single-observation initialization. Excessively small factors risk the filter retaining inconsistent prior information that delays convergence. A formal sweep of inflation factor versus convergence time and post-convergence accuracy would be straightforward to conduct with the existing replay infrastructure.

---

### Concern 12: WebSocket Reliability and Message Ordering

*The operator interface relies on WebSocket for real-time data delivery. What happens during WebSocket disconnection? Are messages buffered, ordered, or lost?*

**Response:**

During WebSocket disconnection, messages are lost — the backend does not buffer WebSocket messages for disconnected clients. On reconnection, the frontend fetches the current catalog state via `GET /catalog` and active alerts via `GET /alerts/active`, which provides a consistent snapshot but does not replay missed intermediate state updates or transient anomaly events that were subsequently resolved.

Message ordering within an active WebSocket connection is guaranteed by the WebSocket protocol (TCP-based, ordered byte stream). However, the backend broadcasts messages from two independent sources (state updates and anomaly alerts) that are not sequenced relative to each other. In practice, this has not produced observable ordering issues in the POC because state updates and anomaly alerts carry independent timestamps and are processed independently by the frontend.

For a production deployment with multiple concurrent operator sessions, a message broker (e.g., Redis Pub/Sub or a lightweight message queue) with per-client replay capability would be required to ensure no operator misses a critical alert due to transient connectivity loss. This is identified as a productionization requirement but is not on the current roadmap.

---

### Concern 13: N2YO Data Quality and Provenance

*N2YO is described as a "supplemental fallback." What is the provenance of N2YO TLE data? Is it independently generated, or does it mirror Space-Track? If the latter, what is the value of the fallback?*

**Response:**

N2YO's TLE provenance is not fully transparent. N2YO provides TLE data through a public REST API, but the organization does not publish detailed documentation of its orbit determination methodology or data sources. Based on publicly available information, N2YO appears to aggregate TLE data from multiple sources, potentially including Space-Track, amateur radio tracking networks, and other public repositories.

If N2YO mirrors Space-Track data (which is likely for many objects), the fallback value is limited to availability redundancy — if Space-Track is temporarily unreachable (rate limiting, maintenance downtime, authentication issues), N2YO may still serve cached TLEs. The fallback does not provide independent orbit determination in this case.

If N2YO incorporates independently generated TLEs for some objects (e.g., from amateur observer networks), those TLEs may have different accuracy characteristics, different epoch timing, and different batch-fit methodology than Space-Track TLEs. The current uniform R matrix does not account for these differences.

The honest assessment is that the N2YO fallback provides modest availability improvement at the cost of introducing a second data source with incompletely characterized accuracy. The per-row `source` tagging enables future analysis of inter-source consistency, but that analysis has not been performed. For a production system with direct sensor access, the N2YO fallback becomes irrelevant.

---

### Concern 14: 28-Day Staleness Filter Implications

*The 28-day staleness filter removes objects from the display but not from backend processing. Could stale TLEs propagated by the backend produce misleading conjunction screening results?*

**Response:**

Yes. This is a valid concern. If the backend continues to process an object whose most recent TLE is more than 28 days old, the SGP4-propagated state for that object will have accumulated substantial error (potentially hundreds of kilometers for a high-drag VLEO object). If that stale object is included in conjunction screening triggered by another object's anomaly, the conjunction distance computation will be based on an inaccurate state, potentially producing either false-positive conjunction alerts (stale object's propagated position happens to fall near another object) or false-negative misses (stale object has actually decayed to a different orbit but the propagated state suggests safe separation).

The mitigation in the current implementation is partial: the frontend filters stale objects from the operator's view, so conjunction alerts involving stale objects would still appear in the alert panel but the stale object would not be visible on the globe — creating a confusing operator experience.

The correct remediation is to exclude objects with TLE epochs older than a configurable staleness threshold from conjunction screening as well as from display. This is a straightforward backend change (add an epoch-age check to the conjunction screening loop) that has not been implemented. It should be prioritized ahead of the current roadmap position.

---

### Concern 15: Single Point of Failure in Processing Architecture

*The single-process architecture means any unhandled exception crashes the entire pipeline. What fault isolation exists? Has failure-mode analysis been conducted?*

**Response:**

No formal failure-mode analysis (FMEA) has been conducted. The single-process architecture is a POC simplification that provides no fault isolation. An unhandled exception in any component (ingest, processing, anomaly classification, conjunction screening, WebSocket broadcast) propagates to the FastAPI event loop and can crash the entire process.

Current mitigation is limited to: (a) try/except blocks around per-object processing in the processing loop, so a single object's failure does not crash the full catalog pass; (b) try/except blocks around N2YO per-object fetches, so individual N2YO failures do not interrupt the ingest cycle; (c) SQLite WAL mode, which provides crash-safe database writes.

Missing fault isolation includes: (a) no watchdog or supervisor process to detect and restart crashes; (b) no health-check endpoint for external monitoring; (c) no graceful degradation (e.g., continuing ingest if processing fails, or continuing processing if WebSocket broadcast fails); (d) no circuit breaker for external API calls (Space-Track, N2YO) that fail persistently.

For a production deployment, the minimum requirements are: process supervision (systemd, Kubernetes liveness/readiness probes), structured logging with alerting on error patterns, per-component fault isolation (separate processes or at minimum separate asyncio task groups with independent error handling), and health-check endpoints for external monitoring.

---

### Concern 16: Computational Cost of Numerical Integrator Transition

*Replacing SGP4 with a numerical integrator (RK4/5 + force models) increases per-object compute cost by 1–2 orders of magnitude. Has a computational budget analysis been performed for the production catalog size?*

**Response:**

No formal computational budget analysis has been performed. The estimate of 1–2 orders of magnitude increase is based on published benchmarks for numerical orbit propagation versus SGP4, and represents the ratio of a single RK4/5 integration step (with J₂–J₆ geopotential evaluation, atmospheric density lookup, and solar radiation pressure computation) to a single SGP4 analytical evaluation.

A rough scaling estimate: if the current SGP4-based processing costs approximately 100 ms per object per cycle (including all overhead), a numerical integrator might cost 1–10 seconds per object per cycle depending on force model complexity, integration step size, and the 30-minute propagation interval. At 30,000 objects, single-threaded processing would require 8–83 hours per cycle — clearly infeasible within a 30-minute cycle.

The production architecture must therefore employ per-object parallelism. With 100–1,000 parallel workers (achievable on modern multi-core servers or small compute clusters), the per-cycle wall-clock time for 30,000 objects at 10 seconds per object would be 300–3,000 seconds (5–50 minutes). This is within the 30-minute cycle for the lower end of the range but may require either (a) faster integration (GPU-accelerated force model evaluation), (b) adaptive step-size control to reduce integration cost for objects in near-circular orbits, or (c) tiered processing (high-priority objects at full cadence, lower-priority objects at reduced cadence).

A formal computational budget study is identified as a prerequisite for the production architecture design. It should include profiling of the numerical integrator under representative force model configurations, memory bandwidth analysis for concurrent filter state, and I/O throughput analysis for the chosen database backend.

---

### Concern 17: Adaptive Q Estimation Convergence and Stability

*The proposed EM-based adaptive Q estimation (POST-002) requires convergence from the NIS time series. What is the expected convergence time? Is the estimator stable under non-stationary conditions (e.g., solar cycle variations in atmospheric drag)?*

**Response:**

Convergence time and stability under non-stationarity have not been characterized because adaptive Q estimation has not been implemented. The following is a theoretical assessment based on published literature.

EM-based Q estimation (Mehra 1972, Myers and Tapley 1976) typically requires O(10–100) observation cycles to converge, depending on the signal-to-noise ratio and the dimensionality of Q. For the current 6×6 diagonal Q with a 30-minute observation cycle, convergence on the order of 5–50 hours is expected. This is acceptable for steady-state operation but means the estimator cannot track rapid changes in process noise (e.g., the onset of a geomagnetic storm that dramatically increases atmospheric drag within hours).

Non-stationarity is the fundamental challenge. Atmospheric drag varies with the solar cycle (11-year period), geomagnetic storms (hours to days), and seasonal/diurnal density variations. A fixed-window EM estimator that averages over too long a history will smooth out transient drag increases; one that uses too short a history will produce noisy Q estimates. The standard approach is an exponentially weighted moving average (EWMA) variant of the EM estimator, where older innovations are downweighted with a forgetting factor. Tuning the forgetting factor introduces a new hyperparameter with its own sensitivity characteristics.

The proposed implementation would start with a conservative approach: a sliding-window EM estimator with a window length of 20 observation cycles (the current NIS history buffer size), with the option to extend or shorten the window based on observed convergence behavior. Formal stability analysis under simulated non-stationary drag conditions would be conducted as part of the implementation validation.

---

### Concern 18: HMM Research Direction Feasibility

*The bioinformatics HMM analogy is intellectually interesting but has not been demonstrated in an orbital mechanics context. What evidence supports the claim that this approach will outperform the current threshold-based classifier?*

**Response:**

There is no empirical evidence from this effort. The HMM analogy is presented as a proposed research direction, not a validated result. The structural soundness of the analogy — that TLE generation is a stochastic emission process from a hidden osculating state, analogous to sequence emission from hidden biological states — is defensible at the mathematical level. However, mathematical analogy does not guarantee practical performance advantage.

The specific conditions under which the HMM approach would be expected to outperform the threshold-based classifier are: (a) when the TLE error correlation structure produces NIS autocorrelation that degrades the chi-squared threshold's calibration (the HMM explicitly models temporal correlation); (b) when anomaly signatures are subtle enough that single-threshold detection has poor sensitivity but multi-cycle pattern matching (the HMM's strength) can accumulate evidence over time; (c) when the three anomaly classes have sufficiently distinct innovation sequence signatures that generative models can discriminate between them.

The conditions under which the HMM approach might not provide material advantage are: (a) if TLE errors are approximately independent at the 30-minute observation cadence (making the correlation model unnecessary); (b) if the anomaly signatures are large enough that single-threshold detection is adequate (as appears to be the case for ISS reboost events in the POC); (c) if the training data for the competing HMMs is insufficient to learn reliable generative models (ground-truth maneuver data scarcity is a limiting factor).

A rigorous feasibility assessment would require: (1) autocorrelation analysis of the NIS time series to determine whether temporal correlation is material, (2) simulation studies comparing HMM-based classification against threshold-based classification under controlled conditions, and (3) sensitivity analysis of HMM performance to training data quality and quantity. This work is proposed but unfunded.

---

### Concern 19: ITAR and Export Control Compliance at Scale

*The POC uses publicly available TLE data and claims ITAR awareness. As the system scales to include classified sensor data and operational deployment, what is the export control compliance strategy?*

**Response:**

The POC's ITAR posture is minimal and appropriate for its scope: it ingests only publicly available, unclassified TLE data from Space-Track and N2YO, does not redistribute raw TLEs, and does not process any classified or CUI data. The Space-Track account registration includes acknowledgment of export-control terms.

For production deployment with classified sensor data, the export control compliance strategy would need to address: (a) classification guide development — determining the classification level of derived products (filter states, conjunction assessments, anomaly alerts) when inputs include classified sensor data; (b) ITAR registration if the system or its outputs constitute defense articles under USML Category XV (spacecraft systems and associated equipment); (c) Technology Control Plan (TCP) development for any international collaboration or data sharing with allied nations; (d) software classification review to determine whether the algorithms themselves (UKF, NIS, conjunction screening) constitute controlled technical data when implemented for SSA applications.

The algorithmic components (UKF, NIS test, SGP4 propagation) are well-established in the open literature and are not individually export-controlled. However, their integration into an operational SSA system with classified inputs may constitute a controlled defense service or technical data package. This determination requires legal review by export control counsel, which has not been conducted for the POC.

A formal export control compliance plan would be developed as part of any Phase II effort that involves classified data access.

---

### Concern 20: Operational Relevance — What Does This POC Actually Prove?

*Setting aside the production vision, what specific, defensible claims can be made about the POC as demonstrated? What has been proven versus what has been asserted?*

**Response:**

This is the appropriate summary question. The following distinguishes demonstrated claims from asserted claims:

**Demonstrated (supported by implementation and observation):**

1. A per-object UKF can be maintained continuously across a 75-object VLEO catalog using TLE-derived state vectors as observations, with a single-process Python backend, without systematic filter divergence over weeks of operation.
2. The NIS consistency test, applied at the chi-squared 6-DOF p=0.05 threshold with an ISS-calibrated R matrix, produces anomaly detections that qualitatively correspond to known orbital events (ISS reboost sequences, Starlink station-keeping maneuvers) within one observation cycle.
3. Anomaly-triggered conjunction screening can be coupled to the detection pipeline and executed within the same processing cycle, providing immediate re-screening at the moment of detection.
4. A browser-based telemetry interface can present filter state, anomaly alerts, and conjunction risks in real time via WebSocket, with obtrusive alerting designed for extended monitoring.
5. Dual-source TLE ingestion (Space-Track primary, N2YO fallback) with per-row provenance tagging provides availability redundancy and the data infrastructure for per-source noise characterization.

**Asserted but not validated:**

1. The detection performance (false-positive rate, false-negative rate, detection latency distribution) meets any specific operational threshold — no formal performance characterization has been conducted.
2. The architecture scales to production catalog sizes (30,000+ objects) without fundamental redesign — the filter topology is modular, but scaling has not been tested.
3. The Q and R matrices are appropriately tuned for the full catalog — they are calibrated against ISS only.
4. The anomaly classifier correctly discriminates maneuver, drag anomaly, and filter divergence across all orbital regimes — the ECI-frame heuristic has known limitations and no confusion matrix has been produced.
5. The HMM-based approach to correlated observation noise will outperform the current threshold-based classifier — this is a proposed research direction with no implementation or empirical validation.
6. The closed-loop architecture provides material operational advantage over existing SSA systems — no A/B comparison with operational systems has been conducted.

The POC's defensible contribution is demonstrating the feasibility of the closed-loop integration pattern (continuous recursive estimation + automated anomaly detection + anomaly-triggered conjunction screening + real-time operator alerting) at proof-of-concept scale. The contribution is architectural, not algorithmic — the individual components are well-established; the integration is the novel element. Advancement beyond TRL 3–4 requires the formal validation activities identified in Section 10 and this appendix.
