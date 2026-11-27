import json
import unittest
from pathlib import Path

from src.state_machine import infer_state, infer_transition


MOCK_STATE_PATH = Path(__file__).parents[1] / "data" / "mock_state.json"


def state_parts(telemetry: dict, previous_state: str | None = None) -> list[str]:
    return infer_state(telemetry, previous_state).split(" | ")


class InferStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with MOCK_STATE_PATH.open(encoding="utf-8") as mock_file:
            cls.telemetry = json.load(mock_file)

    def test_returns_string(self) -> None:
        self.assertIsInstance(infer_state(self.telemetry[0]), str)

    def test_primary_states_from_mock_data(self) -> None:
        self.assertEqual(state_parts(self.telemetry[0])[0], "Boarding")
        self.assertEqual(state_parts(self.telemetry[5])[0], "Idling")
        self.assertEqual(state_parts(self.telemetry[7])[0], "Cruising")
        self.assertEqual(state_parts(self.telemetry[52])[0], "Regenerating")

    def test_boarding_state_dimensions(self) -> None:
        parts = state_parts(self.telemetry[0])

        self.assertEqual(parts[0], "Boarding")
        self.assertEqual(parts[1], "Stopped")
        self.assertEqual(parts[2], "Stationary")
        self.assertEqual(parts[3], "HaltBrake")
        self.assertEqual(parts[5], "Open")

    def test_turning_left_and_right(self) -> None:
        left = state_parts({"speed_kmh": 20, "steering_angle_rad": 0.1})
        right = state_parts({"speed_kmh": 20, "steering_angle_rad": -0.1})

        self.assertEqual(left[2], "TurningLeft")
        self.assertEqual(right[2], "TurningRight")

    def test_reverse_motion_is_supported(self) -> None:
        self.assertEqual(state_parts({"speed_kmh": -5})[1], "Reverse")

    def test_retained_stop_name_does_not_keep_bus_at_stop(self) -> None:
        parts = state_parts({
            "speed_kmh": 20,
            "doors_open": 0,
            "itcs_stopName": "Zürich, Herdernstrasse",
        })

        self.assertEqual(parts[7], "BetweenStops")

    def test_stop_state_lasts_until_bus_departs(self) -> None:
        at_stop = infer_state({"speed_kmh": 0, "doors_open": 1})
        doors_closed = state_parts(
            {"speed_kmh": 0, "doors_open": 0},
            previous_state=at_stop,
        )
        departed = state_parts(
            {"speed_kmh": 10, "doors_open": 0},
            previous_state=at_stop,
        )

        self.assertEqual(doors_closed[7], "AtStop")
        self.assertEqual(departed[7], "BetweenStops")

    def test_meaningful_transition_is_described(self) -> None:
        previous = infer_state({"speed_kmh": 0, "doors_open": 1})
        current = infer_state(
            {"speed_kmh": 10, "doors_open": 0},
            previous_state=previous,
        )

        self.assertEqual(
            infer_transition(previous, current),
            "Boarding/Stopped/AtStop -> Cruising/Forward/BetweenStops",
        )
        self.assertEqual(infer_transition(None, current), "Initialized")
        self.assertEqual(infer_transition(current, current), "Unchanged")

    def test_raw_csv_and_mqtt_field_names_match(self) -> None:
        csv_state = infer_state({
            "odometry_vehicleSpeed": 5,
            "odometry_steeringAngle": 0.1,
            "electric_powerDemand": -10000,
            "traction_brakePressure": 20000,
            "status_gridIsAvailable": 1,
        })
        mqtt_state = infer_state({
            "speed_kmh": 18,
            "steering_angle_rad": 0.1,
            "power_kw": -10,
            "brake_bar": 0.2,
            "grid_available": 1,
        })

        self.assertEqual(csv_state, mqtt_state)


if __name__ == "__main__":
    unittest.main()
