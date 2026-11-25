import json
import unittest

from src.state_bridge.bridge import MESSAGE_STREAM, handle_message, state_key


class FakeRedis:
    """The handful of Redis commands the bridge uses, kept in memory."""

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.streams: dict[str, list[dict[str, str]]] = {}

    def set(self, key, value):
        self.strings[key] = str(value)

    def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update({field: str(value) for field, value in mapping.items()})

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hincrby(self, key, field, amount):
        bucket = self.hashes.setdefault(key, {})
        bucket[field] = str(int(bucket.get(field, "0")) + amount)

    def xadd(self, key, fields, maxlen=None, approximate=True):
        self.streams.setdefault(key, []).append(dict(fields))


def publish(redis_client: FakeRedis, topic: str, payload) -> str | None:
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return handle_message(redis_client, topic, raw)


def publish_second(redis_client: FakeRedis, sequence: int, **sensors) -> str | None:
    """One replayed second: a message per sensor, then the replay tick (docs/sensor_streams.md)."""
    meta = {"timestamp": f"2021-10-05 07:00:{sequence:02d}", "time_unix": 1633417200 + sequence}
    for name, value in sensors.items():
        publish(redis_client, f"ztbus/B208/sensors/{name}", {**meta, name: value})
    return publish(redis_client, "ztbus/B208/replay/tick", {**meta, "snapshot_sequence": sequence})


class StateBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.redis = FakeRedis()

    def stream_topics(self) -> list[str]:
        return [entry["topic"] for entry in self.redis.streams.get(MESSAGE_STREAM, [])]

    def state(self) -> dict[str, str]:
        return self.redis.hgetall(state_key("B208"))

    def test_merges_sensor_topics_into_bus_state_hash(self) -> None:
        publish(self.redis, "ztbus/B208/sensors/speed_kmh", {"time_unix": 1, "speed_kmh": 30.0})
        publish(self.redis, "ztbus/B208/sensors/passengers", {"time_unix": 1, "passengers": 12})
        publish(self.redis, "ztbus/B208/sensors/altitude_m", {"time_unix": 1, "altitude_m": None})

        self.assertEqual(self.state()["speed_kmh"], "30.0")
        self.assertEqual(self.state()["passengers"], "12")
        self.assertEqual(self.state()["altitude_m"], "")

    def test_state_is_only_derived_on_the_replay_tick(self) -> None:
        publish(self.redis, "ztbus/B208/sensors/speed_kmh", {"speed_kmh": 30.0})
        self.assertNotIn("operational_state", self.state())

        state = publish_second(self.redis, 0, doors_open=0)

        self.assertTrue(state.startswith("Cruising"))
        self.assertEqual(self.state()["operational_state"], state)
        self.assertEqual(self.state()["representation_sequence"], "0")
        self.assertEqual(self.state()["snapshot_sequence"], "0")

    def test_streams_state_only_when_it_changes(self) -> None:
        first = publish_second(self.redis, 0, speed_kmh=30.0, doors_open=0)
        repeated = publish_second(self.redis, 1, speed_kmh=30.0, doors_open=0)
        boarding = publish_second(self.redis, 2, speed_kmh=0.0, doors_open=1)

        self.assertTrue(first.startswith("Cruising"))
        self.assertIsNone(repeated)
        self.assertTrue(boarding.startswith("Boarding"))
        self.assertEqual(self.stream_topics().count("ztbus/B208/state"), 2)

    def test_state_message_follows_the_tick_that_caused_it(self) -> None:
        publish_second(self.redis, 7, power_kw=-25.0)

        self.assertEqual(self.stream_topics()[-2:], ["ztbus/B208/replay/tick", "ztbus/B208/state"])
        payload = json.loads(self.redis.streams[MESSAGE_STREAM][-1]["payload"])
        self.assertTrue(payload["operational_state"].startswith("Regenerating"))
        self.assertEqual(payload["time_unix"], 1633417207)

    def test_route_and_stop_events_reach_the_state_machine(self) -> None:
        state = publish_second(self.redis, 0, doors_open=1, itcs_stopName="Zürich, Bucheggplatz", itcs_busRoute="83")

        self.assertIn("AtStop", state.split(" | "))
        self.assertIn("InService", state.split(" | "))

    def test_non_bus_and_non_json_messages_do_not_touch_the_state(self) -> None:
        self.assertIsNone(publish(self.redis, "ztbus/test", b"Hello, MQTT!"))
        self.assertIsNone(publish(self.redis, "ztbus/B208/sensors/raw", b"not json"))

        self.assertEqual(self.redis.hashes.get(state_key("B208")), None)
        self.assertEqual(self.stream_topics(), ["ztbus/test", "ztbus/B208/sensors/raw"])


if __name__ == "__main__":
    unittest.main()
