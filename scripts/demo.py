"""5-act demo orchestration script for ne-body SSA platform DoD/Space Force presentations.

Orchestrates the full demo narrative: baseline tracking, ASAT debris conjunction with ISS,
unannounced Starlink maneuver, ISR asset repositioning, and filter recalibration.

Usage:
    python scripts/demo.py --act [1|2|3|4|5|all] [--base-url http://localhost:8001]
        [--delay-s 5] [--clean]
    python scripts/demo.py --list

Examples:
    python scripts/demo.py --list
    python scripts/demo.py --act all
    python scripts/demo.py --act 2 --base-url http://localhost:8001
    python scripts/demo.py --act all --delay-s 20 --clean
"""
import argparse
import datetime
import json
import math
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

# Add the project root to sys.path so that backend package imports work when
# this script is run as `python scripts/demo.py` from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backend.ingest as ingest
import backend.propagator as propagator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# NORAD IDs for demo objects (confirmed against data/catalog/catalog.json).
_ISS_NORAD_ID: int = 25544
_STARLINK_1990_NORAD_ID: int = 46075
_BLACKSKY7_NORAD_ID: int = 47474

# Synthetic threat object NORAD ID (same as seed_conjunction.py).
_THREAT_NORAD_ID: int = 99999
_THREAT_NAME: str = "THREAT-SIM"
_THREAT_OBJECT_CLASS: str = "debris"

# Default DB path (mirrors ingest._DEFAULT_DB_PATH).
_DEFAULT_DB_PATH: str = "data/catalog/tle_cache.db"
_DEFAULT_CATALOG_PATH: str = "data/catalog/catalog.json"

# Earth gravitational parameter (WGS72) in km^3/s^2 — matches seed_maneuver.py.
_MU_KM3_S2: float = 398600.4418
_SECONDS_PER_DAY: float = 86400.0
_REVS_PER_DAY_FACTOR: float = _SECONDS_PER_DAY / (2.0 * math.pi)

# HTTP request timeout for all backend calls (seconds).
_HTTP_TIMEOUT_S: float = 30.0

# ---------------------------------------------------------------------------
# Presenter scripts — used by both --list and the act functions.
# ---------------------------------------------------------------------------

_PRESENTER_ACT1: str = """\
Presenter: "This is the system in steady state. Every object in the catalog is being
tracked continuously. Residuals are flat, confidence is high. The filter is nominal.
This is what 'nothing happening' looks like — and it's the baseline we need to
detect when something does happen."

Waiting for baseline to establish... (no injection needed)"""

_PRESENTER_ACT2: str = """\
Presenter: "We've just detected a Cosmos 1408 debris fragment on a close approach
trajectory with ISS. Miss distance: 1.5 kilometers. Time to closest approach: 5 minutes.
The system flagged this autonomously — no operator queried for it. The crew has a
decision window right now.\""""

_PRESENTER_ACT3: str = """\
Presenter: "STARLINK-1990 just executed an unannounced maneuver. Residuals are spiking —
the filter is telling us the satellite is no longer where our model predicted.
Confidence is dropping. In 30 seconds we'll know if this is a one-time correction
or the start of a repositioning burn. Either way — we detected it. A static SGP4
prediction would have shown nothing.\""""

_PRESENTER_ACT4: str = """\
Presenter: "BlackSky Global-1 — a commercial ISR satellite with active DoD contracts —
just repositioned. Cross-track burn, consistent with a collection retasking.
The system caught it in the same cycle as the Starlink maneuver. No analyst had to
watch a screen. No threshold had to be manually tuned. The filter flagged it.\""""

_PRESENTER_ACT5: str = """\
Presenter: "Watch the residuals return to baseline. The filter has recalibrated —
it's incorporated the new orbital state for each maneuvered object and reset its
uncertainty model. Confidence is recovering. No operator intervention. No manual
reinitialization. The system healed itself.\""""

# ---------------------------------------------------------------------------
# Internal helpers — TLE manipulation (replicated from seed scripts to avoid
# importing them as modules per the plan constraint).
# ---------------------------------------------------------------------------


def _tle_checksum(line: str) -> int:
    """Compute the standard TLE modulo-10 checksum for a single line.

    Args:
        line: A TLE line string (first 68 characters used).

    Returns:
        Computed checksum integer (0-9).
    """
    total: int = 0
    for ch in line[:68]:
        if ch.isdigit():
            total += int(ch)
        elif ch == "-":
            total += 1
    return total % 10


def _format_tle_epoch(epoch_utc: datetime.datetime) -> str:
    """Format a UTC datetime as a TLE epoch field string (YYDDD.DDDDDDDD).

    Args:
        epoch_utc: UTC-aware datetime.

    Returns:
        14-character TLE epoch string, e.g. '26087.12345678'.
    """
    year_2digit: int = epoch_utc.year % 100
    jan1: datetime.datetime = datetime.datetime(
        epoch_utc.year, 1, 1, tzinfo=datetime.timezone.utc
    )
    delta: datetime.timedelta = epoch_utc - jan1
    day_of_year_frac: float = 1.0 + delta.total_seconds() / _SECONDS_PER_DAY
    return f"{year_2digit:02d}{day_of_year_frac:012.8f}"


def _format_bstar(bstar: float) -> str:
    """Format B* drag term for TLE line 1 (8 characters).

    Args:
        bstar: B* drag term in units of 1/earth_radii.

    Returns:
        8-character TLE B* field string.
    """
    if bstar == 0.0:
        return " 00000-0"

    sign: str = " " if bstar >= 0.0 else "-"
    abs_bstar: float = abs(bstar)

    if abs_bstar == 0.0:
        return " 00000-0"

    exponent: int = math.floor(math.log10(abs_bstar)) + 1
    mantissa: float = abs_bstar / (10.0 ** exponent)

    if mantissa >= 1.0:
        mantissa /= 10.0
        exponent += 1
    elif mantissa < 0.1:
        mantissa *= 10.0
        exponent -= 1

    mantissa_int: int = round(mantissa * 100000)
    if mantissa_int >= 100000:
        mantissa_int = 99999

    exp_sign: str = "+" if exponent >= 0 else "-"
    return f"{sign}{mantissa_int:05d}{exp_sign}{abs(exponent):1d}"


def _true_to_mean_anomaly_rad(
    true_anomaly_rad: float,
    eccentricity: float,
) -> float:
    """Convert true anomaly to mean anomaly.

    Args:
        true_anomaly_rad: True anomaly in radians.
        eccentricity: Orbital eccentricity (0 <= e < 1).

    Returns:
        Mean anomaly in radians, in [0, 2*pi).
    """
    e: float = eccentricity
    nu: float = true_anomaly_rad
    E: float = 2.0 * math.atan2(
        math.sqrt(1.0 - e) * math.sin(nu / 2.0),
        math.sqrt(1.0 + e) * math.cos(nu / 2.0),
    )
    M: float = E - e * math.sin(E)
    return M % (2.0 * math.pi)


def _eci_to_keplerian(
    position_eci_km: list,
    velocity_eci_km_s: list,
) -> dict:
    """Convert ECI state vector to classical Keplerian orbital elements.

    All inputs must be in ECI J2000, km and km/s.

    Args:
        position_eci_km: 3-element ECI position vector in km.
        velocity_eci_km_s: 3-element ECI velocity vector in km/s.

    Returns:
        Dict with keys: a_km, e, i_rad, raan_rad, argp_rad, true_anomaly_rad.

    Raises:
        ValueError: If the state is degenerate.
    """
    import numpy as np

    r_vec = np.array(position_eci_km, dtype=np.float64)
    v_vec = np.array(velocity_eci_km_s, dtype=np.float64)

    r_mag: float = float(np.linalg.norm(r_vec))
    v_mag: float = float(np.linalg.norm(v_vec))

    if r_mag < 1e-10:
        raise ValueError("position_eci_km is a zero vector")
    if v_mag < 1e-10:
        raise ValueError("velocity_eci_km_s is a zero vector")

    h_vec = np.cross(r_vec, v_vec).astype(np.float64)
    h_mag: float = float(np.linalg.norm(h_vec))
    if h_mag < 1e-10:
        raise ValueError("h = r x v is zero — rectilinear trajectory")

    k_hat = np.array([0.0, 0.0, 1.0], dtype=np.float64)
    n_vec = np.cross(k_hat, h_vec).astype(np.float64)
    n_mag: float = float(np.linalg.norm(n_vec))

    e_vec = (np.cross(v_vec, h_vec).astype(np.float64) / _MU_KM3_S2 - r_vec / r_mag)
    e_mag: float = float(np.linalg.norm(e_vec))

    a_km: float = 1.0 / (2.0 / r_mag - v_mag ** 2 / _MU_KM3_S2)
    i_rad: float = math.acos(max(-1.0, min(1.0, float(h_vec[2]) / h_mag)))

    if n_mag < 1e-10:
        raan_rad: float = 0.0
    else:
        raan_rad = math.acos(max(-1.0, min(1.0, float(n_vec[0]) / n_mag)))
        if n_vec[1] < 0.0:
            raan_rad = 2.0 * math.pi - raan_rad

    if e_mag < 1e-10:
        argp_rad: float = 0.0
    elif n_mag < 1e-10:
        argp_rad = math.acos(max(-1.0, min(1.0, float(e_vec[0]) / e_mag)))
        if e_vec[1] < 0.0:
            argp_rad = 2.0 * math.pi - argp_rad
    else:
        argp_rad = math.acos(
            max(-1.0, min(1.0, float(np.dot(n_vec, e_vec)) / (n_mag * e_mag)))
        )
        if e_vec[2] < 0.0:
            argp_rad = 2.0 * math.pi - argp_rad

    if e_mag < 1e-10:
        if n_mag < 1e-10:
            true_anomaly_rad: float = math.acos(
                max(-1.0, min(1.0, float(r_vec[0]) / r_mag))
            )
            if float(r_vec[1]) < 0.0:
                true_anomaly_rad = 2.0 * math.pi - true_anomaly_rad
        else:
            true_anomaly_rad = math.acos(
                max(-1.0, min(1.0, float(np.dot(n_vec, r_vec)) / (n_mag * r_mag)))
            )
            if float(r_vec[2]) < 0.0:
                true_anomaly_rad = 2.0 * math.pi - true_anomaly_rad
    else:
        true_anomaly_rad = math.acos(
            max(-1.0, min(1.0, float(np.dot(e_vec, r_vec)) / (e_mag * r_mag)))
        )
        if float(np.dot(r_vec, v_vec)) < 0.0:
            true_anomaly_rad = 2.0 * math.pi - true_anomaly_rad

    return {
        "a_km": a_km,
        "e": e_mag,
        "i_rad": i_rad,
        "raan_rad": raan_rad,
        "argp_rad": argp_rad,
        "true_anomaly_rad": true_anomaly_rad,
    }


def _rsw_to_eci_delta_v_km_s(
    position_eci_km: list,
    velocity_eci_km_s: list,
    delta_v_radial_km_s: float,
    delta_v_along_track_km_s: float,
    delta_v_cross_track_km_s: float,
) -> list:
    """Convert a delta-V in RSW frame to ECI.

    Args:
        position_eci_km: 3-element ECI position vector in km.
        velocity_eci_km_s: 3-element ECI velocity vector in km/s.
        delta_v_radial_km_s: Delta-V radial component in km/s.
        delta_v_along_track_km_s: Delta-V along-track component in km/s.
        delta_v_cross_track_km_s: Delta-V cross-track component in km/s.

    Returns:
        3-element ECI delta-V vector in km/s.

    Raises:
        ValueError: If position or velocity are zero vectors.
    """
    import numpy as np

    r_vec = np.array(position_eci_km, dtype=np.float64)
    v_vec = np.array(velocity_eci_km_s, dtype=np.float64)

    r_norm: float = float(np.linalg.norm(r_vec))
    if r_norm < 1e-10:
        raise ValueError("position_eci_km is a zero vector")

    r_hat = r_vec / r_norm
    h_vec = np.cross(r_vec, v_vec).astype(np.float64)
    h_norm: float = float(np.linalg.norm(h_vec))
    if h_norm < 1e-10:
        raise ValueError("position x velocity is a zero vector")

    w_hat = h_vec / h_norm
    s_hat = np.cross(w_hat, r_hat).astype(np.float64)

    dv_eci = (
        delta_v_radial_km_s * r_hat
        + delta_v_along_track_km_s * s_hat
        + delta_v_cross_track_km_s * w_hat
    )
    return dv_eci.tolist()


def _keplerian_to_tle_lines(
    norad_id: int,
    epoch_utc: datetime.datetime,
    a_km: float,
    e: float,
    i_rad: float,
    raan_rad: float,
    argp_rad: float,
    mean_anomaly_rad: float,
    bstar: float,
) -> tuple:
    """Format Keplerian orbital elements as TLE line 1 and line 2 strings.

    Args:
        norad_id: NORAD catalog ID (5-digit integer).
        epoch_utc: UTC epoch for the TLE. Must be UTC-aware.
        a_km: Semi-major axis in km.
        e: Eccentricity (dimensionless, must be in [0, 1)).
        i_rad: Inclination in radians.
        raan_rad: Right ascension of ascending node in radians.
        argp_rad: Argument of perigee in radians.
        mean_anomaly_rad: Mean anomaly in radians.
        bstar: B* drag term in units of 1/earth_radii.

    Returns:
        Tuple (tle_line1, tle_line2) as 69-character strings.

    Raises:
        ValueError: If epoch_utc is not UTC-aware or eccentricity is out of range.
    """
    if epoch_utc.tzinfo is None:
        raise ValueError("epoch_utc must be UTC-aware")
    if not (0.0 <= e < 1.0):
        raise ValueError(f"eccentricity must be in [0, 1), got {e}")

    n_rad_s: float = math.sqrt(_MU_KM3_S2 / (a_km ** 3))
    n_rev_day: float = n_rad_s * _REVS_PER_DAY_FACTOR

    i_deg: float = math.degrees(i_rad)
    raan_deg: float = math.degrees(raan_rad) % 360.0
    argp_deg: float = math.degrees(argp_rad) % 360.0
    M_deg: float = math.degrees(mean_anomaly_rad) % 360.0

    e_int: int = round(e * 1e7)
    if e_int >= 10000000:
        e_int = 9999999

    epoch_str: str = _format_tle_epoch(epoch_utc)
    bstar_str: str = _format_bstar(bstar)

    intl_desig: str = "26001A  "
    first_deriv: str = " .00000000"
    second_deriv: str = " 00000-0"
    elem_set_num: int = 999

    line1_body: str = (
        f"1 {norad_id:05d}U {intl_desig} {epoch_str} "
        f"{first_deriv} {second_deriv} {bstar_str} 0 {elem_set_num:4d}"
    )
    cs1: int = _tle_checksum(line1_body)
    tle_line1: str = line1_body + str(cs1)

    rev_num: int = 0
    line2_body: str = (
        f"2 {norad_id:05d} "
        f"{i_deg:8.4f} "
        f"{raan_deg:8.4f} "
        f"{e_int:07d} "
        f"{argp_deg:8.4f} "
        f"{M_deg:8.4f} "
        f"{n_rev_day:11.8f}"
        f"{rev_num:5d}"
    )
    cs2: int = _tle_checksum(line2_body)
    tle_line2: str = line2_body + str(cs2)

    return tle_line1, tle_line2


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------


def _post_trigger_process(base_url: str) -> None:
    """POST to /admin/trigger-process and print [OK] or [ERROR: ...].

    Args:
        base_url: Backend base URL, e.g. 'http://localhost:8001'.
    """
    url: str = f"{base_url.rstrip('/')}/admin/trigger-process"
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S) as client:
            resp = client.post(url)
            resp.raise_for_status()
            result: dict = resp.json()
            processed: int = result.get("processed", 0)
            print(f"[OK] trigger-process: {processed} objects processed")
    except httpx.HTTPStatusError as exc:
        print(f"[ERROR: trigger-process HTTP {exc.response.status_code}]")
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR: trigger-process unreachable — {exc}]")


# ---------------------------------------------------------------------------
# Injection helpers (replicated from seed scripts; no module import)
# ---------------------------------------------------------------------------


def _resolve_db_path() -> str:
    """Return the DB path from env var or default.

    Returns:
        Resolved SQLite DB path string.
    """
    return os.environ.get("NBODY_DB_PATH") or _DEFAULT_DB_PATH


def _inject_conjunction_into_db(
    primary_norad_id: int,
    offset_min: float,
    miss_km: float,
    catalog_path: str,
    db_path: str,
) -> bool:
    """Inject synthetic threat object (NORAD 99999) into catalog.json and TLE cache.

    Replicates the core logic of seed_conjunction.py without importing it as a module.
    Uses Option B (TLE mean-element space manipulation) to avoid ECI-to-TLE fitting error.

    Args:
        primary_norad_id: NORAD ID of the primary conjunction target.
        offset_min: Minutes from now to the conjunction epoch.
        miss_km: Target miss distance in km.
        catalog_path: Path to catalog.json.
        db_path: Path to SQLite TLE cache.

    Returns:
        True if injection succeeded, False if it failed.
    """
    import numpy as np
    from sgp4.api import Satrec, WGS72

    db = ingest.init_catalog_db(db_path)

    # Load primary TLE.
    tle_record: Optional[dict] = ingest.get_latest_tle(db, primary_norad_id)
    if tle_record is None:
        print(
            f"[ERROR: No cached TLE for NORAD {primary_norad_id}. "
            "Populate the TLE cache first by running the server with a "
            "live Space-Track connection or python scripts/replay.py]"
        )
        db.close()
        return False

    tle_line1: str = tle_record["tle_line1"]
    tle_line2: str = tle_record["tle_line2"]

    now_utc: datetime.datetime = datetime.datetime.now(datetime.timezone.utc)
    conjunction_epoch_utc: datetime.datetime = now_utc + datetime.timedelta(
        minutes=offset_min
    )

    # Propagate primary to conjunction epoch to get orbital radius.
    try:
        position_eci_km, _ = propagator.propagate_tle(
            tle_line1, tle_line2, conjunction_epoch_utc
        )
    except ValueError as exc:
        print(f"[ERROR: SGP4 propagation failed for primary — {exc}]")
        db.close()
        return False

    orbital_radius_km: float = float(np.linalg.norm(position_eci_km))

    ratio: float = miss_km / orbital_radius_km
    if ratio >= 1.0:
        print(
            f"[ERROR: miss_km ({miss_km:.3f} km) exceeds orbital radius "
            f"({orbital_radius_km:.1f} km) — unphysical]"
        )
        db.close()
        return False

    delta_M_deg: float = math.degrees(math.asin(ratio))

    satrec = Satrec.twoline2rv(tle_line1, tle_line2, WGS72)
    primary_M_deg: float = math.degrees(satrec.mo) % 360.0
    threat_M_deg: float = (primary_M_deg + delta_M_deg) % 360.0

    norad_str: str = f"{_THREAT_NORAD_ID:5d}"
    line1_body: str = "1 " + norad_str + tle_line1[7:68]
    syn_line1: str = line1_body + str(_tle_checksum(line1_body))

    mean_anomaly_str: str = f"{threat_M_deg:8.4f}"
    line2_body: str = "2 " + norad_str + tle_line2[7:43] + mean_anomaly_str + tle_line2[51:68]
    syn_line2: str = line2_body + str(_tle_checksum(line2_body))

    if not ingest.validate_tle(syn_line1, syn_line2):
        print("[ERROR: Generated synthetic conjunction TLE failed checksum validation]")
        db.close()
        return False

    # Insert NORAD 99999 into catalog.json.
    try:
        with open(catalog_path, "r", encoding="utf-8") as fh:
            catalog: list = json.load(fh)
    except FileNotFoundError:
        print(f"[ERROR: Catalog file not found at {catalog_path}]")
        db.close()
        return False

    catalog = [e for e in catalog if int(e.get("norad_id", 0)) != _THREAT_NORAD_ID]
    catalog.append(
        {
            "norad_id": _THREAT_NORAD_ID,
            "name": _THREAT_NAME,
            "object_class": _THREAT_OBJECT_CLASS,
        }
    )
    with open(catalog_path, "w", encoding="utf-8") as fh:
        json.dump(catalog, fh, indent=2)

    # Insert synthetic TLE into cache.
    epoch_utc_str: str = conjunction_epoch_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    tle_dict: dict = {
        "norad_id": _THREAT_NORAD_ID,
        "epoch_utc": epoch_utc_str,
        "tle_line1": syn_line1,
        "tle_line2": syn_line2,
    }
    ingest.cache_tles(db, [tle_dict], fetched_at_utc=now_utc, source="demo_injection")
    db.close()

    print(
        f"[OK] Synthetic threat NORAD {_THREAT_NORAD_ID} ({_THREAT_NAME}) injected. "
        f"Miss distance target: {miss_km} km. Conjunction epoch: {epoch_utc_str}"
    )
    return True


def _inject_maneuver_into_db(
    norad_id: int,
    delta_v_ms: float,
    direction: str,
    db_path: str,
    catalog_path: str,
) -> bool:
    """Inject a synthetic maneuver TLE into the TLE cache.

    Replicates the core logic of seed_maneuver.py without importing it as a module.
    Propagates the current TLE to now, applies delta-V in RSW frame, converts to
    Keplerian elements, and writes a new TLE to the cache.

    Args:
        norad_id: NORAD catalog ID of the target object.
        delta_v_ms: Delta-V magnitude in m/s.
        direction: One of 'along-track', 'cross-track', 'radial'.
        db_path: Path to SQLite TLE cache.
        catalog_path: Path to catalog.json (used for object name lookup only).

    Returns:
        True if injection succeeded, False if it failed.
    """
    import numpy as np
    from sgp4.api import Satrec, WGS72

    db = ingest.init_catalog_db(db_path)

    tle_record: Optional[dict] = ingest.get_latest_tle(db, norad_id)
    if tle_record is None:
        print(
            f"[ERROR: No cached TLE for NORAD {norad_id}. "
            "Populate the TLE cache first.]"
        )
        db.close()
        return False

    tle_line1: str = tle_record["tle_line1"]
    tle_line2: str = tle_record["tle_line2"]

    now_utc: datetime.datetime = datetime.datetime.now(datetime.timezone.utc)
    # epoch_offset_min=0: maneuver epoch is now.
    maneuver_epoch_utc: datetime.datetime = now_utc

    try:
        base_state_eci_km = propagator.tle_to_state_vector_eci_km(
            tle_line1, tle_line2, maneuver_epoch_utc
        )
    except ValueError as exc:
        print(f"[ERROR: SGP4 propagation failed for NORAD {norad_id} — {exc}]")
        db.close()
        return False

    base_position_eci_km = base_state_eci_km[:3].tolist()
    base_velocity_eci_km_s = base_state_eci_km[3:].tolist()

    dv_km_s: float = delta_v_ms / 1000.0

    if direction == "along-track":
        dv_radial_km_s: float = 0.0
        dv_along_km_s: float = dv_km_s
        dv_cross_km_s: float = 0.0
    elif direction == "cross-track":
        dv_radial_km_s = 0.0
        dv_along_km_s = 0.0
        dv_cross_km_s = dv_km_s
    elif direction == "radial":
        dv_radial_km_s = dv_km_s
        dv_along_km_s = 0.0
        dv_cross_km_s = 0.0
    else:
        print(
            f"[ERROR: Unknown direction '{direction}'. "
            "Use along-track, cross-track, or radial.]"
        )
        db.close()
        return False

    try:
        delta_v_eci_km_s: list = _rsw_to_eci_delta_v_km_s(
            position_eci_km=base_position_eci_km,
            velocity_eci_km_s=base_velocity_eci_km_s,
            delta_v_radial_km_s=dv_radial_km_s,
            delta_v_along_track_km_s=dv_along_km_s,
            delta_v_cross_track_km_s=dv_cross_km_s,
        )
    except ValueError as exc:
        print(f"[ERROR: RSW-to-ECI conversion failed — {exc}]")
        db.close()
        return False

    dv_arr = [delta_v_eci_km_s[i] for i in range(3)]
    perturbed_velocity_eci_km_s: list = [
        base_velocity_eci_km_s[i] + dv_arr[i] for i in range(3)
    ]

    try:
        elements: dict = _eci_to_keplerian(
            position_eci_km=base_position_eci_km,
            velocity_eci_km_s=perturbed_velocity_eci_km_s,
        )
    except ValueError as exc:
        print(f"[ERROR: ECI-to-Keplerian conversion failed — {exc}]")
        db.close()
        return False

    mean_anomaly_rad: float = _true_to_mean_anomaly_rad(
        true_anomaly_rad=elements["true_anomaly_rad"],
        eccentricity=elements["e"],
    )

    satrec = Satrec.twoline2rv(tle_line1, tle_line2, WGS72)
    bstar: float = satrec.bstar

    try:
        syn_tle_line1, syn_tle_line2 = _keplerian_to_tle_lines(
            norad_id=norad_id,
            epoch_utc=maneuver_epoch_utc,
            a_km=elements["a_km"],
            e=elements["e"],
            i_rad=elements["i_rad"],
            raan_rad=elements["raan_rad"],
            argp_rad=elements["argp_rad"],
            mean_anomaly_rad=mean_anomaly_rad,
            bstar=bstar,
        )
    except ValueError as exc:
        print(f"[ERROR: TLE line generation failed — {exc}]")
        db.close()
        return False

    if not ingest.validate_tle(syn_tle_line1, syn_tle_line2):
        print(
            f"[ERROR: Generated synthetic maneuver TLE for NORAD {norad_id} "
            "failed checksum validation]"
        )
        db.close()
        return False

    epoch_utc_str: str = maneuver_epoch_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    tle_dict: dict = {
        "norad_id": norad_id,
        "epoch_utc": epoch_utc_str,
        "tle_line1": syn_tle_line1,
        "tle_line2": syn_tle_line2,
    }
    ingest.cache_tles(db, [tle_dict], fetched_at_utc=now_utc, source="demo_injection")
    db.close()

    print(
        f"[OK] Maneuver injected for NORAD {norad_id}: "
        f"{delta_v_ms} m/s {direction} @ {epoch_utc_str}"
    )
    return True


def _clear_demo_injections(catalog_path: str, db_path: str) -> None:
    """Remove all demo-injected objects and synthetic TLEs, restoring clean state.

    Removes:
    - NORAD 99999 (THREAT-SIM) from catalog.json
    - All rows with source='demo_injection' from tle_catalog (acts 2, 3, 4)
    - state_history rows for the maneuvered objects (46075, 47474) that were
      created after their synthetic TLEs, so the filter resets cleanly on next run
    - Active/recalibrating alert rows for all demo-affected objects

    Args:
        catalog_path: Path to catalog.json.
        db_path: Path to SQLite TLE cache.
    """
    # --- catalog.json: remove THREAT-SIM entry ---
    try:
        with open(catalog_path, "r", encoding="utf-8") as fh:
            catalog: list = json.load(fh)
        before_count: int = len(catalog)
        catalog = [e for e in catalog if int(e.get("norad_id", 0)) != _THREAT_NORAD_ID]
        with open(catalog_path, "w", encoding="utf-8") as fh:
            json.dump(catalog, fh, indent=2)
        removed: int = before_count - len(catalog)
        print(
            f"[clean] Removed {removed} entry/entries with NORAD {_THREAT_NORAD_ID} "
            f"from {catalog_path}."
        )
    except FileNotFoundError:
        print(
            f"[clean] WARNING: Catalog file not found at {catalog_path} — "
            "skipping catalog cleanup."
        )

    try:
        db: sqlite3.Connection = ingest.init_catalog_db(db_path)

        # --- tle_catalog: remove all demo-injected TLEs (all acts) ---
        cur = db.execute(
            "DELETE FROM tle_catalog WHERE source = 'demo_injection'"
        )
        demo_tle_removed: int = cur.rowcount
        db.commit()
        print(
            f"[clean] Removed {demo_tle_removed} demo-injected TLE row(s) "
            "from tle_catalog (source='demo_injection')."
        )

        # --- state_history: drop entries for maneuvered objects produced after
        #     the original TLE epoch so their filter resets on next run ---
        maneuver_norad_ids: list[int] = [
            _STARLINK_1990_NORAD_ID,
            _BLACKSKY7_NORAD_ID,
            _THREAT_NORAD_ID,
        ]
        for nid in maneuver_norad_ids:
            # Find the latest non-demo TLE epoch so we only delete state rows
            # that were produced AFTER the synthetic injection, not the original ones.
            row = db.execute(
                "SELECT MAX(epoch_utc) FROM tle_catalog "
                "WHERE norad_id = ? AND source != 'demo_injection'",
                (nid,),
            ).fetchone()
            pre_injection_epoch: Optional[str] = row[0] if row else None

            if pre_injection_epoch:
                cur2 = db.execute(
                    "DELETE FROM state_history "
                    "WHERE norad_id = ? AND epoch_utc > ?",
                    (nid, pre_injection_epoch),
                )
                deleted_states: int = cur2.rowcount
            else:
                # No real TLE in cache for this object — delete all its state rows.
                cur2 = db.execute(
                    "DELETE FROM state_history WHERE norad_id = ?",
                    (nid,),
                )
                deleted_states = cur2.rowcount

            if deleted_states:
                print(
                    f"[clean] NORAD {nid}: removed {deleted_states} post-injection "
                    "state_history row(s)."
                )

        db.commit()

        # --- alerts: resolve open demo alerts for affected objects ---
        resolution_ts: str = datetime.datetime.now(
            datetime.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        affected_ids_sql: str = ",".join(
            str(n) for n in [_ISS_NORAD_ID, _STARLINK_1990_NORAD_ID,
                             _BLACKSKY7_NORAD_ID, _THREAT_NORAD_ID]
        )
        cur3 = db.execute(
            f"UPDATE alerts SET status = 'resolved', "
            f"resolution_epoch_utc = ? "
            f"WHERE status IN ('active', 'recalibrating') "
            f"AND norad_id IN ({affected_ids_sql})",
            (resolution_ts,),
        )
        resolved_alerts: int = cur3.rowcount
        db.commit()
        if resolved_alerts:
            print(f"[clean] Resolved {resolved_alerts} open alert(s) for demo objects.")

        # --- conjunction tables: remove THREAT-SIM rows ---
        cur4 = db.execute(
            "DELETE FROM conjunction_risks "
            "WHERE conjunction_event_id IN ("
            "  SELECT id FROM conjunction_events "
            "  WHERE anomalous_norad_id = ?"
            ")",
            (_THREAT_NORAD_ID,),
        )
        cur5 = db.execute(
            "DELETE FROM conjunction_events WHERE anomalous_norad_id = ?",
            (_THREAT_NORAD_ID,),
        )
        db.commit()
        if cur4.rowcount or cur5.rowcount:
            print(
                f"[clean] Removed {cur5.rowcount} conjunction event(s) and "
                f"{cur4.rowcount} risk row(s) for NORAD {_THREAT_NORAD_ID}."
            )

        db.close()
        print("[clean] Demo teardown complete — system restored to pre-demo state.")

    except Exception as exc:  # noqa: BLE001
        print(f"[clean] WARNING: DB cleanup encountered an error — {exc}")


# ---------------------------------------------------------------------------
# Act functions
# ---------------------------------------------------------------------------


def act1(delay_s: int) -> None:
    """Act 1 — Baseline: narration only, no injection.

    Args:
        delay_s: Seconds to wait after printing (used when running all acts).
    """
    print()
    print("=" * 70)
    print("[ACT 1] NORMAL OPERATIONS")
    print("=" * 70)
    print()
    print(_PRESENTER_ACT1)
    print()


def act2(base_url: str, catalog_path: str, db_path: str) -> None:
    """Act 2 — ASAT debris conjunction with ISS.

    Injects synthetic threat NORAD 99999 near ISS (1.5 km miss distance,
    5-minute TCA), inserts it into catalog.json and TLE cache, then triggers
    a processing cycle.

    Args:
        base_url: Backend base URL.
        catalog_path: Path to catalog.json.
        db_path: Path to SQLite TLE cache.
    """
    print()
    print("=" * 70)
    print("[ACT 2] ASAT DEBRIS CONJUNCTION — ISS")
    print("=" * 70)
    print()
    print(_PRESENTER_ACT2)
    print()

    success: bool = _inject_conjunction_into_db(
        primary_norad_id=_ISS_NORAD_ID,
        offset_min=5.0,
        miss_km=1.5,
        catalog_path=catalog_path,
        db_path=db_path,
    )

    if success:
        _post_trigger_process(base_url)
        print()
        print(
            "[ACT 2] Conjunction injected. Watch the globe — ISS should highlight "
            "within 30-90 seconds as the conjunction screener completes."
        )
    else:
        print("[ACT 2] Conjunction injection failed — see error above.")

    print()


def act3(base_url: str, catalog_path: str, db_path: str) -> None:
    """Act 3 — Unannounced Starlink maneuver (STARLINK-1990, NORAD 46075).

    Injects a 5 m/s along-track delta-V for STARLINK-1990, then triggers
    a processing cycle.

    Args:
        base_url: Backend base URL.
        catalog_path: Path to catalog.json.
        db_path: Path to SQLite TLE cache.
    """
    print()
    print("=" * 70)
    print("[ACT 3] UNANNOUNCED STARLINK MANEUVER — STARLINK-1990 (NORAD 46075)")
    print("=" * 70)
    print()
    print(_PRESENTER_ACT3)
    print()

    success: bool = _inject_maneuver_into_db(
        norad_id=_STARLINK_1990_NORAD_ID,
        delta_v_ms=5.0,
        direction="along-track",
        db_path=db_path,
        catalog_path=catalog_path,
    )

    if success:
        _post_trigger_process(base_url)
        print()
        print(
            "[ACT 3] Maneuver injected for STARLINK-1990. "
            "Watch the residual chart — anomaly should appear within 30 seconds."
        )
    else:
        print("[ACT 3] Maneuver injection failed — see error above.")

    print()


def act4(base_url: str, catalog_path: str, db_path: str) -> None:
    """Act 4 — ISR asset repositioning (BLACKSKY GLOBAL-1, NORAD 47474).

    Injects a 5 m/s cross-track delta-V for BLACKSKY GLOBAL-1, then triggers
    a processing cycle.

    Args:
        base_url: Backend base URL.
        catalog_path: Path to catalog.json.
        db_path: Path to SQLite TLE cache.
    """
    print()
    print("=" * 70)
    print("[ACT 4] ISR ASSET REPOSITIONING — BLACKSKY GLOBAL-1 (NORAD 47474)")
    print("=" * 70)
    print()
    print(_PRESENTER_ACT4)
    print()

    success: bool = _inject_maneuver_into_db(
        norad_id=_BLACKSKY7_NORAD_ID,
        delta_v_ms=5.0,
        direction="cross-track",
        db_path=db_path,
        catalog_path=catalog_path,
    )

    if success:
        _post_trigger_process(base_url)
        print()
        print(
            "[ACT 4] Maneuver injected for BLACKSKY GLOBAL-1. "
            "Cross-track burn detected — consistent with collection retasking."
        )
    else:
        print("[ACT 4] Maneuver injection failed — see error above.")

    print()


def act5(base_url: str) -> None:
    """Act 5 — Recalibration / resolution.

    Triggers four processing cycles (with pauses between) to drive the Kalman
    filter recalibration loop. Each cycle the filter updates its state estimate
    toward the new orbit; by cycle 3-4 residuals and confidence return to nominal.

    Args:
        base_url: Backend base URL.
    """
    print()
    print("=" * 70)
    print("[ACT 5] RECALIBRATION / RESOLUTION")
    print("=" * 70)
    print()
    print(_PRESENTER_ACT5)
    print()

    num_cycles: int = 4
    pause_s: int = 8

    for cycle in range(1, num_cycles + 1):
        print(f"[ACT 5] Recalibration cycle {cycle}/{num_cycles}...")
        _post_trigger_process(base_url)
        if cycle < num_cycles:
            print(f"[ACT 5] Pausing {pause_s}s — watch residuals converge on the chart...")
            time.sleep(pause_s)

    print()
    print(
        "[ACT 5] Recalibration complete. Monitor residual charts — "
        "alerts should transition active → recalibrating → resolved."
    )
    print()


# ---------------------------------------------------------------------------
# --list output
# ---------------------------------------------------------------------------


def print_all_scripts() -> None:
    """Print presenter scripts for all 5 acts without running any injections.

    Used for rehearsal (--list flag).
    """
    acts = [
        ("[ACT 1] NORMAL OPERATIONS", _PRESENTER_ACT1),
        ("[ACT 2] ASAT DEBRIS CONJUNCTION — ISS", _PRESENTER_ACT2),
        ("[ACT 3] UNANNOUNCED STARLINK MANEUVER — STARLINK-1990", _PRESENTER_ACT3),
        ("[ACT 4] ISR ASSET REPOSITIONING — BLACKSKY GLOBAL-1", _PRESENTER_ACT4),
        ("[ACT 5] RECALIBRATION / RESOLUTION", _PRESENTER_ACT5),
    ]
    print()
    print("ne-body SSA Demo — Presenter Scripts (rehearsal mode)")
    print("=" * 70)
    for header, script in acts:
        print()
        print(f"--- {header} ---")
        print()
        print(script)
        print()
    print("=" * 70)
    print("End of presenter scripts.")
    print()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Parse arguments and run the requested demo acts."""
    parser = argparse.ArgumentParser(
        description=(
            "ne-body SSA platform — 5-act demo orchestration script for "
            "DoD/Space Force/NASA presentations."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scripts/demo.py --list\n"
            "  python scripts/demo.py --act all\n"
            "  python scripts/demo.py --act 2 --base-url http://localhost:8001\n"
            "  python scripts/demo.py --act all --delay-s 20 --clean\n"
        ),
    )
    parser.add_argument(
        "--act",
        type=str,
        choices=["1", "2", "3", "4", "5", "all"],
        default=None,
        metavar="ACT",
        help=(
            "Which act to run: 1, 2, 3, 4, 5, or all. "
            "'all' runs acts 1 through 5 in sequence with --delay-s pauses between."
        ),
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default="http://localhost:8001",
        metavar="URL",
        help="Backend base URL (default: http://localhost:8001)",
    )
    parser.add_argument(
        "--delay-s",
        type=int,
        default=15,
        metavar="SECONDS",
        help=(
            "Seconds to pause between acts when running --act all (default: 15). "
            "Acts 2-4 each need time for the browser to reflect the change."
        ),
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help=(
            "After running, remove synthetic conjunction object NORAD 99999 "
            "from catalog.json and TLE cache (demo teardown). "
            "Also run `git checkout data/catalog/catalog.json` to restore git state."
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print all presenter scripts without running any injections (rehearsal mode).",
    )
    parser.add_argument(
        "--catalog",
        type=str,
        default=_DEFAULT_CATALOG_PATH,
        metavar="PATH",
        help=f"Path to catalog.json (default: {_DEFAULT_CATALOG_PATH})",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        metavar="PATH",
        help=(
            "Path to SQLite TLE cache. "
            "Defaults to NBODY_DB_PATH env var or data/catalog/tle_cache.db."
        ),
    )

    args = parser.parse_args()

    # --list: rehearsal mode, no backend required.
    if args.list:
        print_all_scripts()
        return

    # Require --act unless --list was given.
    if args.act is None:
        parser.error("--act is required unless --list is specified.")

    # Resolve DB path: CLI arg > env var > default.
    db_path: str = args.db or _resolve_db_path()
    catalog_path: str = args.catalog
    base_url: str = args.base_url
    delay_s: int = args.delay_s

    def _pause(label: str) -> None:
        """Print a countdown pause between acts.

        Args:
            label: Description of what comes next.
        """
        if delay_s > 0:
            print(f"[demo] Pausing {delay_s}s before {label}...")
            time.sleep(delay_s)

    if args.act == "all":
        act1(delay_s=delay_s)
        _pause("Act 2")
        act2(base_url=base_url, catalog_path=catalog_path, db_path=db_path)
        _pause("Act 3")
        act3(base_url=base_url, catalog_path=catalog_path, db_path=db_path)
        _pause("Act 4")
        act4(base_url=base_url, catalog_path=catalog_path, db_path=db_path)
        _pause("Act 5")
        act5(base_url=base_url)
    elif args.act == "1":
        act1(delay_s=0)
    elif args.act == "2":
        act2(base_url=base_url, catalog_path=catalog_path, db_path=db_path)
    elif args.act == "3":
        act3(base_url=base_url, catalog_path=catalog_path, db_path=db_path)
    elif args.act == "4":
        act4(base_url=base_url, catalog_path=catalog_path, db_path=db_path)
    elif args.act == "5":
        act5(base_url=base_url)

    if args.clean:
        print()
        print("=" * 70)
        print("[CLEAN] Restoring pre-demo state...")
        print("=" * 70)
        _clear_demo_injections(catalog_path=catalog_path, db_path=db_path)
        print(
            "[CLEAN] Done. Run `git checkout data/catalog/catalog.json` "
            "to restore the clean git state."
        )
        print()


if __name__ == "__main__":
    main()
