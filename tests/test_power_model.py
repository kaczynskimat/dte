import json
import unittest
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
import xgboost


PROJECT_ROOT = Path(__file__).parents[1]
MODEL_PATH = PROJECT_ROOT / "src" / "ml" / "models" / "model.pkl"
METRICS_PATH = PROJECT_ROOT / "src" / "ml" / "models" / "metrics.json"
PREDICTIONS_PATH = PROJECT_ROOT / "data" / "test_predictions_2021-10-05.csv"


class PowerModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.artifact = joblib.load(MODEL_PATH)
        cls.metrics = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
        cls.predictions = pd.read_csv(PREDICTIONS_PATH)

    def test_artifact_describes_future_prediction(self) -> None:
        self.assertEqual(self.artifact["target"], "mean_power_kw_next_10s")
        self.assertEqual(self.artifact["history_seconds"], 60)
        self.assertEqual(self.artifact["forecast_seconds"], 10)
        self.assertGreater(self.artifact["anomaly_threshold_kw"], 0)
        self.assertEqual(self.artifact["sklearn_version"], sklearn.__version__)
        self.assertEqual(self.artifact["xgboost_version"], xgboost.__version__)

    def test_model_predicts_numeric_future_power(self) -> None:
        sample = pd.DataFrame([
            {feature: 0.0 for feature in self.artifact["feature_columns"]}
        ])
        prediction = self.artifact["model"].predict(sample)
        self.assertEqual(prediction.shape, (1,))
        self.assertTrue(np.isfinite(prediction[0]))

    def test_missions_remain_separate(self) -> None:
        self.assertEqual(
            self.metrics["training_missions"],
            ["train_mission_2021-09-25.csv", "train_mission_2021-09-29.csv"],
        )
        self.assertEqual(
            self.metrics["validation_mission"],
            "validation_mission_2021-10-04.csv",
        )
        self.assertEqual(self.metrics["test_mission"], "test_mission_2021-10-05.csv")
        expected_training_rows = sum(self.metrics["training_rows_by_mission"])
        self.assertEqual(self.metrics["training_rows"], expected_training_rows)

    def test_xgboost_beats_both_test_baselines(self) -> None:
        model = self.metrics["test"]["xgboost"]
        persistence = self.metrics["test"]["persistence_baseline"]
        recent_mean = self.metrics["test"]["recent_mean_baseline"]
        self.assertLess(model["mae_kw"], persistence["mae_kw"])
        self.assertLess(model["mae_kw"], recent_mean["mae_kw"])

    def test_prediction_output_is_complete(self) -> None:
        self.assertEqual(len(self.predictions), self.metrics["test_rows"])
        required = {
            "timestamp",
            "actual_mean_power_kw_next_10s",
            "predicted_mean_power_kw_next_10s",
            "predicted_energy_kwh_next_10s",
            "absolute_error_kw",
            "is_anomaly",
        }
        self.assertTrue(required.issubset(self.predictions.columns))
        numeric_required = required - {"timestamp", "is_anomaly"}
        self.assertFalse(self.predictions[list(numeric_required)].isna().any().any())
        self.assertFalse(self.predictions["is_anomaly"].isna().any())

    def test_target_uses_only_the_following_ten_seconds(self) -> None:
        raw = pd.read_csv(
            PROJECT_ROOT / "data" / "test_mission_2021-10-05.csv",
            usecols=["timestamp", "power_kw"],
        )
        first_prediction = self.predictions.iloc[0]
        expected_timestamp = raw.loc[59, "timestamp"]
        expected_target = raw.loc[60:69, "power_kw"].mean()
        self.assertEqual(first_prediction["timestamp"], expected_timestamp)
        self.assertAlmostEqual(
            first_prediction["actual_mean_power_kw_next_10s"],
            expected_target,
            places=10,
        )


if __name__ == "__main__":
    unittest.main()
