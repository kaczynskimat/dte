"""Measure how faithfully MQTT replay snapshots reach the Redis representation."""

from __future__ import annotations

import argparse
import csv
import json
import math
import queue
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from src.state_bridge.quality import PERIODIC_FIELDS


PROJECT_ROOT = Path(__file__).parents[2]
DEFAULT_MISSION = PROJECT_ROOT / "data" / "test_mission_2021-10-05.csv"

SOURCE_COLUMNS = {
    "latitude": "latitude",
    "longitude": "longitude",
    "altitude_m": "gnss_altitude",
    "heading_deg": "heading_deg",
    "speed_kmh": "speed_kmh",
    "power_kw": "power_kw",
    "grid_available": "status_gridIsAvailable",
    "doors_open": "status_doorIsOpen",
    "passengers": "itcs_numberOfPassengers",
    "temp_celsius": "temp_celsius",
    "steering_angle_rad": "odometry_steeringAngle",
    "articulation_angle_rad": "odometry_articulationAngle",
    "wheel_speed_fl": "odometry_wheelSpeed_fl",
    "wheel_speed_fr": "odometry_wheelSpeed_fr",
    "wheel_speed_ml": "odometry_wheelSpeed_ml",
    "wheel_speed_mr": "odometry_wheelSpeed_mr",
    "wheel_speed_rl": "odometry_wheelSpeed_rl",
    "wheel_speed_rr": "odometry_wheelSpeed_rr",
    "halt_brake": "status_haltBrakeIsActive",
    "park_brake": "status_parkBrakeIsActive",
    "brake_bar": "brake_bar",
    "traction_force_n": "traction_tractionForce",
}


def expected_snapshot(row: Mapping[str, Any]) -> dict[str, Any]:
    """Convert one source CSV row to the names used in the Redis hash."""
    return {field: row[SOURCE_COLUMNS[field]] for field in PERIODIC_FIELDS}


def _values_match(expected: Any, actual: Any) -> bool:
    if expected is None or str(expected).strip() == "":
        return actual is None or str(actual).strip() == ""
    try:
        expected_number = float(expected)
        actual_number = float(actual)
    except (TypeError, ValueError):
        return str(expected).strip() == str(actual).strip()
    return math.isclose(expected_number, actual_number, rel_tol=1e-9, abs_tol=1e-9)


def compare_snapshot(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
) -> tuple[list[str], list[str]]:
    """Return missing and mismatched fields for one representation snapshot."""
    missing = [field for field in PERIODIC_FIELDS if field not in actual]
    mismatched = [
        field
        for field in PERIODIC_FIELDS
        if field in actual and not _values_match(expected.get(field), actual[field])
    ]
    return missing, mismatched


@dataclass
class FidelityReport:
    snapshots: int = 0
    missing_fields: int = 0
    mismatched_fields: int = 0
    invalid_snapshots: int = 0
    latencies_ms: list[float] = field(default_factory=list)

    def record(
        self,
        missing: list[str],
        mismatched: list[str],
        latency_ms: float,
        quality_status: str,
    ) -> None:
        self.snapshots += 1
        self.missing_fields += len(missing)
        self.mismatched_fields += len(mismatched)
        self.latencies_ms.append(latency_ms)
        if quality_status == "invalid":
            self.invalid_snapshots += 1

    def as_dict(self) -> dict[str, Any]:
        comparisons = self.snapshots * len(PERIODIC_FIELDS)
        correct = comparisons - self.missing_fields - self.mismatched_fields
        return {
            "snapshots": self.snapshots,
            "field_comparisons": comparisons,
            "matching_fields": correct,
            "missing_fields": self.missing_fields,
            "mismatched_fields": self.mismatched_fields,
            "match_rate_percent": round(100.0 * correct / comparisons, 4)
            if comparisons
            else 0.0,
            "invalid_snapshots": self.invalid_snapshots,
            "latency_mean_ms": round(sum(self.latencies_ms) / len(self.latencies_ms), 3)
            if self.latencies_ms
            else None,
            "latency_max_ms": round(max(self.latencies_ms), 3)
            if self.latencies_ms
            else None,
        }


def load_rows_by_timestamp(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    return {row["timestamp"]: row for row in rows}


def measure_live_fidelity(
    mission_path: Path,
    broker_host: str,
    broker_port: int,
    redis_host: str,
    redis_port: int,
    bus_id: str,
    samples: int,
    timeout: float,
) -> FidelityReport:
    """Observe replay ticks and compare the corresponding completed Redis states."""
    import paho.mqtt.client as mqtt
    import redis

    rows = load_rows_by_timestamp(mission_path)
    ticks: queue.Queue[tuple[dict[str, Any], float]] = queue.Queue()
    redis_client = redis.Redis(
        host=redis_host,
        port=redis_port,
        decode_responses=True,
        socket_connect_timeout=timeout,
    )
    state_key = f"bus:{bus_id}:state"

    def on_message(client, userdata, message):
        try:
            ticks.put((json.loads(message.payload.decode("utf-8")), time.monotonic()))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:
        client = mqtt.Client()
    client.on_message = on_message
    client.connect(broker_host, broker_port, 60)
    client.subscribe(f"ztbus/{bus_id}/replay/tick")
    client.loop_start()

    report = FidelityReport()
    try:
        while report.snapshots < samples:
            try:
                tick, received_at = ticks.get(timeout=timeout)
            except queue.Empty as error:
                raise TimeoutError(
                    f"no replay tick received within {timeout}s; start the observer "
                    "before the replay producer"
                ) from error
            timestamp = str(tick["timestamp"])
            if timestamp not in rows:
                raise ValueError(f"tick timestamp {timestamp} is absent from {mission_path}")

            deadline = time.monotonic() + timeout
            state: dict[str, str] = {}
            while time.monotonic() < deadline:
                state = redis_client.hgetall(state_key)
                if (
                    state.get("representation_timestamp") == timestamp
                    and state.get("representation_sequence")
                    == str(tick["snapshot_sequence"])
                ):
                    break
                time.sleep(0.001)
            else:
                raise TimeoutError(
                    f"Redis did not represent tick {tick['snapshot_sequence']} within {timeout}s"
                )

            missing, mismatched = compare_snapshot(
                expected_snapshot(rows[timestamp]),
                state,
            )
            report.record(
                missing,
                mismatched,
                (time.monotonic() - received_at) * 1000.0,
                state.get("quality_status", ""),
            )
    finally:
        client.loop_stop()
        client.disconnect()
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=DEFAULT_MISSION)
    parser.add_argument("--broker-host", default="localhost")
    parser.add_argument("--broker-port", type=int, default=1883)
    parser.add_argument("--redis-host", default="localhost")
    parser.add_argument("--redis-port", type=int, default=6379)
    parser.add_argument("--bus-id", default="B208")
    parser.add_argument("--samples", type=int, default=60)
    parser.add_argument("--timeout", type=float, default=5.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = measure_live_fidelity(
        mission_path=args.file.resolve(),
        broker_host=args.broker_host,
        broker_port=args.broker_port,
        redis_host=args.redis_host,
        redis_port=args.redis_port,
        bus_id=args.bus_id,
        samples=args.samples,
        timeout=args.timeout,
    )
    result = report.as_dict()
    print(json.dumps(result, indent=2))
    if result["missing_fields"] or result["mismatched_fields"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
