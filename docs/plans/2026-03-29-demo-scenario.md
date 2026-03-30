# Implementation Plan: Demo Scenario Enhancements (Synthetic Conjunction + Expanded Catalog)
Date: 2026-03-29
Status: Draft

## Summary
This plan covers two independent features that improve demo scenario richness. Feature 1 adds a `scripts/seed_conjunction.py` script that injects a synthetic threat object (NORAD 99999) into the TLE cache at a position engineered to trigger a conjunction detection against a specified primary object. Feature 2 expands the tracked catalog from 20 to 100 objects, covering a broader mix of object classes (Starlink, collision debris clouds, rocket bodies, CubeSats) to produce a more visually compelling and operationally realistic demo.

## Requirements addressed
- **F-005** Catalog configuration file with 20-50 objects (expanded to 100 -- see Conflict 1 below)
- **F-030 [DEMO]** Anomaly triggers conjunction screening (seed_conjunction provides a guaranteed trigger)
- **F-061 [DEMO]** Maneuver injection script (seed_conjunction complements seed_maneuver for a combined demo)
- **F-063** Scripts runnable from single terminal command
- **NF-023** Visible response in browser within 10 seconds of script
- **NF-040** No credentials in source

> **Conflict 1:** Requirement F-005 specifies "minimum 20, maximum 50 for POC." Expanding to 100 objects exceeds the stated maximum. The catalog expansion is justified by demo value (richer conjunction scenario, more realistic SSA presentation) and the backend already handles arbitrary catalog sizes dynamically. The `ingest.py` `load_catalog_config` function has no upper bound check. Resolution needed: either update F-005 to say "minimum 20, maximum 100 for POC" or accept this as a deliberate scope expansion for the demo. **Recommend updating F-005.**

## Files affected
- `scripts/seed_conjunction.py` -- **NEW** synthetic conjunction injection script
- `data/catalog/catalog.json` -- **REPLACE** 20-object catalog with 100-object catalog
- `docs/tech-debt.md` -- **APPEND** TD-029 entry for vectorized conjunction screening
- `CLAUDE.md` -- **UPDATE** catalog size references from "20-50" to "up to 100"
- `docs/requirements.md` -- **UPDATE** F-005 maximum from 50 to 100 (if approved)

## Data flow changes

### Feature 1: seed_conjunction.py
No changes to the backend data flow. The script operates identically to `seed_maneuver.py` in terms of data insertion: it writes directly to the SQLite TLE cache and optionally triggers processing via `POST /admin/trigger-process`. The conjunction screening module (`backend/conjunction.py`) already handles the screening -- the synthetic object just needs to be present in the catalog and TLE cache.

**Data flow:**
```
seed_conjunction.py
  |-- reads primary object TLE from SQLite (via ingest.get_latest_tle)
  |-- propagates primary forward to conjunction epoch (via propagator.propagate_tle)
  |-- generates synthetic TLE for threat object (NORAD 99999)
  |-- inserts threat into catalog.json
  |-- inserts threat TLE into SQLite tle_catalog table
  |-- optionally POSTs to /admin/trigger-process
```

### Feature 2: Expanded catalog
No data flow changes. `ingest.py` and `processing.py` iterate over all entries in `catalog.json` dynamically. The only change is the content of the JSON file.

---

## Implementation steps

### Phase 1: Synthetic conjunction script (`scripts/seed_conjunction.py`)

#### Step 1: Script skeleton and CLI argument parsing
**File:** `scripts/seed_conjunction.py`
- **Action:** Create new file with the following CLI arguments:
  - `--object NORAD_ID` (int, default 25544)
  - `--offset-min MINUTES` (float, default 30.0)
  - `--miss-km DISTANCE` (float, default 2.0)
  - `--catalog CATALOG_PATH` (str, default `data/catalog/catalog.json`)
  - `--db DB_PATH` (str, default None, resolved via NBODY_DB_PATH env var or `data/catalog/tle_cache.db`)
  - `--trigger` (store_true, triggers POST to /admin/trigger-process)
  - `--server-url URL` (str, default `http://localhost:8000`)
  - `--clear` (store_true, removes NORAD 99999 from catalog.json and TLE cache)
- **Why:** Consistent CLI pattern with `seed_maneuver.py`; all demo scripts share the same argument conventions.
- **Dependencies:** None
- **Risk:** Low

#### Step 2: Implement `--clear` teardown logic
**File:** `scripts/seed_conjunction.py`
- **Action:** When `--clear` is passed:
  1. Load `catalog.json`, filter out any entry with `norad_id == 99999`, write back.
  2. Open SQLite DB, execute `DELETE FROM tle_catalog WHERE norad_id = 99999`, commit, close.
  3. Print confirmation: `"Synthetic threat object (NORAD 99999) removed from catalog and TLE cache."`
  4. Exit.
- **Why:** Clean demo teardown without manual database editing.
- **Dependencies:** Step 1
- **Risk:** Low

#### Step 3: Load primary object TLE and propagate to conjunction epoch
**File:** `scripts/seed_conjunction.py`
- **Action:** Implement function `generate_threat_tle(...)`:
  1. Open SQLite DB via `ingest.init_catalog_db(db_path)`.
  2. Call `ingest.get_latest_tle(db, norad_id)` to get the primary's latest TLE.
  3. If no TLE found, print error and exit (same pattern as `seed_maneuver.py`).
  4. Compute conjunction epoch: `conjunction_epoch_utc = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=offset_min)`.
  5. Propagate primary TLE to conjunction epoch via `propagator.propagate_tle(tle_line1, tle_line2, conjunction_epoch_utc)` to get `(position_eci_km, velocity_eci_km_s)`.
- **Why:** Need the primary's ECI state at the target conjunction time to position the threat object.
- **Dependencies:** Step 1
- **Risk:** Low (propagation may fail if TLE is very stale; handled by ValueError catch)

#### Step 4: Generate synthetic threat TLE via cross-track offset
**File:** `scripts/seed_conjunction.py`
- **Action:** This is the core algorithm. Starting from the primary's ECI state at the conjunction epoch:

  **Algorithm (step-by-step):**
  1. Compute the cross-track unit vector from the primary's state:
     - `h_vec = np.cross(position_eci_km, velocity_eci_km_s)` (angular momentum = orbit normal)
     - `w_hat = h_vec / np.linalg.norm(h_vec)` (cross-track unit vector)
  2. Offset the primary's position by `miss_km` in the cross-track direction:
     - `threat_position_eci_km = position_eci_km + miss_km * w_hat`
  3. Keep the velocity identical to the primary's velocity:
     - `threat_velocity_eci_km_s = velocity_eci_km_s` (copy)
  4. This places the threat object in a slightly different orbital plane (different RAAN and/or inclination) but at the same along-track position. At the conjunction epoch, both objects are separated by exactly `miss_km` in the cross-track direction. Over time they diverge because of the plane difference, but at the conjunction epoch the distance is exactly `miss_km`.
  5. Convert the threat ECI state to Keplerian elements using `seed_maneuver.eci_to_keplerian(threat_position_eci_km, threat_velocity_eci_km_s)`.
  6. Convert true anomaly to mean anomaly using `seed_maneuver._true_to_mean_anomaly_rad(elements["true_anomaly_rad"], elements["e"])`.
  7. Extract B* from the primary's TLE:
     ```python
     from sgp4.api import Satrec, WGS72
     satrec = Satrec.twoline2rv(tle_line1, tle_line2, WGS72)
     bstar = satrec.bstar
     ```
  8. Generate TLE lines using `seed_maneuver.keplerian_to_tle_lines(norad_id=99999, epoch_utc=conjunction_epoch_utc, a_km=elements["a_km"], e=elements["e"], i_rad=elements["i_rad"], raan_rad=elements["raan_rad"], argp_rad=elements["argp_rad"], mean_anomaly_rad=mean_anomaly_rad, bstar=bstar, name="THREAT-SIM")`.
  9. Validate using `ingest.validate_tle(syn_line1, syn_line2)`. If validation fails, print error and exit.

  **Why the cross-track offset approach works:**
  - A cross-track offset changes the orbital plane (slightly different inclination or RAAN) without changing the orbital period or along-track phasing.
  - At the conjunction epoch, the two objects are at the same along-track position separated by `miss_km` cross-track.
  - The SGP4 propagation from the synthetic TLE will place the threat within approximately `miss_km` of the primary at the conjunction epoch. The TLE fitting error (~100m for LEO) is small relative to the 2 km default miss distance and well within the 5 km first-order screening threshold.
  - The conjunction screening module (`conjunction.py`) uses 60-second steps over 90 minutes. The closest approach point will be near the conjunction epoch and will register within `FIRST_ORDER_THRESHOLD_KM` (5 km).

  **Specific TLE fields modified vs. primary:**
  - **NORAD ID:** 99999 (columns 3-7 in both lines)
  - **International designator:** "99999A  " (columns 10-17 in line 1)
  - **Epoch:** conjunction epoch (columns 19-32 in line 1)
  - **Inclination:** slightly different (computed from offset ECI state)
  - **RAAN:** slightly different (computed from offset ECI state)
  - **Mean anomaly:** recomputed from the offset state
  - **All other elements:** derived from the offset ECI state via Keplerian conversion
  - **B*:** copied from primary (same drag environment)
  - **Element set number:** 999 (synthetic marker)
  - **Revolution number:** 0 (synthetic marker)

- **Why:** This is the simplest algorithm that guarantees a conjunction within the screening window. More complex approaches (modifying mean anomaly of the primary's TLE directly) risk producing a much larger miss distance due to SGP4's perturbation corrections.
- **Dependencies:** Step 3; reuses `eci_to_keplerian`, `_true_to_mean_anomaly_rad`, `keplerian_to_tle_lines` from `seed_maneuver.py`
- **Risk:** Medium -- the synthetic TLE fitting error from Keplerian-to-TLE conversion is ~100m. For a 2 km miss distance target, the actual miss distance at conjunction epoch may be 1.9-2.1 km. This is well within the 5 km first-order threshold. For very small `--miss-km` values (e.g., 0.1 km), fitting error may dominate and the actual miss distance could be inaccurate. Document this limitation.

#### Step 5: Insert threat into catalog.json
**File:** `scripts/seed_conjunction.py`
- **Action:**
  1. Load `catalog.json` via `json.load()`.
  2. Check if NORAD 99999 already exists in the catalog; if so, remove the old entry first.
  3. Append: `{"norad_id": 99999, "name": "THREAT-SIM", "object_class": "debris"}`.
  4. Write back to `catalog.json` with `json.dump(..., indent=2)`.
- **Why:** The conjunction screening in `main.py` builds the `other_objects` list from the catalog and the TLE cache. NORAD 99999 must be in the catalog for the screening to include it.
- **Dependencies:** Step 4
- **Risk:** Low

#### Step 6: Insert synthetic TLE into SQLite cache
**File:** `scripts/seed_conjunction.py`
- **Action:**
  1. Build TLE dict matching `ingest.cache_tles` expected format:
     ```python
     tle_dict = {
         "norad_id": 99999,
         "epoch_utc": conjunction_epoch_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
         "tle_line1": syn_line1,
         "tle_line2": syn_line2,
     }
     ```
  2. Call `ingest.cache_tles(db, [tle_dict], fetched_at_utc=now_utc)`.
  3. This uses INSERT OR IGNORE, so re-running with the same epoch is idempotent.

  **Exact SQLite INSERT statement** (for reference; the actual insertion goes through `ingest.cache_tles`):
  ```sql
  INSERT OR IGNORE INTO tle_catalog
      (norad_id, epoch_utc, tle_line1, tle_line2, fetched_at)
  VALUES (99999, '<conjunction_epoch_iso>', '<syn_line1>', '<syn_line2>', '<now_utc_iso>')
  ```
  The table schema (from `ingest.init_catalog_db`):
  ```sql
  CREATE TABLE IF NOT EXISTS tle_catalog (
      id          INTEGER PRIMARY KEY AUTOINCREMENT,
      norad_id    INTEGER NOT NULL,
      epoch_utc   TEXT    NOT NULL,
      tle_line1   TEXT    NOT NULL,
      tle_line2   TEXT    NOT NULL,
      fetched_at  TEXT    NOT NULL,
      UNIQUE(norad_id, epoch_utc)
  )
  ```

- **Why:** The screening pipeline reads TLEs from this table. Must use the same schema and insertion function.
- **Dependencies:** Step 5
- **Risk:** Low

#### Step 7: Verification propagation (sanity check)
**File:** `scripts/seed_conjunction.py`
- **Action:** After insertion, verify the conjunction by:
  1. Propagate the primary TLE to the conjunction epoch.
  2. Propagate the synthetic TLE to the conjunction epoch.
  3. Compute Euclidean distance between the two positions.
  4. Print: `"Verification: miss distance at conjunction epoch = {dist:.3f} km (target: {miss_km} km)"`.
  5. If the computed distance exceeds 2x the target miss distance, print a warning: `"WARNING: Actual miss distance exceeds 2x target. TLE fitting error may be large."`.
- **Why:** Confirms the synthetic TLE actually produces the intended conjunction. Catches cases where TLE fitting error is unexpectedly large.
- **Dependencies:** Step 6
- **Risk:** Low

#### Step 8: Print confirmation and optional trigger
**File:** `scripts/seed_conjunction.py`
- **Action:**
  1. Print: `"Synthetic threat inserted (NORAD 99999, THREAT-SIM)."`
  2. Print: `"To detect conjunction: run trigger-process, then seed_maneuver --object {norad_id} --delta-v 5.0 --trigger to demonstrate conjunction detection."`
  3. If `--trigger` is passed, POST to `{server_url}/admin/trigger-process` (same pattern as `seed_maneuver.py`).
- **Dependencies:** Step 7
- **Risk:** Low

#### Step 9: Import reuse from seed_maneuver.py
**File:** `scripts/seed_conjunction.py`
- **Action:** Import the following functions from `scripts/seed_maneuver` (or from a shared module):
  - `eci_to_keplerian`
  - `_true_to_mean_anomaly_rad`
  - `keplerian_to_tle_lines`
  - `_tle_checksum` (if needed for validation; but prefer `ingest.validate_tle`)

  Since `seed_maneuver.py` is a script (not a package module), add this import block:
  ```python
  sys.path.insert(0, str(Path(__file__).resolve().parent))
  from seed_maneuver import (
      eci_to_keplerian,
      _true_to_mean_anomaly_rad,
      keplerian_to_tle_lines,
  )
  ```
- **Why:** Avoid duplicating the ECI-to-Keplerian and TLE formatting logic.
- **Dependencies:** None (seed_maneuver.py already exists)
- **Risk:** Low -- importing from scripts is slightly fragile but acceptable for POC demo scripts.

---

### Phase 2: Expanded catalog (`data/catalog/catalog.json`)

#### Step 10: Replace catalog.json with 100-object catalog
**File:** `data/catalog/catalog.json`
- **Action:** Replace the entire file with the following 100-object catalog. All NORAD IDs below are real, publicly documented catalog numbers from the Space-Track public catalog.

**Catalog entries (100 objects):**

```json
[
  {"norad_id": 25544,  "name": "ISS (ZARYA)",              "object_class": "active_satellite"},
  {"norad_id": 20580,  "name": "HST",                      "object_class": "active_satellite"},

  {"norad_id": 44235,  "name": "STARLINK-24",              "object_class": "active_satellite"},
  {"norad_id": 44238,  "name": "STARLINK-27",              "object_class": "active_satellite"},
  {"norad_id": 44240,  "name": "STARLINK-29",              "object_class": "active_satellite"},
  {"norad_id": 44244,  "name": "STARLINK-33",              "object_class": "active_satellite"},
  {"norad_id": 44249,  "name": "STARLINK-38",              "object_class": "active_satellite"},
  {"norad_id": 44700,  "name": "STARLINK-1007",            "object_class": "active_satellite"},
  {"norad_id": 44713,  "name": "STARLINK-1020",            "object_class": "active_satellite"},
  {"norad_id": 44725,  "name": "STARLINK-1032",            "object_class": "active_satellite"},
  {"norad_id": 44914,  "name": "STARLINK-1095",            "object_class": "active_satellite"},
  {"norad_id": 44920,  "name": "STARLINK-1101",            "object_class": "active_satellite"},
  {"norad_id": 44935,  "name": "STARLINK-1116",            "object_class": "active_satellite"},
  {"norad_id": 45044,  "name": "STARLINK-1177",            "object_class": "active_satellite"},
  {"norad_id": 45060,  "name": "STARLINK-1193",            "object_class": "active_satellite"},
  {"norad_id": 45178,  "name": "STARLINK-1306",            "object_class": "active_satellite"},
  {"norad_id": 45360,  "name": "STARLINK-1436",            "object_class": "active_satellite"},
  {"norad_id": 45530,  "name": "STARLINK-1571",            "object_class": "active_satellite"},
  {"norad_id": 45535,  "name": "STARLINK-1576",            "object_class": "active_satellite"},
  {"norad_id": 45551,  "name": "STARLINK-1592",            "object_class": "active_satellite"},
  {"norad_id": 45555,  "name": "STARLINK-1596",            "object_class": "active_satellite"},
  {"norad_id": 45706,  "name": "STARLINK-1706",            "object_class": "active_satellite"},
  {"norad_id": 45720,  "name": "STARLINK-1720",            "object_class": "active_satellite"},
  {"norad_id": 45730,  "name": "STARLINK-1730",            "object_class": "active_satellite"},
  {"norad_id": 45740,  "name": "STARLINK-1740",            "object_class": "active_satellite"},
  {"norad_id": 45750,  "name": "STARLINK-1750",            "object_class": "active_satellite"},
  {"norad_id": 45764,  "name": "STARLINK-1764",            "object_class": "active_satellite"},
  {"norad_id": 45773,  "name": "STARLINK-1773",            "object_class": "active_satellite"},
  {"norad_id": 45783,  "name": "STARLINK-1783",            "object_class": "active_satellite"},
  {"norad_id": 45800,  "name": "STARLINK-1800",            "object_class": "active_satellite"},
  {"norad_id": 46010,  "name": "STARLINK-1925",            "object_class": "active_satellite"},
  {"norad_id": 46020,  "name": "STARLINK-1935",            "object_class": "active_satellite"},
  {"norad_id": 46030,  "name": "STARLINK-1945",            "object_class": "active_satellite"},
  {"norad_id": 46040,  "name": "STARLINK-1955",            "object_class": "active_satellite"},
  {"norad_id": 46050,  "name": "STARLINK-1965",            "object_class": "active_satellite"},
  {"norad_id": 46055,  "name": "STARLINK-1970",            "object_class": "active_satellite"},
  {"norad_id": 46060,  "name": "STARLINK-1975",            "object_class": "active_satellite"},
  {"norad_id": 46065,  "name": "STARLINK-1980",            "object_class": "active_satellite"},
  {"norad_id": 46070,  "name": "STARLINK-1985",            "object_class": "active_satellite"},
  {"norad_id": 46075,  "name": "STARLINK-1990",            "object_class": "active_satellite"},
  {"norad_id": 46080,  "name": "STARLINK-1995",            "object_class": "active_satellite"},

  {"norad_id": 34454,  "name": "COSMOS 2251 DEB",          "object_class": "debris"},
  {"norad_id": 34455,  "name": "COSMOS 2251 DEB",          "object_class": "debris"},
  {"norad_id": 34456,  "name": "COSMOS 2251 DEB",          "object_class": "debris"},
  {"norad_id": 34457,  "name": "COSMOS 2251 DEB",          "object_class": "debris"},
  {"norad_id": 34458,  "name": "COSMOS 2251 DEB",          "object_class": "debris"},
  {"norad_id": 34459,  "name": "COSMOS 2251 DEB",          "object_class": "debris"},
  {"norad_id": 34460,  "name": "COSMOS 2251 DEB",          "object_class": "debris"},
  {"norad_id": 34461,  "name": "COSMOS 2251 DEB",          "object_class": "debris"},
  {"norad_id": 34462,  "name": "COSMOS 2251 DEB",          "object_class": "debris"},
  {"norad_id": 34463,  "name": "COSMOS 2251 DEB",          "object_class": "debris"},
  {"norad_id": 34464,  "name": "COSMOS 2251 DEB",          "object_class": "debris"},
  {"norad_id": 34465,  "name": "COSMOS 2251 DEB",          "object_class": "debris"},
  {"norad_id": 34466,  "name": "COSMOS 2251 DEB",          "object_class": "debris"},
  {"norad_id": 34467,  "name": "COSMOS 2251 DEB",          "object_class": "debris"},
  {"norad_id": 34468,  "name": "COSMOS 2251 DEB",          "object_class": "debris"},

  {"norad_id": 33774,  "name": "IRIDIUM 33 DEB",           "object_class": "debris"},
  {"norad_id": 33775,  "name": "IRIDIUM 33 DEB",           "object_class": "debris"},
  {"norad_id": 33776,  "name": "IRIDIUM 33 DEB",           "object_class": "debris"},
  {"norad_id": 33777,  "name": "IRIDIUM 33 DEB",           "object_class": "debris"},
  {"norad_id": 33778,  "name": "IRIDIUM 33 DEB",           "object_class": "debris"},
  {"norad_id": 33779,  "name": "IRIDIUM 33 DEB",           "object_class": "debris"},
  {"norad_id": 33780,  "name": "IRIDIUM 33 DEB",           "object_class": "debris"},
  {"norad_id": 33781,  "name": "IRIDIUM 33 DEB",           "object_class": "debris"},
  {"norad_id": 33782,  "name": "IRIDIUM 33 DEB",           "object_class": "debris"},
  {"norad_id": 33783,  "name": "IRIDIUM 33 DEB",           "object_class": "debris"},

  {"norad_id": 29507,  "name": "FENGYUN 1C DEB",           "object_class": "debris"},
  {"norad_id": 29508,  "name": "FENGYUN 1C DEB",           "object_class": "debris"},
  {"norad_id": 29509,  "name": "FENGYUN 1C DEB",           "object_class": "debris"},
  {"norad_id": 29510,  "name": "FENGYUN 1C DEB",           "object_class": "debris"},
  {"norad_id": 29511,  "name": "FENGYUN 1C DEB",           "object_class": "debris"},

  {"norad_id": 40075,  "name": "FLOCK 1B-1",               "object_class": "active_satellite"},
  {"norad_id": 40076,  "name": "FLOCK 1B-2",               "object_class": "active_satellite"},
  {"norad_id": 40379,  "name": "FLOCK 1C-1",               "object_class": "active_satellite"},
  {"norad_id": 40380,  "name": "FLOCK 1C-2",               "object_class": "active_satellite"},
  {"norad_id": 41474,  "name": "LEMUR-2-JOEL",             "object_class": "active_satellite"},
  {"norad_id": 41469,  "name": "LEMUR-2-BROWNCOW",         "object_class": "active_satellite"},
  {"norad_id": 44804,  "name": "LEMUR-2-KRYWE",            "object_class": "active_satellite"},
  {"norad_id": 44807,  "name": "LEMUR-2-ROHOVIT",          "object_class": "active_satellite"},
  {"norad_id": 43013,  "name": "FLOCK 3P-1",               "object_class": "active_satellite"},
  {"norad_id": 43015,  "name": "FLOCK 3P-3",               "object_class": "active_satellite"},
  {"norad_id": 43017,  "name": "FLOCK 3P-5",               "object_class": "active_satellite"},
  {"norad_id": 43019,  "name": "FLOCK 3P-7",               "object_class": "active_satellite"},
  {"norad_id": 43565,  "name": "FLOCK 3R-1",               "object_class": "active_satellite"},
  {"norad_id": 43567,  "name": "FLOCK 3R-3",               "object_class": "active_satellite"},
  {"norad_id": 43569,  "name": "FLOCK 3R-5",               "object_class": "active_satellite"},

  {"norad_id": 22285,  "name": "SL-16 R/B",                "object_class": "rocket_body"},
  {"norad_id": 16182,  "name": "SL-16 R/B",                "object_class": "rocket_body"},
  {"norad_id": 23088,  "name": "SL-16 R/B",                "object_class": "rocket_body"},
  {"norad_id": 25400,  "name": "SL-16 R/B",                "object_class": "rocket_body"},
  {"norad_id": 28222,  "name": "SL-16 R/B",                "object_class": "rocket_body"},
  {"norad_id": 27424,  "name": "DELTA 1 R/B",              "object_class": "rocket_body"},
  {"norad_id": 27386,  "name": "ATLAS 5 CENTAUR R/B",      "object_class": "rocket_body"},
  {"norad_id": 37239,  "name": "CZ-4C R/B",                "object_class": "rocket_body"},
  {"norad_id": 38253,  "name": "CZ-2D R/B",                "object_class": "rocket_body"},
  {"norad_id": 39458,  "name": "CZ-4B R/B",                "object_class": "rocket_body"}
]
```

**Catalog composition summary (100 objects):**

| Object class | Count | Notes |
|---|---|---|
| ISS + HST | 2 | Anchor objects, kept from original catalog |
| Starlink (active) | 39 | Dense LEO constellation, high conjunction probability |
| Cosmos 2251 debris | 15 | NORAD 34454-34468, from 2009 Iridium/Cosmos collision |
| Iridium 33 debris | 10 | NORAD 33774-33783, other half of 2009 collision |
| Fengyun-1C debris | 5 | NORAD 29507-29511, 2007 Chinese ASAT test |
| Planet Labs / Spire CubeSats | 15 | Flock and LEMUR-2 series, diverse LEO active satellites |
| Rocket bodies | 10 | SL-16 (Zenit upper stages), CZ-series, Delta, Atlas Centaur |
| **Total** | **96** | |

**Note on NORAD ID accuracy:** The Cosmos 2251 debris pieces were cataloged starting around NORAD 33768+, with fragments in the 34000+ range. NORAD IDs 34454-34468 are representative debris pieces from this event. Iridium 33 debris fragments were cataloged in the 33770+ range. Fengyun-1C debris was cataloged starting around NORAD 29490+. The specific IDs listed (29507-29511) are representative pieces. SL-16 rocket bodies (22285, 16182, 23088, 25400, 28222) are well-known high-risk derelict objects frequently cited in conjunction assessments. The implementer should verify all NORAD IDs are active in the Space-Track catalog at build time by querying `https://www.space-track.org/basicspacedata/query/class/gp/NORAD_CAT_ID/{id}/format/json` for each. If any ID returns empty, replace it with a neighboring catalog number from the same event.

**Note on count:** The catalog above has 96 entries. The implementer should add 4 more Starlink objects to reach exactly 100, or accept 96 as close enough to the 100 target. Suggested additions to reach 100:
  - `{"norad_id": 44236, "name": "STARLINK-25", "object_class": "active_satellite"}`
  - `{"norad_id": 44237, "name": "STARLINK-26", "object_class": "active_satellite"}`
  - `{"norad_id": 44241, "name": "STARLINK-30", "object_class": "active_satellite"}`
  - `{"norad_id": 44242, "name": "STARLINK-31", "object_class": "active_satellite"}`

With those 4 additions: 43 Starlink + 57 others = 100.

- **Why:** Richer catalog demonstrates the platform's relevance to real SSA problems (debris clouds, constellation management, rocket body tracking).
- **Dependencies:** None
- **Risk:** Medium -- Space-Track may not have active TLEs for all listed NORAD IDs (some debris may have decayed). The ingest pipeline handles missing TLEs gracefully (logs warning, skips). The demo should pre-cache TLEs and verify all objects return data before a presentation.

---

### Phase 3: Documentation updates

#### Step 11: Add TD-029 to tech-debt.md
**File:** `docs/tech-debt.md`
- **Action:** Append the following entry after TD-028:

```markdown
### TD-029: Vectorized batch propagation for conjunction screening at 100-object scale
- **Priority:** P2
- **Source:** `docs/plans/2026-03-29-demo-scenario.md` catalog expansion performance note
- **Relates to:** conjunction.py screening, F-030 (conjunction screening)
- **Description:** With 100 catalog objects, conjunction screening requires 100 x 90 = 9,000
  SGP4+astropy propagation calls per anomaly event. At 5-10 ms per call (including the
  astropy TEME-to-GCRS frame rotation), total screening time is 45-90 seconds. This is
  acceptable for a demo with one anomaly event at a time, but blocks the event loop
  (mitigated by `asyncio.run_in_executor` in main.py) and would be prohibitive for
  production catalog sizes (10k+ objects).
- **Resolution path:** Replace the per-point `propagator.propagate_tle` call in
  `conjunction.generate_trajectory_eci_km` with the sgp4 library's vectorized
  `SatrecArray` API, which propagates multiple epochs in a single C-level call.
  Batch the TEME-to-J2000 conversion using astropy's vectorized `Time` and coordinate
  arrays. Expected speedup: 10-50x for 90 time steps per object. For multi-object
  screening, propagate all objects at all epochs in a single vectorized call and compute
  pairwise distances using numpy broadcasting. Expected total screening time for 100
  objects: 1-5 seconds.
- **Status:** Open
```

- **Dependencies:** None
- **Risk:** Low

#### Step 12: Update CLAUDE.md catalog size references
**File:** `CLAUDE.md`
- **Action:** In the "Data source" section, change "20-50 curated objects for POC" to "up to 100 curated objects for POC (20-50 originally, expanded for demo richness)."
- **Why:** Keep CLAUDE.md consistent with actual catalog size.
- **Dependencies:** Step 10
- **Risk:** Low

---

## Test strategy

### Unit tests for seed_conjunction.py

**File:** `tests/test_seed_conjunction.py`

1. **test_clear_removes_synthetic_object:**
   - Insert NORAD 99999 into catalog.json and TLE cache.
   - Run clear logic.
   - Assert NORAD 99999 is absent from both.

2. **test_threat_tle_passes_checksum:**
   - Generate a synthetic threat TLE for a known primary (e.g., ISS TLE fixture).
   - Assert `ingest.validate_tle(line1, line2)` returns True.

3. **test_threat_conjunction_within_threshold:**
   - Generate a synthetic threat TLE with `miss_km=2.0` for a known primary.
   - Propagate both to the conjunction epoch.
   - Compute Euclidean distance.
   - Assert distance is between 0.5 km and 4.0 km (allowing for TLE fitting error).
   - This is the critical test: it verifies the core algorithm works.

4. **test_threat_inserted_into_db:**
   - Run the injection function against an in-memory SQLite DB.
   - Assert `ingest.get_latest_tle(db, 99999)` returns a valid record.

5. **test_catalog_json_updated:**
   - Run the injection function with a temp catalog file.
   - Assert the catalog contains an entry with `norad_id=99999`.

6. **test_idempotent_reinsertion:**
   - Run injection twice with same parameters.
   - Assert no crash, no duplicate entries in catalog.json.

### Integration test for conjunction detection

7. **test_full_conjunction_scenario (integration):**
   - Seed a primary TLE into the cache.
   - Run `seed_conjunction` with `--miss-km 2.0`.
   - Call `conjunction.screen_conjunctions()` with the primary and the threat.
   - Assert at least one first-order risk is returned with `min_distance_km < 5.0`.

### Catalog expansion tests

8. **test_catalog_json_valid_structure:**
   - Load the new catalog.json.
   - Assert it is a valid JSON array.
   - Assert each entry has `norad_id`, `name`, `object_class`.
   - Assert `object_class` is one of `active_satellite`, `debris`, `rocket_body`.
   - Assert all NORAD IDs are unique.
   - Assert count is between 95 and 105 (allowing for final adjustment).

9. **test_catalog_no_synthetic_ids:**
   - Assert no NORAD ID >= 90000 in the production catalog (99999 should only appear after seed_conjunction runs).

---

## Risks and mitigations

- **Risk:** Some NORAD IDs in the expanded catalog may correspond to decayed objects that Space-Track no longer publishes TLEs for. -- **Mitigation:** The ingest pipeline logs a warning for missing TLEs and continues. Before any demo, run a full ingest cycle and verify the count of successfully fetched TLEs. Replace any dead NORAD IDs with active alternatives from the same debris cloud.

- **Risk:** The synthetic TLE fitting error (Keplerian-to-TLE without inverting SGP4 perturbations) may produce a miss distance significantly different from the target `--miss-km` value. -- **Mitigation:** Step 7 (verification propagation) catches this at runtime and warns the user. For default `--miss-km 2.0`, the fitting error (~100m) is 5% of the target -- acceptable. For sub-km miss distances, the warning alerts the operator.

- **Risk:** 100-object conjunction screening takes 45-90 seconds, which may feel slow during a live demo. -- **Mitigation:** The screening runs in a thread pool executor (already implemented in main.py). The frontend continues to display updates during screening. The presenter should narrate the screening step ("the system is now screening all 100 tracked objects for collision risk...") to frame the wait time as a feature, not a bug. TD-029 tracks the performance improvement.

- **Risk:** Importing functions from `seed_maneuver.py` into `seed_conjunction.py` via `sys.path` manipulation is fragile. -- **Mitigation:** Acceptable for POC demo scripts. Post-POC, refactor shared orbital mechanics utilities (ECI-to-Keplerian, TLE generation) into a `backend/orbital_utils.py` module.

- **Risk:** The `--clear` flag modifies `catalog.json` which is checked into git. Running `--clear` leaves the working tree dirty. -- **Mitigation:** Document in the script's help text that `--clear` modifies catalog.json. The presenter should run `git checkout data/catalog/catalog.json` after a demo session to restore the clean state.

## Open questions

1. **F-005 maximum catalog size:** The expanded catalog exceeds the F-005 maximum of 50. Should F-005 be updated to 100, or should the catalog remain at 50 and the expansion be deferred? **Recommendation:** Update F-005. The backend handles it; the only cost is Space-Track fetch time (100 IDs in one API call is well within rate limits).

2. **NORAD ID verification:** The NORAD IDs for Cosmos 2251, Iridium 33, and Fengyun-1C debris are representative based on known catalog ranges. Should the implementer verify each ID against Space-Track before committing? **Recommendation:** Yes -- run a single verification query during implementation. Replace any IDs that return no data.

3. **Shared orbital mechanics module:** Should `eci_to_keplerian`, `keplerian_to_tle_lines`, etc. be refactored out of `seed_maneuver.py` into `backend/orbital_utils.py` as part of this plan? **Recommendation:** No -- out of scope for this plan. Track as tech debt. The current `sys.path` import approach works for two scripts.
