# Telemetry data contract

This is the reference for the data that flows through the digital twin (issue #1). Related
documents:

- [`sensor_streams.md`](sensor_streams.md): the MQTT topics (one per sensor) and the replay tick.
- [`representation_model.md`](representation_model.md): what the representation models and why.

## 1. Mission records (CSV)

`data/*_mission_*.csv` and `data/clean_replay_mission.csv` hold one flat row per second of a ZTBus
mission. `data/mock_state.json` holds 100 sample rows with the same columns. They contain the
cleaned ZTBus columns plus the columns derived in `data_preparation.ipynb`.

| File | Use |
|---|---|
| `train_mission_2021-09-25.csv`, `train_mission_2021-09-29.csv` | ML training (route 83) |
| `validation_mission_2021-10-04.csv` | ML model selection and anomaly threshold |
| `test_mission_2021-10-05.csv` | Replayed mission (producer and dashboard) and final ML test |
| `test_predictions_2021-10-05.csv` | Offline model predictions for the test mission |
| `mock_state.json` | Sample record shown by the dashboard before a replay starts |

| Column | Unit | Source |
|---|---|---|
| `time_unix` | s (UTC epoch) | ZTBus |
| `timestamp` | `YYYY-MM-DD HH:MM:SS` (UTC) | derived from `time_unix` |
| `latitude`, `longitude` | degrees | ZTBus `gnss_latitude` / `gnss_longitude` (radians → degrees) |
| `gnss_altitude` | m | ZTBus |
| `heading_deg` | degrees | ZTBus `gnss_course` (radians → degrees) |
| `odometry_vehicleSpeed` | m/s | ZTBus |
| `speed_kmh` | km/h | `odometry_vehicleSpeed * 3.6` |
| `electric_powerDemand` | W | ZTBus |
| `power_kw` | kW | `electric_powerDemand / 1000` (negative = regenerative braking) |
| `traction_brakePressure` | Pa | ZTBus |
| `brake_bar` | bar | `traction_brakePressure / 100000` |
| `traction_tractionForce` | N | ZTBus |
| `odometry_steeringAngle`, `odometry_articulationAngle` | rad | ZTBus |
| `odometry_wheelSpeed_{fl,fr,ml,mr,rl,rr}` | m/s | ZTBus |
| `status_doorIsOpen`, `status_gridIsAvailable`, `status_haltBrakeIsActive`, `status_parkBrakeIsActive` | 0 / 1 | ZTBus |
| `itcs_numberOfPassengers` | passengers | ZTBus |
| `itcs_stopName`, `itcs_busRoute` | text, `-` when there is no event | ZTBus |
| `temp_celsius` | °C | ZTBus `temperature_ambient` (K → °C) |

## 2. MQTT

The producer publishes one message per sensor reading on `ztbus/<bus>/sensors/<sensor>`, then
`ztbus/<bus>/replay/tick` once every reading of that second is out. It also publishes
`ztbus/<bus>/replay` when a replay starts and ends. Sensor names, units and cadence are listed in
[`sensor_streams.md`](sensor_streams.md). In short, the MQTT names match the record columns except
for these:

| MQTT sensor | Record column |
|---|---|
| `altitude_m` | `gnss_altitude` |
| `grid_available`, `doors_open`, `halt_brake`, `park_brake` | `status_*` |
| `passengers` | `itcs_numberOfPassengers` |
| `steering_angle_rad`, `articulation_angle_rad`, `wheel_speed_*` | `odometry_*` |
| `traction_force_n` | `traction_tractionForce` |

`itcs_busRoute` and `itcs_stopName` are event-based. They are only published when the record has a
value, so the state keeps the last known route and stop.

## 3. Redis keys

| Key | Type | Written by | Content |
|---|---|---|---|
| `bus:<bus>:state` | hash | state bridge, ML worker | The current representation: latest value of every sensor (null → `""`), `timestamp` / `time_unix` / `snapshot_sequence` of the last tick, `operational_state`, `representation_timestamp`, `representation_sequence`, and the `ml_*` fields below |
| `bus:<bus>:prediction` | hash | ML worker | The same ML result without the `ml_` prefix |
| `ztbus:replay:messages` | stream (capped at ~5000) | state bridge | Every MQTT message (`topic`, `payload`, `received_at`), plus a `ztbus/<bus>/state` entry each time `operational_state` changes |
| `ztbus:replay:topic_latest:<topic>` | string | state bridge | Latest JSON payload per topic |
| `ztbus:replay:topic_counts` | hash | state bridge | Messages received per topic |
| `ztbus:replay:bridge_status` | string | state bridge | `ready` once subscribed |
| `ztbus:replay:control` | hash | dashboard and producer (`--control-redis-host`) | `status` (`running` / `paused` / `stopped` / `completed` / `error`), `speed` (0.25–10), `index`, `total`, `started_at`, `pid`, `error` |

ML fields in `bus:<bus>:state`, written by `src/ml/predictor.py` on every tick:

| Field | Meaning |
|---|---|
| `ml_status` | `warming_up` until 60 consecutive seconds are available, then `ready` |
| `ml_samples_collected`, `ml_samples_required` | Warm-up progress |
| `ml_predicted_mean_power_kw_next_10s` | Forecast mean power over the next 10 s |
| `ml_current_power_kw` | Power at the time of the forecast |
| `ml_evaluated_prediction_kw`, `ml_actual_mean_power_kw_next_10s`, `ml_absolute_error_kw` | The forecast made 10 s ago, what actually happened, and the error |
| `ml_anomaly_threshold_kw`, `ml_is_anomaly` | 99th percentile of validation errors; `1` when the last error exceeds it |

## 4. Operational state

On every `replay/tick` the bridge runs
`src/state_machine/inference.py::infer_state(telemetry) -> str` on the complete state. The function
accepts both the record and the MQTT names. It returns 11 dimensions joined by `" | "`:

```
operation | motion | direction | braking | energy | doors | grid | stop | service | occupancy | heading
```

| Dimension | Values |
|---|---|
| operation | `Boarding` (doors open), `Regenerating` (power < −0.5 kW), `Cruising` (\|speed\| > 1 km/h), `Idling` |
| motion | `Forward`, `Reverse`, `Stopped` |
| direction | `Straight`, `TurningLeft`, `TurningRight`, `Stationary` |
| braking | `ParkBrake`, `HaltBrake`, `Braking` (> 0.1 bar), `Released` |
| energy | `Regenerating`, `Consuming`, `Neutral` |
| doors / grid | `Open`/`Closed`, `Available`/`Unavailable` |
| stop / service | `AtStop`/`BetweenStops`, `InService`/`Unknown` |
| occupancy | `Empty`, `Low` (≤ 15), `Medium` (≤ 30), `High` |
| heading | `N`, `NE`, `E`, `SE`, `S`, `SW`, `W`, `NW` |

The result is stored as `operational_state`.
