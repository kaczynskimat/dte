"""MQTT -> Redis state bridge for the ZTBus digital twin.

Every message on ``ztbus/<bus>/...`` (one topic per sensor, see docs/sensor_streams.md) is:

* merged into the hash ``bus:<bus>:state``, the current representation of the bus,
* kept as the latest payload of its topic (``ztbus:replay:topic_latest:<topic>``),
* appended to the capped stream ``ztbus:replay:messages`` the dashboard follows.

``ztbus/<bus>/replay/tick`` closes one complete one-second situation. On each tick the rule-based
state machine (``src.state_machine.infer_state``) runs on the full state and replaces
``operational_state``; when that state changes it is also appended to the stream as a
``ztbus/<bus>/state`` message, so the dashboard sees transitions in order with the telemetry.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt
import redis

PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:  # also runnable as `python src/state_bridge/bridge.py`
    sys.path.insert(0, str(PROJECT_DIR))

from src.state_machine import infer_state  # noqa: E402


SUBSCRIPTION = "ztbus/#"
MESSAGE_STREAM = "ztbus:replay:messages"
# ~25 messages per replayed second: about 20 s of history at 10x, enough for the dashboard's 250 ms polls.
MESSAGE_LIMIT = 5000
TOPIC_LATEST_PREFIX = "ztbus:replay:topic_latest:"
TOPIC_COUNTS_KEY = "ztbus:replay:topic_counts"
BRIDGE_STATUS_KEY = "ztbus:replay:bridge_status"
TICK_SUFFIX = "/replay/tick"


def state_key(bus_id: str) -> str:
    return f"bus:{bus_id}:state"


def _flatten(data: dict[str, Any]) -> dict[str, Any]:
    """Redis hashes only hold scalars: nested objects become JSON, null becomes ""."""
    clean: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, (dict, list)):
            clean[key] = json.dumps(value, separators=(",", ":"))
        elif value is None:
            clean[key] = ""
        elif isinstance(value, bool):
            clean[key] = int(value)
        else:
            clean[key] = value
    return clean


def _append_to_stream(r: redis.Redis, topic: str, data: Any) -> None:
    r.xadd(
        MESSAGE_STREAM,
        {
            "topic": topic,
            "payload": json.dumps(data, separators=(",", ":")),
            "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        maxlen=MESSAGE_LIMIT,
        approximate=True,
    )


def apply_tick(r: redis.Redis, bus_id: str, tick: dict[str, Any]) -> str | None:
    """Derive the operational state of the complete situation; returns it only when it changed."""
    key = state_key(bus_id)
    state = r.hgetall(key)
    operational_state = infer_state(state)
    previous = state.get("operational_state")
    r.hset(key, mapping={
        "operational_state": operational_state,
        "representation_timestamp": tick.get("timestamp", ""),
        "representation_sequence": tick.get("snapshot_sequence", ""),
    })
    if operational_state == previous:
        return None
    _append_to_stream(r, f"ztbus/{bus_id}/state", {
        "timestamp": tick.get("timestamp"),
        "time_unix": tick.get("time_unix"),
        "operational_state": operational_state,
    })
    return operational_state


def handle_message(r: redis.Redis, topic: str, raw_payload: bytes) -> str | None:
    """Store one MQTT message in Redis; returns the operational state when a tick changed it."""
    payload = raw_payload.decode("utf-8")
    try:
        data: Any = json.loads(payload)
        is_telemetry = isinstance(data, dict)
    except json.JSONDecodeError:
        data = {"value": payload}  # still shown on the Replay page, but never merged into the state
        is_telemetry = False

    # Every message is visible on the Replay page, even ones that do not describe a bus.
    r.set(f"{TOPIC_LATEST_PREFIX}{topic}", json.dumps(data, separators=(",", ":")))
    _append_to_stream(r, topic, data)
    r.hincrby(TOPIC_COUNTS_KEY, topic, 1)

    parts = topic.split("/")
    if len(parts) < 3 or not is_telemetry:
        # Only ztbus/<bus>/... objects describe the bus (ztbus/test, plain strings... do not).
        return None

    bus_id = parts[1]
    r.hset(state_key(bus_id), mapping=_flatten(data))
    if topic.endswith(TICK_SUFFIX):
        return apply_tick(r, bus_id, data)
    return None


def _redis_from_env() -> redis.Redis:
    return redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        decode_responses=True,  # strings instead of raw bytes
    )


def new_mqtt_client() -> mqtt.Client:
    """paho-mqtt 2.x wants the callback API version; 1.x does not know the argument."""
    api_version = getattr(mqtt, "CallbackAPIVersion", None)
    return mqtt.Client(api_version.VERSION2) if api_version else mqtt.Client()


def main() -> None:
    r = _redis_from_env()

    def on_message(client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
        try:
            handle_message(r, msg.topic, msg.payload)
        except (UnicodeDecodeError, redis.RedisError, ValueError, TypeError) as error:
            # A bad message or a Redis hiccup must not stop the MQTT loop.
            print(f"Ignoring message on {msg.topic}: {error}", flush=True)

    def on_connect(client: mqtt.Client, userdata: Any, flags: Any, reason_code: Any, properties: Any = None) -> None:
        if reason_code == 0:
            client.subscribe(SUBSCRIPTION)
            print(f"Connected to MQTT broker and subscribed to {SUBSCRIPTION}", flush=True)
            try:
                r.set(BRIDGE_STATUS_KEY, "ready")
            except redis.RedisError as error:
                print(f"Could not mark the bridge as ready in Redis: {error}", flush=True)
        else:
            print(f"MQTT connection failed: {reason_code}", flush=True)

    client = new_mqtt_client()
    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect_async(os.getenv("MQTT_HOST", "localhost"), int(os.getenv("MQTT_PORT", "1883")), 60)
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    main()
