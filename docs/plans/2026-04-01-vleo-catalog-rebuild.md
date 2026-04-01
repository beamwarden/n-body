# Implementation Plan: VLEO Catalog Rebuild (<=600 km)
Date: 2026-04-01
Status: Draft

## Summary

Rebuild `data/catalog/catalog.json` to contain only objects at or below 600 km altitude, targeting the VLEO/low-LEO operational band relevant to drone C2 relay, military ISR, crewed stations, and SAR missions. The current catalog of 100 objects was built for demo richness without altitude coherence; this plan replaces it with 95 operationally relevant objects (80-100 target range) while maintaining all three required object classes (`active_satellite`, `debris`, `rocket_body`).

## Requirements addressed

- **F-005**: Catalog configuration file listing NORAD IDs of objects to track (minimum 20, maximum 100 for POC)
- **F-001**: System retrieves TLEs for configured catalog -- all replacement NORAD IDs must have valid public TLEs
- **C-001**: Only publicly available, unclassified data sources

## Current catalog composition (108 objects)

| Category | Count | Altitude range | Notes |
|----------|-------|---------------|-------|
| ISS | 1 | ~420 km | Keep |
| HST | 1 | ~535 km | Keep |
| Starlink batch | 43 | ~550 km | Operationally relevant but over-represented |
| Cosmos 2251 debris | 15 | ~770-850 km | **Above 600 km -- REMOVE** |
| Iridium 33 debris | 10 | ~770-850 km | **Above 600 km -- REMOVE** |
| Fengyun 1C debris | 5 | ~600-900 km | **Most above 600 km -- REMOVE** |
| Planet Labs Flock (early) | 4 | ~400-475 km (some re-entered) | **Likely re-entered by 2026 -- REMOVE** |
| Planet Labs Flock 3P/3R | 7 | ~475 km | **Likely re-entered by 2026 -- REMOVE** |
| Spire Lemur-2 | 4 | ~400-500 km | **Likely re-entered by 2026 -- REMOVE** |
| SL-16 R/B | 5 | ~600-850 km | **Most above 600 km -- REMOVE** |
| Other rocket bodies | 5 | ~400-800 km | **Mixed altitudes -- evaluate individually** |

## Objects to remove (78 objects)

### Cosmos 2251 debris (15 objects) -- all above 600 km

The Cosmos 2251/Iridium 33 collision occurred at ~790 km. Debris field is predominantly at 700-900 km, well above the 600 km cutoff.

| NORAD ID | Name | Est. altitude | Reason |
|----------|------|--------------|--------|
| 34454 | COSMOS 2251 DEB | ~780 km | Above 600 km |
| 34455 | COSMOS 2251 DEB | ~780 km | Above 600 km |
| 34456 | COSMOS 2251 DEB | ~790 km | Above 600 km |
| 34457 | COSMOS 2251 DEB | ~770 km | Above 600 km |
| 34458 | COSMOS 2251 DEB | ~780 km | Above 600 km |
| 34459 | COSMOS 2251 DEB | ~785 km | Above 600 km |
| 34460 | COSMOS 2251 DEB | ~790 km | Above 600 km |
| 34461 | COSMOS 2251 DEB | ~775 km | Above 600 km |
| 34462 | COSMOS 2251 DEB | ~780 km | Above 600 km |
| 34463 | COSMOS 2251 DEB | ~785 km | Above 600 km |
| 34464 | COSMOS 2251 DEB | ~790 km | Above 600 km |
| 34465 | COSMOS 2251 DEB | ~775 km | Above 600 km |
| 34466 | COSMOS 2251 DEB | ~780 km | Above 600 km |
| 34467 | COSMOS 2251 DEB | ~785 km | Above 600 km |
| 34468 | COSMOS 2251 DEB | ~790 km | Above 600 km |

### Iridium 33 debris (10 objects) -- all above 600 km

Same collision event as Cosmos 2251. Debris at ~770-850 km.

| NORAD ID | Name | Est. altitude | Reason |
|----------|------|--------------|--------|
| 33774 | IRIDIUM 33 DEB | ~780 km | Above 600 km |
| 33775 | IRIDIUM 33 DEB | ~785 km | Above 600 km |
| 33776 | IRIDIUM 33 DEB | ~790 km | Above 600 km |
| 33777 | IRIDIUM 33 DEB | ~775 km | Above 600 km |
| 33778 | IRIDIUM 33 DEB | ~780 km | Above 600 km |
| 33779 | IRIDIUM 33 DEB | ~785 km | Above 600 km |
| 33780 | IRIDIUM 33 DEB | ~790 km | Above 600 km |
| 33781 | IRIDIUM 33 DEB | ~775 km | Above 600 km |
| 33782 | IRIDIUM 33 DEB | ~780 km | Above 600 km |
| 33783 | IRIDIUM 33 DEB | ~785 km | Above 600 km |

### Fengyun 1C debris (5 objects) -- above 600 km

Fengyun 1C ASAT test occurred at ~865 km. Debris field spans ~200-3500 km but the specific pieces in catalog are high-altitude.

| NORAD ID | Name | Est. altitude | Reason |
|----------|------|--------------|--------|
| 29507 | FENGYUN 1C DEB | ~850 km | Above 600 km |
| 29508 | FENGYUN 1C DEB | ~860 km | Above 600 km |
| 29509 | FENGYUN 1C DEB | ~840 km | Above 600 km |
| 29510 | FENGYUN 1C DEB | ~855 km | Above 600 km |
| 29511 | FENGYUN 1C DEB | ~845 km | Above 600 km |

### Starlink -- reduce from 43 to 10

Starlink V1 satellites operate at ~550 km (within cutoff), but 43 is over-represented for a 95-object catalog. Retain 10 for adequate Starlink representation, remove 33.

**Retain (10):**

| NORAD ID | Name | Reason to keep |
|----------|------|---------------|
| 44235 | STARLINK-24 | Early batch representative |
| 44700 | STARLINK-1007 | Mid-batch representative |
| 44914 | STARLINK-1095 | Mid-batch representative |
| 45178 | STARLINK-1306 | Mid-batch representative |
| 45530 | STARLINK-1571 | Mid-batch representative |
| 45706 | STARLINK-1706 | Mid-batch representative |
| 45800 | STARLINK-1800 | Mid-batch representative |
| 46010 | STARLINK-1925 | Late-batch representative |
| 46050 | STARLINK-1965 | Late-batch representative |
| 46075 | STARLINK-1990 | **Keep -- live anomaly detected 2026-03-31** |

**Remove (33):**

| NORAD ID | Name | Reason |
|----------|------|--------|
| 44236 | STARLINK-25 | Redundant Starlink representation |
| 44237 | STARLINK-26 | Redundant |
| 44238 | STARLINK-27 | Redundant |
| 44240 | STARLINK-29 | Redundant |
| 44241 | STARLINK-30 | Redundant |
| 44242 | STARLINK-31 | Redundant |
| 44244 | STARLINK-33 | Redundant |
| 44249 | STARLINK-38 | Redundant |
| 44713 | STARLINK-1020 | Redundant |
| 44725 | STARLINK-1032 | Redundant |
| 44920 | STARLINK-1101 | Redundant |
| 44935 | STARLINK-1116 | Redundant |
| 45044 | STARLINK-1177 | Redundant |
| 45060 | STARLINK-1193 | Redundant |
| 45360 | STARLINK-1436 | Redundant |
| 45535 | STARLINK-1576 | Redundant |
| 45551 | STARLINK-1592 | Redundant |
| 45555 | STARLINK-1596 | Redundant |
| 45720 | STARLINK-1720 | Redundant |
| 45730 | STARLINK-1730 | Redundant |
| 45740 | STARLINK-1740 | Redundant |
| 45750 | STARLINK-1750 | Redundant |
| 45764 | STARLINK-1764 | Redundant |
| 45773 | STARLINK-1773 | Redundant |
| 45783 | STARLINK-1783 | Redundant |
| 46020 | STARLINK-1935 | Redundant |
| 46030 | STARLINK-1945 | Redundant |
| 46040 | STARLINK-1955 | Redundant |
| 46055 | STARLINK-1970 | Redundant |
| 46060 | STARLINK-1975 | Redundant |
| 46065 | STARLINK-1980 | Redundant |
| 46070 | STARLINK-1985 | Redundant |
| 46080 | STARLINK-1995 | Redundant |

### Planet Labs Flock (early gen) -- likely re-entered

Flock 1B/1C launched 2014, deployed from ISS at ~400 km. Designed for 1-3 year mission life. Very likely de-orbited by 2026.

| NORAD ID | Name | Reason |
|----------|------|--------|
| 40075 | FLOCK 1B-1 | Probable re-entry by 2026 |
| 40076 | FLOCK 1B-2 | Probable re-entry by 2026 |
| 40379 | FLOCK 1C-1 | Probable re-entry by 2026 |
| 40380 | FLOCK 1C-2 | Probable re-entry by 2026 |

### Planet Labs Flock 3P/3R -- likely re-entered

Flock 3P launched 2017, Flock 3R launched 2018. ISS-deployed at ~400 km, 2-3 year operational life. Likely de-orbited by 2026.

| NORAD ID | Name | Reason |
|----------|------|--------|
| 43013 | FLOCK 3P-1 | Probable re-entry by 2026 |
| 43015 | FLOCK 3P-3 | Probable re-entry by 2026 |
| 43017 | FLOCK 3P-5 | Probable re-entry by 2026 |
| 43019 | FLOCK 3P-7 | Probable re-entry by 2026 |
| 43565 | FLOCK 3R-1 | Probable re-entry by 2026 |
| 43567 | FLOCK 3R-3 | Probable re-entry by 2026 |
| 43569 | FLOCK 3R-5 | Probable re-entry by 2026 |

### Spire Lemur-2 -- likely re-entered

Lemur-2 CubeSats launched 2016-2019, ISS-deployed or low-altitude PSLV. 3U CubeSats at ~400-500 km with no propulsion. Expected 2-4 year orbital life.

| NORAD ID | Name | Reason |
|----------|------|--------|
| 41474 | LEMUR-2-JOEL | Probable re-entry by 2026 |
| 41469 | LEMUR-2-BROWNCOW | Probable re-entry by 2026 |
| 44804 | LEMUR-2-KRYWE | Probable re-entry by 2026 |
| 44807 | LEMUR-2-ROHOVIT | Probable re-entry by 2026 |

### Rocket bodies -- above 600 km or stale

SL-16 (Zenit-2 upper stage) bodies orbit at ~600-850 km. Most are above cutoff. Other rocket bodies need individual evaluation.

| NORAD ID | Name | Est. altitude | Reason |
|----------|------|--------------|--------|
| 22285 | SL-16 R/B | ~840 km | Above 600 km |
| 16182 | SL-16 R/B | ~610 km | Above 600 km (marginal) |
| 23088 | SL-16 R/B | ~830 km | Above 600 km |
| 25400 | SL-16 R/B | ~645 km | Above 600 km |
| 28222 | SL-16 R/B | ~620 km | Above 600 km |
| 27424 | DELTA 1 R/B | ~800 km | Above 600 km |
| 27386 | ATLAS 5 CENTAUR R/B | ~750 km | Above 600 km |
| 37239 | CZ-4C R/B | ~640 km | Above 600 km |
| 38253 | CZ-2D R/B | ~480 km | **KEEP -- within cutoff** |
| 39458 | CZ-4B R/B | ~620 km | Above 600 km (marginal) |

**Retained from original catalog: 38253 (CZ-2D R/B)**. Verify altitude from TLE.

**Total removed: 78 objects.** Retained from original: 12 (ISS, HST, 10 Starlink) + 1 rocket body (CZ-2D R/B) = 13 objects. Need 82 new objects to reach ~95.

---

## Objects to add (82 objects)

### Crewed stations (1 new object)

| NORAD ID | Name | Est. altitude | Object class | Operational relevance | Verification needed |
|----------|------|--------------|-------------|----------------------|-------------------|
| 48274 | CSS (TIANHE) | ~385 km | `active_satellite` | Chinese Space Station core module -- crewed station tracking alongside ISS | **VERIFY**: This is the Tianhe core module NORAD ID. TLE availability should be confirmed. |

### BlackSky constellation -- EO/ISR (~450 km, 5 objects)

BlackSky operates Gen-3 Earth observation satellites for defense/intelligence customers. Revisit-optimized constellation for ISR.

| NORAD ID | Name | Est. altitude | Object class | Operational relevance | Verification needed |
|----------|------|--------------|-------------|----------------------|-------------------|
| 47474 | BLACKSKY GLOBAL-7 | ~450 km | `active_satellite` | Commercial ISR | VERIFY TLE currency |
| 47475 | BLACKSKY GLOBAL-8 | ~450 km | `active_satellite` | Commercial ISR | VERIFY TLE currency |
| 49432 | BLACKSKY GLOBAL-9 | ~450 km | `active_satellite` | Commercial ISR | VERIFY TLE currency |
| 49433 | BLACKSKY GLOBAL-10 | ~450 km | `active_satellite` | Commercial ISR | VERIFY TLE currency |
| 51067 | BLACKSKY GLOBAL-11 | ~450 km | `active_satellite` | Commercial ISR | VERIFY TLE currency |

### Capella Space SAR constellation (~525 km, 5 objects)

Capella operates X-band SAR satellites. Defense customers use for all-weather ISR.

| NORAD ID | Name | Est. altitude | Object class | Operational relevance | Verification needed |
|----------|------|--------------|-------------|----------------------|-------------------|
| 46496 | CAPELLA-2 (SEQUOIA) | ~525 km | `active_satellite` | SAR/ISR, all-weather | VERIFY TLE currency |
| 48899 | CAPELLA-5 | ~525 km | `active_satellite` | SAR/ISR | VERIFY TLE currency |
| 48900 | CAPELLA-6 | ~525 km | `active_satellite` | SAR/ISR | VERIFY TLE currency |
| 51069 | CAPELLA-7 | ~525 km | `active_satellite` | SAR/ISR | VERIFY TLE currency |
| 51070 | CAPELLA-8 | ~525 km | `active_satellite` | SAR/ISR | VERIFY TLE currency |

### Umbra Space SAR (~450-525 km, 4 objects)

Umbra operates commercial SAR satellites with ultra-high-resolution modes.

| NORAD ID | Name | Est. altitude | Object class | Operational relevance | Verification needed |
|----------|------|--------------|-------------|----------------------|-------------------|
| 51074 | UMBRA-04 | ~500 km | `active_satellite` | High-res SAR | VERIFY TLE currency and NORAD ID |
| 53087 | UMBRA-05 | ~500 km | `active_satellite` | High-res SAR | VERIFY |
| 53088 | UMBRA-06 | ~500 km | `active_satellite` | High-res SAR | VERIFY |
| 55548 | UMBRA-07 | ~500 km | `active_satellite` | High-res SAR | VERIFY |

### ICEYE SAR constellation (~570 km, 6 objects)

Finnish-American SAR constellation. Defense and intelligence applications.

| NORAD ID | Name | Est. altitude | Object class | Operational relevance | Verification needed |
|----------|------|--------------|-------------|----------------------|-------------------|
| 43114 | ICEYE-X1 | ~570 km | `active_satellite` | SAR/ISR | VERIFY -- early unit, may have lowered |
| 46495 | ICEYE-X6 | ~570 km | `active_satellite` | SAR/ISR | VERIFY TLE currency |
| 47510 | ICEYE-X7 | ~570 km | `active_satellite` | SAR/ISR | VERIFY TLE currency |
| 48918 | ICEYE-X9 | ~570 km | `active_satellite` | SAR/ISR | VERIFY TLE currency |
| 49438 | ICEYE-X11 | ~570 km | `active_satellite` | SAR/ISR | VERIFY TLE currency |
| 51073 | ICEYE-X14 | ~570 km | `active_satellite` | SAR/ISR | VERIFY TLE currency |

### Satellogic Earth observation (~500 km, 5 objects)

Argentine EO constellation, sub-meter imagery. Defense/intelligence applications.

| NORAD ID | Name | Est. altitude | Object class | Operational relevance | Verification needed |
|----------|------|--------------|-------------|----------------------|-------------------|
| 52190 | NUSAT-31 (MARIE) | ~500 km | `active_satellite` | EO/ISR | VERIFY TLE currency |
| 52191 | NUSAT-32 (ADA) | ~500 km | `active_satellite` | EO/ISR | VERIFY TLE currency |
| 52175 | NUSAT-33 | ~500 km | `active_satellite` | EO/ISR | VERIFY |
| 52176 | NUSAT-34 | ~500 km | `active_satellite` | EO/ISR | VERIFY |
| 53107 | NUSAT-35 | ~500 km | `active_satellite` | EO/ISR | VERIFY |

### HawkEye 360 RF geolocation (~575 km, 6 objects)

HawkEye 360 detects and geolocates RF emissions from space. DoD customer for maritime domain awareness and SIGINT.

| NORAD ID | Name | Est. altitude | Object class | Operational relevance | Verification needed |
|----------|------|--------------|-------------|----------------------|-------------------|
| 43810 | HAWKEYE PATHFINDER 1 | ~575 km | `active_satellite` | RF geolocation/SIGINT | VERIFY -- launched 2018, may have lowered |
| 43811 | HAWKEYE PATHFINDER 2 | ~575 km | `active_satellite` | RF geolocation/SIGINT | VERIFY |
| 43812 | HAWKEYE PATHFINDER 3 | ~575 km | `active_satellite` | RF geolocation/SIGINT | VERIFY |
| 49736 | HAWKEYE CLUSTER 2-1 | ~575 km | `active_satellite` | RF geolocation/SIGINT | VERIFY TLE currency |
| 49737 | HAWKEYE CLUSTER 2-2 | ~575 km | `active_satellite` | RF geolocation/SIGINT | VERIFY TLE currency |
| 49738 | HAWKEYE CLUSTER 2-3 | ~575 km | `active_satellite` | RF geolocation/SIGINT | VERIFY TLE currency |

### Planet Labs SuperDoves (current gen, ~475-525 km, 10 objects)

Planet SuperDoves are the current operational generation, launched 2020+. Sun-synchronous orbit ~475-525 km. Likely still active in 2026.

| NORAD ID | Name | Est. altitude | Object class | Operational relevance | Verification needed |
|----------|------|--------------|-------------|----------------------|-------------------|
| 46853 | FLOCK 4S-1 | ~475 km | `active_satellite` | EO, defense analytics | VERIFY TLE currency |
| 46854 | FLOCK 4S-2 | ~475 km | `active_satellite` | EO | VERIFY |
| 46855 | FLOCK 4S-3 | ~475 km | `active_satellite` | EO | VERIFY |
| 48897 | FLOCK 4X-1 | ~525 km | `active_satellite` | EO | VERIFY |
| 48898 | FLOCK 4X-2 | ~525 km | `active_satellite` | EO | VERIFY |
| 49808 | FLOCK 4V-1 | ~500 km | `active_satellite` | EO | VERIFY |
| 49809 | FLOCK 4V-2 | ~500 km | `active_satellite` | EO | VERIFY |
| 49810 | FLOCK 4V-3 | ~500 km | `active_satellite` | EO | VERIFY |
| 52173 | FLOCK 4Y-1 | ~500 km | `active_satellite` | EO | VERIFY |
| 52174 | FLOCK 4Y-2 | ~500 km | `active_satellite` | EO | VERIFY |

### Swarm Technologies / SpaceX (~450-550 km, 5 objects)

SpaceBEE IoT constellation (acquired by SpaceX 2021). 0.25U CubeSats, LEO IoT relay. Relevant to military mesh/relay demonstration.

| NORAD ID | Name | Est. altitude | Object class | Operational relevance | Verification needed |
|----------|------|--------------|-------------|----------------------|-------------------|
| 44117 | SPACEBEE-8 | ~500 km | `active_satellite` | IoT/C2 relay demo | VERIFY -- early unit, altitude uncertain |
| 44118 | SPACEBEE-9 | ~500 km | `active_satellite` | IoT/C2 relay | VERIFY |
| 47522 | SPACEBEE-88 | ~500 km | `active_satellite` | IoT/C2 relay | VERIFY |
| 47523 | SPACEBEE-89 | ~500 km | `active_satellite` | IoT/C2 relay | VERIFY |
| 47524 | SPACEBEE-90 | ~500 km | `active_satellite` | IoT/C2 relay | VERIFY |

### Low-altitude debris (<=600 km, 15 objects)

Finding tracked debris at <=600 km is harder because drag removes small objects quickly. However, large debris objects from recent events and high-area-to-mass-ratio objects persist. The following are candidate sources:

**Cosmos 1408 ASAT test debris (Nov 2021, ~480 km)** -- Russia's ASAT test generated ~1500 tracked fragments at ISS altitude. Some fragments have descended to <=400 km by 2026 while others remain near 480 km. This is the most operationally relevant debris field for VLEO.

| NORAD ID | Name | Est. altitude | Object class | Operational relevance | Verification needed |
|----------|------|--------------|-------------|----------------------|-------------------|
| 49863 | COSMOS 1408 DEB | ~450 km | `debris` | ASAT test debris, ISS threat | **VERIFY** altitude via TLE -- some decaying |
| 49864 | COSMOS 1408 DEB | ~460 km | `debris` | ASAT test debris | VERIFY |
| 49865 | COSMOS 1408 DEB | ~470 km | `debris` | ASAT test debris | VERIFY |
| 49866 | COSMOS 1408 DEB | ~455 km | `debris` | ASAT test debris | VERIFY |
| 49867 | COSMOS 1408 DEB | ~465 km | `debris` | ASAT test debris | VERIFY |
| 49868 | COSMOS 1408 DEB | ~440 km | `debris` | ASAT test debris | VERIFY |
| 49869 | COSMOS 1408 DEB | ~450 km | `debris` | ASAT test debris | VERIFY |
| 49870 | COSMOS 1408 DEB | ~460 km | `debris` | ASAT test debris | VERIFY |
| 49871 | COSMOS 1408 DEB | ~470 km | `debris` | ASAT test debris | VERIFY |
| 49872 | COSMOS 1408 DEB | ~445 km | `debris` | ASAT test debris | VERIFY |
| 49873 | COSMOS 1408 DEB | ~455 km | `debris` | ASAT test debris | VERIFY |
| 49874 | COSMOS 1408 DEB | ~460 km | `debris` | ASAT test debris | VERIFY |
| 49875 | COSMOS 1408 DEB | ~465 km | `debris` | ASAT test debris | VERIFY |
| 49876 | COSMOS 1408 DEB | ~470 km | `debris` | ASAT test debris | VERIFY |
| 49877 | COSMOS 1408 DEB | ~475 km | `debris` | ASAT test debris | VERIFY |

**Note on Cosmos 1408 debris:** These NORAD IDs are sequential assignments starting from the first tracked fragments. By April 2026, some of the lower-perigee fragments may have re-entered. The implementer **must** verify each ID has a current TLE (epoch within the last 30 days) before including it. If any have re-entered, substitute with the next sequential Cosmos 1408 DEB ID (49878, 49879, etc.) until 15 valid debris objects are obtained.

### Rocket bodies at <=600 km (5 objects)

Low-altitude rocket bodies are relatively rare (most deorbit or are in higher orbits). Candidates include recent Chinese launch stages and ISS cargo vehicle adapters.

| NORAD ID | Name | Est. altitude | Object class | Operational relevance | Verification needed |
|----------|------|--------------|-------------|----------------------|-------------------|
| 38253 | CZ-2D R/B | ~480 km | `rocket_body` | **RETAINED** from current catalog | Verify altitude from TLE |
| 48275 | CZ-5B R/B | ~350-380 km | `rocket_body` | CSS launch debris -- large, ISS-altitude | **HIGH RISK**: CZ-5B stages have short orbital life, may have re-entered |
| 52765 | CZ-5B R/B | ~350-380 km | `rocket_body` | CSS launch debris | **HIGH RISK**: Same concern |
| 54216 | FALCON 9 R/B | ~400-500 km | `rocket_body` | SpaceX rideshare stage | VERIFY -- may have deorbited |
| 53819 | CZ-2C R/B | ~500 km | `rocket_body` | Chinese LEO launch stage | VERIFY TLE currency |
| 44354 | CZ-4B R/B | ~490 km | `rocket_body` | Chinese LEO launch stage | VERIFY TLE currency |

**Note on rocket bodies:** Low-altitude rocket bodies are the highest re-entry risk category. The implementer must verify all 6 candidates and may need to search Space-Track for substitutes. Query: `class/gp/OBJECT_TYPE/rocket body/MEAN_MOTION/>14.5/orderby/NORAD_CAT_ID/format/json` to find rocket bodies with mean motion >14.5 rev/day (corresponding to roughly <=600 km altitude).

---

## Final catalog composition target (95 objects)

| Category | Count | Altitude band | Object class |
|----------|-------|--------------|-------------|
| ISS | 1 | ~420 km | `active_satellite` |
| CSS (Tianhe) | 1 | ~385 km | `active_satellite` |
| HST | 1 | ~535 km | `active_satellite` |
| Starlink (sampled) | 10 | ~550 km | `active_satellite` |
| BlackSky | 5 | ~450 km | `active_satellite` |
| Capella Space SAR | 5 | ~525 km | `active_satellite` |
| Umbra Space SAR | 4 | ~500 km | `active_satellite` |
| ICEYE SAR | 6 | ~570 km | `active_satellite` |
| Satellogic | 5 | ~500 km | `active_satellite` |
| HawkEye 360 | 6 | ~575 km | `active_satellite` |
| Planet SuperDoves | 10 | ~475-525 km | `active_satellite` |
| Swarm/SpaceBEE | 5 | ~500 km | `active_satellite` |
| Cosmos 1408 debris | 15 | ~440-475 km | `debris` |
| CZ-2D R/B (retained) | 1 | ~480 km | `rocket_body` |
| New rocket bodies | 5 | ~350-500 km | `rocket_body` |
| **Total** | **80** | **<=600 km** | **3 classes** |

**Note:** Count is 80 before verification. If all rocket body and debris candidates verify successfully with some spares from Cosmos 1408 sequential IDs, count can be pushed to 85-95. The implementer should fill to 90-100 if substitutes are available.

## Validation approach

### Altitude verification formula

Each NORAD ID must have its altitude verified before inclusion. The procedure:

1. Retrieve the latest TLE from Space-Track for the candidate NORAD ID
2. Extract mean motion `n` from TLE line 2 (columns 53-63, in revolutions/day)
3. Compute semi-major axis: `a_km = (mu_earth / (2 * pi * n / 86400)^2)^(1/3)` where `mu_earth = 398600.4418 km^3/s^2`
4. Compute mean altitude: `alt_km = a_km - R_earth` where `R_earth = 6378.137 km`
5. Accept only if `alt_km <= 600.0`

**Units note:** Mean motion in TLE is in rev/day. Convert to rad/s: `n_rad_s = n_rev_day * 2 * pi / 86400`.

### TLE currency check

Each candidate NORAD ID must have a TLE epoch within the last 30 days of the catalog build date. Objects with stale TLEs (epoch >30 days old) are assumed re-entered or not tracked and must be excluded.

### Verification script

The implementer should write a one-time verification script (`scripts/verify_catalog_altitudes.py`) that:
1. Reads the proposed catalog.json
2. For each entry, retrieves the latest TLE from the local cache (or Space-Track if cached data is unavailable)
3. Computes altitude using the formula above
4. Prints a report: NORAD ID, name, mean motion, computed altitude, pass/fail
5. Flags any object above 600 km or with a stale TLE

This script is a build-time tool, not a runtime dependency. It does not need tests but must use `ingest.py` for any Space-Track calls (per architecture constraint).

## Data flow changes

No data flow changes. The catalog.json format (`norad_id`, `name`, `object_class`) is unchanged. `ingest.py` loads it the same way. The only change is the content of the JSON array.

## Files affected

- **`data/catalog/catalog.json`** -- Complete replacement of the JSON array contents. Same schema, different NORAD IDs.
- **`scripts/verify_catalog_altitudes.py`** -- New file. One-time verification script for altitude and TLE currency validation.
- **`data/catalog/tle_cache.db`** -- Will need to be rebuilt after catalog change (old TLE cache contains data for removed NORAD IDs). Run `scripts/replay.py --hours 72` to rebuild.

## Implementation steps

### Phase 1: Verification

1. **Write altitude verification script** (`scripts/verify_catalog_altitudes.py`)
   - Action: Create script that reads a candidate catalog JSON, queries Space-Track for latest TLE per NORAD ID, computes mean altitude, reports pass/fail
   - Why: Must validate all proposed NORAD IDs before committing catalog
   - Dependencies: Requires Space-Track credentials and network access
   - Risk: Medium -- some NORAD IDs may be invalid or re-entered

2. **Run verification against all 82 proposed new NORAD IDs**
   - Action: Execute verification script, collect results
   - Why: Cannot commit unverified NORAD IDs
   - Dependencies: Step 1
   - Risk: Medium -- expect 5-15% of candidates to fail (re-entered or stale TLE)

3. **Identify substitutes for failed candidates**
   - Action: For each failed debris ID, try the next sequential Cosmos 1408 DEB ID. For failed satellites, search Space-Track for same constellation/operator. For failed rocket bodies, query by OBJECT_TYPE and MEAN_MOTION range.
   - Why: Must maintain target count of 80-100
   - Dependencies: Step 2
   - Risk: Low -- Cosmos 1408 debris field alone has ~1500 tracked fragments

### Phase 2: Catalog replacement

4. **Build new catalog.json** (`data/catalog/catalog.json`)
   - Action: Replace entire JSON array with verified objects. Maintain same schema. Group by operational category with blank-line separators for readability.
   - Why: Core deliverable of this plan
   - Dependencies: Step 3
   - Risk: Low

5. **Clear and rebuild TLE cache**
   - Action: Delete `data/catalog/tle_cache.db`, run `scripts/replay.py --hours 72` to fetch fresh TLEs for the new catalog
   - Why: Old cache contains TLEs for removed objects; new objects need initial data
   - Dependencies: Step 4, requires Space-Track access
   - Risk: Low

6. **Smoke test**
   - Action: Start backend (`uvicorn backend.main:app`), verify `/catalog` endpoint returns correct count, verify WebSocket connects and begins streaming state updates for new objects
   - Why: End-to-end validation that the new catalog works with the existing pipeline
   - Dependencies: Step 5
   - Risk: Low

## Test strategy

### Unit tests -- no changes expected

Tests in `tests/test_ingest.py` use inline catalog fixtures (not the real `catalog.json`), so they are unaffected by catalog content changes. Verified by grep: test files reference `25544` (ISS) as a hardcoded fixture NORAD ID, not by loading `data/catalog/catalog.json`.

The following test files were checked and confirmed to use self-contained fixtures:
- `tests/test_ingest.py` -- uses inline JSON fixtures
- `tests/test_seed_conjunction.py` -- uses hardcoded ISS NORAD ID
- `tests/test_seed_maneuver.py` -- uses hardcoded ISS NORAD ID
- `tests/test_replay.py` -- uses inline catalog fixtures
- `tests/test_main.py` -- uses inline catalog fixtures

**No test file changes required.**

### Integration test

After catalog replacement and TLE cache rebuild:
1. `pytest tests/ -v` -- all existing tests must pass (regression)
2. Start backend, confirm `GET /catalog` returns correct object count
3. Confirm `GET /object/{norad_id}/history` works for at least one object from each new constellation (BlackSky, Capella, ICEYE, etc.)
4. Confirm WebSocket `/ws/live` streams state updates for new objects after a `POST /trigger-cycle`

### Verification script output

The `scripts/verify_catalog_altitudes.py` output should be saved as `data/catalog/altitude_verification_report.txt` for audit trail.

## Risks and mitigations

- **Risk:** Cosmos 1408 debris fragments below ~450 km may have re-entered by April 2026 due to atmospheric drag at that altitude. **Mitigation:** Verify TLE currency (epoch <30 days old). Cosmos 1408 field has ~1500 fragments; substitute with next sequential ID if any have decayed.

- **Risk:** CZ-5B rocket body stages (48275, 52765) are known for uncontrolled re-entries within months of launch. **Mitigation:** These are flagged HIGH RISK in the plan. If re-entered, replace with other low-altitude rocket bodies found via Space-Track query.

- **Risk:** Some NORAD IDs listed above may be incorrect (wrong satellite assigned to the ID, or ID reassigned after deorbit). **Mitigation:** Verification script cross-checks name from TLE against expected name. Flag mismatches for human review.

- **Risk:** Reducing Starlink from 43 to 10 objects reduces the chance of observing a live Starlink maneuver event during demos. **Mitigation:** 10 Starlinks is still sufficient -- SpaceX maneuvers hundreds of satellites daily, and 10 randomly sampled units will catch events within a few days of live monitoring. STARLINK-1990 is specifically retained because it produced a live anomaly on 2026-03-31.

- **Risk:** New catalog invalidates the existing `tle_cache.db`, so the system cannot run offline until a fresh 72-hour cache is built. **Mitigation:** Explicitly schedule a cache rebuild as a required step before any demo.

## Open questions

1. **Altitude cutoff strictness:** The plan uses 600 km as a hard cutoff. Several objects are marginal (e.g., 16182 SL-16 R/B at ~610 km, 39458 CZ-4B R/B at ~620 km). Should the cutoff have a 5% tolerance (630 km), or is 600 km absolute? **Decision needed before implementation.**

2. **Cosmos 1408 ASAT debris as primary debris source:** This is operationally compelling (ISS-threatening debris from a 2021 Russian ASAT test) but all 15 debris NORAD IDs need verification. If fewer than 10 are still in orbit, should the plan fall back to Fengyun 1C low-perigee fragments (some fragments have perigees below 600 km even if apogees are higher), or accept a smaller debris count? **Decision needed before implementation.**

3. **Should `seed_maneuver.py` and `seed_conjunction.py` default `--object` arguments be updated?** Both currently default to NORAD 25544 (ISS), which is retained. No change strictly required, but the demo scenario plan (`docs/plans/2026-03-29-demo-scenario.md`) may reference specific NORAD IDs from the old catalog. **Review recommended.**

4. **Architecture document section 3.1 says "20-50 objects".** F-005 was updated to "maximum 100 for POC" per the engineering log catalog expansion. The architecture document at `docs/architecture.md` line 56 still says "20-50". Should it be updated to "80-100"? **This is a documentation consistency issue, not a blocker, but NF-032 requires keeping the architecture document current.**

> **Conflict:** Architecture document section 3.1 specifies "a curated catalog of 20-50 objects" but F-005 was updated to allow up to 100, and this plan targets ~95. The architecture document needs a corresponding update. This is not a functional conflict (code supports any count) but violates NF-032 (architecture document currency). Resolution: update `docs/architecture.md` line 56 to say "80-100 objects" when this plan is implemented.
