# n-body SSA Platform — Engineering Log

Chronological record of development sessions, real-world events, and observations.

---

## 2026-03-29 — Day 2 (morning session)

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
- Duplicate anomaly history entries in info panel (same event shown 3× with identical timestamp/NIS)
- CLAUDE.md demo command had wrong --delta-v (0.5 vs. 5.0)
- Globe imagery: Ion "Upgrade for commercial use" banner (TD-026)
- Uncertainty cone scale factor hardcoded (TD-025)

---

## 2026-03-30 — Day 3 (morning observation)

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

**Significance:** Establishes a pattern of real-world detections. Two autonomous events in two consecutive nights, both correctly classified and resolved. This constitutes the first multi-event validation dataset for the system.

**Conjunction screening:** Panel shows "No conjunctions within 5 km / 10 km in next 90 min — Resolved (timeout)" — conjunction cleared cleanly post-recalibration.

**NIS chart observation:** Chart shows yesterday's spike (~03:00), clean flat residuals throughout the day, and the new spike appearing at the far right. The visual narrative is exactly the intended demo contrast — the system catching what static SGP4 would miss.

**Also noted:** Duplicate anomaly history bug confirmed still present — 2026-03-29 03:11 entry appears twice with identical NIS=247.2. Open thread #1, needs investigation before demo.

---

### Day 3 work initiated

- Documentation set scoped (plan: 2026-03-29-documentation-set.md)
- Target: P1 documentation sprint (CONOPS, Algorithmic Foundation, System Architecture, API Spec)
- SBIR targets: Space Force (SpaceWERX/AFWERX) and NASA SBIR
- Exit strategy: acquisition by Accenture Federal Services (AFS)
