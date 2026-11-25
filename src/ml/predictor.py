"""Run the trained power model continuously against bus snapshots in Redis."""

import argparse
import math
import time
from collections import deque
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import redis

from src.ml.features import (
    CANONICAL_INPUT_COLUMNS,
    FORECAST_SECONDS,
    HISTORY_SECONDS,
    engineer_features,
)


PROJECT_ROOT = Path(__file__).parents[2]
DEFAULT_MODEL = PROJECT_ROOT / "src" / "ml" / "models" / "model.pkl"
ML_STATE_FIELDS = (
    "ml_status",
    "ml_timestamp",
    "ml_samples_collected",
    "ml_samples_required",
    "ml_prediction_timestamp",
    "ml_source_sequence",
    "ml_current_power_kw",
    "ml_predicted_mean_power_kw_next_10s",
    "ml_predicted_energy_kwh_next_10s",
    "ml_history_seconds",
    "ml_forecast_horizon_seconds",
    "ml_anomaly_threshold_kw",
    "ml_evaluated_prediction_timestamp",
    "ml_evaluated_prediction_kw",
    "ml_actual_mean_power_kw_next_10s",
    "ml_prediction_error_kw",
    "ml_absolute_error_kw",
    "ml_is_anomaly",
)


def parse_snapshot(state: dict[str, str]) -> tuple[int, dict[str, Any]]:
    if "snapshot_sequence" not in state:
        raise ValueError("waiting for a complete producer snapshot")

    missing = sorted(set(CANONICAL_INPUT_COLUMNS) - set(state))
    if missing:
        raise ValueError(f"snapshot is missing fields: {', '.join(missing)}")

    snapshot: dict[str, Any] = {"timestamp": state["timestamp"]}
    for field in CANONICAL_INPUT_COLUMNS:
        if field == "timestamp":
            continue
        raw_value = state[field]
        snapshot[field] = float(raw_value) if raw_value not in {"", None} else np.nan
    return int(state["snapshot_sequence"]), snapshot


class PredictionEngine:
    """Maintain the rolling telemetry window and evaluate delayed forecasts."""

    def __init__(self, artifact: dict[str, Any]) -> None:
        self.model = artifact["model"]
        self.feature_columns = artifact["feature_columns"]
        self.history_seconds = int(artifact["history_seconds"])
        self.forecast_seconds = int(artifact["forecast_seconds"])
        self.anomaly_threshold_kw = float(artifact["anomaly_threshold_kw"])
        if self.history_seconds != HISTORY_SECONDS:
            raise ValueError("model history does not match feature engineering")
        if self.forecast_seconds != FORECAST_SECONDS:
            raise ValueError("model forecast horizon does not match feature engineering")

        self.history: deque[dict[str, Any]] = deque(maxlen=self.history_seconds)
        self.power_history: deque[tuple[int, float]] = deque(
            maxlen=self.history_seconds + self.forecast_seconds + 1
        )
        self.pending: deque[dict[str, Any]] = deque()
        self.last_sequence: int | None = None
        self.last_timestamp: pd.Timestamp | None = None
        self.latest_evaluation: dict[str, Any] | None = None
        self.was_reset = False

    def reset(self) -> None:
        self.history.clear()
        self.power_history.clear()
        self.pending.clear()
        self.latest_evaluation = None

    @property
    def samples_collected(self) -> int:
        return len(self.history)

    def _evaluate_due_forecasts(self, sequence: int) -> None:
        powers = dict(self.power_history)
        while self.pending and sequence >= (
            self.pending[0]["sequence"] + self.forecast_seconds
        ):
            forecast = self.pending.popleft()
            future_sequences = range(
                forecast["sequence"] + 1,
                forecast["sequence"] + self.forecast_seconds + 1,
            )
            if not all(item in powers for item in future_sequences):
                continue
            actual = float(np.mean([powers[item] for item in future_sequences]))
            error = actual - forecast["prediction_kw"]
            self.latest_evaluation = {
                "evaluated_prediction_timestamp": forecast["timestamp"],
                "evaluated_prediction_kw": forecast["prediction_kw"],
                "actual_mean_power_kw_next_10s": actual,
                "prediction_error_kw": error,
                "absolute_error_kw": abs(error),
                "is_anomaly": int(abs(error) > self.anomaly_threshold_kw),
            }

    def add_snapshot(self, sequence: int, snapshot: dict[str, Any]) -> dict[str, Any] | None:
        timestamp = pd.Timestamp(snapshot["timestamp"])
        self.was_reset = False
        if self.last_sequence is not None:
            continuous_sequence = sequence == self.last_sequence + 1
            continuous_time = timestamp - self.last_timestamp == pd.Timedelta(seconds=1)
            if not continuous_sequence or not continuous_time:
                self.reset()
                self.was_reset = True

        self.last_sequence = sequence
        self.last_timestamp = timestamp
        self.history.append(snapshot)
        self.power_history.append((sequence, float(snapshot["power_kw"])))
        self._evaluate_due_forecasts(sequence)

        if len(self.history) < self.history_seconds:
            return None

        history_frame = pd.DataFrame(self.history)
        features = engineer_features(history_frame).iloc[[-1]]
        features = features[self.feature_columns]
        prediction_kw = float(self.model.predict(features)[0])
        self.pending.append({
            "sequence": sequence,
            "timestamp": str(snapshot["timestamp"]),
            "prediction_kw": prediction_kw,
        })

        payload: dict[str, Any] = {
            "status": "ready",
            "prediction_timestamp": str(snapshot["timestamp"]),
            "source_sequence": sequence,
            "current_power_kw": float(snapshot["power_kw"]),
            "predicted_mean_power_kw_next_10s": prediction_kw,
            "predicted_energy_kwh_next_10s": (
                prediction_kw * self.forecast_seconds / 3600
            ),
            "history_seconds": self.history_seconds,
            "forecast_horizon_seconds": self.forecast_seconds,
            "anomaly_threshold_kw": self.anomaly_threshold_kw,
        }
        if self.latest_evaluation:
            payload.update(self.latest_evaluation)
        return payload


def _redis_mapping(payload: dict[str, Any]) -> dict[str, str | int | float]:
    result: dict[str, str | int | float] = {}
    for key, value in payload.items():
        if isinstance(value, (bool, np.bool_)):
            result[key] = int(value)
        elif isinstance(value, (int, np.integer)):
            result[key] = int(value)
        elif isinstance(value, (float, np.floating)):
            numeric = float(value)
            result[key] = numeric if math.isfinite(numeric) else ""
        else:
            result[key] = str(value)
    return result


def _replace_hash(
    client: redis.Redis,
    key: str,
    mapping: dict[str, str | int | float],
) -> None:
    with client.pipeline(transaction=True) as pipeline:
        pipeline.delete(key)
        pipeline.hset(key, mapping=mapping)
        pipeline.execute()


def _update_twin_ml_state(
    client: redis.Redis,
    state_key: str,
    payload: dict[str, str | int | float],
    clear_previous: bool,
) -> None:
    ml_payload = {f"ml_{key}": value for key, value in payload.items()}
    with client.pipeline(transaction=True) as pipeline:
        if clear_previous:
            pipeline.hdel(state_key, *ML_STATE_FIELDS)
        pipeline.hset(state_key, mapping=ml_payload)
        pipeline.execute()


def run_worker(
    model_path: Path,
    redis_host: str,
    redis_port: int,
    bus_id: str,
    poll_interval: float,
) -> None:
    artifact = joblib.load(model_path)
    engine = PredictionEngine(artifact)
    client = redis.Redis(
        host=redis_host,
        port=redis_port,
        decode_responses=True,
        socket_connect_timeout=5,
    )
    state_key = f"bus:{bus_id}:state"
    prediction_key = f"bus:{bus_id}:prediction"
    last_sequence: int | None = None

    while True:
        try:
            state = client.hgetall(state_key)
            if not state or "snapshot_sequence" not in state:
                time.sleep(poll_interval)
                continue

            sequence = int(state["snapshot_sequence"])
            if sequence == last_sequence:
                time.sleep(poll_interval)
                continue

            sequence, snapshot = parse_snapshot(state)
            prediction = engine.add_snapshot(sequence, snapshot)
            last_sequence = sequence

            if engine.was_reset:
                client.delete(prediction_key)

            if prediction is None:
                warmup = {
                    "status": "warming_up",
                    "timestamp": str(snapshot["timestamp"]),
                    "samples_collected": engine.samples_collected,
                    "samples_required": engine.history_seconds,
                }
                _replace_hash(client, prediction_key, warmup)
                _update_twin_ml_state(
                    client,
                    state_key,
                    warmup,
                    clear_previous=True,
                )
            else:
                redis_prediction = _redis_mapping(prediction)
                _replace_hash(
                    client,
                    prediction_key,
                    redis_prediction,
                )
                _update_twin_ml_state(
                    client,
                    state_key,
                    redis_prediction,
                    clear_previous=False,
                )
                print(
                    f"Bus {bus_id} | {prediction['prediction_timestamp']} | "
                    f"next 10 s: {prediction['predicted_mean_power_kw_next_10s']:.2f} kW"
                )
        except (redis.RedisError, OSError) as error:
            print(f"Redis unavailable: {error}; retrying")
            time.sleep(max(1.0, poll_interval))
        except ValueError as error:
            print(f"Ignoring incomplete snapshot: {error}")
            time.sleep(poll_interval)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--redis-host", default="localhost")
    parser.add_argument("--redis-port", type=int, default=6379)
    parser.add_argument("--bus-id", default="B208")
    parser.add_argument("--poll-interval", type=float, default=0.05)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.poll_interval <= 0:
        raise ValueError("poll interval must be greater than zero")
    run_worker(
        model_path=args.model.resolve(),
        redis_host=args.redis_host,
        redis_port=args.redis_port,
        bus_id=args.bus_id,
        poll_interval=args.poll_interval,
    )


if __name__ == "__main__":
    main()
