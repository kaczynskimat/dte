import json
import unittest

from src.state_bridge import bridge
from src.state_bridge.quality import BOOLEAN_FIELDS, PERIODIC_FIELDS, PHYSICAL_RANGES


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, object]] = {}

    def hset(self, name, key=None, value=None, mapping=None):
        target = self.hashes.setdefault(name, {})
        if mapping is not None:
            target.update(mapping)
        elif key is not None:
            target[key] = value

    def hgetall(self, name):
        return dict(self.hashes.get(name, {}))

    def delete(self, *names):
        for name in names:
            self.hashes.pop(name, None)


class Message:
    def __init__(self, topic: str, payload: dict[str, object]) -> None:
        self.topic = topic
        self.payload = json.dumps(payload).encode("utf-8")


def valid_value(field: str) -> object:
    if field in BOOLEAN_FIELDS:
        return 0
    lower, upper = PHYSICAL_RANGES[field]
    return (lower + upper) / 2


class StateBridgeQualityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.redis = FakeRedis()
        bridge.r = self.redis

    def publish(self, suffix: str, payload: dict[str, object]) -> None:
        bridge.on_message(None, None, Message(f"ztbus/B208/{suffix}", payload))

    def publish_snapshot(
        self,
        omitted: set[str] | None = None,
        overrides: dict[str, object] | None = None,
        tick_time: int = 100,
        sequence: int = 0,
    ) -> None:
        omitted = omitted or set()
        overrides = overrides or {}
        for field in PERIODIC_FIELDS:
            if field not in omitted:
                self.publish(
                    f"sensors/{field}",
                    {
                        "timestamp": "2021-10-05 07:00:00",
                        "time_unix": tick_time,
                        field: overrides.get(field, valid_value(field)),
                    },
                )
        self.publish(
            "replay/tick",
            {
                "timestamp": "2021-10-05 07:00:00",
                "time_unix": tick_time,
                "snapshot_sequence": sequence,
            },
        )

    def test_tick_writes_valid_quality_result_to_bus_state(self) -> None:
        self.publish_snapshot()

        state = self.redis.hgetall("bus:B208:state")
        self.assertEqual(state["quality_status"], "valid")
        self.assertEqual(json.loads(state["quality_flags"]), [])
        self.assertEqual(state["representation_sequence"], 0)
        self.assertEqual(state["state_transition"], "Initialized")
        self.assertEqual(state["state_changed"], 0)

    def test_tick_reports_a_sensor_missing_from_the_snapshot(self) -> None:
        self.publish_snapshot({"latitude"})

        state = self.redis.hgetall("bus:B208:state")
        self.assertEqual(state["quality_status"], "invalid")
        self.assertIn("missing:latitude", json.loads(state["quality_flags"]))

    def test_new_replay_clears_previous_sensor_state(self) -> None:
        self.redis.hset("bus:B208:state", mapping={"speed_kmh": 25})
        self.redis.hset("bus:B208:sensor:timestamps", "speed_kmh", 99)

        self.publish(
            "replay",
            {"replay_status": "running", "source_file": "mission.csv"},
        )

        self.assertNotIn("speed_kmh", self.redis.hgetall("bus:B208:state"))
        self.assertEqual(
            self.redis.hgetall("bus:B208:sensor:timestamps"),
            {},
        )

    def test_bridge_records_departure_transition(self) -> None:
        self.publish_snapshot(overrides={"doors_open": 1, "speed_kmh": 0})
        self.publish_snapshot(
            overrides={"doors_open": 0, "speed_kmh": 10},
            tick_time=101,
            sequence=1,
        )

        state = self.redis.hgetall("bus:B208:state")
        self.assertEqual(
            state["state_transition"],
            "Boarding/Stopped/AtStop -> Cruising/Forward/BetweenStops",
        )
        self.assertEqual(state["state_changed"], 1)


if __name__ == "__main__":
    unittest.main()
