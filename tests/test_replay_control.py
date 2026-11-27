import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import paho.mqtt.client as mqtt

from src.replay import producer


TEST_MISSION = Path(__file__).parents[1] / "data" / "test_mission_2021-10-05.csv"


class FakeClient:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict]] = []
        self.tick_times: list[float] = []

    def connect(self, *args) -> None:
        pass

    def loop_start(self) -> None:
        pass

    def loop_stop(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def publish(self, topic, message, qos=0):
        self.messages.append((topic, json.loads(message)))
        if topic.endswith("/replay/tick"):
            self.tick_times.append(time.monotonic())
        return SimpleNamespace(rc=mqtt.MQTT_ERR_SUCCESS)

    def ticks(self) -> list[int]:
        return [payload["snapshot_sequence"] for topic, payload in self.messages if topic.endswith("/replay/tick")]


class FakeControl:
    """Replays a scripted list of (status, speed) answers, then keeps the last one."""

    def __init__(self, answers: list[tuple[str, float | None]]) -> None:
        self.answers = answers
        self.updates: dict = {}

    def read(self):
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]

    def update(self, **fields) -> None:
        self.updates.update(fields)


def run_replay(client: FakeClient, rows: int, speed: float, control=None) -> None:
    with mock.patch.object(producer, "_new_client", return_value=client):
        producer.replay(
            mission_path=TEST_MISSION,
            broker_host="localhost",
            broker_port=1883,
            bus_id="B208",
            expected_route="83",
            speed=speed,
            startup_delay=0,
            start_time="2021-10-05 07:00:00",
            end_time=None,
            start_row=0,
            max_rows=rows,
            qos=0,
            control=control,
        )


class ReplayControlTests(unittest.TestCase):
    def test_without_control_every_row_is_published_at_the_given_speed(self) -> None:
        client = FakeClient()
        run_replay(client, rows=3, speed=10)

        self.assertEqual(client.ticks(), [0, 1, 2])
        self.assertEqual(client.messages[-1][1]["replay_status"], "completed")

    def test_stop_from_the_dashboard_ends_the_replay(self) -> None:
        client = FakeClient()
        control = FakeControl([("running", 10.0), ("stopped", 10.0)])
        run_replay(client, rows=50, speed=10, control=control)

        self.assertLess(len(client.ticks()), 50)
        self.assertEqual(control.updates["status"], "stopped")
        self.assertEqual(client.messages[-1][1]["replay_status"], "stopped")

    def test_speed_change_applies_to_the_running_replay(self) -> None:
        client = FakeClient()
        control = FakeControl([("running", 10.0)])
        # 4 rows are 3 s of data: about 0.3 s at the 10x the dashboard asks for, instead of 3 s at 1x.
        run_replay(client, rows=4, speed=1, control=control)

        self.assertEqual(client.ticks(), [0, 1, 2, 3])
        self.assertLess(client.tick_times[-1] - client.tick_times[0], 1.5)
        self.assertEqual(control.updates["index"], 4)
        self.assertEqual(control.updates["status"], "completed")


if __name__ == "__main__":
    unittest.main()
