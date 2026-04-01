# Engineering Log — ne-body SSA Platform

Narrative record of daily progress, decisions, and open threads.
Most recent entry first.

---

## 2026-03-29

### End of session state
159 tests passing. Demo is visually compelling end-to-end. All three new visualization
features are working: anomaly history in the object info panel, historical ground track
(cyan), and predictive forward track with uncertainty corridor (orange). The real-world
ISS anomaly from overnight provided a live demonstration of the full detection pipeline.

### Real-world anomaly detection
At 03:11:03 UTC, the system autonomously detected a FILTER_DIVERGENCE on ISS (NORAD
25544) with NIS=247.2 and peak residual ~383 km. This almost certainly corresponds to
an ISS reboost — Progress MS-33 docked March 24 carrying 828 kg of reboost propellant,
and the timing aligns with a planned post-docking altitude adjustment. Web search
confirmed Progress MS-33 carried reboost propellant but no specific March 28/29 burn
announcement was found. The exponentially-growing residual shape (classic post-maneuver
signature) and NIS=247 strongly support a real orbital change rather than a data artifact.
The system detected this before any public NASA announcement — strong demo talking point.

### Frontend improvements (four phases shipped)
**Hover tooltips** — both residual (km) and NIS chart dots now show epoch, residual,
and NIS on hover via invisible r=8 hit-target circles overlaid on visible r=3 dots.

**Anomaly event markers** — red dashed vertical lines on both charts at the anomaly
epoch, labeled MNV/DRG/DIV. Persists across object switches via anomalyMarkerStore.

**Alert card enrichment** — peak NIS and peak residual magnitude now shown on each
card. Alert lifecycle is now 3-state (ACTIVE → RECALIBRATING → RESOLVED) with accurate
time-to-resolution. 5-minute auto-resolve timeout guards against stuck alerts.

**Object info panel** — floating card in the upper-left of the globe (position: fixed,
z-index: 100) triggered by globe click or alert card click. Shows NORAD ID, name,
object class, ECI position/velocity, confidence, last update epoch, and scrollable
anomaly history section fetched from the new `/object/{norad_id}/anomalies` endpoint.
Panel was initially clipped by Cesium's stacking context — fixed by switching from
`position: absolute` to `position: fixed`.

### Historical track and uncertainty cone
New `GET /object/{norad_id}/track` endpoint back- and forward-propagates the latest
cached TLE using SGP4 at 60s steps (25 back + 25 forward = 50 total calls). Reference
epoch uses the filter's `last_epoch_utc` if initialized, otherwise falls back to the
TLE epoch. Forward track includes `uncertainty_radius_km` from filter covariance grown
linearly by `P + Q * (t / 1800)`.

Frontend renders: cyan polyline for historical track, orange dashed polyline for
forward track, stepped orange corridor for uncertainty cone (Option A — segmented
corridors at increasing width). A 10× display scale factor is applied to the corridor
width so it's visible at full-Earth zoom (~35,000 km altitude). Clamped to [50, 2000] km
per segment.

The demo money shot: inject `--delta-v 5.0`, click ISS immediately after the anomaly
fires. The cone visibly widens (inflated post-recalibration covariance) and the forward
track is displaced from the historical track, showing the maneuver signature clearly.
This is the hurricane-cone analogue for orbital uncertainty.

### .env / python-dotenv setup
Added `load_dotenv()` to `main.py` and `python-dotenv` to requirements. Credentials
now persist across backend restarts via `.env` file in repo root. `.gitignore` created
to exclude `.env`, SQLite WAL files, and standard Python artifacts. `.env.example`
committed as a template.

### Anomaly history endpoint
New `GET /object/{norad_id}/anomalies` endpoint — returns last 20 events from the
`alerts` SQLite table for a given NORAD ID, ordered newest-first, with resolution
epoch and recalibration duration. Read-only. Frontend fetches on object selection
and appends a scrollable history section to the info panel asynchronously (panel
renders immediately, history appends when fetch completes).

### Bug fixes
- `topGroup is not defined` in residuals.js — variable was used in anomaly marker
  rendering but not included in the chartState destructure. Added `topGroup, bottomGroup`
  to the destructure block.
- Info panel clipped by Cesium — switched from `position: absolute` to `position: fixed`
  with `z-index: 100`.
- Alert panel moved to top of side panel (above residual chart) per user request.
- `latestStateMap` now pre-seeded from `GET /catalog` response so info panel shows
  name/class immediately on first click, even before any WS messages arrive.
- `OpenStreetMapImageryProvider` deprecated in Cesium 1.104+ — switched to
  `UrlTemplateImageryProvider`, then reverted to Ion default after tile rendering
  issues with offline alternatives. TD-026 filed for globe imagery configurability.

### Open threads going into next session
- Three anomaly history entries in the panel all show same timestamp/NIS — likely
  duplicate rows being inserted in the `alerts` table. Needs investigation.
- `--delta-v 0.5` still doesn't cross the anomaly threshold (need 5.0 for demo).
  CLAUDE.md demo command needs updating.
- Globe imagery still Ion default with "Upgrade for commercial use" banner.
  TD-026 filed. Options: pay for Ion, use a self-hosted tile server, or find a
  Cesium 1.114-compatible free provider.
- Uncertainty cone scale factor (10×) and minimum width (50 km) are hardcoded.
  TD-025 filed for making step_s user-configurable; same applies to cone scaling.
- Real-world ISS maneuver correlation — worth monitoring NASA ISS blog for
  confirmation of March 28/29 reboost to validate detection.

---

## 2026-03-28

### End of day state
All 140 tests passing. Demo runs end-to-end: globe loads, object markers visible,
maneuver injection produces a residual spike and NIS elevation. Anomaly detection
fires correctly when `--delta-v 5.0` or larger is used.

### Zero-NIS bug (root cause + fix)
Spent the bulk of the session tracking down why NIS was always 0 in the residual
panel. There were three independent causes stacked on top of each other.

**Cause 1 — zero innovation.** The predict step and the observation were both
derived from the same TLE. Since the UKF process model is SGP4 (a trajectory model,
not a force model — see POST-002 in kalman.py), all sigma points collapse to the
same propagated state. So `y = z - H*x_pred = 0` identically every cycle. The fix
was to store the previous TLE on `filter_state` at the end of each update cycle and
use it for the next predict step. The new TLE is used only as the observation.
This gives innovation proportional to the TLE-to-TLE prediction error (~30 km for
ISS).

**Cause 2 — NIS wiped before message.** `recalibrate()` creates a fresh filter
state via `init_filter()`, which resets `nis=0.0` and `innovation_eci_km=zeros`.
The anomaly WS message was built *after* recalibrate, so it always reported NIS=0
even when the update step had computed NIS=930. Fix: capture `nis_val` and
`innovation_eci_km` from `filter_state` immediately after `kalman.update()` returns,
then override those two fields on the anomaly message after `_build_ws_message`.

**Cause 3 — DEFAULT_R too tight.** Measurement noise R was set to 1.0 km² position
diagonal, implying ~1 km TLE accuracy. Real ISS TLE-to-TLE prediction error is
~30 km (measured by comparing consecutive Space-Track TLEs propagated to the same
epoch). With R=1, NIS ≈ 930 on every normal update, driving the filter into
perpetual recalibration and making the NIS panel useless as a discriminator.
Rescaled R to 900 km² position diagonal (30 km sigma). Normal NIS is now O(1);
a 5 m/s maneuver injection crosses the 12.592 chi-squared threshold cleanly.

### Maneuver detection threshold and demo calibration
After fixing the NIS pipeline, the default `--delta-v 0.5` (m/s) produced NIS ~3-5
but did not cross the 12.592 anomaly threshold. With R_pos=900, a residual of ~65 km
is needed to cross the threshold; a 0.5 m/s maneuver over a 30-minute TLE interval
produces ~33 km. Use `--delta-v 5.0` for the demo. 5 m/s is within the range of
real ISS debris-avoidance and reboost maneuvers, so the narrative holds.

Longer-term: if we want to detect smaller maneuvers (~1 m/s), R needs to be tightened
for well-tracked active satellites (R_pos ~25–100 km²). This is a post-POC calibration
task — the per-object R tuning hook already exists in `init_filter`.

### CORS fix (earlier in session)
The globe was blank on first launch. Root cause: CesiumJS fetched the `/config`
endpoint from `localhost:3000` but the FastAPI server had no CORS headers, so the
browser blocked it. Added `CORSMiddleware` to `main.py` allowing `localhost:3000`
and `127.0.0.1:3000`. Globe appeared immediately after backend restart.

### Propagator test fixes
Two test failures needed resolving before the session ended cleanly:
- Removed the bad-checksum test case — `sgp4` silently accepts any TLE regardless
  of checksum; checksum validation is the responsibility of `ingest.py`, not the
  propagator.
- Widened the TEME→GCRS position difference bounds from 20 km to 50 km. The
  ~0.33° precession accumulated over 24 years (J2000 to 2024) legitimately
  produces a 27–30 km frame offset at ISS altitude. The old bounds were too tight
  and failed on correct behavior.

### Architecture decisions logged
- `MANEUVER_CONSECUTIVE_CYCLES = 2` in anomaly.py (conservative; chosen over the
  more aggressive single-cycle trigger to reduce false positives in the demo).
- Entity API used for CesiumJS instead of CZML (TD-024 — CZML DataSource is the
  right long-term approach but requires a build step or CZML writer library).
- `asyncio.Queue(maxsize=10)` internal event bus; `MAX_WS_CONNECTIONS=20`.

### What's working
- Full F-001 through F-063 implemented (ingest, propagator, kalman, anomaly,
  main API, frontend, scripts)
- 140 tests passing
- Demo flow: start backend → serve frontend → `trigger-process` → inject maneuver
  → observe NIS spike and anomaly alert in browser

### Open threads
- `--delta-v 0.5` (the CLAUDE.md default) does not trigger anomaly with R=900.
  Either update the docs to say `--delta-v 5.0` for demo, or tune R per object class.
- Space-Track HTTP 429 backoff not yet implemented (noted in CLAUDE.md known issues).
- UKF process noise Q is hand-tuned; adaptive noise estimation is post-POC.
- CesiumJS Ion token hardcoded in globe.js — must move to env var before any
  public-facing deployment.
