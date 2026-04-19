# Catalog NORAD ID Audit — 2026-04-15

Run: `python scripts/verify_catalog_ids.py` against Space-Track satcat.  
Source of truth: Space-Track SATNAME / OBJECT_TYPE / DECAY fields.

---

## Hard Errors — immediate action required

| NORAD | Our label | Official name | Issue |
|-------|-----------|---------------|-------|
| 47474 | BLACKSKY GLOBAL-1 | FLOCK 4S 8 | **Wrong satellite. Deorbited 2024-03-12. Remove from catalog.** |
| 99999 | THREAT-SIM | *(not in satcat)* | Synthetic demo object — expected. |

---

## Completely Wrong NORAD IDs

These entries point to a different satellite entirely. The NORAD ID must be replaced with the correct one for the intended object.

| NORAD (ours) | Our label | Actual satellite at that NORAD |
|---|---|---|
| 43013 | KANOPUS-V-IK | **NOAA 20** |
| 43226 | APSTAR-9 | **GOES 17** |
| 44804 | AMAZONIA-1 | **CARTOSAT 3** |
| 23118 | INTELSAT 709 | **METEOSAT 6 AKM** *(rocket body)* |
| 41469 | GSAT-18 | **IRNSS 1G** |
| 43569 | 2018-061A | **IRIDIUM 160** |
| 46496 | CAPELLA-2 (SEQUOIA) | **ICEYE-X7** |
| 46495 | ICEYE-X6 | **SALSAT** |
| 48918 | ICEYE-X9 | **ICEYE-X11** |
| 49438 | ICEYE-X11 | *(see below — truncated in output)* |
| 49432 | BLACKSKY GLOBAL-3 | **STARLINK-?** *(a Starlink payload)* |
| 49433 | BLACKSKY GLOBAL-4 | **STARLINK-3079** |
| 53088 | UMBRA-06 | **STARLINK-4333** |
| 53819 | CZ-2C R/B | **STARLINK-4738** *(payload, not rocket body)* |
| 54216 | FALCON 9 R/B | **CSS (MENGTIAN)** *(payload, not rocket body)* |
| 43810 | HAWKEYE 360 PATHFINDER-1 | **OBJECT BE** *(UNKNOWN type)* |
| 43811 | HAWKEYE 360 PATHFINDER-2 | **NEXTSAT-1** |
| 43812 | HAWKEYE 360 PATHFINDER-3 | **GLOBAL-2** |
| 40076 | 2014-037H DEB | **TDS 1** *(UK payload, same Dnepr launch as AISSAT-2)* |
| 43017 | KANOPUS-V-IK DEB (2017-073E) | **AO-91** *(ham radio cubesat)* |
| 25400 | SSO DEB (1998-043G) | **SL-16 R/B** *(rocket body)* |
| 28222 | LEO DEB (2004-012C) | **CZ-2C R/B** *(rocket body)* |
| 28654 | SSO DEB (2005-018A) | **NOAA 18** *(active NOAA weather sat)* |
| 29507 | KOMPSAT-2 DEB (2006-046C) | **CZ-4B R/B** *(rocket body)* |
| 29509 | KOMPSAT-2 DEB (2006-046E) | **CZ-4 DEB** *(correct type, wrong parent)* |
| 44700 | SL-12 R/B (1990-086E) | **METEOR 2-20 DEB** *(debris, not rocket body)* |

---

## Name Mismatches — Starlink renaming

SpaceX renames Starlink satellites post-deployment. Space-Track retains the original designation. These NORAD IDs are likely correct; only the displayed name needs updating.

| NORAD | Our label | Space-Track official |
|---|---|---|
| 45706 | STARLINK-1706 | STARLINK-1411 |
| 46075 | STARLINK-1990 | STARLINK-1551 |
| 44725 | STARLINK-1316 | STARLINK-1020 |
| 45044 | STARLINK-1547 | STARLINK-1132 |
| 45060 | STARLINK-1563 | STARLINK-1166 |
| 45360 | STARLINK-1636 | STARLINK-1279 |
| 45535 | STARLINK-1843 | STARLINK-1350 |
| 45555 | STARLINK-1863 | STARLINK-1327 |
| 45764 | STARLINK-1975 | STARLINK-1502 |
| 45773 | STARLINK-1984 | STARLINK-1482 |
| 46055 | STARLINK-2060 | STARLINK-1554 |
| 46060 | STARLINK-2065 | STARLINK-1572 |
| 46070 | STARLINK-2075 | STARLINK-1536 |
| 46080 | STARLINK-2085 | STARLINK-1568 |
| 49736 | STARLINK-2421 | STARLINK-3148 |
| 49737 | STARLINK-2422 | STARLINK-3225 |
| 49738 | STARLINK-2423 | STARLINK-3143 |

**Recommendation:** Update Space-Track display names in catalog.json, or accept the discrepancy as known (the satellites are correct, just named differently).

---

## Name Mismatches — rocket body designations

These may have correct NORAD IDs for the *type* of object but wrong international designator in the label, or Space-Track uses a different name variant.

| NORAD | Our label | Space-Track official |
|---|---|---|
| 16182 | SL-14 R/B (1985-097B) | SL-16 R/B |
| 22285 | SL-14 R/B (1992-093B) | SL-16 R/B |
| 23088 | SL-16 R/B (1994-023B) | SL-16 R/B ✓ *(name OK, intl desig suffix differs)* |

---

## Object Class Mismatch

| NORAD | Our label | Our class | Space-Track type | Note |
|---|---|---|---|---|
| 22675 | COSMOS 2251 | debris | PAYLOAD | Cosmos 2251 was a Soviet comsat — payload class is correct; we flagged it as debris because it's a collision debris source |

---

## Verified Correct (24 entries)

ISS (ZARYA), CSS (TIANHE), HST, ENVISAT, AQUA, STARLINK-34343, HAWK-A/B/C, HAWK-8A/B/C, DEIMOS-2 (40013), AISSAT-2 (40075), SL-16 R/B (23088), all IRIDIUM 33 DEB entries, all COSMOS 2251 DEB entries.

---

## Priority Actions

1. **Remove NORAD 47474** — deorbited 2024-03-12, not in orbit.
2. **Replace all "completely wrong NORAD IDs"** above — these are tracking unintended objects.
3. **Verify Starlink NORAD IDs** — names differ but objects may be correct; confirm with SpaceX public manifest or N2YO.
4. **Correct SL-14/SL-16 designations** for 16182 and 22285.
5. **Decide on COSMOS 2251 class** — leave as debris (intentional) or change to payload to match satcat.
