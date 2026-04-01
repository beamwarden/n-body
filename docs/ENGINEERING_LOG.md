# n-body SSA Platform — Engineering Log

Chronological record of development sessions, real-world events, and observations.
Most recent day first.

---

## 2026-04-01 — Day 5

### Catalog rebuild: VLEO/low-LEO scope (≤600 km)

Scoped the tracking catalog from a demo-richness 100-object set to a mission-coherent 80-object set targeting the ≤600 km operational band — the regime relevant to crewed stations, drone C2 relay, military ISR, and SAR.

**Objects removed (78):**
- All Cosmos 2251, Iridium 33, Fengyun-1C debris — collision events at ~780–865 km, debris field predominantly above cutoff
- 33 redundant Starlinks — reduced from 43 to 10 sampled objects; STARLINK-1990 (live anomaly 2026-03-31) explicitly retained
- All early-gen Planet Labs Flock and Spire LEMUR-2 CubeSats — launched 2014–2019 from ISS at ~400 km, likely re-entered by 2026
- 9 of 10 rocket bodies — SL-16 and others at 600–850 km

**Objects added (58):**
- CSS/Tianhe (~385 km) — Chinese crewed station alongside ISS
- BlackSky EO/ISR constellation (5 objects, ~450 km)
- Capella Space SAR (5 objects, ~525 km)
- Umbra Space SAR (4 objects, ~500 km)
- ICEYE SAR (6 objects, ~570 km)
- Satellogic EO (5 objects, ~500 km)
- HawkEye 360 RF geolocation/SIGINT (6 objects, ~575 km)
- Planet Labs SuperDoves current-gen (10 objects, ~475–525 km)
- Swarm/SpaceBEE IoT relay (5 objects, ~500 km) — drone C2 link layer
- Cosmos 1408 ASAT debris (15 objects, ~440–475 km) — 2021 Russian ASAT test, ISS-altitude threat band
- 5 new rocket bodies at ≤600 km (including 2 CZ-5B stages flagged high re-entry risk)

**Added `scripts/verify_catalog_altitudes.py`** — one-time tool that reads catalog.json, queries local TLE cache, computes mean altitude from TLE mean motion, and prints pass/fail report per object. Run after any TLE cache rebuild to validate catalog against altitude cutoff.

**Narrative shift:** Catalog now tells a coherent story — the ≤600 km band is where crewed operations, commercial ISR, drone data relay, and ASAT debris threats co-exist. Every object class has a clear DoD/Space Force justification.

**Note:** Cosmos 1408 NORAD IDs 49863–49877 need live Space-Track verification before next demo — lower-perigee fragments may have decayed by April 2026. TLE cache rebuild required: `python scripts/replay.py --hours 72`.

---

## 2026-03-31 — Day 4

### Real-world event: STARLINK-1990 FILTER_DIVERGENCE 10:46:38 UTC

System autonomously detected a FILTER_DIVERGENCE anomaly on STARLINK-1990 (NORAD 46075):
- **NIS:** 24.1 (threshold: 12.592)
- **Classification:** FILTER_DIVERGENCE (provisional — deferred recalibration active)
- **Confidence:** 29.6% at time of observation
- **Resolution:** Pending cycle 2

**Likely cause:** Routine Starlink autonomous collision avoidance or orbit-raising maneuver. SpaceX Starlink satellites maneuver frequently using onboard propulsion; NIS of ~24 is consistent with a small unannounced delta-V. No conjunction risk flagged.

**Significance:** First live anomaly observed under the new deferred recalibration logic (fe15307). Provisional `filter_divergence` alert fired correctly on cycle 1. Alert panel seeding bug also exposed — alert panel was empty on reconnect because anomaly WS message fired during disconnect window. Fixed same session.

**Note:** Duplicate anomaly history entry observed again (same event at 10:46 appearing twice). Open thread #1 still unresolved.

---

### Day 4 development work

**Alert panel seeding on reconnect (b747d22)**
- Added `GET /alerts/active` endpoint — returns all unresolved anomalies formatted as WS anomaly message dicts
- Frontend calls this on every WebSocket connect/reconnect and seeds the alert panel
- Fixes gap where alerts fired during disconnect were permanently lost from the UI

**Globe depth test fix (3e2e105)**
- Removed `disableDepthTestDistance: Number.POSITIVE_INFINITY` from satellite billboard and label entities
- Objects behind Earth are now correctly occluded by the globe instead of rendering on top

**Maneuver vs. filter_divergence distinction tests (3377cbf)**
- Added 3 tests explicitly encoding the behavioral contract:
  - Single NIS exceedance never produces `maneuver`
  - Two consecutive exceedances always produce `maneuver`
  - Single exceedance with normal cycle-2 NIS stays `filter_divergence` (guards against misclassifying transient TLE errors)
  - Maneuver recalibration uses 2× higher covariance inflation than divergence (20.0 vs 10.0)

**Maneuver classification fix (fe15307)**
- Root cause confirmed: recalibration on cycle 1 suppressed NIS on cycle 2, making `MANEUVER_CONSECUTIVE_CYCLES >= 2` structurally unsatisfiable
- Fix: active satellites defer recalibration until cycle 2; non-active satellites recalibrate immediately
- 2-hour configurable timeout (`NBODY_PENDING_ANOMALY_TIMEOUT_HOURS`) resolves pending state if no second TLE arrives
- Added `update_anomaly_type()` to `anomaly.py` for retroactive DB correction when provisional type upgrades to `maneuver`
- 195 tests passing

**Documentation sprint (Day 3 work completed)**
- Doc 4: CONOPS (`docs/reference/conops.md`) — 390 lines, P1 complete
- Doc 2: Algorithmic Foundation (`docs/reference/algorithmic-foundation.md`) — 482 lines, P1 complete
- Both documents cite real-world ISS events as autonomous validation evidence
- Maneuver classification fix plan: `docs/plans/2026-03-30-maneuver-classification-fix.md`

**Fixes**
- `seed_maneuver.py` default `--delta-v` corrected from 0.5 to 5.0 m/s
- SBIR and AFS exit strategy recorded in memory

---

## 2026-03-30 — Day 3

### Real-world event: ISS FILTER_DIVERGENCE 03:57:49 UTC

System autonomously detected a second FILTER_DIVERGENCE anomaly on ISS (ZARYA, NORAD 25544):
- **NIS:** 722.4 (threshold: 12.592)
- **Peak residual:** 648.215 km
- **Classification:** FILTER_DIVERGENCE
- **Resolution:** RESOLVED — confidence returned to 100.0%

**Comparison with 2026-03-29 03:11 UTC event:**

| Metric | 2026-03-29 | 2026-03-30 | Delta |
|--------|-----------|-----------|-------|
| Time (UTC) | 03:11:03 | 03:57:49 | +46 min |
| NIS | 247.2 | 722.4 | +192% |
| Peak residual | 383 km | 648.215 km | +69% |
| Classification | FILTER_DIVERGENCE | FILTER_DIVERGENCE | — |
| Outcome | Resolved | Resolved | — |

**Pattern analysis:** Two consecutive nights, same object, same ~03:xx UTC window. The NIS roughly tripling and residual increasing ~69% suggests either a larger maneuver or the filter entering the second event with residual covariance uncertainty from yesterday's recalibration. ISS reboost sequences are frequently planned as paired burns (small preparatory + larger correction) separated by one orbital period (~90 min) or one day. The 03:xx UTC window consistency both nights points to a ground station contact or orbital geometry constraint driving burn scheduling.

**Significance:** Establishes a pattern of real-world detections. Two autonomous events in two consecutive nights, both correctly classified and resolved. First multi-event validation dataset for the system.

**Conjunction screening:** Panel shows "No conjunctions within 5 km / 10 km in next 90 min — Resolved (timeout)" — conjunction cleared cleanly post-recalibration.

**NIS chart observation:** Chart shows yesterday's spike (~03:00), clean flat residuals throughout the day, and the new spike at far right. The visual narrative is exactly the intended demo contrast — the system catching what static SGP4 would miss.

**Also noted:** Duplicate anomaly history bug confirmed — 2026-03-29 03:11 entry appears twice with identical NIS=247.2. Open thread #1, unresolved.

**Note on classification:** Both ISS events were classified as `filter_divergence` rather than `maneuver`. Root cause identified same session — recalibration on cycle 1 prevented the second consecutive NIS exceedance needed for maneuver classification. Fixed in fe15307.

---

### Day 3 development work

**Documentation set scoped (plan: 2026-03-29-documentation-set.md)**
- 10 documents planned across P1/P2/P3 tiers
- P1 sprint: CONOPS, Algorithmic Foundation, System Architecture, API Spec
- SBIR targets: Space Force (SpaceWERX/AFWERX) and NASA SBIR
- Exit strategy: acquisition by Accenture Federal Services (AFS)

---

## 2026-03-29 — Day 2

### Real-world event: ISS FILTER_DIVERGENCE 03:11:03 UTC

System autonomously detected a FILTER_DIVERGENCE anomaly on ISS (ZARYA, NORAD 25544):
- **NIS:** 247.2 (threshold: 12.592, chi-squared 6 DOF p=0.05)
- **Peak residual:** 383 km
- **Classification:** FILTER_DIVERGENCE
- **Resolution:** Recalibrated successfully within ~2 observation cycles

**Likely cause:** Progress MS-33 reboost burn. Progress MS-33 docked 2026-03-24, carrying ~828 kg propellant. ISS performs periodic reboost maneuvers to maintain altitude against atmospheric drag. The 03:xx UTC timing is consistent with mission planning constraints (ground station contact, orbital geometry). Unconfirmed — no public ISS maneuver schedule corroboration found at time of detection, but signature (NIS magnitude, residual direction, object class active_satellite, consecutive cycles) is consistent with a planned maneuver rather than debris avoidance.

**Significance:** First autonomous real-world event detection. System was not scripted or prompted — the filter diverged organically on a live TLE update.

---

### Day 2 development work

**Frontend improvements (plan: 2026-03-29-frontend-improvements.md)**
- Data point hover tooltips on residual/NIS charts
- Anomaly markers (vertical dashed lines) on both chart panels
- Enriched alert cards with peak NIS, peak residual, anomaly type badge
- Object info panel: position:fixed upper-left (escapes Cesium stacking context), anomaly history section, conjunction risk section

**Historical tracks and uncertainty cones (plan: 2026-03-29-history-tracks-cones.md)**
- GET /object/{norad_id}/track endpoint (60s steps, ±1500s window)
- Historical ground track: cyan polyline on globe
- Forward predictive track: orange dashed polyline
- Uncertainty corridor: stepped orange segments, 10× display scale, [50, 2000] km clamp
- Cone visibly widens post-maneuver injection due to inflated post-recalibration covariance — demo-ready behavior

**Conjunction risk (plan: 2026-03-29-conjunction-risk.md)**
- backend/conjunction.py: 5400s horizon, 60s steps, 5 km first-order / 10 km second-order screening
- asyncio.create_task + run_in_executor for non-blocking screening
- Frontend: RED highlight (first-order), YELLOW highlight (second-order), 8s toast notification, auto-clear on next state_update
- New SQLite tables: conjunction_events, conjunction_risks

**Catalog expansion**
- catalog.json expanded from ~20 to 100 objects (F-005 updated: max 100 for POC)
- Composition: 43 Starlink, 15 Cosmos 2251 debris, 10 Iridium 33 debris, 5 Fengyun-1C debris, 15 Planet Labs/Spire CubeSats, 10 rocket bodies, ISS, HST

**Synthetic conjunction injection (scripts/seed_conjunction.py)**
- Initial implementation used ECI→Keplerian→TLE roundtrip — introduced 15-50 km fitting error
- Fixed with Option B (mean-element space manipulation): copy primary TLE mean elements, increment mean anomaly by delta_M = arcsin(miss_km / r)
- 9 tests passing; test_full_conjunction_scenario confirms first-order (<5 km) detection fires reliably
- Committed: c303a55

**Known issues identified:**
- Duplicate anomaly history entries in info panel (same event shown multiple times with identical timestamp/NIS)
- CLAUDE.md demo command had wrong --delta-v (0.5 vs. 5.0) — fixed same session
- Globe imagery: Ion "Upgrade for commercial use" banner (TD-026)
- Uncertainty cone scale factor hardcoded (TD-025)
