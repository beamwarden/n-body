# Implementation Plan: Scripted Demo Orchestrator (`scripts/demo.py`)
Date: 2026-04-01
Status: Draft

## Summary
Create a single CLI script `scripts/demo.py` that orchestrates a complete 5-minute demo narrative for DoD/Space Force audiences. The script sequences injectable events (conjunction, maneuvers, recalibration) with timed delays and prints presenter-readable narration to stdout. It reuses existing `seed_conjunction.py` and `seed_maneuver.py` injection functions rather than reimplementing injection logic.

## Requirements addressed
- **F-030 [DEMO]** Anomaly detection fires on conjunction screening trigger
- **F-031 [DEMO]** Anomaly classification (maneuver, filter_divergence) visible in demo
- **F-033 [DEMO]** Filter recalibration demonstrated
- **F-042 [DEMO]** WebSocket pushes real-time updates during demo
- **F-050-F-055 [DEMO]** Visualization reflects all events in real time
- **F-061 [DEMO]** Maneuver injection script (invoked programmatically)
- **F-063** Scripts runnable from single terminal command
- **NF-020** Demo launchable without interactive prompts
- **NF-023** Visible browser response within 10 seconds of injection

## Files affected
- `scripts/demo.py` -- **NEW** demo orchestrator script
- `scripts/seed_conjunction.py` -- **NO CHANGES** (imported, not modified)
- `scripts/seed_maneuver.py` -- **NO CHANGES** (imported, not modified)

## Data flow changes
No backend data flow changes. `demo.py` is a pure orchestration layer that:
1. Calls `seed_conjunction.inject_conjunction()` and `seed_maneuver.inject_maneuver()` as library functions
2. POSTs to `POST /admin/trigger-process` between acts to force processing cycles
3. Prints narration text to stdout for the presenter

```
demo.py
  |-- [Act 2] calls seed_conjunction.inject_conjunction(primary=25544, miss_km=1.5, trigger=True)
  |-- [Act 3] calls seed_maneuver.inject_maneuver(norad_id=46075, delta_v=5.0, trigger=True)
  |-- [Act 4] calls seed_maneuver.inject_maneuver(norad_id=47474, delta_v=5.0, trigger=True)
  |-- [Act 5] POSTs to /admin/trigger-process 2-3 times to show recalibration
```

## Implementation steps

### Phase 1: Script structure and CLI

1. **Create `scripts/demo.py` with argparse CLI** (`scripts/demo.py`)
   - Action: Create a new Python script with the following CLI interface:
     ```
     python scripts/demo.py [--act 1|2|3|4|5|all] [--server-url URL] [--delay-s SECONDS] [--db PATH] [--catalog PATH]
     ```
   - Arguments:
     - `--act`: Which act to run. Default `all`. Individual acts can be triggered independently for rehearsal.
     - `--server-url`: Base URL of the running server. Default `http://localhost:8000`.
     - `--delay-s`: Base delay between acts in seconds. Default `15`. Acts use multiples of this value.
     - `--db`: Path to SQLite TLE cache. Default from `NBODY_DB_PATH` env var or `data/catalog/tle_cache.db`.
     - `--catalog`: Path to catalog.json. Default `data/catalog/catalog.json`.
   - Add `sys.path.insert(0, ...)` for project root, same pattern as `seed_maneuver.py` line 41.
   - Why: Single entry point for the entire demo sequence.
   - Dependencies: None.
   - Risk: Low.

2. **Define act functions and narration constants** (`scripts/demo.py`)
   - Action: Define five functions: `act_1_normal_ops()`, `act_2_conjunction()`, `act_3_starlink_maneuver()`, `act_4_isr_maneuver()`, `act_5_resolution()`.
   - Each function:
     - Prints a section header with act number and name (e.g., `"\n{'='*60}\n  ACT 2: ASAT DEBRIS CONJUNCTION WITH ISS\n{'='*60}\n"`)
     - Prints the presenter narration text (see Presenter Script section below)
     - Executes the injection or trigger
     - Prints confirmation of what happened
     - Returns normally (no sys.exit on success)
   - Why: Modular structure allows `--act N` to call individual functions.
   - Dependencies: Step 1.
   - Risk: Low.

### Phase 2: Act implementations

3. **Act 1 -- Normal ops** (`scripts/demo.py`)
   - Action: Print narration only. No injection. Optionally POST to `/admin/trigger-process` once to ensure the browser has current state. Print a reminder to the presenter to select an object and show the flat NIS chart.
   - Narration text: see Presenter Script below.
   - Sleep: `delay_s` seconds after narration to let the presenter talk.
   - Dependencies: Step 2.
   - Risk: Low.

4. **Act 2 -- Cosmos 1408 debris conjunction with ISS** (`scripts/demo.py`)
   - Action: Import `seed_conjunction.inject_conjunction` and call it with:
     - `primary_norad_id=25544` (ISS)
     - `offset_min=5.0` (conjunction 5 minutes from now -- close enough to be urgent)
     - `miss_km=1.5` (below the 5 km first-order threshold -- guaranteed red alert)
     - `trigger=True`
     - `server_url` from CLI args
     - `catalog_path` and `db_path` from CLI args
   - After injection, print confirmation including the verification miss distance.
   - Print narration about ASAT debris, crew safety, conjunction screening.
   - Sleep: `2 * delay_s` seconds (let the presenter show the conjunction panel).
   - Why: This is the highest-impact demo moment. Cosmos 1408 ASAT test debris threatening a crewed station is the most DoD-relevant scenario possible. The 1.5 km miss distance is chosen to be well within the 5 km first-order conjunction threshold while remaining physically plausible (real ISS conjunction warnings fire at <1 km).
   - Dependencies: Step 2. Requires `seed_conjunction.py` importable (already verified working).
   - Risk: Medium -- `seed_conjunction` modifies `catalog.json` by adding NORAD 99999. The cleanup step in Act 5 or `--clear` must handle this. If the script crashes mid-demo, `catalog.json` will have an extra entry until manually cleaned (`git checkout data/catalog/catalog.json`).

5. **Act 3 -- Unannounced Starlink maneuver** (`scripts/demo.py`)
   - Action: Import `seed_maneuver.inject_maneuver` and call it with:
     - `norad_id=46075` (STARLINK-1990 -- the one that already fired a real anomaly, per memory)
     - `delta_v_m_s=5.0` (working demo value per conops.md and open_threads.md)
     - `direction="along-track"` (standard Starlink orbit-raising maneuver direction)
     - `epoch_offset_min=0.0` (immediate)
     - `trigger=True`
     - `server_url`, `db_path`, `catalog_config_path` from CLI args
   - Print narration about unannounced satellite maneuvers, prediction loss, adversary relevance.
   - Sleep: `2 * delay_s` seconds.
   - Why: Starlink maneuvers are frequent, real-world events. STARLINK-1990 was specifically chosen because it has prior anomaly history in the system, making the narrative more credible ("we've seen this satellite maneuver before").
   - Dependencies: Step 2.
   - Risk: Low -- `seed_maneuver` does not modify `catalog.json`, only the TLE cache.

6. **Act 4 -- ISR asset maneuver (optional)** (`scripts/demo.py`)
   - Action: Import `seed_maneuver.inject_maneuver` and call it with:
     - `norad_id=47474` (BLACKSKY GLOBAL-7 -- ISR/EO constellation)
     - `delta_v_m_s=5.0`
     - `direction="cross-track"` (inclination change -- suggests collection geometry adjustment)
     - `epoch_offset_min=0.0`
     - `trigger=True`
   - Print narration about ISR satellite repositioning, collection tasking inference, cross-track maneuver significance.
   - Sleep: `2 * delay_s` seconds.
   - Why: Cross-track maneuver on an ISR satellite tells a different story than along-track Starlink. It implies the satellite is adjusting its ground track for a specific collection target. This resonates with Space Force intelligence mission context.
   - Dependencies: Step 2.
   - Risk: Low.

7. **Act 5 -- Resolution and contrast** (`scripts/demo.py`)
   - Action:
     - POST to `/admin/trigger-process` 2 times with `delay_s / 2` seconds between each to simulate multiple observation cycles arriving. Each trigger causes the filter to re-update all objects, driving recalibration convergence.
     - Print narration about self-healing, no analyst intervention, contrast with static SGP4.
     - Clean up: call `seed_conjunction._clear_synthetic_threat()` to remove NORAD 99999 from `catalog.json` and the TLE cache. Print a note that the synthetic threat has been cleaned up.
   - Sleep: `delay_s` seconds after the trigger sequence.
   - Why: The recalibration sequence is the "system self-heals" moment. The cleanup ensures the demo can be re-run immediately without manual intervention.
   - Dependencies: Steps 3-6 (events must have been injected for there to be anything to resolve).
   - Risk: Medium -- the 2x trigger-process calls may not be sufficient for full recalibration convergence if the filter needs 3+ observation cycles. The presenter should be coached that full convergence may take one more cycle.

### Phase 3: Orchestration and error handling

8. **Main orchestrator function** (`scripts/demo.py`)
   - Action: Define `run_demo(args)` that:
     - If `args.act == "all"`: runs acts 1 through 5 in sequence
     - If `args.act == "N"`: runs only the specified act
     - Wraps each act in try/except and prints a clear error message if an act fails, then asks the presenter whether to continue to the next act (only in `--interactive` mode) or continues automatically (default)
   - Print a preamble at the start: system name, date/time, catalog size, server URL.
   - Print a summary at the end: what was injected, cleanup status.
   - Why: Graceful error handling prevents a demo crash from being unrecoverable.
   - Dependencies: Steps 3-7.
   - Risk: Low.

9. **Add `--clean` flag for post-demo cleanup** (`scripts/demo.py`)
   - Action: Add `--clean` argument that:
     - Calls `seed_conjunction._clear_synthetic_threat()` to remove NORAD 99999
     - POSTs to `/admin/trigger-process` once to re-process the catalog without the synthetic object
     - Prints confirmation
   - This is for cases where the demo is interrupted before Act 5 runs cleanup.
   - Why: Demo environments need to be resettable quickly.
   - Dependencies: Step 1.
   - Risk: Low.

## Presenter script

The following narration text should be printed to stdout at each act. The presenter reads these aloud or uses them as talking points. Text is written for a DoD/Space Force audience.

### Act 1: Normal Operations (30 seconds)

```
PRESENTER NOTES:
  "You're looking at a live space domain awareness dashboard tracking
   [N] objects in low Earth orbit -- crewed stations, commercial
   constellations, ISR assets, ASAT debris, and rocket bodies.

   Every 30 minutes, the system ingests new orbital data from
   Space-Track. Each update runs through an Unscented Kalman Filter
   that tests whether the satellite is where we predicted it would be.

   Right now, everything is green. Every object's orbit matches our
   prediction within expected noise. This is what a quiet day looks like.

   [Click on ISS to show the residual chart -- flat line near zero.]

   Now let's see what happens when the situation changes."
```

### Act 2: ASAT Debris Conjunction with ISS (60 seconds)

```
PRESENTER NOTES:
  "We just injected a Cosmos 1408 ASAT debris fragment on a collision
   course with the International Space Station. Miss distance: 1.5 km.

   [Point to the ISS marker -- it should be turning red/amber.]
   [Point to the alert feed -- conjunction_risk alert should appear.]
   [Point to the conjunction panel -- miss distance and time to closest approach.]

   In November 2021, Russia destroyed Cosmos 1408 with a direct-ascent
   anti-satellite weapon, creating over 1,500 trackable debris fragments.
   Many of those fragments are in the ISS altitude band.

   A traditional Space-Track workflow would require an analyst to manually
   screen conjunction candidates after each TLE update. Our system detected
   this threat automatically within seconds of the orbital data arriving.

   This is crew safety. This is why continuous monitoring matters."
```

### Act 3: Unannounced Starlink Maneuver (60 seconds)

```
PRESENTER NOTES:
  "Now a different scenario. We just detected an unannounced orbit-raising
   maneuver on STARLINK-1990.

   [Point to STARLINK-1990 marker -- amber/red.]
   [Point to the residual chart -- NIS spike above the threshold line.]
   [Point to the alert feed -- maneuver classification.]

   Starlink satellites maneuver constantly -- orbit raising, collision
   avoidance, deorbit. SpaceX does not announce these maneuvers in advance.
   After a maneuver, any static orbital prediction for this satellite is
   wrong. The satellite is not where the catalog says it should be.

   Our system detected the maneuver within one TLE update cycle. The
   uncertainty cone on the globe just expanded -- that's the filter telling
   us it has reduced confidence in this track.

   For an adversary tracking our assets, this is the scenario that breaks
   their prediction. For us monitoring theirs, this is early warning."
```

### Act 4: ISR Asset Repositioning (60 seconds)

```
PRESENTER NOTES:
  "One more. BlackSky Global-7 -- a commercial imaging satellite -- just
   performed a cross-track maneuver. That's an inclination adjustment.

   [Point to BLACKSKY GLOBAL-7 marker.]
   [Point to the alert -- note 'maneuver' classification.]

   An along-track maneuver changes timing -- when a satellite passes over
   a target. A cross-track maneuver changes geometry -- which targets the
   satellite can see. This one suggests a collection tasking: someone just
   repositioned an ISR asset to look at something specific.

   The system detected it autonomously. No analyst queried it. No tasking
   was required. The filter saw the orbit change and flagged it."
```

### Act 5: Resolution and Contrast (30 seconds)

```
PRESENTER NOTES:
  "Watch the affected objects. The filter is recalibrating -- incorporating
   new observations, re-estimating the orbit, collapsing the uncertainty.

   [Point to markers transitioning from red/amber back to green.]
   [Point to the residual chart -- NIS returning to baseline.]

   Two observation cycles. No human intervention. The system detected the
   anomaly, classified it, screened for conjunction risk, and self-corrected.

   Compare that to the alternative: a static SGP4 prediction that keeps
   extrapolating the old orbit. No detection. No alert. No recalibration.
   The position error grows silently until someone manually notices.

   That's the difference between continuous monitoring and periodic updates.
   That's what this system provides."
```

## CLI invocations

### Full 5-minute demo (default)
```bash
# Prerequisites: backend running, frontend open, 72-hour TLE cache loaded
python scripts/demo.py
```

### Individual acts (for rehearsal or re-runs)
```bash
python scripts/demo.py --act 1    # Normal ops narration only
python scripts/demo.py --act 2    # Conjunction injection only
python scripts/demo.py --act 3    # Starlink maneuver only
python scripts/demo.py --act 4    # ISR maneuver only
python scripts/demo.py --act 5    # Resolution + cleanup only
```

### Custom timing (slower for larger audiences, faster for tech reviewers)
```bash
python scripts/demo.py --delay-s 20   # 20 seconds between acts (slower)
python scripts/demo.py --delay-s 8    # 8 seconds between acts (faster)
```

### Post-demo cleanup (if demo was interrupted)
```bash
python scripts/demo.py --clean
# or: git checkout data/catalog/catalog.json
```

## Test strategy

- **Unit tests:** Not applicable -- `demo.py` is an orchestration script that calls tested functions. The injection functions (`inject_maneuver`, `inject_conjunction`) are already tested in `tests/test_seed_maneuver.py` and `tests/test_seed_conjunction.py`.
- **Integration test:** Run `python scripts/demo.py --act 2 --delay-s 1` with the backend running and verify:
  1. No Python exceptions
  2. NORAD 99999 appears in `catalog.json` after Act 2
  3. `POST /admin/trigger-process` returns 200
  4. WebSocket client receives `anomaly` and `conjunction_risk` messages
- **Dry-run test:** Run `python scripts/demo.py --act 1` (narration only) and verify it prints correctly without calling any injection functions or server endpoints.
- **Cleanup test:** Run `python scripts/demo.py --clean` and verify NORAD 99999 is removed from `catalog.json` and TLE cache.

## Risks and mitigations

- **Risk:** `inject_conjunction` modifies `catalog.json` (a git-tracked file). If the demo crashes between Act 2 and Act 5, the file will have an extra entry. -- Mitigation: Act 5 includes cleanup. `--clean` flag provides manual cleanup. The file can always be restored with `git checkout data/catalog/catalog.json`.

- **Risk:** The 5.0 m/s delta-V threshold may not produce a visible NIS exceedance if TLE cache is very stale (natural NIS is already elevated, masking the injection signal). -- Mitigation: Print a warning if the TLE cache age exceeds 48 hours. The presenter should run `scripts/replay.py --hours 72` to refresh the cache before the demo.

- **Risk:** Multiple rapid `trigger-process` calls in Act 5 could cause duplicate anomaly alerts (known issue: open_threads.md item 1 -- duplicate anomaly history entries). -- Mitigation: Space the trigger calls by `delay_s / 2` seconds. The duplicate alert issue is cosmetic and does not affect the demo narrative.

- **Risk:** The conjunction screening in Act 2 takes 45-90 seconds at 100-object scale (POC-LIM-004). The conjunction_risk WebSocket message may not arrive before the presenter finishes talking about it. -- Mitigation: Set `offset_min=5.0` (conjunction epoch 5 minutes out) so the screening has time to complete. Coach the presenter to fill time by discussing the alert panel first, then check the conjunction panel 30-60 seconds later.

- **Risk:** Running Act 3 or Act 4 before Act 2's conjunction screening completes could cause processing contention (sequential processing loop, POC-LIM-003). -- Mitigation: The `2 * delay_s` sleep between acts (default 30 seconds) provides buffer. The conjunction screening runs in `asyncio.run_in_executor` and does not block the processing loop from handling subsequent trigger-process calls.

## Open questions

1. **Should Act 4 (ISR asset maneuver) be included in the default `--act all` sequence or be opt-in (`--act 4`)?** It adds 60 seconds to the demo and may feel repetitive after Act 3. However, the cross-track vs. along-track distinction and the ISR narrative are uniquely valuable for a Space Force audience. Recommend including it by default but noting in the presenter script that it can be skipped if time is short.

2. **Should the script print countdown timers between acts (e.g., "Next act in 15 seconds...") or just sleep silently?** A countdown helps the presenter pace themselves. Recommend printing a countdown with one line per 5-second interval.

3. **Should there be an `--act 2+3` syntax for running a subset of acts?** This would add CLI complexity. Recommend keeping it simple: `--act all`, `--act N`, or run individual acts in sequence manually. If multi-act selection is needed, it can be added later.

4. **The conops.md demo walkthrough (Section "Demo scenario walkthrough") describes a different scenario (ISS maneuver, not conjunction + multi-asset). Should conops.md be updated to match this new 5-act structure, or should both demo scripts coexist?** Recommend keeping both: `demo.py` for the full 5-act narrative, and the manual `seed_maneuver.py --object 25544` flow for the simpler single-event demo described in conops.md. The conops.md walkthrough can reference `demo.py` as the preferred presentation tool.
