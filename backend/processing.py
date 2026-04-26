"""Shared predict-update-anomaly-recalibrate pipeline for one catalog object.

This module is imported by both main.py (for the live server processing loop)
and scripts/replay.py (for offline historical TLE replay). It contains no
FastAPI or WebSocket types so it can be imported in non-server contexts.

Coordinate frame: all state vectors are ECI J2000 km and km/s throughout.
Units: km for position, km/s for velocity, seconds for time deltas.
"""

import datetime
import logging
import sqlite3

import numpy as np
from numpy.typing import NDArray

import backend.anomaly as anomaly
import backend.kalman as kalman
import backend.propagator as propagator

logger = logging.getLogger(__name__)

# Valid WebSocket message types — mirrors the constants in main.py so callers
# can interpret the returned dicts. main.py still owns the broadcast logic.
WS_TYPE_STATE_UPDATE: str = "state_update"
WS_TYPE_ANOMALY: str = "anomaly"
WS_TYPE_RECALIBRATION: str = "recalibration"
WS_TYPE_TRACK_UPDATE: str = "track_update"


def generate_track_samples(
    tle_line1: str,
    tle_line2: str,
    start_epoch_utc: datetime.datetime,
    num_samples: int = 60,
    interval_s: float = 60.0,
) -> list[dict]:
    """Propagate a TLE forward and return a list of ECI J2000 position samples.

    Each sample is propagated from start_epoch_utc + i * interval_s using
    propagator.propagate_tle(). Samples that raise ValueError (e.g., SGP4 decay
    or epoch too far) are skipped and logged at DEBUG level.

    Coordinate frame: output positions are ECI J2000 km as produced by
    propagator.propagate_tle() (TEME→GCRS conversion applied internally).

    Args:
        tle_line1: TLE line 1.
        tle_line2: TLE line 2.
        start_epoch_utc: UTC-aware datetime for the first sample.
        num_samples: Number of samples to generate (default 60).
        interval_s: Time step between samples in seconds (default 60.0).

    Returns:
        List of dicts with keys:
            epoch_utc (str): ISO-8601 UTC string ending in 'Z'.
            eci_km (list[float]): [x, y, z] ECI J2000 position in km.
        May be shorter than num_samples if some propagations failed.
    """
    # TECH DEBT TD-030: 60 SGP4+TEME-to-GCRS calls per object is ~5ms each
    # (~300ms total per object). For 71 objects per cycle = ~21s added CPU time.
    # Vectorised astropy Time arrays can reduce this 10-20x post-POC.
    samples: list[dict] = []
    for i in range(num_samples):
        sample_epoch: datetime.datetime = start_epoch_utc + datetime.timedelta(seconds=i * interval_s)
        try:
            position_eci_km, _ = propagator.propagate_tle(tle_line1, tle_line2, sample_epoch)
            epoch_str: str = sample_epoch.strftime("%Y-%m-%dT%H:%M:%SZ")
            samples.append(
                {
                    "epoch_utc": epoch_str,
                    "eci_km": position_eci_km.tolist(),
                }
            )
        except ValueError as exc:
            logger.debug(
                "generate_track_samples: SGP4 failed at sample %d (epoch=%s): %s",
                i,
                sample_epoch.isoformat(),
                exc,
            )
    return samples


# Timeout for deferred recalibration on active satellites.  If no second TLE
# arrives within this window (e.g., Space-Track outage), the pending
# classification is resolved using the provisional type and recalibration
# proceeds.  Configurable via NBODY_PENDING_ANOMALY_TIMEOUT_HOURS env var.
import os as _os

_PENDING_ANOMALY_TIMEOUT_HOURS: float = float(_os.environ.get("NBODY_PENDING_ANOMALY_TIMEOUT_HOURS", "2.0"))


def _ensure_state_history_table(db: sqlite3.Connection) -> None:
    """Create the state_history table and index if they do not already exist.

    Identical schema to the one created by main.py._ensure_state_history_table.
    Extracted here so replay.py can call it without importing main.py.

    Args:
        db: Open SQLite connection.
    """
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS state_history (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            norad_id          INTEGER NOT NULL,
            epoch_utc         TEXT    NOT NULL,
            x_km              REAL    NOT NULL,
            y_km              REAL    NOT NULL,
            z_km              REAL    NOT NULL,
            vx_km_s           REAL    NOT NULL,
            vy_km_s           REAL    NOT NULL,
            vz_km_s           REAL    NOT NULL,
            cov_x_km2         REAL    NOT NULL,
            cov_y_km2         REAL    NOT NULL,
            cov_z_km2         REAL    NOT NULL,
            nis               REAL    NOT NULL,
            confidence        REAL    NOT NULL,
            anomaly_type      TEXT,
            message_type      TEXT    NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_state_history_norad_epoch
        ON state_history (norad_id, epoch_utc)
        """
    )
    db.commit()


def _build_ws_message(
    norad_id: int,
    filter_state: dict,
    message_type: str,
    anomaly_type: str | None = None,
    tle_epoch_utc_str: str | None = None,
    observation_eci_km: NDArray[np.float64] | None = None,
) -> dict:
    """Construct a WebSocket message conforming to architecture Section 3.5 schema.

    Numpy arrays are converted to plain Python lists for JSON serialization.
    The epoch_utc field always ends with the Z suffix (UTC).

    Args:
        norad_id: NORAD catalog ID.
        filter_state: Filter state dict from kalman.init_filter / kalman.update.
        message_type: One of WS_TYPE_STATE_UPDATE, WS_TYPE_ANOMALY, WS_TYPE_RECALIBRATION.
        anomaly_type: One of the ANOMALY_* constants from anomaly.py, or None.
        tle_epoch_utc_str: ISO-8601 UTC string for the TLE epoch (for display).
        observation_eci_km: Raw SGP4 observation vector [x,y,z,vx,vy,vz] in km/km·s⁻¹.

    Returns:
        Dict matching the F-043 schema, extended with tle_epoch_utc and sgp4_eci_km.
    """
    state = kalman.get_state(filter_state)

    epoch_dt: datetime.datetime = state["last_epoch_utc"]
    if epoch_dt.tzinfo is None:
        raise ValueError("filter_state last_epoch_utc must be UTC-aware")
    epoch_str: str = epoch_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    state_eci_km: NDArray[np.float64] = state["state_eci_km"]
    cov_km2: NDArray[np.float64] = state["covariance_km2"]

    eci_km_list: list = state_eci_km[:3].tolist()
    eci_km_s_list: list = state_eci_km[3:].tolist()
    cov_diag_list: list = [
        float(cov_km2[0, 0]),
        float(cov_km2[1, 1]),
        float(cov_km2[2, 2]),
    ]

    return {
        "type": message_type,
        "norad_id": norad_id,
        "epoch_utc": epoch_str,
        "eci_km": eci_km_list,
        "eci_km_s": eci_km_s_list,
        "covariance_diagonal_km2": cov_diag_list,
        "nis": float(state["nis"]),
        "innovation_eci_km": state.get("innovation_eci_km", np.zeros(6, dtype=np.float64)).tolist(),
        "confidence": float(state["confidence"]),
        "anomaly_type": anomaly_type,
        "tle_epoch_utc": tle_epoch_utc_str,
        "sgp4_eci_km": observation_eci_km[:3].tolist() if observation_eci_km is not None else None,
    }


def _insert_state_history_row(
    db: sqlite3.Connection,
    norad_id: int,
    epoch_utc: datetime.datetime,
    state_eci_km: list,
    covariance_km2: list,
    nis: float,
    confidence: float,
    anomaly_type: str | None,
    message_type: str,
) -> None:
    """Write one state snapshot row to the state_history table.

    Args:
        db: Open SQLite connection.
        norad_id: NORAD catalog ID.
        epoch_utc: UTC epoch of the state (must be UTC-aware).
        state_eci_km: 6-element list [x,y,z,vx,vy,vz] in km and km/s (ECI J2000).
        covariance_km2: 3-element list of position covariance diagonal [P00,P11,P22].
        nis: NIS value.
        confidence: Confidence score.
        anomaly_type: Anomaly type string or None.
        message_type: One of state_update, anomaly, recalibration.
    """
    epoch_str: str = epoch_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute(
        """
        INSERT INTO state_history
            (norad_id, epoch_utc,
             x_km, y_km, z_km, vx_km_s, vy_km_s, vz_km_s,
             cov_x_km2, cov_y_km2, cov_z_km2,
             nis, confidence, anomaly_type, message_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            norad_id,
            epoch_str,
            state_eci_km[0],
            state_eci_km[1],
            state_eci_km[2],
            state_eci_km[3],
            state_eci_km[4],
            state_eci_km[5],
            covariance_km2[0],
            covariance_km2[1],
            covariance_km2[2],
            nis,
            confidence,
            anomaly_type,
            message_type,
        ),
    )
    db.commit()


def process_single_object(
    db: sqlite3.Connection,
    entry: dict,
    norad_id: int,
    filter_states: dict,
    tle_record: dict,
    generate_tracks: bool = True,
) -> list[dict]:
    """Run predict-update-anomaly-recalibrate for one catalog object.

    This function is the shared core of both the live server processing loop
    (main.py) and the offline replay script (scripts/replay.py). It is
    synchronous and writes to SQLite directly. Callers decide whether to
    broadcast the returned WebSocket message dicts.

    Coordinate frame: all state vectors entering and leaving this function are
    ECI J2000 km and km/s, as produced by propagator.tle_to_state_vector_eci_km.

    Args:
        db: Open SQLite connection (WAL mode recommended for concurrent access).
        entry: Catalog entry dict with at minimum keys: norad_id, name,
               object_class.
        norad_id: NORAD catalog ID (int, already extracted from entry for
                  convenience — must match entry["norad_id"]).
        filter_states: Mutable dict of filter state dicts keyed by norad_id.
                       Modified in place: new entries are added on cold start,
                       existing entries are updated after each cycle.
        tle_record: TLE dict with keys: norad_id, epoch_utc (ISO 8601 str),
                    tle_line1, tle_line2, fetched_at.

    Returns:
        List of WebSocket message dicts (0–3 messages depending on path taken):
        - Cold start: [state_update]
        - Warm, no anomaly: [state_update]
        - Warm, anomaly: [anomaly, recalibration]
        Returns an empty list if the TLE epoch is not after the last filter epoch
        (duplicate or out-of-order TLE).
    """
    tle_line1: str = tle_record["tle_line1"]
    tle_line2: str = tle_record["tle_line2"]
    epoch_utc_str: str = tle_record["epoch_utc"]

    # Parse TLE epoch string to UTC-aware datetime.
    # ingest.py stores epochs as 'YYYY-MM-DDTHH:MM:SSZ'.
    epoch_utc: datetime.datetime = datetime.datetime.strptime(epoch_utc_str, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc
    )

    is_active_satellite: bool = entry.get("object_class") == "active_satellite"

    if norad_id not in filter_states:
        # Cold start: initialize filter from TLE state vector (ECI J2000).
        logger.info("Initializing filter for NORAD %d", norad_id)
        initial_state_eci_km: NDArray[np.float64] = propagator.tle_to_state_vector_eci_km(
            tle_line1, tle_line2, epoch_utc
        )
        object_class: str = entry.get("object_class", kalman.OBJECT_CLASS_ACTIVE)
        q_matrix: NDArray[np.float64] = kalman.OBJECT_CLASS_Q.get(
            object_class, kalman.OBJECT_CLASS_Q[kalman.OBJECT_CLASS_ACTIVE]
        )
        filter_state: dict = kalman.init_filter(
            state_eci_km=initial_state_eci_km,
            epoch_utc=epoch_utc,
            process_noise_q=q_matrix,
        )
        # Store the TLE used for this init so the next predict step uses
        # the PREVIOUS TLE (not the new observation TLE) for propagation.
        filter_state["last_tle_line1"] = tle_line1
        filter_state["last_tle_line2"] = tle_line2
        filter_states[norad_id] = filter_state

        ws_message = _build_ws_message(
            norad_id=norad_id,
            filter_state=filter_state,
            message_type=WS_TYPE_STATE_UPDATE,
            anomaly_type=None,
            tle_epoch_utc_str=epoch_utc_str,
            observation_eci_km=None,
        )

        _insert_state_history_row(
            db=db,
            norad_id=norad_id,
            epoch_utc=epoch_utc,
            state_eci_km=ws_message["eci_km"] + ws_message["eci_km_s"],
            covariance_km2=ws_message["covariance_diagonal_km2"],
            nis=ws_message["nis"],
            confidence=ws_message["confidence"],
            anomaly_type=None,
            message_type=WS_TYPE_STATE_UPDATE,
        )
        cold_start_messages: list[dict] = [ws_message]
        if generate_tracks:
            _track_now = datetime.datetime.now(tz=datetime.timezone.utc)
            cold_start_samples = generate_track_samples(
                tle_line1=tle_line1,
                tle_line2=tle_line2,
                start_epoch_utc=_track_now,
            )
            cold_start_messages.append(
                {
                    "type": WS_TYPE_TRACK_UPDATE,
                    "norad_id": norad_id,
                    "epoch_utc": _track_now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "samples": cold_start_samples,
                }
            )
        return cold_start_messages

    # Existing filter: run predict -> update cycle.
    filter_state = filter_states[norad_id]

    # Guard: skip if new epoch is not strictly after last filter epoch.
    last_epoch_utc: datetime.datetime = filter_state["last_epoch_utc"]
    if epoch_utc <= last_epoch_utc:
        logger.debug(
            "NORAD %d: TLE epoch %s is not after last filter epoch %s — skipping",
            norad_id,
            epoch_utc.isoformat(),
            last_epoch_utc.isoformat(),
        )
        return []

    # Predict step: propagate using the PREVIOUS TLE (stored in filter_state)
    # to the new observation epoch. Using the new TLE here would make prediction
    # and observation identical, producing zero innovation.
    prev_tle1: str = filter_state.get("last_tle_line1", tle_line1)
    prev_tle2: str = filter_state.get("last_tle_line2", tle_line2)
    kalman.predict(filter_state, epoch_utc, prev_tle1, prev_tle2)

    # Observation: convert the NEW TLE to an ECI state vector (ECI J2000).
    # This is distinct from the predicted state, producing a non-zero innovation.
    observation_eci_km: NDArray[np.float64] = propagator.tle_to_state_vector_eci_km(tle_line1, tle_line2, epoch_utc)

    # Store TLE for the next predict cycle.
    filter_state["last_tle_line1"] = tle_line1
    filter_state["last_tle_line2"] = tle_line2

    # Update step: incorporate new observation.
    kalman.update(filter_state, observation_eci_km, epoch_utc)

    nis_val: float = filter_state["nis"]
    nis_history: list = filter_state["nis_history"]
    innovation_eci_km_list: list = filter_state["innovation_eci_km"].tolist()

    # Anomaly classification.
    detected_anomaly_type: str | None = anomaly.classify_anomaly(
        norad_id=norad_id,
        nis_history=nis_history,
        innovation_eci_km=innovation_eci_km_list,
        is_active_satellite=is_active_satellite,
    )

    messages: list[dict] = []

    # --- Deferred classification resolution (cycle 2 for active satellites) ---
    #
    # On the previous cycle an anomaly was detected on an active satellite and
    # recalibration was deferred so that a second consecutive NIS exceedance
    # could confirm a maneuver classification.  Resolve that pending state now.
    if filter_state.get("_pending_anomaly_check"):
        pending_row_id: int = filter_state["_pending_anomaly_row_id"]
        pending_type: str = filter_state["_pending_anomaly_type"]
        pending_nis: float = filter_state["_pending_anomaly_nis"]
        pending_innovation: list = filter_state["_pending_anomaly_innovation"]
        pending_epoch: datetime.datetime = filter_state["_pending_anomaly_epoch_utc"]
        pending_timeout: datetime.datetime = filter_state["_pending_anomaly_timeout_utc"]

        # Determine final classification.
        timed_out: bool = epoch_utc >= pending_timeout
        if timed_out:
            # Space-Track outage or long gap — keep provisional type.
            final_type: str = pending_type
            logger.warning(
                "NORAD %d: pending anomaly timed out after %.1f h — resolving as %s",
                norad_id,
                _PENDING_ANOMALY_TIMEOUT_HOURS,
                pending_type,
            )
        else:
            # Re-run classifier against updated nis_history (now has cycle 2 NIS).
            reclassified: str | None = anomaly.classify_anomaly(
                norad_id=norad_id,
                nis_history=nis_history,
                innovation_eci_km=innovation_eci_km_list,
                is_active_satellite=True,
            )
            # If classifier returns None (cycle 2 NIS below threshold) the
            # maneuver signal dissipated — keep the provisional type.
            final_type = reclassified if reclassified is not None else pending_type

        # Retroactively correct the DB record if the type changed.
        if final_type != pending_type:
            try:
                anomaly.update_anomaly_type(db, pending_row_id, final_type)
                logger.info(
                    "NORAD %d: provisional %s upgraded to %s (row_id=%d)",
                    norad_id,
                    pending_type,
                    final_type,
                    pending_row_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "NORAD %d: failed to update anomaly type: %s",
                    norad_id,
                    exc,
                )

        # Clear pending state before recalibrate() so keys are not carried forward.
        for _k in (
            "_pending_anomaly_check",
            "_pending_anomaly_row_id",
            "_pending_anomaly_type",
            "_pending_anomaly_nis",
            "_pending_anomaly_innovation",
            "_pending_anomaly_epoch_utc",
            "_pending_anomaly_timeout_utc",
        ):
            filter_state.pop(_k, None)

        recal_params: dict = anomaly.trigger_recalibration(
            norad_id=norad_id,
            anomaly_type=final_type,
            epoch_utc=pending_epoch,
        )
        filter_state = kalman.recalibrate(
            filter_state=filter_state,
            new_observation_eci_km=observation_eci_km,
            epoch_utc=epoch_utc,
            inflation_factor=recal_params["inflation_factor"],
        )
        filter_state["last_tle_line1"] = tle_line1
        filter_state["last_tle_line2"] = tle_line2
        filter_states[norad_id] = filter_state

        # Track this anomaly row for recalibration-complete detection.
        filter_state["_anomaly_row_id"] = pending_row_id
        filter_state["_anomaly_detection_epoch_utc"] = pending_epoch

        # Anomaly WS message: use cycle-1 NIS/innovation (the actual detection).
        anomaly_ws_message = _build_ws_message(
            norad_id=norad_id,
            filter_state=filter_state,
            message_type=WS_TYPE_ANOMALY,
            anomaly_type=final_type,
            tle_epoch_utc_str=epoch_utc_str,
            observation_eci_km=observation_eci_km,
        )
        anomaly_ws_message["nis"] = pending_nis
        anomaly_ws_message["innovation_eci_km"] = pending_innovation

        recal_ws_message = _build_ws_message(
            norad_id=norad_id,
            filter_state=filter_state,
            message_type=WS_TYPE_RECALIBRATION,
            anomaly_type=final_type,
            tle_epoch_utc_str=epoch_utc_str,
            observation_eci_km=observation_eci_km,
        )

        # Update the existing state_history row written in cycle 1 to reflect
        # the final (possibly upgraded) anomaly_type rather than inserting a
        # duplicate row for the same pending_epoch.  On a restart the row may
        # not exist; the UPDATE is a no-op in that case and the WS message is
        # still emitted for connected clients.
        pending_epoch_str: str = pending_epoch.strftime("%Y-%m-%dT%H:%M:%SZ")
        db.execute(
            "UPDATE state_history SET anomaly_type = ? WHERE norad_id = ? AND epoch_utc = ? AND message_type = ?",
            (final_type, norad_id, pending_epoch_str, WS_TYPE_ANOMALY),
        )
        db.commit()

        messages.append(anomaly_ws_message)
        messages.append(recal_ws_message)
        if messages and generate_tracks:
            _track_start_epoch: datetime.datetime = datetime.datetime.now(tz=datetime.timezone.utc)
            _track_samples = generate_track_samples(
                tle_line1=filter_state["last_tle_line1"],
                tle_line2=filter_state["last_tle_line2"],
                start_epoch_utc=_track_start_epoch,
            )
            messages.append(
                {
                    "type": WS_TYPE_TRACK_UPDATE,
                    "norad_id": norad_id,
                    "epoch_utc": _track_start_epoch.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "samples": _track_samples,
                }
            )
        return messages

    # --- First-cycle anomaly detection ---

    if detected_anomaly_type is not None:
        anomaly_row_id: int = anomaly.record_anomaly(
            db=db,
            norad_id=norad_id,
            detection_epoch_utc=epoch_utc,
            anomaly_type=detected_anomaly_type,
            nis_value=nis_val,
        )

        if is_active_satellite:
            # Defer recalibration for active satellites so the next cycle can
            # confirm whether this is a maneuver (2+ consecutive NIS exceedances)
            # or a transient divergence.  No recalibrate() call here.
            filter_state["_pending_anomaly_check"] = True
            filter_state["_pending_anomaly_row_id"] = anomaly_row_id
            filter_state["_pending_anomaly_type"] = detected_anomaly_type
            filter_state["_pending_anomaly_nis"] = nis_val
            filter_state["_pending_anomaly_innovation"] = innovation_eci_km_list
            filter_state["_pending_anomaly_epoch_utc"] = epoch_utc
            filter_state["_pending_anomaly_timeout_utc"] = epoch_utc + datetime.timedelta(
                hours=_PENDING_ANOMALY_TIMEOUT_HOURS
            )
            filter_states[norad_id] = filter_state

            # Emit provisional anomaly message.  No recalibration message yet.
            anomaly_ws_message = _build_ws_message(
                norad_id=norad_id,
                filter_state=filter_state,
                message_type=WS_TYPE_ANOMALY,
                anomaly_type=detected_anomaly_type,
                tle_epoch_utc_str=epoch_utc_str,
                observation_eci_km=observation_eci_km,
            )
            anomaly_ws_message["nis"] = nis_val
            anomaly_ws_message["innovation_eci_km"] = innovation_eci_km_list

            _insert_state_history_row(
                db=db,
                norad_id=norad_id,
                epoch_utc=epoch_utc,
                state_eci_km=anomaly_ws_message["eci_km"] + anomaly_ws_message["eci_km_s"],
                covariance_km2=anomaly_ws_message["covariance_diagonal_km2"],
                nis=nis_val,
                confidence=anomaly_ws_message["confidence"],
                anomaly_type=detected_anomaly_type,
                message_type=WS_TYPE_ANOMALY,
            )

            messages.append(anomaly_ws_message)

        else:
            # Non-active satellites (debris, rocket bodies) cannot maneuver —
            # recalibrate immediately as before.
            recal_params = anomaly.trigger_recalibration(
                norad_id=norad_id,
                anomaly_type=detected_anomaly_type,
                epoch_utc=epoch_utc,
            )

            filter_state = kalman.recalibrate(
                filter_state=filter_state,
                new_observation_eci_km=observation_eci_km,
                epoch_utc=epoch_utc,
                inflation_factor=recal_params["inflation_factor"],
            )
            filter_state["last_tle_line1"] = tle_line1
            filter_state["last_tle_line2"] = tle_line2
            filter_states[norad_id] = filter_state

            filter_state["_anomaly_row_id"] = anomaly_row_id
            filter_state["_anomaly_detection_epoch_utc"] = epoch_utc

            anomaly_ws_message = _build_ws_message(
                norad_id=norad_id,
                filter_state=filter_state,
                message_type=WS_TYPE_ANOMALY,
                anomaly_type=detected_anomaly_type,
                tle_epoch_utc_str=epoch_utc_str,
                observation_eci_km=observation_eci_km,
            )
            anomaly_ws_message["nis"] = nis_val
            anomaly_ws_message["innovation_eci_km"] = innovation_eci_km_list

            recal_ws_message = _build_ws_message(
                norad_id=norad_id,
                filter_state=filter_state,
                message_type=WS_TYPE_RECALIBRATION,
                anomaly_type=detected_anomaly_type,
                tle_epoch_utc_str=epoch_utc_str,
                observation_eci_km=observation_eci_km,
            )

            _insert_state_history_row(
                db=db,
                norad_id=norad_id,
                epoch_utc=epoch_utc,
                state_eci_km=anomaly_ws_message["eci_km"] + anomaly_ws_message["eci_km_s"],
                covariance_km2=anomaly_ws_message["covariance_diagonal_km2"],
                nis=nis_val,
                confidence=anomaly_ws_message["confidence"],
                anomaly_type=detected_anomaly_type,
                message_type=WS_TYPE_ANOMALY,
            )

            messages.append(anomaly_ws_message)
            messages.append(recal_ws_message)

    else:
        # No anomaly — check if a previously-flagged anomaly has now resolved.
        anomaly_row_id_pending: int | None = filter_state.pop("_anomaly_row_id", None)
        detection_epoch_pending: datetime.datetime | None = filter_state.pop("_anomaly_detection_epoch_utc", None)
        if (
            anomaly_row_id_pending is not None
            and detection_epoch_pending is not None
            and not anomaly.evaluate_nis(nis_val)
        ):
            try:
                anomaly.record_recalibration_complete(
                    db=db,
                    anomaly_row_id=anomaly_row_id_pending,
                    resolution_epoch_utc=epoch_utc,
                )
                logger.info(
                    "NORAD %d recalibration complete, row_id=%d",
                    norad_id,
                    anomaly_row_id_pending,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Failed to record recalibration complete for NORAD %d: %s",
                    norad_id,
                    exc,
                )

        ws_message = _build_ws_message(
            norad_id=norad_id,
            filter_state=filter_state,
            message_type=WS_TYPE_STATE_UPDATE,
            anomaly_type=None,
            tle_epoch_utc_str=epoch_utc_str,
            observation_eci_km=observation_eci_km,
        )

        _insert_state_history_row(
            db=db,
            norad_id=norad_id,
            epoch_utc=epoch_utc,
            state_eci_km=ws_message["eci_km"] + ws_message["eci_km_s"],
            covariance_km2=ws_message["covariance_diagonal_km2"],
            nis=ws_message["nis"],
            confidence=ws_message["confidence"],
            anomaly_type=None,
            message_type=WS_TYPE_STATE_UPDATE,
        )

        messages.append(ws_message)

    if messages and generate_tracks:
        _final_track_start_epoch: datetime.datetime = datetime.datetime.now(tz=datetime.timezone.utc)
        _final_track_samples = generate_track_samples(
            tle_line1=filter_state["last_tle_line1"],
            tle_line2=filter_state["last_tle_line2"],
            start_epoch_utc=_final_track_start_epoch,
        )
        messages.append(
            {
                "type": WS_TYPE_TRACK_UPDATE,
                "norad_id": norad_id,
                "epoch_utc": _final_track_start_epoch.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "samples": _final_track_samples,
            }
        )
    return messages
