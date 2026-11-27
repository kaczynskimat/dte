# Use scenario: short-term energy prediction

## Purpose

This scenario shows how the digital twin helps an energy analyst move from
observing the bus's current electrical power to anticipating its near-future
power demand. The forecast enriches the current bus representation with a value
that cannot be obtained directly from a sensor.

## Primary user

The primary user is an **energy analyst** responsible for understanding power
demand during bus operation and identifying behaviour that differs from the
energy patterns learned from earlier missions.

## Context and preconditions

- The selected mission belongs to bus B208 on route 83.
- The historical replay publishes one complete telemetry snapshot per source
  second through MQTT.
- The MQTT-to-Redis bridge maintains the latest complete bus state.
- The XGBoost model has already been trained offline using missions that occur
  before the replayed mission.
- The prediction worker is connected to Redis and has access to the trained
  model artifact.

## Main flow

1. The user starts or observes a historical mission replay.
2. The prediction worker reads each completed bus snapshot from Redis.
3. During the first 59 consecutive snapshots, the worker reports
   `warming_up`, because the model requires 60 seconds of recent telemetry.
4. On the 60th consecutive snapshot, the worker engineers the model features
   using only past and current information.
5. The XGBoost model predicts the mean electrical power demand over the next
   10 seconds.
6. The worker converts the predicted power into predicted energy for the same
   interval and writes both results to `bus:B208:prediction` and
   `bus:B208:state`.
7. The dashboard displays the current power and the near-future forecast to the
   energy analyst.
8. After the following 10 telemetry snapshots have arrived, the worker computes
   the actual mean power for that period and compares it with the prediction.
9. The worker stores the signed error, absolute error and anomaly flag. A
   prediction is anomalous when its absolute error is greater than the threshold
   learned from validation residuals.

## Alternative and invalid flows

- If a replay sequence or timestamp is discontinuous, the worker clears its
  history and returns to `warming_up`. This prevents a forecast from combining
  unrelated time periods.
- If a snapshot is incomplete, the worker ignores it instead of creating a
  prediction from missing required fields.
- If Redis is temporarily unavailable, the worker keeps running and retries the
  connection.
- During warm-up, the absence of a prediction is reported explicitly; it is not
  presented as a zero-power forecast.

## Expected outcome and user value

The analyst can distinguish among:

- power currently measured by the bus;
- mean power predicted for the next 10 seconds;
- the value actually observed after those 10 seconds;
- ordinary prediction error and anomalous prediction error.

This makes the digital twin predictive rather than a simple dashboard of
historical sensor readings. An anomaly indicates that the observed energy
behaviour differs from the validated model; it does not by itself diagnose a
mechanical fault.

## Requirements

| ID | Requirement | Verification |
|---|---|---|
| EN-01 | The system shall use 60 consecutive one-second snapshots before producing a forecast. | `test_prediction_starts_after_sixty_consecutive_samples` |
| EN-02 | The system shall predict mean power demand over the following 10 seconds. | Model metadata and `test_artifact_describes_future_prediction` |
| EN-03 | The prediction shall use only information available at or before the prediction timestamp. | Feature/target construction and `test_target_uses_only_the_following_ten_seconds` |
| EN-04 | The system shall expose current power, predicted power and predicted energy in Redis. | Prediction worker output fields and `test_prediction_output_is_complete` |
| EN-05 | The system shall evaluate a forecast after the corresponding 10 future snapshots arrive. | `test_prediction_is_evaluated_after_ten_future_samples` |
| EN-06 | The system shall flag an anomaly when absolute prediction error exceeds the validation threshold. | Prediction engine evaluation and model artifact metadata |
| EN-07 | The system shall reset the prediction history after a sequence or timestamp gap. | `test_gap_resets_the_warmup_window` |
| EN-08 | The dashboard shall distinguish warm-up, ready prediction and anomaly states. | Dashboard forecast indicator and replay page |

## Validation evidence

The model was trained on the 25 and 29 September 2021 missions, selected using
the 4 October mission, and tested on the later 5 October mission. On that held-
out mission, it achieved MAE 16.77 kW, RMSE 24.35 kW and R² 0.800. The anomaly
threshold is calculated from the 99th percentile of absolute validation
residuals rather than from the final test mission.

## Limitations

- The model is specific to the available B208 route-83 data and has not been
  validated on another bus or route.
- A prediction anomaly identifies unusual model error, not its physical cause.
- The system uses historical replay rather than telemetry from a currently
  operating bus.
- The model is retrained offline; it does not update itself during replay.
