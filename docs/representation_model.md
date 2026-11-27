# Bus digital-shadow representation

## Purpose and users

The primary users are fleet operators and energy analysts. The representation
combines vehicle movement, passenger, braking, location and energy telemetry to
show the current bus situation, forecast near-future power demand and identify
behaviour that differs strongly from the learned energy profile.

## Entities and relations

- **Bus B208** is the represented physical asset.
- **Sensor streams** belong to the bus and provide one timestamped property per
  MQTT topic.
- **Route and stop** locate the bus in its public-transport service context.
- **Replay clock** orders the historical sensor events and closes each complete
  one-second situation.
- **Operational state** is derived from the latest complete sensor situation.
- **Energy forecast** belongs to the current operational state and estimates
  average power demand over the following 10 seconds.
- **Anomaly assessment** compares a forecast with the ten readings that follow
  it and belongs to the forecast that was evaluated.

Redis hash `bus:B208:state` is the current representation. It contains the most
recent individual sensor values, replay timestamp and sequence, the inferred
operational state and the current ML result. `bus:B208:prediction` exposes the
same ML result separately for consumers interested only in forecasting.

## State and transitions

The rule-based state combines independent operational dimensions into one
state string:

- operation: Boarding, Regenerating, Cruising or Idling;
- motion: Forward, Reverse or Stopped;
- direction: Straight, TurningLeft, TurningRight or Stationary;
- braking: ParkBrake, HaltBrake, Braking or Released;
- energy: Consuming, Regenerating or Neutral;
- doors, grid availability, stop/service status, occupancy and heading.

Every `replay/tick` event marks a transition. The bridge reads the complete
sensor state, applies the rules and replaces the derived operational state. A
missing or discontinuous replay sequence resets the ML history so that a
forecast is never constructed across a gap.

## Initial situation and replay interval

The user selects a timestamp interval and replay speed. The first selected
timestamp defines the initial situation: its 24 readings populate Redis before
the first tick is applied. The representation then advances once per replayed
timestamp. The ML component reports `warming_up` until 60 consecutive situations
are available and produces a new forecast on every subsequent tick.

## Machine-learning enrichment

XGBoost is trained only on route-83 missions from 25 and 29 September 2021.
The 4 October mission is used for validation. The replayed and final-test
mission is 5 October 2021, so every training and model-selection observation is
strictly earlier than the replayed period.

The model uses 60 seconds of past and current telemetry to predict mean power
demand over the next 10 seconds. On the held-out replay mission it achieved
MAE 16.77 kW, RMSE 24.35 kW and R² 0.800. The anomaly threshold is the 99th
percentile of absolute validation residuals. Ten ticks after a prediction, the
worker calculates its actual error and updates the representation.

## Alternative representations

- A raw key/value telemetry mirror was rejected as the complete representation
  because it does not express higher-level bus operation or transitions.
- A detailed physical simulation was rejected because the dataset does not
  provide every mechanical parameter required to calibrate one reliably.
- A graph model was considered for relations among bus, route and stops, but a
  single-bus replay does not currently require graph traversal. The state model
  is simpler and directly supports the dashboard and 3D animation.

The selected hybrid model retains sensor values for fidelity, adds deterministic
operational semantics and enriches them with a learned energy forecast.

## Fidelity, drift and limitations

Replay fidelity is checked by preserving source timestamps, the exact cadence
formula and one message per recorded sensor reading. State rules are tested at
their thresholds. ML fidelity is measured on an unseen, chronologically later
mission and compared with persistence baselines.

At every replay tick, the bridge also verifies that all 22 periodic sensors
belong to the current source timestamp and pass conservative physical-range
checks. It exposes a `valid`, `warning` or `invalid` quality status plus explicit
missing, stale and out-of-range flags. Route and stop are excluded from the
staleness check because they are event-based. The end-to-end fidelity observer
compares every completed Redis snapshot with its original CSV row and measures
the field match rate and bridge latency. The reproducible procedure is described
in `docs/fidelity_validation.md`.

Operational drift can be monitored through rolling forecast MAE and the rate of
anomaly flags. A sustained increase above validation levels indicates that the
energy relationship has changed. The safe response is to retain the warning,
collect a new labelled period, evaluate it separately, retrain only offline and
replace the model only after held-out validation. The model is not updated
silently during replay.

The main limitations are that the system replays historical data rather than a
physical live bus, represents one bus and route, and cannot infer causal faults
from prediction error alone. GNSS and route values can be missing in individual
readings. The visualisation must therefore display unavailable values honestly
and distinguish a detected anomaly from a diagnosed mechanical problem.
