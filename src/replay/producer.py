"""Replay a cleaned ZTBus mission over MQTT as simulated live telemetry."""

import argparse
import json
import math
import time
from pathlib import Path
from typing import Any

import pandas as pd
import paho.mqtt.client as mqtt


PROJECT_ROOT = Path(__file__).parents[2]
DEFAULT_MISSION = PROJECT_ROOT / "data" / "test_mission_2021-10-05.csv"
DEFAULT_BUS_ID = "B208"
DEFAULT_ROUTE = "83"

REQUIRED_COLUMNS = {
    "timestamp",
    "time_unix",
    "latitude",
    "longitude",
    "gnss_altitude",
    "speed_kmh",
    "heading_deg",
    "power_kw",
    "status_gridIsAvailable",
    "status_doorIsOpen",
    "itcs_numberOfPassengers",
    "temp_celsius",
    "odometry_steeringAngle",
    "odometry_articulationAngle",
    "odometry_wheelSpeed_fl",
    "odometry_wheelSpeed_fr",
    "odometry_wheelSpeed_ml",
    "odometry_wheelSpeed_mr",
    "odometry_wheelSpeed_rl",
    "odometry_wheelSpeed_rr",
    "status_haltBrakeIsActive",
    "status_parkBrakeIsActive",
    "brake_bar",
    "traction_tractionForce",
    "itcs_busRoute",
    "itcs_stopName",
}

SENSOR_FIELDS = {
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
    "itcs_busRoute": "itcs_busRoute",
    "itcs_stopName": "itcs_stopName",
}
EVENT_SENSOR_FIELDS = {"itcs_busRoute", "itcs_stopName"}


def _json_value(value: Any) -> Any:
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def _new_client() -> mqtt.Client:
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:
        return mqtt.Client()


def validate_mission(data: pd.DataFrame, expected_route: str) -> None:
    missing = sorted(REQUIRED_COLUMNS - set(data.columns))
    if missing:
        raise ValueError(f"mission is missing required columns: {', '.join(missing)}")
    if data.empty:
        raise ValueError("mission contains no rows")

    timestamps = pd.to_datetime(data["timestamp"])
    if not timestamps.is_monotonic_increasing:
        raise ValueError("mission timestamps are not increasing")
    time_steps = pd.to_numeric(data["time_unix"], errors="coerce").diff().dropna()
    if not time_steps.eq(1).all():
        raise ValueError("mission must be sampled continuously at 1 Hz")

    routes = {
        str(route).strip()
        for route in data["itcs_busRoute"].dropna()
        if str(route).strip() not in {"", "-"}
    }
    if routes != {expected_route}:
        raise ValueError(
            f"expected route {expected_route}, found {sorted(routes) or ['unknown']}"
        )


def build_payloads(row: pd.Series, sequence: int) -> dict[str, dict[str, Any]]:
    meta = {
        "timestamp": str(row["timestamp"]),
        "time_unix": int(row["time_unix"]),
    }
    payloads = {}
    for sensor_name, source_column in SENSOR_FIELDS.items():
        value = row[source_column]
        if sensor_name in EVENT_SENSOR_FIELDS and (
            pd.isna(value) or str(value).strip() in {"", "-"}
        ):
            continue
        payloads[f"sensors/{sensor_name}"] = {
            **meta,
            sensor_name: value,
        }
    payloads["replay/tick"] = {
        **meta,
        "snapshot_sequence": sequence,
    }
    return {
        topic: {key: _json_value(value) for key, value in payload.items()}
        for topic, payload in payloads.items()
    }


def select_mission_period(
    data: pd.DataFrame,
    start_time: str | None,
    end_time: str | None,
    start_row: int,
    max_rows: int | None,
) -> pd.DataFrame:
    timestamps = pd.to_datetime(data["timestamp"])
    selected = data
    if start_time:
        selected = selected.loc[timestamps >= pd.Timestamp(start_time)]
    if end_time:
        selected = selected.loc[timestamps <= pd.Timestamp(end_time)]
    if start_row < 0 or start_row >= len(selected):
        raise ValueError(
            f"start row must be between 0 and {max(0, len(selected) - 1)} "
            "after applying the timestamp range"
        )
    stop_row = len(selected) if max_rows is None else min(
        len(selected),
        start_row + max_rows,
    )
    selected = selected.iloc[start_row:stop_row]
    if selected.empty:
        raise ValueError("the selected replay period contains no rows")
    return selected


def publish_json(
    client: mqtt.Client,
    topic: str,
    payload: dict[str, Any],
    qos: int,
) -> None:
    message = json.dumps(payload, allow_nan=False, separators=(",", ":"))
    result = client.publish(topic, message, qos=qos)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        raise RuntimeError(f"MQTT publish failed for {topic}: {result.rc}")


def replay(
    mission_path: Path,
    broker_host: str,
    broker_port: int,
    bus_id: str,
    expected_route: str,
    speed: float,
    startup_delay: float,
    start_time: str | None,
    end_time: str | None,
    start_row: int,
    max_rows: int | None,
    qos: int,
) -> None:
    if speed <= 0 or not math.isfinite(speed):
        raise ValueError("speed must be a finite number greater than zero")
    if startup_delay < 0 or not math.isfinite(startup_delay):
        raise ValueError("startup delay must be a finite non-negative number")

    data = pd.read_csv(mission_path)
    validate_mission(data, expected_route)
    selected = select_mission_period(
        data,
        start_time=start_time,
        end_time=end_time,
        start_row=start_row,
        max_rows=max_rows,
    )

    client = _new_client()
    client.connect(broker_host, broker_port, 60)
    client.loop_start()
    time.sleep(startup_delay)
    prefix = f"ztbus/{bus_id}"

    publish_json(client, f"{prefix}/replay", {
        "replay_status": "running",
        "source_file": mission_path.name,
        "replay_speed": speed,
        "route": expected_route,
        "replay_start": str(selected["timestamp"].iloc[0]),
        "replay_end": str(selected["timestamp"].iloc[-1]),
    }, qos)

    dataset_start = float(selected["time_unix"].iloc[0])
    real_start = time.monotonic()
    try:
        last_sequence = start_row - 1
        completed = False
        for sequence, (_, row) in enumerate(selected.iterrows(), start=start_row):
            target = real_start + (float(row["time_unix"]) - dataset_start) / speed
            delay = target - time.monotonic()
            if delay > 0:
                time.sleep(delay)

            for suffix, payload in build_payloads(row, sequence).items():
                publish_json(client, f"{prefix}/{suffix}", payload, qos)
            last_sequence = sequence

            if (sequence - start_row) % 50 == 0:
                print(
                    f"Bus {bus_id} | {row['timestamp']} | "
                    f"sample {sequence - start_row + 1}/{len(selected)} | "
                    f"{row['speed_kmh']:.1f} km/h"
                )
        completed = True
    finally:
        status = "completed" if completed else "interrupted"
        publish_json(client, f"{prefix}/replay", {
            "replay_status": status,
            "source_file": mission_path.name,
            "last_sequence": last_sequence,
        }, qos)
        time.sleep(0.1)
        client.loop_stop()
        client.disconnect()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=DEFAULT_MISSION)
    parser.add_argument("--broker-host", default="localhost")
    parser.add_argument("--broker-port", type=int, default=1883)
    parser.add_argument("--bus-id", default=DEFAULT_BUS_ID)
    parser.add_argument("--expected-route", default=DEFAULT_ROUTE)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--startup-delay", type=float, default=1.0)
    parser.add_argument("--start-time")
    parser.add_argument("--end-time")
    parser.add_argument("--start-row", type=int, default=0)
    parser.add_argument("--max-rows", type=int)
    parser.add_argument("--qos", type=int, choices=(0, 1), default=0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    replay(
        mission_path=args.file.resolve(),
        broker_host=args.broker_host,
        broker_port=args.broker_port,
        bus_id=args.bus_id,
        expected_route=args.expected_route,
        speed=args.speed,
        startup_delay=args.startup_delay,
        start_time=args.start_time,
        end_time=args.end_time,
        start_row=args.start_row,
        max_rows=args.max_rows,
        qos=args.qos,
    )


if __name__ == "__main__":
    main()
