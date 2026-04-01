# n-body SSA Platform — API Specification

**Version:** 0.1.0 (POC)
**Status:** P1 — SBIR review and integration partner reference

---

## 1. Overview

### Base URL

```
http://localhost:8000
```

This is the proof-of-concept base URL. Production deployments will substitute the appropriate hostname and TLS scheme.

### Protocols

| Protocol | Usage |
|----------|-------|
| HTTP/1.1 REST | Catalog queries, history retrieval, admin operations |
| WebSocket (RFC 6455) | Real-time streaming of state updates, anomaly alerts, conjunction risks |

### Authentication

None. The POC is designed for controlled local and demo environments. WebSocket connections are not authenticated. Do not expose the server publicly without adding an authentication layer.

### Content types

All REST endpoints consume and produce `application/json`. WebSocket messages are UTF-8 JSON text frames.

### CORS

The backend allows cross-origin requests from `http://localhost:3000` and `http://127.0.0.1:3000` (the frontend dev server). All methods and headers are permitted.

### Units and coordinate conventions

| Quantity | Unit | Notes |
|----------|------|-------|
| Position | km | ECI J2000 (GCRS) frame |
| Velocity | km/s | ECI J2000 (GCRS) frame |
| Covariance (position axes) | km² | Diagonal elements only |
| Time | UTC, ISO 8601 | All epoch strings end with the `Z` suffix |
| Duration | seconds (s) | |

**Coordinate frame:** All state vectors exposed by this API are in the Earth-Centered Inertial (ECI) J2000 frame, equivalent to GCRS. Conversions to ECEF or geodetic happen only in the frontend. Never mix frames when interpreting these values.

**ECI position array ordering:** `[x_km, y_km, z_km]` where +x points toward the vernal equinox, +z toward the celestial north pole.

**Covariance diagonal ordering:** `[P_xx_km2, P_yy_km2, P_zz_km2]` — position axes only. Velocity covariance is tracked internally but not exposed in this version.

---

## 2. REST Endpoints

### 2.1 GET /config

Return non-secret frontend configuration values. Used by the frontend to obtain the CesiumJS Ion token without embedding it in source code.

#### Parameters

None.

#### Response body

| Field | Type | Description |
|-------|------|-------------|
| `cesium_ion_token` | string | CesiumJS Ion access token from the `CESIUM_ION_TOKEN` environment variable. Empty string if the variable is not set; CesiumJS will render a grey globe. |

#### Example request

```bash
curl http://localhost:8000/config
```

#### Example response

```json
{
  "cesium_ion_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
}
```

#### Error cases

This endpoint always returns 200. If the environment variable is absent the token field is an empty string, not an error.

---

### 2.2 GET /catalog

Return the full list of tracked objects with their current filter state. This is the primary seeding endpoint for the frontend globe on load or reconnect.

Objects that have not yet received a TLE from Space-Track (no filter initialized) still appear in the list; their state fields are `null`.

#### Parameters

None.

#### Response body

Array of **CatalogEntry** objects (one per tracked object).

| Field | Type | Nullable | Description |
|-------|------|----------|-------------|
| `norad_id` | integer | no | NORAD catalog number. |
| `name` | string | no | Human-readable object name (e.g., `"ISS (ZARYA)"`). |
| `object_class` | string | no | Classification. One of `active_satellite`, `debris`, `rocket_body`. |
| `last_update_epoch_utc` | string | yes | ISO-8601 UTC epoch of the most recent filter update (or TLE epoch if filter not yet initialized). `null` if no TLE cached. |
| `confidence` | float | yes | Filter confidence score in [0, 1]. Higher is better. `null` if filter not initialized. |
| `eci_km` | array[float, 3] | yes | ECI J2000 position in km: `[x, y, z]`. `null` if filter not initialized. |
| `eci_km_s` | array[float, 3] | yes | ECI J2000 velocity in km/s: `[vx, vy, vz]`. `null` if filter not initialized. |
| `covariance_diagonal_km2` | array[float, 3] | yes | Position covariance diagonal `[P_xx, P_yy, P_zz]` in km². `null` if filter not initialized. |
| `nis` | float | yes | Most recent Normalized Innovation Squared value. `null` if filter not initialized. See Section 4 for interpretation. |
| `anomaly_flag` | boolean | yes | `true` if the most recent filter update produced an anomaly classification. `null` if filter not initialized. |
| `innovation_eci_km` | array[float, 6] | yes | Most recent innovation (residual) vector `[dx, dy, dz, dvx, dvy, dvz]` in ECI km and km/s. `null` if filter not initialized. |

#### Example request

```bash
curl http://localhost:8000/catalog
```

#### Example response

```json
[
  {
    "norad_id": 25544,
    "name": "ISS (ZARYA)",
    "object_class": "active_satellite",
    "last_update_epoch_utc": "2026-03-28T19:00:00Z",
    "confidence": 0.94,
    "eci_km": [-4527.0, 3613.2, 3780.1],
    "eci_km_s": [-5.121, -4.872, 2.301],
    "covariance_diagonal_km2": [0.0025, 0.0031, 0.0019],
    "nis": 2.3,
    "anomaly_flag": false,
    "innovation_eci_km": [0.12, -0.08, 0.05, 0.001, -0.002, 0.001]
  },
  {
    "norad_id": 46075,
    "name": "STARLINK-1990",
    "object_class": "active_satellite",
    "last_update_epoch_utc": "2026-03-28T18:32:10Z",
    "confidence": 0.97,
    "eci_km": [3011.5, -6241.3, 1445.8],
    "eci_km_s": [4.822, 2.181, -5.674],
    "covariance_diagonal_km2": [0.0018, 0.0022, 0.0015],
    "nis": 1.8,
    "anomaly_flag": false,
    "innovation_eci_km": [0.04, 0.02, -0.03, 0.0005, 0.0008, -0.0003]
  },
  {
    "norad_id": 49863,
    "name": "COSMOS 1408 DEB",
    "object_class": "debris",
    "last_update_epoch_utc": null,
    "confidence": null,
    "eci_km": null,
    "eci_km_s": null,
    "covariance_diagonal_km2": null,
    "nis": null,
    "anomaly_flag": null,
    "innovation_eci_km": null
  }
]
```

#### Error cases

Always returns 200. Empty array if the catalog file could not be loaded at startup.

---

### 2.3 GET /object/{norad_id}/history

Return alert history for one tracked object. Returns up to the 100 most recent anomaly event records ordered chronologically. An optional `since_utc` parameter allows incremental polling.

> **Tech debt note (TD-013):** This endpoint currently returns records from the `alerts` table only. The `state_history` table (which stores every filter update) exists in the database but is not served by this endpoint in the POC. Full continuous state history is a post-POC deliverable.

#### Path parameters

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `norad_id` | integer | yes | NORAD catalog number. |

#### Query parameters

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `since_utc` | string | no | ISO-8601 UTC timestamp. When provided, only records with `detection_epoch_utc` strictly after this value are returned. Useful for incremental polling. |

#### Response body

Array of alert history records, ordered by `epoch_utc` ascending.

| Field | Type | Nullable | Description |
|-------|------|----------|-------------|
| `id` | integer | no | Internal database row ID. |
| `epoch_utc` | string | no | ISO-8601 UTC epoch of anomaly detection. |
| `anomaly_type` | string | no | Classification. See anomaly type table in Section 4. |
| `nis` | float | no | NIS value at time of detection. |
| `status` | string | no | `"active"` if not yet resolved; `"resolved"` after the filter returned to normal range. |

#### Example request

```bash
curl "http://localhost:8000/object/25544/history"
curl "http://localhost:8000/object/25544/history?since_utc=2026-03-28T00:00:00Z"
```

#### Example response

```json
[
  {
    "id": 14,
    "epoch_utc": "2026-03-28T19:00:00+00:00",
    "anomaly_type": "maneuver",
    "nis": 247.1,
    "status": "resolved"
  }
]
```

#### Error cases

| Status | Condition |
|--------|-----------|
| 404 | `norad_id` is not present in the catalog. Body: `{"detail": "NORAD ID <id> not found in catalog."}` |

---

### 2.4 GET /object/{norad_id}/anomalies

Return anomaly history for one tracked object, including resolution timing. Returns up to the 20 most recent anomaly events, ordered newest-first.

This endpoint is a superset of `/object/{norad_id}/history`: it includes `resolution_epoch_utc` and `recalibration_duration_s`, which the history endpoint omits.

#### Path parameters

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `norad_id` | integer | yes | NORAD catalog number. |

#### Query parameters

None.

#### Response body

Array of anomaly event records, ordered by `detection_epoch_utc` descending (newest first).

| Field | Type | Nullable | Description |
|-------|------|----------|-------------|
| `id` | integer | no | Internal database row ID. |
| `norad_id` | integer | no | NORAD catalog number. |
| `detection_epoch_utc` | string | no | ISO-8601 UTC epoch when the anomaly was first detected. |
| `anomaly_type` | string | no | Classification: `maneuver`, `drag_anomaly`, or `filter_divergence`. |
| `nis_value` | float | no | NIS at time of detection. |
| `resolution_epoch_utc` | string | yes | ISO-8601 UTC epoch when NIS returned to normal range. `null` if still active. |
| `recalibration_duration_s` | float | yes | Seconds elapsed from detection to resolution. `null` if still active. |
| `status` | string | no | `"active"` or `"resolved"`. |

#### Example request

```bash
curl http://localhost:8000/object/25544/anomalies
```

#### Example response

```json
[
  {
    "id": 14,
    "norad_id": 25544,
    "detection_epoch_utc": "2026-03-28T19:00:00+00:00",
    "anomaly_type": "maneuver",
    "nis_value": 247.1,
    "resolution_epoch_utc": "2026-03-28T20:30:00+00:00",
    "recalibration_duration_s": 5400.0,
    "status": "resolved"
  }
]
```

#### Error cases

| Status | Condition |
|--------|-----------|
| 404 | `norad_id` is not present in the catalog. Body: `{"detail": "NORAD ID <id> not found in catalog."}` |

---

### 2.5 GET /object/{norad_id}/track

Return propagated track points for one tracked object: a backward track (historical positions) and an optional forward track (predicted positions with uncertainty).

Track points are generated by running SGP4 on the latest cached TLE. The reference epoch is the filter's last update epoch, or the TLE epoch if no filter has been initialized. The forward track includes a per-point `uncertainty_radius_km` derived from the filter covariance and process noise matrix.

**Coordinate frame:** All positions are ECI J2000 km. The frontend (globe.js) converts to ECEF using per-point GMST rotation. Do not apply a single rotation to the entire track.

#### Path parameters

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `norad_id` | integer | yes | NORAD catalog number. |

#### Query parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `seconds_back` | integer | no | 1500 | Seconds of history to back-propagate from the reference epoch. |
| `seconds_forward` | integer | no | 0 | Seconds into the future to forward-propagate. Set to 0 for history-only. |
| `step_s` | integer | no | 60 | Time step in seconds between consecutive track points. |

#### Response body

| Field | Type | Description |
|-------|------|-------------|
| `norad_id` | integer | NORAD catalog number. |
| `reference_epoch_utc` | string | ISO-8601 UTC epoch used as the origin point for both backward and forward propagation. Equals the filter's last update epoch, or the TLE epoch if no filter state exists. |
| `step_s` | integer | Actual time step used (echoes the query parameter). |
| `backward_track` | array[TrackPoint] | Back-propagated positions from `reference_epoch_utc - seconds_back` to `reference_epoch_utc`. Ordered chronologically oldest-first. |
| `forward_track` | array[ForwardTrackPoint] | Forward-propagated positions from `reference_epoch_utc + step_s` to `reference_epoch_utc + seconds_forward`. Empty array if `seconds_forward` is 0. |

**TrackPoint** (backward track element):

| Field | Type | Description |
|-------|------|-------------|
| `epoch_utc` | string | ISO-8601 UTC epoch of this point. |
| `eci_km` | array[float, 3] | ECI J2000 position `[x, y, z]` in km. |

**ForwardTrackPoint** (forward track element):

| Field | Type | Description |
|-------|------|-------------|
| `epoch_utc` | string | ISO-8601 UTC epoch of this point. |
| `eci_km` | array[float, 3] | ECI J2000 position `[x, y, z]` in km. |
| `uncertainty_radius_km` | float | 3-sigma uncertainty radius in km at this time step, clamped to [1.0, 500.0]. Derived from filter covariance + process noise linear growth. If no filter state exists, uses the fallback model: `1.0 + 0.5 * (t_s / 300)` km. |

#### Example request

```bash
# Historical track only (default)
curl "http://localhost:8000/object/25544/track"

# 25-minute history + 30-minute prediction, 30-second steps
curl "http://localhost:8000/object/25544/track?seconds_back=1500&seconds_forward=1800&step_s=30"
```

#### Example response

```json
{
  "norad_id": 25544,
  "reference_epoch_utc": "2026-03-28T19:00:00+00:00",
  "step_s": 60,
  "backward_track": [
    {
      "epoch_utc": "2026-03-28T18:35:00+00:00",
      "eci_km": [-4814.2, 3201.7, 3910.4]
    },
    {
      "epoch_utc": "2026-03-28T18:36:00+00:00",
      "eci_km": [-4791.5, 3238.1, 3897.8]
    }
  ],
  "forward_track": [
    {
      "epoch_utc": "2026-03-28T19:01:00+00:00",
      "eci_km": [-4503.1, 3649.8, 3762.0],
      "uncertainty_radius_km": 1.2
    },
    {
      "epoch_utc": "2026-03-28T19:02:00+00:00",
      "eci_km": [-4479.3, 3686.1, 3743.7],
      "uncertainty_radius_km": 1.4
    }
  ]
}
```

#### Error cases

| Status | Condition |
|--------|-----------|
| 404 | `norad_id` is not in the catalog. Body: `{"detail": "NORAD ID <id> not found in catalog."}` |
| 404 | `norad_id` is in the catalog but no TLE is cached yet. Body: `{"detail": "No cached TLE for NORAD ID <id>."}` |

---

### 2.6 GET /object/{norad_id}/conjunctions

Return the five most recent conjunction screening results for a given NORAD ID. Results are returned newest-first and use the same schema as the `conjunction_risk` WebSocket message.

Conjunction screening runs automatically after every confirmed anomaly detection. Results are persisted to SQLite and can be retrieved here after the fact.

#### Path parameters

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `norad_id` | integer | yes | NORAD catalog number of the anomalous object (not the potential conjunctor). |

#### Query parameters

None.

#### Response body

Array of up to 5 **ConjunctionResult** objects, newest first. Each object matches the `conjunction_risk` WebSocket message schema exactly.

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | Always `"conjunction_risk"`. |
| `anomalous_norad_id` | integer | NORAD ID of the object that triggered the screening. |
| `screening_epoch_utc` | string | ISO-8601 UTC epoch at which trajectory propagation began. |
| `horizon_s` | integer | Propagation window in seconds (currently 5400 s = 90 minutes). |
| `threshold_km` | float | First-order miss distance threshold in km (currently 5.0 km). |
| `first_order` | array[ConjunctionRisk] | Objects within `threshold_km` of the anomalous object. |
| `second_order` | array[ConjunctionRisk] | Objects within 10 km of any first-order object (excluding the anomalous object). |

**ConjunctionRisk** (first-order element):

| Field | Type | Description |
|-------|------|-------------|
| `norad_id` | integer | NORAD ID of the risk object. |
| `name` | string | Name from catalog (falls back to NORAD ID string if not found). |
| `min_distance_km` | float | Minimum Euclidean separation in km over the screening horizon. |
| `time_of_closest_approach_utc` | string | ISO-8601 UTC epoch of closest approach. |

**ConjunctionRisk** (second-order element, additional field):

| Field | Type | Description |
|-------|------|-------------|
| `via_norad_id` | integer | NORAD ID of the first-order object through which this second-order risk was identified. |

#### Example request

```bash
curl http://localhost:8000/object/25544/conjunctions
```

#### Example response

```json
[
  {
    "type": "conjunction_risk",
    "anomalous_norad_id": 25544,
    "screening_epoch_utc": "2026-03-28T19:00:00Z",
    "horizon_s": 5400,
    "threshold_km": 5.0,
    "first_order": [
      {
        "norad_id": 49863,
        "name": "COSMOS 1408 DEB",
        "min_distance_km": 3.21,
        "time_of_closest_approach_utc": "2026-03-28T19:47:00Z"
      }
    ],
    "second_order": [
      {
        "norad_id": 49864,
        "name": "COSMOS 1408 DEB",
        "min_distance_km": 8.74,
        "via_norad_id": 49863,
        "time_of_closest_approach_utc": "2026-03-28T20:01:00Z"
      }
    ]
  }
]
```

#### Error cases

| Status | Condition |
|--------|-----------|
| 404 | `norad_id` is not in the catalog. Body: `{"detail": "NORAD ID <id> not found in catalog."}` |

Returns an empty array (not 404) if the object is in the catalog but no conjunction screenings have been run yet.

---

### 2.7 GET /alerts/active

Return all currently unresolved anomaly alerts across the entire catalog. Intended for use by the frontend on WebSocket connect or reconnect to seed the alert panel with events that fired while the client was disconnected.

Each record is formatted identically to a `anomaly` WebSocket message so the frontend can call `addAlert()` directly without transformation.

#### Parameters

None.

#### Response body

Array of active alert records ordered by detection time ascending. Returns an empty array if no alerts are currently active.

Each element uses the same field set as the WebSocket `anomaly` message type (see Section 3.2). The `innovation_eci_km` field is set to `[0.0, 0.0, 0.0, 0.0, 0.0, 0.0]` for records retrieved from the database (the raw innovation vector is not persisted).

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | Always `"anomaly"`. |
| `norad_id` | integer | NORAD catalog number. |
| `epoch_utc` | string | ISO-8601 UTC epoch of detection. |
| `anomaly_type` | string | `maneuver`, `drag_anomaly`, or `filter_divergence`. |
| `nis` | float | NIS value at time of detection. |
| `innovation_eci_km` | array[float, 6] | Always `[0.0, 0.0, 0.0, 0.0, 0.0, 0.0]` for REST-retrieved records. |
| `confidence` | float | Current filter confidence score (from live filter state if available, else 0.0). |
| `eci_km` | array[float, 3] | Current ECI J2000 position in km (from live filter state if available, else `[0.0, 0.0, 0.0]`). |
| `eci_km_s` | array[float, 3] | Current ECI J2000 velocity in km/s (from live filter state if available, else `[0.0, 0.0, 0.0]`). |
| `covariance_diagonal_km2` | array[float, 3] | Current position covariance diagonal in km² (from live filter state if available, else `[1000.0, 1000.0, 1000.0]`). |

#### Example request

```bash
curl http://localhost:8000/alerts/active
```

#### Example response

```json
[
  {
    "type": "anomaly",
    "norad_id": 25544,
    "epoch_utc": "2026-03-28T19:00:00+00:00",
    "anomaly_type": "maneuver",
    "nis": 247.1,
    "innovation_eci_km": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    "confidence": 0.61,
    "eci_km": [-4527.0, 3613.2, 3780.1],
    "eci_km_s": [-5.121, -4.872, 2.301],
    "covariance_diagonal_km2": [0.25, 0.31, 0.19]
  }
]
```

#### Error cases

Always returns 200. Empty array if no active alerts exist.

---

### 2.8 POST /admin/trigger-process

Force-run one processing cycle for all catalog objects. Reads all cached TLEs from the database in chronological order, runs the predict → update → anomaly → recalibrate pipeline for each, and broadcasts results to all connected WebSocket clients.

This endpoint exists to support `scripts/seed_maneuver.py`: after the script inserts a synthetic TLE, calling this endpoint makes the anomaly visible in the browser within seconds without waiting for the next Space-Track polling interval.

**Processing order:** All TLE records across all objects are sorted globally by epoch before processing. This ensures the Kalman filter covariance converges on prior TLEs before the most-recent (potentially maneuver-injected) TLE is processed.

**Broadcast behavior:** Only the final (most-recent) message per object is broadcast to WebSocket clients to avoid flooding. All anomaly messages are always broadcast regardless of whether they are the final message for an object.

#### Parameters

None. No request body.

#### Response body

| Field | Type | Description |
|-------|------|-------------|
| `processed` | integer | Count of objects for which at least one WebSocket message was produced. |

#### Example request

```bash
curl -X POST http://localhost:8000/admin/trigger-process
```

#### Example response

```json
{
  "processed": 58
}
```

#### Error cases

Always returns 200. Per-object errors are logged but do not abort the cycle or cause an error response.

---

## 3. WebSocket API

### Connection

```
ws://localhost:8000/ws/live
```

### Connection lifecycle

1. The client opens a WebSocket connection to `ws://localhost:8000/ws/live`.
2. If the server already has `MAX_WS_CONNECTIONS` (20) active connections, the server closes the socket with code 1013 (Try Again Later) before completing the handshake.
3. On successful connection, the server immediately sends one `state_update` message per currently tracked object to seed the client with the current state of the globe. This prevents the client from displaying an empty scene after reconnect.
4. The server then enters a broadcast-only mode for state-driven messages. The client may send any text frame as a keepalive ping; the server reads and discards all incoming frames.
5. On client disconnect (or any send error), the server removes the connection from the broadcast set. Other clients are unaffected.

### Reconnection guidance

On reconnect, the server replays current state via the initial `state_update` burst (step 3 above). Additionally, call `GET /alerts/active` immediately after reconnect to retrieve any anomaly alerts that fired while the client was disconnected. The returned records have the same shape as the `anomaly` WebSocket message and can be passed directly to the alert panel without transformation.

### Message envelope

All WebSocket messages share a common envelope:

| Field | Type | Present on | Description |
|-------|------|-----------|-------------|
| `type` | string | all messages | Message discriminator. One of `state_update`, `anomaly`, `recalibration`, `conjunction_risk`. |
| `norad_id` | integer | state messages | NORAD catalog number of the subject object. Absent on `conjunction_risk`. |

---

### 3.1 Message type: `state_update`

**Trigger:** Sent to all clients after each successful Kalman filter predict-update cycle that produces no anomaly, and also sent individually to new clients immediately on connect (one message per initialized object).

**Meaning:** Normal tracking state. The NIS value is within the chi-squared acceptance region (threshold 12.592 for 6 degrees of freedom). No action is required.

#### Fields

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"state_update"` |
| `norad_id` | integer | NORAD catalog number. |
| `epoch_utc` | string | ISO-8601 UTC epoch of this state estimate, ending with `Z`. |
| `eci_km` | array[float, 3] | ECI J2000 position `[x, y, z]` in km. |
| `eci_km_s` | array[float, 3] | ECI J2000 velocity `[vx, vy, vz]` in km/s. |
| `covariance_diagonal_km2` | array[float, 3] | Position covariance diagonal `[P_xx, P_yy, P_zz]` in km². |
| `nis` | float | Normalized Innovation Squared. Values below 12.592 indicate normal filter behavior. |
| `innovation_eci_km` | array[float, 6] | Innovation (residual) vector `[dx, dy, dz, dvx, dvy, dvz]` in ECI km and km/s. |
| `confidence` | float | Filter confidence score in [0, 1]. Derived from NIS relative to the chi-squared threshold. |
| `anomaly_type` | null | Always `null` for `state_update` messages. |

#### Example

```json
{
  "type": "state_update",
  "norad_id": 46075,
  "epoch_utc": "2026-03-28T18:32:10Z",
  "eci_km": [3011.5, -6241.3, 1445.8],
  "eci_km_s": [4.822, 2.181, -5.674],
  "covariance_diagonal_km2": [0.0018, 0.0022, 0.0015],
  "nis": 1.8,
  "innovation_eci_km": [0.04, 0.02, -0.03, 0.0005, 0.0008, -0.0003],
  "confidence": 0.97,
  "anomaly_type": null
}
```

---

### 3.2 Message type: `anomaly`

**Trigger:** Sent when the Kalman filter NIS exceeds the chi-squared threshold (12.592 for 6 DOF) and an anomaly type has been classified. For active satellites, this message may be provisional (emitted on the first exceedance cycle before a second cycle confirms the maneuver classification). The `anomaly_type` field reflects the classification at the time of emission; it may be retroactively corrected in the database on the second cycle but the WebSocket message is not resent.

**What to do:** Display an alert for the indicated object. Show the `anomaly_type` label, `nis` value, and `epoch_utc`. Expand the object's uncertainty ellipsoid on the globe using `covariance_diagonal_km2`. A `recalibration` message will follow shortly for non-active satellites; for active satellites, recalibration follows on the next processing cycle.

#### Fields

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"anomaly"` |
| `norad_id` | integer | NORAD catalog number. |
| `epoch_utc` | string | ISO-8601 UTC epoch of anomaly detection. |
| `eci_km` | array[float, 3] | ECI J2000 position at detection epoch, in km. |
| `eci_km_s` | array[float, 3] | ECI J2000 velocity at detection epoch, in km/s. |
| `covariance_diagonal_km2` | array[float, 3] | Position covariance diagonal at detection epoch. Elevated relative to normal tracking. |
| `nis` | float | NIS value at detection. Values for reference: ~247 for ISS reboost maneuver, ~24 for Starlink station-keeping maneuver. |
| `innovation_eci_km` | array[float, 6] | Innovation vector at detection: `[dx, dy, dz, dvx, dvy, dvz]` in ECI km and km/s. The magnitude and direction encode the anomaly signature (e.g., predominantly velocity-direction component for a maneuver). |
| `confidence` | float | Filter confidence after anomaly detection (typically reduced). |
| `anomaly_type` | string | Classification. One of `"maneuver"`, `"drag_anomaly"`, `"filter_divergence"`. See Section 4 for definitions. |

#### Example — maneuver on ISS

```json
{
  "type": "anomaly",
  "norad_id": 25544,
  "epoch_utc": "2026-03-28T19:00:00Z",
  "eci_km": [-4527.0, 3613.2, 3780.1],
  "eci_km_s": [-5.121, -4.872, 2.301],
  "covariance_diagonal_km2": [0.25, 0.31, 0.19],
  "nis": 247.1,
  "innovation_eci_km": [0.8, 0.3, -1.2, 0.042, 0.019, -0.031],
  "confidence": 0.61,
  "anomaly_type": "maneuver"
}
```

#### Example — Starlink maneuver (lower NIS, station-keeping)

```json
{
  "type": "anomaly",
  "norad_id": 46075,
  "epoch_utc": "2026-03-28T20:14:30Z",
  "eci_km": [3021.1, -6238.7, 1448.2],
  "eci_km_s": [4.831, 2.175, -5.669],
  "covariance_diagonal_km2": [0.12, 0.15, 0.10],
  "nis": 24.3,
  "innovation_eci_km": [0.3, -0.1, 0.5, 0.009, -0.004, 0.011],
  "confidence": 0.72,
  "anomaly_type": "maneuver"
}
```

---

### 3.3 Message type: `recalibration`

**Trigger:** Sent immediately after the filter covariance has been inflated and the state reset to absorb the anomaly. This follows an `anomaly` message. For active satellites, it arrives on the second processing cycle (after the maneuver classification is confirmed); for debris and rocket bodies, it arrives in the same cycle as the `anomaly` message.

**What to do:** Update the object's state on the globe with the recalibrated position and reduced (growing) uncertainty. The covariance will be larger than during normal tracking because inflation has reset it; it will converge back down over subsequent cycles. Mark the alert as "recalibrating" in the UI.

#### Fields

Identical field set to `state_update`, with the following differences:

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"recalibration"` |
| `anomaly_type` | string | Classification of the anomaly that triggered recalibration. Same values as the preceding `anomaly` message. |

All other fields (`norad_id`, `epoch_utc`, `eci_km`, `eci_km_s`, `covariance_diagonal_km2`, `nis`, `innovation_eci_km`, `confidence`) have the same meaning as in `state_update` and reflect the filter state after covariance inflation.

#### Example

```json
{
  "type": "recalibration",
  "norad_id": 25544,
  "epoch_utc": "2026-03-28T19:30:00Z",
  "eci_km": [-3817.2, 4102.5, 3501.8],
  "eci_km_s": [-5.443, -4.312, 2.711],
  "covariance_diagonal_km2": [5.0, 6.2, 3.8],
  "nis": 3.1,
  "innovation_eci_km": [0.11, 0.07, -0.09, 0.002, -0.001, 0.003],
  "confidence": 0.78,
  "anomaly_type": "maneuver"
}
```

Note that `covariance_diagonal_km2` is significantly larger than normal (reflecting the inflation factor of 20.0 applied for maneuver events) and will shrink over subsequent update cycles.

---

### 3.4 Message type: `conjunction_risk`

**Trigger:** Sent asynchronously after an anomaly is confirmed, once the CPU-bound conjunction screening computation completes (typically 9–18 seconds after the `anomaly` message). The screening propagates the anomalous object's predicted trajectory forward 90 minutes (5400 seconds) in 60-second steps and compares it against all other tracked objects.

**Screening thresholds:** First-order risks are objects with a minimum miss distance ≤ 5 km. Second-order risks are objects within 10 km of any first-order object (excluding the anomalous object).

**What to do:** Highlight the first-order objects on the globe. Display a conjunction risk table showing miss distance and time of closest approach (TCA). Second-order entries represent indirect collision chain risks and should be displayed at lower prominence.

#### Fields

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"conjunction_risk"` |
| `anomalous_norad_id` | integer | NORAD ID of the object that triggered screening. |
| `screening_epoch_utc` | string | ISO-8601 UTC epoch at which trajectory propagation began (the anomaly epoch). |
| `horizon_s` | integer | Propagation window in seconds (5400 = 90 minutes). |
| `threshold_km` | float | First-order screening threshold in km (5.0). |
| `first_order` | array | Objects within `threshold_km` of the anomalous object's predicted trajectory. May be empty. |
| `second_order` | array | Objects within 10 km of any first-order object. May be empty. |

**First-order entry:**

| Field | Type | Description |
|-------|------|-------------|
| `norad_id` | integer | NORAD ID of the risk object. |
| `name` | string | Object name. Falls back to the NORAD ID as a string if not in catalog. |
| `min_distance_km` | float | Minimum Euclidean separation in km over the 90-minute screening window. |
| `time_of_closest_approach_utc` | string | ISO-8601 UTC epoch (with `Z` suffix) of closest approach. |

**Second-order entry (additional field):**

| Field | Type | Description |
|-------|------|-------------|
| `via_norad_id` | integer | NORAD ID of the first-order object through which this second-order risk is linked. |

#### Example

```json
{
  "type": "conjunction_risk",
  "anomalous_norad_id": 25544,
  "screening_epoch_utc": "2026-03-28T19:00:00Z",
  "horizon_s": 5400,
  "threshold_km": 5.0,
  "first_order": [
    {
      "norad_id": 49863,
      "name": "COSMOS 1408 DEB",
      "min_distance_km": 3.21,
      "time_of_closest_approach_utc": "2026-03-28T19:47:00Z"
    }
  ],
  "second_order": [
    {
      "norad_id": 49864,
      "name": "COSMOS 1408 DEB",
      "min_distance_km": 8.74,
      "via_norad_id": 49863,
      "time_of_closest_approach_utc": "2026-03-28T20:01:00Z"
    }
  ]
}
```

An empty `first_order` array indicates the post-anomaly trajectory has no close approaches within the screening horizon.

---

## 4. Data Types

### ObjectState

The common tracking state structure. Fields appear in `state_update`, `anomaly`, and `recalibration` WebSocket messages and in the `GET /catalog` response body.

| Field | Type | Description |
|-------|------|-------------|
| `norad_id` | integer | NORAD catalog number. International standard identifier for all tracked space objects. |
| `epoch_utc` | string | ISO-8601 UTC epoch of the state estimate. Always ends with `Z`. |
| `eci_km` | array[float, 3] | ECI J2000 position `[x, y, z]` in km. |
| `eci_km_s` | array[float, 3] | ECI J2000 velocity `[vx, vy, vz]` in km/s. |
| `covariance_diagonal_km2` | array[float, 3] | Position covariance diagonal `[P_xx, P_yy, P_zz]` in km². The square root of each element is the 1-sigma position uncertainty on that axis. |
| `nis` | float | **Normalized Innovation Squared.** Scalar measure of filter consistency. Computed as `y^T S^{-1} y` where `y` is the innovation vector and `S` is the innovation covariance. Under the filter's model assumptions, NIS follows a chi-squared distribution with 6 degrees of freedom. The 95th percentile threshold is 12.592; values above this indicate a model mismatch (anomaly). Nominal ISS tracking produces NIS ≈ 1–5. An ISS reboost produces NIS ≈ 247. A Starlink station-keeping maneuver produces NIS ≈ 24. |
| `innovation_eci_km` | array[float, 6] | Innovation vector `[dx, dy, dz, dvx, dvy, dvz]` in ECI km and km/s. Represents the difference between the predicted state and the TLE-derived observation at the current epoch. Non-zero under normal tracking; large magnitude during anomalies. |
| `confidence` | float | Scalar in [0, 1] summarizing filter health. Higher values indicate the NIS is comfortably within the acceptance region. Drops toward 0 when NIS is elevated. |
| `anomaly_type` | string or null | `null` during normal tracking. Set to one of the values below when an anomaly is detected. |

### Anomaly types

| Value | Meaning | NIS pattern | Inflation factor applied |
|-------|---------|-------------|--------------------------|
| `"maneuver"` | Deliberate orbit change by an active satellite. Requires NIS exceedance on at least 2 consecutive processing cycles AND `object_class == "active_satellite"`. | Sustained, large NIS. Example: ~247 for ISS reboost. | 20.0 — large inflation because the orbit has physically changed. |
| `"drag_anomaly"` | Unmodeled atmospheric drag causing systematic along-track position error. Detected when the along-track component of the position residual dominates the cross-track component by ≥ 3:1 and cross-track is < 1 km. | Moderate NIS; residual predominantly along-track. | 10.0 |
| `"filter_divergence"` | Catch-all for any NIS exceedance not meeting the criteria for the above two types. | Single-cycle NIS spike without systematic direction. | 10.0 |

### CatalogEntry

See Section 2.2 (GET /catalog) for the full field list. The three catalog object classes are:

| `object_class` | Description |
|----------------|-------------|
| `active_satellite` | Operational satellite capable of maneuvering. Triggers the deferred 2-cycle maneuver confirmation logic. |
| `debris` | Non-maneuvering debris fragment. Anomalies trigger immediate recalibration. |
| `rocket_body` | Spent upper stage or launch vehicle body. Non-maneuvering; same logic as debris. |

### AnomalyEvent

Returned by `GET /object/{norad_id}/anomalies`.

| Field | Type | Nullable | Description |
|-------|------|----------|-------------|
| `id` | integer | no | Database row ID. |
| `norad_id` | integer | no | NORAD catalog number. |
| `detection_epoch_utc` | string | no | ISO-8601 UTC epoch of detection. |
| `anomaly_type` | string | no | `maneuver`, `drag_anomaly`, or `filter_divergence`. |
| `nis_value` | float | no | NIS at detection. |
| `resolution_epoch_utc` | string | yes | ISO-8601 UTC epoch when NIS returned to normal range. `null` while active. |
| `recalibration_duration_s` | float | yes | Seconds from detection to resolution. `null` while active. |
| `status` | string | no | `"active"` or `"resolved"`. |

### ConjunctionRisk

See Section 3.4 for the full field definitions for first-order and second-order entries.

---

## 5. Error Handling

### HTTP status codes

| Code | Meaning |
|------|---------|
| 200 | Success. |
| 404 | Resource not found. The `norad_id` is not in the catalog, or no TLE is cached for the object. |
| 422 | Unprocessable entity. FastAPI validation error (e.g., a path parameter that cannot be parsed as an integer). |
| 500 | Internal server error. Unexpected exception in endpoint logic. Check server logs. |

### Error response body (4xx and 5xx)

FastAPI standard error format:

```json
{
  "detail": "Human-readable description of the error."
}
```

For 422 validation errors, `detail` is an array of validation error objects:

```json
{
  "detail": [
    {
      "loc": ["path", "norad_id"],
      "msg": "value is not a valid integer",
      "type": "type_error.integer"
    }
  ]
}
```

### WebSocket error codes

| Code | Meaning |
|------|---------|
| 1013 | Connection refused. The server has reached the maximum of 20 simultaneous WebSocket connections. Retry after a delay. |
| 1000 | Normal closure (client-initiated). |
| 1001 | Going away (server shutdown). |

---

## 6. Rate Limiting and Operational Notes

### Space-Track.org polling

The backend polls Space-Track.org for TLE updates at most once every **30 minutes**, in compliance with Space-Track rate limits. The `ingest.py` module is the sole point of contact with the Space-Track API; no other module calls the API directly.

TLE responses are cached to SQLite (`data/catalog/tle_cache.db` by default, overridden by the `NBODY_DB_PATH` environment variable). The system can operate entirely offline once an initial TLE pull has been completed. For demo presentations, run `scripts/replay.py --hours 72` before going offline to ensure a 72-hour TLE window is cached.

### Processing cycle cadence

The processing pipeline runs event-driven: each time the ingest loop fetches new TLEs from Space-Track, it publishes a `catalog_update` event to an internal queue. The processing loop consumes this event and runs one predict-update cycle per tracked object. Under normal Space-Track availability, this means one processing cycle every 30 minutes per object.

Conjunction screening, when triggered, runs asynchronously off the main event loop and typically completes within 9–18 seconds (90 SGP4 propagation steps × number of catalog objects).

### WebSocket reconnection

The server does not persist WebSocket session state. On reconnect:

1. The server sends a `state_update` burst covering all currently tracked objects with initialized filter states.
2. Call `GET /alerts/active` to retrieve anomaly alerts that fired during the disconnection window.
3. Call `GET /object/{norad_id}/conjunctions` for any objects with active alerts to retrieve the most recent conjunction screening results.

Clients should implement exponential backoff on reconnect (suggested: 1 s, 2 s, 4 s, up to 30 s maximum interval).

### Pending anomaly timeout

For active satellites, the maneuver classification requires two consecutive NIS exceedance cycles. If a second TLE does not arrive within **2 hours** (configurable via the `NBODY_PENDING_ANOMALY_TIMEOUT_HOURS` environment variable), the pending anomaly is resolved using the provisional classification from the first cycle. This prevents stale pending state during Space-Track outages.

### Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SPACETRACK_USER` | yes | — | Space-Track.org account email. |
| `SPACETRACK_PASS` | yes | — | Space-Track.org account password. |
| `CESIUM_ION_TOKEN` | yes | `""` | CesiumJS Ion access token. Served to the frontend via `GET /config`. |
| `NBODY_DB_PATH` | no | `data/catalog/tle_cache.db` | Path to the SQLite database file. |
| `NBODY_CATALOG_CONFIG` | no | `data/catalog/catalog.json` | Path to the catalog JSON configuration file. |
| `NBODY_PENDING_ANOMALY_TIMEOUT_HOURS` | no | `2.0` | Hours before a pending maneuver check times out. |

---

## Appendix A: Example NORAD IDs

| NORAD ID | Name | Object class |
|----------|------|--------------|
| 25544 | ISS (ZARYA) | active\_satellite |
| 48274 | CSS (TIANHE) | active\_satellite |
| 20580 | HST | active\_satellite |
| 46075 | STARLINK-1990 | active\_satellite |
| 44235 | STARLINK-24 | active\_satellite |
| 46496 | CAPELLA-2 (SEQUOIA) | active\_satellite |
| 47474 | BLACKSKY GLOBAL-7 | active\_satellite |
| 49863 | COSMOS 1408 DEB | debris |
| 48275 | CZ-5B R/B (HIGH REENTRY RISK) | rocket\_body |
| 54216 | FALCON 9 R/B | rocket\_body |

---

*This document is derived from `backend/main.py`, `backend/processing.py`, `backend/anomaly.py`, and `backend/conjunction.py`. All field names, endpoint paths, and message schemas are authoritative as of the code in this repository. Any discrepancy between this document and the source code should be resolved in favor of the source code.*
