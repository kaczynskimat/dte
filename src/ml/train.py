"""Train the offline XGBoost model for future bus power prediction.

At each second, the model uses only telemetry available in the previous
60 seconds (including the current reading) to predict the mean power demand
over the following 10 seconds. Large validation residuals define anomalies.
"""

import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
import xgboost
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from xgboost import XGBRegressor

from src.ml.features import (
    FORECAST_SECONDS,
    HISTORY_SECONDS,
    TARGET,
    build_mission_examples,
)


PROJECT_ROOT = Path(__file__).parents[2]
TRAINING_DATA = [
    PROJECT_ROOT / "data" / "train_mission_2021-09-25.csv",
    PROJECT_ROOT / "data" / "train_mission_2021-09-29.csv",
]
VALIDATION_DATA = PROJECT_ROOT / "data" / "validation_mission_2021-10-04.csv"
TEST_DATA = PROJECT_ROOT / "data" / "test_mission_2021-10-05.csv"
MODEL_DIR = Path(__file__).parent / "models"
MODEL_PATH = MODEL_DIR / "model.pkl"
METRICS_PATH = MODEL_DIR / "metrics.json"
TEST_PREDICTIONS_PATH = PROJECT_ROOT / "data" / "test_predictions_2021-10-05.csv"

ANOMALY_PERCENTILE = 0.99


def score_model(actual: pd.Series | np.ndarray, predicted: np.ndarray) -> dict[str, float]:
    return {
        "mae_kw": float(mean_absolute_error(actual, predicted)),
        "rmse_kw": float(np.sqrt(mean_squared_error(actual, predicted))),
        "r2": float(r2_score(actual, predicted)),
    }


def _load_training_data() -> tuple[pd.DataFrame, pd.Series, list[int]]:
    feature_frames = []
    target_frames = []
    row_counts = []
    for path in TRAINING_DATA:
        features, target, _ = build_mission_examples(path)
        feature_frames.append(features)
        target_frames.append(target)
        row_counts.append(len(features))
    return (
        pd.concat(feature_frames, ignore_index=True),
        pd.concat(target_frames, ignore_index=True),
        row_counts,
    )


def train() -> dict:
    x_train, y_train, training_rows_by_mission = _load_training_data()
    x_validation, y_validation, _ = build_mission_examples(VALIDATION_DATA)
    x_test, y_test, test_metadata = build_mission_examples(TEST_DATA)

    model = XGBRegressor(
        objective="reg:squarederror",
        eval_metric="mae",
        n_estimators=1500,
        learning_rate=0.03,
        max_depth=6,
        min_child_weight=5,
        subsample=0.8,
        colsample_bytree=0.8,
        reg_alpha=0.05,
        reg_lambda=1.0,
        early_stopping_rounds=60,
        tree_method="hist",
        random_state=42,
        n_jobs=4,
    )
    model.fit(
        x_train,
        y_train,
        eval_set=[(x_validation, y_validation)],
        verbose=False,
    )

    validation_prediction = model.predict(x_validation)
    test_prediction = model.predict(x_test)
    validation_residuals = np.abs(y_validation.to_numpy() - validation_prediction)
    anomaly_threshold = float(np.quantile(validation_residuals, ANOMALY_PERCENTILE))

    validation_persistence = x_validation["power_kw"].to_numpy()
    validation_recent_mean = x_validation["power_kw__mean_10s"].to_numpy()
    test_persistence = x_test["power_kw"].to_numpy()
    test_recent_mean = x_test["power_kw__mean_10s"].to_numpy()

    importances = sorted(
        zip(x_train.columns, model.feature_importances_, strict=True),
        key=lambda item: item[1],
        reverse=True,
    )
    top_features = [
        {"feature": feature, "importance": float(importance)}
        for feature, importance in importances[:20]
    ]

    metrics = {
        "objective": TARGET,
        "history_seconds": HISTORY_SECONDS,
        "forecast_seconds": FORECAST_SECONDS,
        "training_missions": [path.name for path in TRAINING_DATA],
        "validation_mission": VALIDATION_DATA.name,
        "test_mission": TEST_DATA.name,
        "training_rows": int(len(x_train)),
        "training_rows_by_mission": training_rows_by_mission,
        "validation_rows": int(len(x_validation)),
        "test_rows": int(len(x_test)),
        "best_iteration": int(model.best_iteration),
        "validation": {
            "xgboost": score_model(y_validation, validation_prediction),
            "persistence_baseline": score_model(y_validation, validation_persistence),
            "recent_mean_baseline": score_model(y_validation, validation_recent_mean),
        },
        "test": {
            "xgboost": score_model(y_test, test_prediction),
            "persistence_baseline": score_model(y_test, test_persistence),
            "recent_mean_baseline": score_model(y_test, test_recent_mean),
        },
        "anomaly_threshold_kw": anomaly_threshold,
        "anomaly_percentile": ANOMALY_PERCENTILE,
        "top_features": top_features,
    }

    artifact = {
        "model": model,
        "feature_columns": list(x_train.columns),
        "target": TARGET,
        "history_seconds": HISTORY_SECONDS,
        "forecast_seconds": FORECAST_SECONDS,
        "anomaly_threshold_kw": anomaly_threshold,
        "metrics": metrics,
        "sklearn_version": sklearn.__version__,
        "xgboost_version": xgboost.__version__,
    }

    residual = y_test.to_numpy() - test_prediction
    test_results = test_metadata.copy()
    test_results["actual_mean_power_kw_next_10s"] = y_test.to_numpy()
    test_results["predicted_mean_power_kw_next_10s"] = test_prediction
    test_results["predicted_energy_kwh_next_10s"] = (
        test_prediction * FORECAST_SECONDS / 3600
    )
    test_results["persistence_baseline_kw"] = test_persistence
    test_results["recent_mean_baseline_kw"] = test_recent_mean
    test_results["residual_kw"] = residual
    test_results["absolute_error_kw"] = np.abs(residual)
    test_results["is_anomaly"] = np.abs(residual) > anomaly_threshold

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, MODEL_PATH)
    METRICS_PATH.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    test_results.to_csv(TEST_PREDICTIONS_PATH, index=False)
    return metrics


if __name__ == "__main__":
    results = train()
    print(json.dumps(results, indent=2))
    print(f"Saved model to {MODEL_PATH}")
    print(f"Saved test predictions to {TEST_PREDICTIONS_PATH}")
