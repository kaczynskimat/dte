import unittest
from pathlib import Path

import joblib
import pandas as pd

from src.ml.features import canonicalize_mission
from src.ml.predictor import PredictionEngine
from src.replay.producer import (
    EVENT_SENSOR_FIELDS,
    SENSOR_FIELDS,
    build_payloads,
    select_mission_period,
    validate_mission,
)


PROJECT_ROOT = Path(__file__).parents[1]
TEST_MISSION = PROJECT_ROOT / "data" / "test_mission_2021-10-05.csv"
MODEL_PATH = PROJECT_ROOT / "src" / "ml" / "models" / "model.pkl"


class ReplayProducerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data = pd.read_csv(TEST_MISSION)

    def test_test_mission_is_continuous_route_83(self) -> None:
        validate_mission(self.data, "83")

    def test_wrong_route_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "expected route 46"):
            validate_mission(self.data, "46")

    def test_every_sensor_has_its_own_topic_and_message(self) -> None:
        payloads = build_payloads(self.data.iloc[0], sequence=0)
        periodic_sensors = set(SENSOR_FIELDS) - EVENT_SENSOR_FIELDS
        self.assertEqual(len(payloads), len(periodic_sensors) + 1)
        for sensor_name in periodic_sensors:
            topic = f"sensors/{sensor_name}"
            self.assertIn(topic, payloads)
            sensor_fields = set(payloads[topic]) - {"timestamp", "time_unix"}
            self.assertEqual(sensor_fields, {sensor_name})

        tick = payloads["replay/tick"]
        self.assertEqual(tick["snapshot_sequence"], 0)
        self.assertEqual(tick["timestamp"], self.data.iloc[0]["timestamp"])

    def test_route_and_stop_are_published_as_events(self) -> None:
        event_rows = self.data[
            (self.data["itcs_busRoute"].astype(str) != "-")
            & (self.data["itcs_stopName"].astype(str) != "-")
        ]
        self.assertFalse(event_rows.empty)
        payloads = build_payloads(event_rows.iloc[0], sequence=0)
        self.assertIn("sensors/itcs_busRoute", payloads)
        self.assertIn("sensors/itcs_stopName", payloads)

    def test_period_can_be_selected_by_timestamp(self) -> None:
        selected = select_mission_period(
            self.data,
            start_time="2021-10-05 04:04:00",
            end_time="2021-10-05 04:04:09",
            start_row=0,
            max_rows=None,
        )
        self.assertEqual(len(selected), 10)
        self.assertEqual(selected.iloc[0]["timestamp"], "2021-10-05 04:04:00")
        self.assertEqual(selected.iloc[-1]["timestamp"], "2021-10-05 04:04:09")


class LivePredictionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        data = pd.read_csv(TEST_MISSION).iloc[:70]
        cls.snapshots = canonicalize_mission(data).to_dict(orient="records")
        cls.artifact = joblib.load(MODEL_PATH)

    def test_prediction_starts_after_sixty_consecutive_samples(self) -> None:
        engine = PredictionEngine(self.artifact)
        for sequence, snapshot in enumerate(self.snapshots[:59]):
            self.assertIsNone(engine.add_snapshot(sequence, snapshot))

        prediction = engine.add_snapshot(59, self.snapshots[59])
        self.assertIsNotNone(prediction)
        self.assertEqual(prediction["status"], "ready")
        self.assertEqual(prediction["forecast_horizon_seconds"], 10)

    def test_prediction_is_evaluated_after_ten_future_samples(self) -> None:
        engine = PredictionEngine(self.artifact)
        latest = None
        for sequence, snapshot in enumerate(self.snapshots):
            latest = engine.add_snapshot(sequence, snapshot)

        self.assertIsNotNone(latest)
        expected_actual = pd.Series(
            [snapshot["power_kw"] for snapshot in self.snapshots[60:70]]
        ).mean()
        self.assertAlmostEqual(
            latest["actual_mean_power_kw_next_10s"],
            expected_actual,
            places=10,
        )
        self.assertIn("is_anomaly", latest)

    def test_gap_resets_the_warmup_window(self) -> None:
        engine = PredictionEngine(self.artifact)
        for sequence, snapshot in enumerate(self.snapshots[:59]):
            engine.add_snapshot(sequence, snapshot)

        self.assertIsNone(engine.add_snapshot(61, self.snapshots[61]))
        self.assertTrue(engine.was_reset)
        self.assertEqual(engine.samples_collected, 1)


if __name__ == "__main__":
    unittest.main()
