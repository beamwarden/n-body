# ne-body (Near Earth body): Closed-Loop Continuous Space Situational Awareness — Technical Whitepaper
Version: 0.1.0
Status: Draft
Last updated: 2026-04-12

---

## Overview

This whitepaper is a technical capability overview of the ne-body (Near Earth body) Space Situational Awareness (SSA) platform for subject matter experts in orbital mechanics, recursive state estimation, and SSA operations. It is not a marketing document. It describes a proof of concept (POC) that implements closed-loop, continuous orbit determination and anomaly detection for a curated Very Low Earth Orbit (VLEO) catalog, using publicly available Two-Line Element sets (TLEs) as synthetic observations. The intended reader is an engineer or program manager evaluating technical feasibility for an operational SSA deployment.

All quantitative claims in this document are drawn directly from the implemented code, configuration files, and engineering log. Where the POC makes simplifications relative to a production deployment, those simplifications are named explicitly.

---

## Context

The authoritative architecture reference is `docs/reference/system-architecture.md` (version 0.2.0). The mathematical foundation is documented in `docs/reference/algorithmic-foundation.md`. The operational concept is in `docs/reference/conops.md`. This whitepaper consolidates the technically load-bearing elements of those documents for an SME reader who does not need to read the full architecture pack.

---

## 1. Thesis: closed-loop tracking beats static propagation

Standard SSA workflows propagate a TLE forward using the Simplified General Perturbations 4 (SGP4) algorithm and treat the result as the object's predicted state until a new TLE arrives. This procedure is mathematically limited by the **Lyapunov instability** of orbital dynamics. Low Earth orbit (LEO) is a nonlinear dynamical system with a positive Lyapunov exponent: small perturbations in initial conditions — atmospheric drag variability, unmodeled solar radiation pressure, unannounced delta-V events — grow exponentially in time. A TLE accurate to 100 m at epoch can degrade to kilometer-level position error within hours for an object in an active drag regime or following an unannounced maneuver.

The standard response has been to publish updated TLEs more frequently and to improve propagator fidelity. Both approaches have diminishing returns. More frequent TLEs reduce the integration horizon but do not eliminate the instability. Higher-fidelity propagators reduce systematic modeling error but cannot model events that are, by definition, not in the model.

The ne-body platform reframes the problem. Instead of asking "how accurately can we predict position in 72 hours?", it asks "how quickly can we detect that our current prediction is wrong and correct it?" This is a control-systems reframing: the orbit is the plant, TLE updates are noisy sensor measurements, an Unscented Kalman Filter (UKF) is the estimator, and anomaly detection is the alarm. The figure of merit becomes **detection latency**, not prediction accuracy. Detection latency is bounded above by one observation interval (30 minutes at Space-Track rate limits) plus filter processing time (measured ~100 ms per object). Static propagation has no detection latency bound — it fails silently until an analyst manually compares ephemerides.

The system's architectural novelty is not the UKF or the Normalized Innovation Squared (NIS) test, both of which are well established in the orbit determination literature. The novelty is the operational integration: a per-object UKF maintained continuously across a heterogeneous catalog, tested on every new TLE, classified automatically into three anomaly types, coupled to an automatic conjunction screening cascade, and surfaced to an operator through a real-time telemetry dashboard — all within a single processing cycle.

---

## 2. System architecture

The pipeline is:

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

**Backend.** Python 3.11+, FastAPI, `sgp4` (Vallado implementation), `filterpy` (UKF), `astropy` (TEME→GCRS frame rotation), SQLite (WAL mode). Single-process architecture with two asyncio background tasks: the ingest loop (30-minute Space-Track poll interval plus N2YO fallback) and the processing loop (consumes `catalog_update` events and runs the predict-update-anomaly-recalibrate cycle per object).

**Frontend.** Vanilla ES2022 modules, no build step. CesiumJS 1.114 for the 3D globe, D3.js v7 for the residual / NIS timeline. Web Audio API for the audio alarm. All CDN-loaded.

**Storage.** Five SQLite tables: `tle_catalog` (TLE cache with `source` provenance column), `state_history` (per-cycle filter state and NIS), `alerts` (anomaly records), `conjunction_events`, `conjunction_risks`.

**Frame discipline.** SGP4 produces True Equator Mean Equinox (TEME) output, which is rotated to the Geocentric Celestial Reference System (GCRS) via astropy before entering the filter. GCRS is used as the Earth-Centered Inertial (ECI) J2000 equivalent; the frame-tie difference (~20 milliarcseconds) translates to sub-meter position error for LEO — below filter accuracy in the POC. All filter state, all database storage, and all WebSocket messages are in ECI J2000 km and km/s. Earth-Centered Earth-Fixed (ECEF) conversion occurs only in the frontend for Cesium rendering.

### 2.1 IDEF0 system boundary

The two figures below use IDEF0 notation: inputs enter from the left, controls from the top, mechanisms from the bottom, and outputs exit to the right.

**Figure 1 — A-0 context: the full pipeline as a single activity**

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

**Figure 2 — A0 decomposition: ingest → process → alert**

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

## 3. Kalman filter formulation

### 3.1 State vector

```
x = [x_km, y_km, z_km, vx_km_s, vy_km_s, vz_km_s]
```

Six-dimensional position and velocity in ECI J2000. Units: km and km/s.

### 3.2 Process and measurement models

**Process model `fx`.** SGP4 applied to the *previous* TLE (stored in the filter state as `last_tle_line1/last_tle_line2`), propagated from the prior observation epoch to the current observation epoch. Using the previous TLE, not the new TLE, is essential: using the new TLE as the process model would make the predicted state identical to the observation, yielding zero innovation.

**Measurement model `hx`.** Identity. The observation is the full six-element ECI state vector derived from the new TLE via SGP4 + TEME→GCRS rotation. This is a deliberate simplification for the POC (see Section 7 on limitations).

**Sigma points.** Merwe Scaled Sigma Points via FilterPy's `MerweScaledSigmaPoints`, with `n=6`, `alpha=1e-3`, `beta=2.0`, `kappa=0.0`, producing 2n+1 = 13 sigma points per cycle.

### 3.3 Process noise Q (per object class)

| Object class | Q position diagonal (km²) | Q velocity diagonal ((km/s)²) |
|---|---|---|
| `active_satellite` | 0.25 | 25e-4 |
| `debris` | 1.00 | 1e-4 |
| `rocket_body` | 0.5625 | 4e-4 |

The active-satellite matrix has elevated velocity variance specifically to accommodate maneuver probability: an unmodeled delta-V primarily appears as a velocity residual before propagating into a position residual. Debris has higher position variance to accommodate drag uncertainty with no maneuver capability modeled. Q is hand-tuned (POST-002 open).

### 3.4 Measurement noise R

```
R = diag([900.0, 900.0, 900.0, 2e-3, 2e-3, 2e-3])   # km² and (km/s)²
```

Position variance 900 km² corresponds to a 1-sigma of 30 km. Velocity variance 2e-3 corresponds to ~0.045 km/s 1-sigma. These values were empirically calibrated against observed International Space Station (ISS) TLE-to-TLE prediction error over 30-minute intervals. Tighter R values (e.g., 1 km² position variance) produced perpetual spurious recalibration because normal Space-Track TLE updates for high-drag or maneuvering LEO objects can differ by tens of kilometers from the prior prediction. The 30 km figure is consistent with published Space-Track TLE accuracy studies (Vallado, Crawford 2008).

### 3.5 NIS consistency test

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

### 3.6 Recalibration

On anomaly detection, the filter is re-initialized from the current observation with an inflated covariance (factor 20.0 for maneuvers, 10.0 for drag anomalies and filter divergences). This avoids the pathological case where the filter stubbornly tries to reconcile an inconsistent prior with a valid new observation.

---

## 4. Anomaly classification

`anomaly.classify_anomaly` applies three rules in priority order when NIS exceeds threshold:

1. **`maneuver`** — Active-satellite object class AND NIS exceeds threshold for at least `MANEUVER_CONSECUTIVE_CYCLES = 2` consecutive cycles. A two-cycle deferred confirmation protocol handles this: the first exceedance is stored as pending anomaly state; if the next cycle also exceeds, the classification is upgraded (the first cycle's `state_history` row is retroactively corrected via `UPDATE`).
2. **`drag_anomaly`** — A single NIS exceedance with along-track velocity residual dominating cross-track by a 3:1 ratio and cross-track residual below 1 km. The along-track determination uses the ECI velocity residual direction as a proxy (limitation: proper decomposition requires the RSW — Radial, Along-Track, Cross-Track — frame).
3. **`filter_divergence`** — Any remaining NIS exceedance. Catch-all category.

Each detection writes a row to the `alerts` table with status `active`, broadcasts an `anomaly` WebSocket message, triggers conjunction screening asynchronously, and triggers filter recalibration.

---

## 5. Data ingestion

### 5.1 Space-Track primary path

The Space-Track.org REST API is polled every 30 minutes (`POLL_INTERVAL_S = 1800`) via authenticated HTTPS. The query returns the most recent TLE per requested NORAD ID. The response is validated against the standard modulo-10 TLE checksum on both lines. Valid TLEs are written to the `tle_catalog` SQLite table via `INSERT OR IGNORE` on the unique key `(norad_id, epoch_utc)`. `ingest.py` is the only module in the system permitted to make external network calls for TLE data.

Credentials (`SPACETRACK_USER`, `SPACETRACK_PASS`) are read from the process environment at startup. Every API call is audit-logged with timestamp, response code, and object count.

### 5.2 N2YO supplemental fallback

N2YO.com is an approved supplemental public TLE source under the amended C-001 requirement. After each Space-Track fetch, `ingest.py` computes the set of catalog objects for which Space-Track returned no TLE or whose most recent cached TLE epoch is older than `N2YO_STALE_THRESHOLD_S = 7` days. This set is capped at `N2YO_MAX_REQUESTS_PER_CYCLE = 50` objects per cycle (ordered oldest-first) and queried per-object from the N2YO REST API. Calls are paced at 100 ms between requests, yielding a worst case of ~100 N2YO requests per hour — well under the N2YO free-tier 1,000-requests-per-hour account limit.

Each row in `tle_catalog` carries a `source` column (`space_track` or `n2yo`) recording its provenance. The filter is currently source-agnostic (uniform R matrix regardless of source), but the tagging enables per-source measurement noise calibration as a post-POC activity. If `N2YO_API_KEY` is unset in the environment, the fallback is skipped silently and the system operates Space-Track-only. N2YO failures (HTTP errors, checksum failures, NORAD ID mismatch) produce per-object `None` returns and never propagate out of `ingest.py`.

### 5.3 ITAR awareness

Both Space-Track and N2YO publish unclassified, publicly releasable TLE data. The Space-Track account is registered under acknowledgment of export-control terms at account creation. No classified or Controlled Unclassified Information (CUI) sources are ingested. The system does not redistribute raw TLE data — `GET /catalog` returns processed state summaries, not TLE strings. Raw TLE data remains in the local SQLite cache only.

---

## 6. Tracked catalog (VLEO scope)

The tracked catalog contains **75 verified objects**, all at or below **600 km altitude**. Altitude verification is documented in `data/catalog/altitude_verification_report.txt`. The catalog deliberately targets Very Low Earth Orbit because it concentrates the highest-activity SSA population: crewed platforms (ISS, CSS), the densest commercial smallsat deployments, active commercial radar-imaging constellations, and the fragment populations with the highest operational conjunction risk.

Catalog composition by category:

- **Crewed platforms.** ISS (ZARYA) NORAD 25544, CSS (TIANHE) NORAD 48274. Highest-priority maneuver detection targets; ISS executes regular Progress-delivered reboost sequences.
- **Legacy active.** Hubble Space Telescope (HST) NORAD 20580.
- **Starlink VLEO subset.** STARLINK-24, -25, -26, -1095, -1306, -1571, -1706, -1800, -1965, -1990, and other Starlink entries at or below 600 km. Dense constellation with regular station-keeping.
- **Fragmentation monitoring.** STARLINK-34343 NORAD 64157, fragmented 2026-03-29 at approximately 560 km. No fragment NORAD IDs publicly assigned as of document publication; expected deorbit within weeks to months at that altitude.
- **Commercial imaging.** BlackSky GLOBAL-1 through -5; CAPELLA-2 (SEQUOIA) and CAPELLA-5 through -8; UMBRA-04, -05, -06; ICEYE-X1, -X6, -X7, -X9, -X11, -X14 and additional ICEYE platforms. Economically high-value active commercial remote-sensing assets.
- **Radio-frequency geolocation.** HawkEye 360 HAWK-A (43765), HAWK-B (43794), HAWK-C (43799), HAWK-8A (59443), HAWK-8B (59445), HAWK-8C (59449). Commercial RF intelligence formation flying. Earlier versions of the catalog used legacy labels (`HAWKEYE PATHFINDER`, `HAWKEYE CLUSTER`); the canonical Space-Track names were adopted in April 2026.
- **Planet and Swarm smallsats.** VLEO subset of FLOCK and SpaceBEE entries.
- **Rocket bodies.** CZ-5B and Falcon 9 upper stages. Non-maneuvering, drag-sensitive.
- **Debris.** Cosmos 1408 fragment cloud members from the November 2021 anti-satellite event.

STARLINK-34343 is retained in the catalog as a worked example of fragmentation-event response. When Space-Track and N2YO stop publishing TLEs for the parent, the frontend's 28-day staleness filter eventually removes it from the live globe view while the alert panel retains its historical anomaly records. When fragment NORAD IDs are publicly assigned, they can be appended to `data/catalog/catalog.json` and tracked through the standard pipeline with no code changes.

---

## 7. Operator interface: real-time telemetry dashboard

The frontend is an operator telemetry dashboard, not a demo application. Three design properties distinguish it from a conventional SSA visualization:

**Always-visible state of health.** The header displays a live tracked-object counter (`N TRACKED`) driven by the WebSocket state-update stream and a WebSocket status indicator (`LIVE` / `RECONNECTING`). The operator can see at a glance whether the data pipeline is healthy and how many objects are currently being tracked.

**Event-driven charts.** The residual / NIS chart panel is collapsed by default. It expands only when the operator selects an object that has an active, recalibrating, or historical anomaly — via click on the globe, click on an alert card, or automatically when a new anomaly fires on the currently selected object. This is a deliberate departure from always-visible chart layouts: a quiet period should show a clean globe and an empty alert panel, not empty charts.

**Obtrusive anomaly alerting.** On every live `anomaly` WebSocket message, the dashboard fires two subsystems simultaneously: a Web Audio API three-beep rising tone (660 Hz, 880 Hz, 1100 Hz) via `alertsound.js`, and a fullscreen red flash overlay bearing the object name and anomaly type via `alertflash.js`. A mute toggle in the header silences audio only; the visual flash is always on. Both subsystems fire only on live messages, never on reconnect-seeded historical alerts loaded via `GET /alerts/active`. The debounce is 2 seconds for audio and single-overlay for visual.

**28-day TLE staleness filter.** `main.js` rejects any `state_update` or `recalibration` message whose epoch is older than 28 days, removing the corresponding entity from the Cesium viewer and decrementing the tracked-object counter. This suppresses a visualization artifact where stale TLEs propagated to "now" cluster near the equator due to accumulated SGP4 drift. The filter is visualization-layer only — the backend continues to process stale TLEs if any arrive. STARLINK-34343 is expected to transition through this filter when Space-Track and N2YO both cease publishing TLEs for the fragmented parent.

**Why this matters for SSA operations.** Standard SSA tools surface anomalies through color changes or log lines that assume continuous operator attention. The ne-body dashboard is designed for extended unattended monitoring: an operator who has looked away from the globe is guaranteed to be alerted audibly and visibly within seconds of detection. The alert card click directly flies the camera to the affected object, expands charts, fetches back-and-forward propagated track data, and displays the object info panel — a single click completes the end-to-end workflow from alert to detailed assessment.

---

## 8. Conjunction screening coupling

Standard conjunction assessment is a scheduled batch process (typically daily). The ne-body platform couples conjunction screening to anomaly detection: whenever the filter flags an anomaly, `conjunction.screen_conjunctions` runs in `loop.run_in_executor` (non-blocking on the main event loop), propagating the anomalous object and all other catalog objects at 60-second steps over a 5400-second horizon (90 minutes, approximately one LEO orbital period). First-order risks are catalog objects with minimum separation < 5 km; second-order risks are objects within 10 km of any first-order risk. The results are broadcast as a `conjunction_risk` WebSocket message and enrich the corresponding alert card.

The rationale is operationally motivated: a maneuver is precisely the condition under which conjunction risk may have changed, and re-screening at the moment of detection is more useful than waiting for the next scheduled batch. The POC uses a spherical miss-distance threshold; the DoD/NASA standard RSW pizza-box (1 km radial, 25 km along-track, 25 km cross-track) is a named post-POC activity (TD-027).

---

## 9. Strategic trajectory: toward full-spectrum Space Domain Awareness

### 9.1 Thesis at scale

The ne-body POC proves a specific architectural claim: a closed-loop observe-propagate-validate-recalibrate cycle, realized as a per-object Unscented Kalman Filter (UKF) driven by periodic TLE updates, detects orbital anomalies within a single observation cycle and couples detection to conjunction screening. The POC is intentionally constrained — 75 objects, public TLEs, single-process backend, browser dashboard. The constraint is not architectural. The closed-loop control structure does not change as the system scales. What changes is what feeds it.

This section describes the aspirational system that the ne-body architecture is designed to grow into: a full-spectrum Space Domain Awareness (SDA) Intelligence, Surveillance, and Reconnaissance (ISR) platform whose primary operational mission is the early detection and pre-emption of cascading debris events — Kessler-regime collisions — before they occur.

### 9.2 The Kessler risk is operational, not theoretical

In 1978, Donald Kessler and Burton Cour-Palais characterized the critical debris density threshold above which fragment-generating collisions become self-sustaining — each collision produces fragments that increase the probability of further collisions, eventually rendering entire orbital shells unusable on human-civilization timescales. The Kessler cascade is not a hypothetical future scenario. Three events have moved the trajectory materially:

1. **Iridium 33 / Cosmos 2251 (2009, 789 km).** The first accidental hypervelocity collision between cataloged satellites. Produced ~2,000 trackable fragments and an unknown number of sub-centimeter lethal particles. Both objects were in Space-Track; the conjunction was not screened in advance.
2. **PLA ASAT test (2007, 863 km), NUDOL test (2021, 485 km).** The Chinese FY-1C and Russian COSMOS 1408 anti-satellite events deliberately injected thousands of trackable and hundreds of thousands of sub-trackable fragments into active orbital regimes. COSMOS 1408 debris remains in the ne-body catalog today as a tracked population.
3. **Commercial constellation growth.** Starlink, OneWeb, and planned VLEO broadband systems collectively represent tens of thousands of active satellites in orbits from 340 to 600 km. At this density, unannounced maneuvers, battery failures, and fragment events generate conjunction cascades faster than current scheduled-batch screening can resolve them.

The Kessler threshold is not a sharp boundary — it is a probabilistic regime where fragment-on-fragment collision probability becomes non-negligible without active management. The VLEO shell that ne-body monitors is the highest-risk zone.

**A critical scope distinction: trackable versus lethal non-trackable debris.** The debris environment spans three size regimes with different threat profiles and different tractability:

| Size | Population (est.) | Threat profile | Trackable? |
|---|---|---|---|
| ≥ 10 cm | ~30,000 cataloged objects | Catastrophic collision; generates fragment clouds | Yes — ground radar (Space Fence, USSTRATCOM) |
| 1–10 cm | ~1,000,000 estimated objects | Kill vehicle for active satellites; below radar sensitivity | No — gap in current architecture |
| < 1 cm | Hundreds of millions | Cumulative surface erosion, solar panel degradation, optical sensor damage | No — statistical flux models only |

ne-body operates exclusively in the ≥ 10 cm cataloged domain. The 1–10 cm population is the most hazardous size class for operational satellites in terms of probability-weighted lethality, yet no ground system currently tracks it directly. This is not a gap ne-body closes in the POC — it requires purpose-built high-sensitivity radar or space-based surveillance assets.

The ne-body platform's leverage point is upstream in the Kessler causal chain: every undetected collision between two cataloged (≥10 cm) objects generates thousands of new 1–10 cm fragments and tens of millions of sub-centimeter particles. By detecting anomalous maneuvers, unexpected drag profiles, and conjunction risks in the cataloged population — within one observation cycle, not days later — the platform enables intervention before the parent-body collision occurs. The sub-trackable spray is not the target; preventing the conditions that create it is.

### 9.3 From TLE observation to multi-sensor fusion

The current measurement pipeline treats a Space-Track or N2YO TLE publication as the system's "observation." This is a deliberate POC approximation: a TLE is itself the output of someone else's orbit determination process, not a direct sensor measurement. The measurement noise R matrix absorbs this conflation — the 30 km 1-sigma position variance represents not sensor noise but TLE-to-TLE prediction error.

The production architecture replaces TLE-derived state vectors with observations from purpose-built sensors, each with its own measurement geometry, noise model, and object visibility domain:

| Sensor type | Observable | Typical 1σ | Object visibility | Availability |
|---|---|---|---|---|
| Phased-array radar (e.g., Space Fence) | Range, range-rate, azimuth, elevation | ~10 m range, ~0.1 m/s Doppler | Non-cooperative, all sizes ≥ ~10 cm, all-weather | DoD-controlled; allied data-sharing via CSpOC |
| Optical telescope network | Right ascension, declination (angles-only) | ~1 arcsecond | Sun-lit objects above atmosphere, clear skies | Commercial (LeoLabs optical, ExoAnalytic), partner nations |
| Laser ranging (SLR) | Two-way range | ~1 cm | Cooperative targets with retroreflectors | ILRS network; sparse, high-accuracy |
| RF emission monitoring | Doppler shift, time-difference of arrival (TDOA) | Depends on frequency | Active RF-emitting objects only | Commercial SIGINT; HawkEye 360 RF geolocation |
| Synthetic Aperture Radar (SAR) imaging | Resolved target position | ~1 m | Non-cooperative; all-weather | Commercial (CAPELLA, ICEYE, UMBRA — already in ne-body catalog) |

The ne-body filter architecture accommodates this directly. The UKF measurement function `hx` is already per-object-class parameterized. Extending it per-sensor requires implementing the appropriate partial-observation `hx` (angles-only for optical, range+Doppler for radar) and a calibrated per-sensor R matrix. The closed-loop detect-recalibrate logic does not change. The significant engineering investment is in the sensor adapters and in fusing asynchronous, heterogeneous observations into a consistent measurement timeline.

### 9.4 Object classification across the full threat spectrum

The current catalog uses three `object_class` values: `active_satellite`, `debris`, `rocket_body`. The production catalog extends this across two axes:

**Origin axis — threat characterization:**
- **Allied** — U.S. and partner-nation government and commercial assets. Expected maneuver patterns are, to the extent releasable, known in advance. Anomaly detection on allied objects drives notification to the asset owner.
- **Neutral** — Commercial or civil objects with no adversarial association. Maneuver anomalies drive conjunction re-screening and operator notification.
- **Adversarial** — Objects associated with competitors or hostile actors. An unannounced maneuver by an adversarial object near a high-value allied asset is a different operational event than the same maneuver by a neutral commercial satellite. The same NIS exceedance generates a different alert escalation path.
- **Unknown** — Objects without confident attribution. The system's anomaly detection is attribution-agnostic; the ISR value is that it flags the anomaly before attribution is resolved.

**Type axis — physical characterization:**
- Payload, rocket body, debris — current taxonomy
- **Natural objects** — near-Earth asteroids (NEAs), meteoroids, cometary material. These require a different propagator (heliocentric orbital elements, n-body gravitational model during close approach) but the same closed-loop detect-classify-alert architecture. Monitoring of NEAs and meteoroid streams is currently the domain of NASA's Planetary Defense Coordination Office (PDCO) and ESA's Space Safety Program. Integration of planetary defense tracking into the same operator dashboard that monitors LEO conjunction risk is a natural extension when the system's catalog scope reaches cislunar and heliocentric regimes.

At full scope, the ne-body catalog spans Low Earth Orbit (LEO), Medium Earth Orbit (MEO), Geostationary Earth Orbit (GEO), Highly Elliptical Orbit (HEO), cislunar space, and near-Earth heliocentric objects — a continuum from Kessler-regime VLEO through to planetary defense.

### 9.5 Predictive Kessler mitigation: the ISR mission

The operational mission of the full-spectrum system is not post-event catalog maintenance. It is **pre-event intervention**: detecting the conditions that precede a Kessler-contributing collision and providing decision advantage to operators who can prevent it.

The detection chain works as follows:

1. **Anomaly detection (already implemented).** A satellite in the catalog executes an unannounced maneuver, suffers a battery event, or experiences an anomalous drag regime. The UKF NIS exceeds threshold within one observation cycle (~30 minutes at TLE rates; potentially 90 seconds at radar update rates). The anomaly is classified and the alert fires.

2. **Immediate conjunction screening (already implemented).** The anomalous object's updated state is propagated against the full catalog over a 90-minute horizon. First- and second-order conjunction risks are identified and broadcast to the dashboard.

3. **Extended forward projection (production extension).** A high-fidelity numerical propagator — Runge-Kutta 4/5 integration with J2–J6 geopotential harmonics, NRLMSISE-00 atmospheric drag, solar radiation pressure — propagates not just the anomalous object but all conjunction-flagged objects forward over a 72–96-hour window. This converts conjunction detection from "something happened" to "given this event, here is the probabilistic collision timeline over the next four days."

4. **Sensor tasking generation (production extension).** When the filter identifies an anomaly it cannot resolve — object type `unknown`, classification uncertain, observation data insufficient — the system generates a structured sensor tasking request. The request specifies the target NORAD ID, the required observation type (angles, range+Doppler, optical), and the time window based on orbital geometry. This request is routed to whatever sensor network the deployment has access to: Space Fence scheduling, partner-nation telescope pointing, commercial radar tasking.

5. **Mitigation coupling (production extension).** For debris objects approaching Kessler-contributing conjunction thresholds, the system computes whether the endangered asset has maneuver capability, estimates the delta-V required to exit the conjunction corridor, and generates a maneuver recommendation. This is not autonomous command — it is decision support that compresses the time from detection to operator action from hours to minutes.

6. **Fragment cascade modeling (research extension).** After a fragmentation event (battery failure, ASAT, hypervelocity collision), the system ingests the new fragment NORAD IDs as they are published, applies a ballistic coefficient distribution model to propagate the cloud, and overlays the evolving conjunction risk on the operator dashboard. STARLINK-34343 (NORAD 64157, fragmented 2026-03-29) is the current worked example; the pipeline requires only that fragment NORAD IDs appear in `catalog.json` to process them automatically.

### 9.6 Architectural continuity — and the limits of that claim

The production system described above shares the same filter topology with the POC — predict, update, NIS test, classify, recalibrate, screen conjunctions. At that level, the architecture is unchanged. However, a technically precise account requires naming three layers where structural changes are mandatory, not optional:

**Layer 1 — Process model.** SGP4 must be replaced with a numerical integrator. This is not just a fidelity upgrade; it is required to restore the UKF's mathematical validity. SGP4 is a deterministic analytic trajectory parameterized by mean elements. All 13 sigma points map to identical output states (TD-007), meaning covariance growth is driven entirely by the additive Q matrix rather than by sigma-point divergence through nonlinear dynamics. A Runge-Kutta integrator with a physical force model propagates each sigma point independently, recovering the UKF's designed behavior.

**Layer 2 — Measurement model and state definition.** This is the most significant structural change. SGP4 operates on *mean elements* — a smoothed, averaged representation of the orbital state that absorbs short-period perturbations over a fitting window typically spanning 3–5 days of observations. Raw sensors (radar, optical, lidar) produce measurements corresponding to the *osculating state* — the instantaneous position and velocity at the observation epoch. These are fundamentally different quantities. The mean-to-osculating transformation (Brouwer/Kozai theory) introduces short-period and long-period oscillations with amplitudes of tens to hundreds of kilometers depending on inclination and altitude. In the current POC, the identity measurement function `hx` conflates these representations, and the inflated R matrix (30 km 1-sigma) partially masks the conflation empirically. A production filter must explicitly model the mean-to-osculating transformation as part of `hx`, or redefine the state vector in osculating elements and use a physical integrator consistently throughout.

**Layer 3 — Noise tuning.** The current Q and R are calibrated against TLE generation artifacts, not against physical sensor noise. Q absorbs unmodeled dynamics in the mean-element space; R absorbs TLE-to-TLE fit residuals that accumulate from the smoothing process. When the measurement model changes to raw sensor observations, both matrices must be rederived from scratch against measured sensor noise characteristics. The current parameter values will not transfer.

The correct claim is: the filter *topology* (predict-update-detect-recalibrate-screen) scales to production without redesign. The filter *parameterization and measurement model* require well-characterized, bounded rework at Layers 1–3. This is a material engineering effort, not a configuration change, and it is acknowledged as such.

| POC today | Production addition |
|---|---|
| TLE ingest (Space-Track, N2YO) | Sensor adapters: radar, optical, lidar, SIGINT |
| SGP4 mean-element process model | Numerical integrator (RK45 + force models), osculating state |
| Identity measurement function `hx` | Per-sensor partial-observation `hx` with mean→osculating correction |
| Hand-tuned Q and R (TLE empirics) | Physically derived Q and R from sensor calibration data |
| 75-object VLEO catalog | Full LEO/MEO/GEO/HEO/cislunar catalog |
| 90-minute conjunction horizon | 72–96-hour probabilistic conjunction timeline |
| NIS anomaly detection → alert | NIS detection → sensor tasking → mitigation recommendation |
| Public TLE sources | Classified sensor data (CUI/S+SI, air-gapped deployment) |
| Local SQLite | Distributed time-series database (InfluxDB or TimescaleDB) |
| Single-process backend | Parallel-processing pipeline (asyncio + ProcessPoolExecutor per catalog segment) |
| Browser dashboard | Multi-seat ops center display; exportable to C2 systems |

### 9.7 A bioinformatics pathway for correlated observation noise

The Markov assumption at the heart of the Kalman filter states that the current state fully encapsulates all information relevant to predicting the next state — past observations carry no additional signal once the current state is known. TLEs violate this assumption structurally. A TLE is not a direct measurement; it is the output of a batch orbit determination process that smooths observations over days into a single mean-element set. The "noise" in successive TLE publications is therefore temporally correlated: two TLEs generated from overlapping observation windows will share fitting residuals, producing autocorrelated measurement errors that the standard Kalman filter treats as independent.

The field of computational genomics has confronted an analogous structure for decades. DNA and protein sequences are not Markov — the emission at any position depends on a hidden biological state (gene vs. intergene, coding frame, regulatory region) that persists across positions and creates long-range correlations in what is observed. The solution is the **Hidden Markov Model (HMM)**: the observable sequence is modeled as emissions from a hidden state sequence that *is* Markov, making the joint model tractable while the observation model captures the correlation structure.

The analogy maps directly to orbital mechanics:

| Bioinformatics HMM | Orbital mechanics equivalent |
|---|---|
| Hidden state sequence | True osculating orbital state trajectory |
| Markov process over hidden states | Physical dynamics (integrator + force model) |
| Emission probability | TLE generation process: mean-element smoothing + fit residuals |
| Observed sequence | Sequence of TLE publications |
| Profile HMM | Per-object-class nominal behavior model |
| Log-likelihood anomaly score | NIS test (chi-squared under Gaussian emission assumption) |
| Viterbi decoding | Rauch-Tung-Striebel (RTS) smoother: retrospective most-likely state |
| Baum-Welch parameter learning | Expectation-Maximization (EM) for online Q and R estimation |

The practical consequence of this framing for ne-body is a three-part near-term upgrade:

**1. Explicit emission model for TLE generation.** Rather than treating the TLE-derived state as a direct noisy observation of the osculating state, model it as an emission from the true osculating state through the known mean-to-osculating transformation. The Brouwer short-period corrections are analytic; implementing them as part of `hx` makes the measurement model physically correct. This addresses Layer 2 above and is implementable without a full sensor architecture change — it improves the POC's fidelity using TLEs as inputs.

**2. EM-based adaptive noise estimation (addressing POST-002).** The Baum-Welch algorithm is the EM algorithm applied to HMM parameter learning. Its orbital mechanics equivalent — the Adaptive Estimation algorithm due to Mehra (1972), later formalized by Myers and Tapley — iteratively estimates Q from the filter's innovation covariance history. Specifically:

```
Q̂ = (1/N) Σ [Kₜ yₜ yₜᵀ Kₜᵀ] + P - F P F ᵀ
```

where K is the Kalman gain, y is the innovation sequence, P is the posterior covariance, and F is the state transition Jacobian. This replaces the hand-tuned Q matrices with a data-driven estimate that converges to the true process noise as the filter accumulates innovation history. The existing NIS history buffer (last 20 values) is the seed data structure for this computation.

**3. Competing HMMs for anomaly classification.** Bioinformaticians detect CpG islands — genomic regions with anomalous dinucleotide composition — by comparing log-likelihoods under two HMMs: one trained on background sequence, one trained on CpG island sequence. The region is classified by the model whose likelihood dominates. The ne-body classifier currently uses a deterministic NIS threshold with hand-coded heuristics to distinguish maneuver, drag anomaly, and filter divergence. A principled replacement is three competing regime HMMs:

- **Nominal operations model**: trained on steady-state NIS and innovation time series for each object class
- **Maneuver model**: trained on NIS and innovation signatures from known maneuver events (ISS reboost sequences in the historical catalog)
- **Drag anomaly model**: trained on along-track-dominated innovation signatures from known drag events

The anomaly is classified by the model under which the observed innovation sequence has the highest posterior probability. This replaces the brittle 3:1 ratio heuristic (TD-011) with a probabilistic discriminator that generalizes across inclinations, altitudes, and object classes — directly addressing the reviewer's concern about misclassification for high-inclination and high-eccentricity orbits.

### 9.7 Why this matters now

The Kessler-mitigating ISR mission is time-limited. The VLEO fragment populations from COSMOS 1408, FY-1C, and commercial failures are decaying — most will reenter within a decade. The commercial VLEO constellations are launching at rates that will fill this window with active, maneuvering objects before the legacy debris clears. The window in which a continuous closed-loop SSA system can meaningfully characterize the full VLEO population — before it becomes unmanageably dense — is the next five to ten years. The ne-body architecture is designed to operate in that window, and to scale into the operational system that remains relevant after it closes.

---

## 10. Current limitations and roadmap (POC scope)

The POC is honest about the gaps between its current scope and an operational system. The following limitations are acknowledged and tracked in the development team's internal issue-tracking system. References of the form `TD-NNN` denote entries in the internal tech-debt register; references of the form `POST-NNN` denote post-POC backlog items. These identifiers are included for traceability and are not part of any external specification.

**SGP4 sigma-point collapse (TD-007, POST-003).** Because SGP4 is a deterministic analytic trajectory propagator parameterized by TLE mean elements, not an ODE-based force model, all 13 UKF sigma points map to identical output states. Covariance growth in the predict step is therefore driven entirely by the additive process noise matrix Q rather than by sigma-point divergence through nonlinear dynamics. The principal advantage of the UKF over the EKF is not exercised. Production fix: replace SGP4 with a numerical integrator (Runge-Kutta + J2/J4 geopotential, NRLMSISE-00 atmospheric drag, solar radiation pressure) as the process model, allowing each sigma point to propagate independently from its perturbed initial condition.

**TLE-as-synthetic-observation (simulation fidelity boundary).** In the POC, TLE publications serve simultaneously as filter process-model input and as "observation" input. Real sensors — radar, optical, Space Fence — produce partial observations (angles, range, Doppler), not full six-element state vectors. Treating TLE-derived states as direct observations conflates the batch orbit determination that produced the TLE with a direct measurement. The `ingest.py` → `kalman.py` boundary is clearly documented so reviewers understand this approximation. A production deployment with real sensor data would implement source-specific measurement functions `hx` and source-specific R matrices; the existing per-row `source` tag in `tle_catalog` is the prototype of the multi-source provenance scheme.

**Hand-tuned process noise Q (POST-002).** The per-object-class Q matrices are hand-calibrated empirically. A production system requires adaptive process noise estimation (SAGE-Holt method or innovation-based covariance estimation) that adjusts Q online to maintain filter consistency as measured by the NIS history.

**Uniform R matrix regardless of source.** The 900 km² position variance is calibrated against ISS TLE-to-TLE error and applied to all object classes and both TLE sources. Per-class and per-source R tuning is a post-POC activity enabled by existing catalog metadata.

**Drag anomaly classifier uses ECI velocity as along-track proxy (TD-011).** Proper classification requires RSW frame decomposition using the object's actual orbital velocity vector. The current heuristic may misclassify for high-inclination or high-eccentricity orbits.

**Sequential per-object processing (TD-017).** The processing loop iterates catalog objects serially. At 75 objects and ~150 ms per object, a full catalog pass takes ~11 seconds — well within the 1800-second inter-cycle window. Catalogs beyond ~500 objects would require `ThreadPoolExecutor` or `asyncio.gather`-based parallelism.

**Conjunction screening spherical threshold (TD-027).** First-order 5 km / second-order 10 km spherical miss-distance thresholds produce more false positives than the DoD/NASA standard asymmetric RSW volume. Production fix: RSW frame decomposition with asymmetric thresholds.

**No authentication (TD-019).** The REST and WebSocket endpoints have no authentication. Acceptable for local demonstration, unacceptable for networked deployment. Production fix: OAuth2/JWT bearer tokens with three-role RBAC (viewer / analyst / operator).

**In-memory filter state lost on restart.** `app.state.filter_states` is not persisted; uvicorn restart forces a 2–5 cycle cold-start convergence period for every object. The `state_history` table captures the necessary information to reconstruct filter state on startup; this reconstruction is not yet implemented.

**Roadmap.** The post-POC roadmap prioritizes (in decreasing order of impact on operational relevance): (1) numerical integrator replacing SGP4 as the UKF process model; (2) adaptive Q estimation; (3) persistent filter state across restarts; (4) RSW-decomposed drag anomaly classifier and conjunction screening; (5) per-source R calibration; (6) authentication and RBAC; (7) multi-source sensor integration with per-row classification propagation. Items (1)–(4) are mathematically substantive improvements; items (5)–(7) are productionization gates.

---

## 11. Summary

The ne-body platform demonstrates that a recursive state estimator maintained continuously across a heterogeneous catalog can detect maneuvers, drag anomalies, and filter divergences within a single TLE update cycle, classify them autonomously, trigger recalibration, and couple to conjunction screening — capabilities that are not present in standard TLE-centric SSA workflows. The POC is scoped to 75 VLEO objects, uses Space-Track.org as primary and N2YO.com as supplemental public TLE sources, treats TLE publications as synthetic observations for the filter, and delivers results to a real-time telemetry dashboard with obtrusive audio and visual anomaly alerting. The mathematical machinery — UKF, NIS test, chi-squared threshold at 12.592 for 6 DOF — is established. The novel contribution is the closed-loop operational architecture and its integration with an operator-facing dashboard. The limitations are named, the production roadmap is concrete, and the TLE-as-synthetic-observation simulation boundary is documented. The system is ready for technical evaluation by SSA program managers, orbital mechanics reviewers, and engineers assessing the path from POC to an operational deployment with real sensor data.

---

## Cross-references

- `docs/reference/system-architecture.md` — authoritative architecture (version 0.2.0)
- `docs/reference/algorithmic-foundation.md` — mathematical basis (UKF, NIS, classifier)
- `docs/reference/conops.md` — operational concept and demo scenario
- `docs/reference/api-spec.md` — REST and WebSocket schemas
- `docs/requirements.md` — functional and non-functional requirements (amended C-001 permits N2YO)
- `backend/kalman.py`, `backend/propagator.py`, `backend/anomaly.py`, `backend/ingest.py` — primary implementation modules
- `data/catalog/catalog.json` — 75-object VLEO tracked catalog
- `data/catalog/altitude_verification_report.txt` — per-object altitude verification
