"""Replay a cleaned ZTBus mission over MQTT as simulated live telemetry.

Optionally (``--control-redis-host``) the replay is also driven through the Redis hash
``ztbus:replay:control``, which is how the dashboard's Replay page pauses, resumes, stops and changes
the speed of a running replay. Without it the producer only needs the MQTT broker.
"""

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
CONTROL_KEY = "ztbus:replay:control"
MIN_SPEED, MAX_SPEED = 0.25, 10.0
CONTROL_POLL_SECONDS = 0.2

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


class ReplayControl:
    """Dashboard control of a running replay through the Redis hash ``ztbus:replay:control``.

    The dashboard writes ``status`` (running / paused / stopped) and ``speed``; the producer writes
    its progress (``index`` / ``total``) and how the replay ended.
    """

    def __init__(self, host: str, port: int) -> None:
        import redis  # only needed for a dashboard-controlled replay

        self.client = redis.Redis(host=host, port=port, decode_responses=True, socket_connect_timeout=5)
        self.client.ping()

    def read(self) -> tuple[str, float | None]:
        control = self.client.hgetall(CONTROL_KEY)
        try:
            speed = min(MAX_SPEED, max(MIN_SPEED, float(control["speed"])))
        except (KeyError, TypeError, ValueError):
            speed = None
        return control.get("status", "running"), speed

    def update(self, **fields: Any) -> None:
        self.client.hset(CONTROL_KEY, mapping=fields)


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
    control: ReplayControl | None = None,
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

    if control:
        control.update(status="running", speed=speed, index=0, total=len(selected))

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

    # Replay clock: dataset time advances by (real time elapsed) x speed. With a constant speed this
    # is exactly t0 + (T - T0) / speed; pauses freeze it and speed changes apply from that moment on.
    replay_time = float(selected["time_unix"].iloc[0])
    last_tick = time.monotonic()
    last_poll = float("-inf")
    paused = False
    last_sequence = start_row - 1
    completed = False
    stopped = False
    try:
        for position, (sequence, (_, row)) in enumerate(
            enumerate(selected.iterrows(), start=start_row), start=1
        ):
            row_time = float(row["time_unix"])
            while True:
                now = time.monotonic()
                if control and now - last_poll >= CONTROL_POLL_SECONDS:
                    last_poll = now
                    requested, requested_speed = control.read()
                    if requested not in {"running", "paused"}:
                        stopped = True
                        break
                    paused = requested == "paused"
                    if requested_speed is not None:
                        speed = requested_speed
                if not paused:
                    replay_time += (now - last_tick) * speed
                last_tick = now
                remaining = (row_time - replay_time) / speed
                if not paused and remaining <= 0:
                    break
                wait = CONTROL_POLL_SECONDS / 2 if paused else remaining
                time.sleep(min(wait, CONTROL_POLL_SECONDS / 2) if control else wait)
            if stopped:
                break

            for suffix, payload in build_payloads(row, sequence).items():
                publish_json(client, f"{prefix}/{suffix}", payload, qos)
            last_sequence = sequence
            if control:
                control.update(index=position)

            if (sequence - start_row) % 50 == 0:
                print(
                    f"Bus {bus_id} | {row['timestamp']} | "
                    f"sample {sequence - start_row + 1}/{len(selected)} | "
                    f"{row['speed_kmh']:.1f} km/h"
                )
        completed = not stopped
    except Exception as error:
        if control:
            control.update(status="error", error=str(error)[:500])
        raise
    finally:
        status = "completed" if completed else ("stopped" if stopped else "interrupted")
        if control and status != "interrupted":
            control.update(status=status)
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
    parser.add_argument(
        "--control-redis-host",
        help="Follow pause/resume/stop and speed changes from the Redis hash "
        f"{CONTROL_KEY} (used by the dashboard)",
    )
    parser.add_argument("--control-redis-port", type=int, default=6379)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    control = (
        ReplayControl(args.control_redis_host, args.control_redis_port)
        if args.control_redis_host
        else None
    )
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
        control=control,
    )


if __name__ == "__main__":
    main()
