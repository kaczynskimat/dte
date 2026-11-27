import json
import unittest

from src.state_bridge.bridge import sensor_times_key, state_key
from src.state_bridge.quality import BOOLEAN_FIELDS, PERIODIC_FIELDS, PHYSICAL_RANGES
from tests.test_state_bridge import FakeRedis, publish


def valid_value(field: str) -> object:
    if field in BOOLEAN_FIELDS:
        return 0
    lower, upper = PHYSICAL_RANGES[field]
    return (lower + upper) / 2


class StateBridgeQualityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.redis = FakeRedis()

    def publish(self, suffix: str, payload: dict[str, object]) -> None:
        publish(self.redis, f"ztbus/B208/{suffix}", payload)

    def publish_snapshot(self, omitted: set[str] | None = None, time_unix: int = 100) -> None:
        omitted = omitted or set()
        meta = {"timestamp": "2021-10-05 07:00:00", "time_unix": time_unix}
        for field in PERIODIC_FIELDS:
            if field not in omitted:
                self.publish(f"sensors/{field}", {**meta, field: valid_value(field)})
        self.publish("replay/tick", {**meta, "snapshot_sequence": 0})

    def state(self) -> dict[str, str]:
        return self.redis.hgetall(state_key("B208"))

    def test_tick_writes_valid_quality_result_to_bus_state(self) -> None:
        self.publish_snapshot()

        state = self.state()
        self.assertEqual(state["quality_status"], "valid")
        self.assertEqual(json.loads(state["quality_flags"]), [])
        self.assertEqual(state["representation_sequence"], "0")

    def test_tick_reports_a_sensor_missing_from_the_snapshot(self) -> None:
        self.publish_snapshot({"latitude"})

        state = self.state()
        self.assertEqual(state["quality_status"], "invalid")
        self.assertIn("missing:latitude", json.loads(state["quality_flags"]))

    def test_reading_kept_from_an_earlier_second_is_stale(self) -> None:
        self.publish_snapshot(time_unix=100)
        self.publish_snapshot({"speed_kmh"}, time_unix=101)

        state = self.state()
        self.assertEqual(state["quality_status"], "warning")
        self.assertEqual(json.loads(state["quality_flags"]), ["stale:speed_kmh"])

    def test_new_replay_clears_previous_sensor_state(self) -> None:
        self.redis.hset(state_key("B208"), mapping={"speed_kmh": 25})
        self.redis.hset(sensor_times_key("B208"), mapping={"speed_kmh": 99})

        self.publish("replay", {"replay_status": "running", "source_file": "mission.csv"})

        self.assertNotIn("speed_kmh", self.state())
        self.assertEqual(self.state()["replay_status"], "running")
        self.assertEqual(self.redis.hgetall(sensor_times_key("B208")), {})


if __name__ == "__main__":
    unittest.main()
