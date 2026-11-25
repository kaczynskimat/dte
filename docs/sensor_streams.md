# Historical sensor replay

The replay uses real B208 bus telemetry sampled once per second. Every sensor
has a separate MQTT topic and every message contains one sensor reading plus
the original absolute timestamp. No combined sensor payload is published.

The continuous and status sensors are periodic at 1 Hz because that is the
cadence of the source dataset. Route and stop readings are event-based: the
source uses `-` when there is no new route/stop event, and the producer publishes
only the actual values. The representation therefore retains the last known
route and stop between events.

| MQTT topic suffix | Reading | Unit/type | Cadence |
|---|---|---|---|
| `sensors/latitude` | Latitude | degrees | periodic, 1 Hz |
| `sensors/longitude` | Longitude | degrees | periodic, 1 Hz |
| `sensors/altitude_m` | GNSS altitude | m | periodic, 1 Hz |
| `sensors/heading_deg` | GNSS heading | degrees | periodic, 1 Hz |
| `sensors/speed_kmh` | Vehicle speed | km/h | periodic, 1 Hz |
| `sensors/power_kw` | Electrical power demand | kW | periodic, 1 Hz |
| `sensors/grid_available` | Grid availability | boolean | periodic, 1 Hz |
| `sensors/doors_open` | Door state | boolean | periodic, 1 Hz |
| `sensors/passengers` | Passenger count | people | periodic, 1 Hz |
| `sensors/temp_celsius` | Ambient temperature | °C | periodic, 1 Hz |
| `sensors/steering_angle_rad` | Steering angle | rad | periodic, 1 Hz |
| `sensors/articulation_angle_rad` | Articulation angle | rad | periodic, 1 Hz |
| `sensors/wheel_speed_fl` | Front-left wheel speed | m/s | periodic, 1 Hz |
| `sensors/wheel_speed_fr` | Front-right wheel speed | m/s | periodic, 1 Hz |
| `sensors/wheel_speed_ml` | Middle-left wheel speed | m/s | periodic, 1 Hz |
| `sensors/wheel_speed_mr` | Middle-right wheel speed | m/s | periodic, 1 Hz |
| `sensors/wheel_speed_rl` | Rear-left wheel speed | m/s | periodic, 1 Hz |
| `sensors/wheel_speed_rr` | Rear-right wheel speed | m/s | periodic, 1 Hz |
| `sensors/halt_brake` | Halt-brake state | boolean | periodic, 1 Hz |
| `sensors/park_brake` | Park-brake state | boolean | periodic, 1 Hz |
| `sensors/brake_bar` | Brake pressure | bar | periodic, 1 Hz |
| `sensors/traction_force_n` | Traction force | N | periodic, 1 Hz |
| `sensors/itcs_busRoute` | Public-transport route | identifier | event-based |
| `sensors/itcs_stopName` | Current stop name | text | event-based |

The full topic is `ztbus/B208/<suffix>`. After publishing the 22 periodic
readings and any route/stop events for one timestamp, the producer publishes
`ztbus/B208/replay/tick`. This is a replay clock event, not a sensor aggregation.
It tells consumers that every reading for the timestamp has been published and
that the representation can transition atomically to the new situation.

The original timing is preserved using:

```text
wall-clock publication time = t0 + (T - T0) / speed
```

The speed and timestamp interval are configurable. For example:

```bash
python -m src.replay.producer \
  --file data/test_mission_2021-10-05.csv \
  --start-time "2021-10-05 07:00:00" \
  --end-time "2021-10-05 09:00:00" \
  --speed 10
```

The producer rejects a mission whose explicit route values differ from route
83. Missing route readings are represented by `-` in the source and do not
indicate a different route.
