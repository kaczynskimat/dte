"""Shared feature engineering for offline training and live prediction."""

from pathlib import Path

import numpy as np
import pandas as pd


HISTORY_SECONDS = 60
FORECAST_SECONDS = 10
TARGET = "mean_power_kw_next_10s"

SOURCE_TO_CANONICAL = {
    "power_kw": "power_kw",
    "gnss_altitude": "altitude_m",
    "itcs_numberOfPassengers": "passengers",
    "odometry_articulationAngle": "articulation_angle_rad",
    "odometry_steeringAngle": "steering_angle_rad",
    "odometry_wheelSpeed_fl": "wheel_speed_fl",
    "odometry_wheelSpeed_fr": "wheel_speed_fr",
    "odometry_wheelSpeed_ml": "wheel_speed_ml",
    "odometry_wheelSpeed_mr": "wheel_speed_mr",
    "odometry_wheelSpeed_rl": "wheel_speed_rl",
    "odometry_wheelSpeed_rr": "wheel_speed_rr",
    "status_doorIsOpen": "doors_open",
    "status_gridIsAvailable": "grid_available",
    "status_haltBrakeIsActive": "halt_brake",
    "status_parkBrakeIsActive": "park_brake",
    "traction_tractionForce": "traction_force_n",
    "latitude": "latitude",
    "longitude": "longitude",
    "temp_celsius": "temp_celsius",
    "speed_kmh": "speed_kmh",
    "brake_bar": "brake_bar",
}
CANONICAL_INPUT_COLUMNS = [
    "timestamp",
    "heading_deg",
    *SOURCE_TO_CANONICAL.values(),
]
ROLLING_SIGNALS = [
    "power_kw",
    "speed_kmh",
    "traction_force_n",
    "brake_bar",
    "altitude_m",
    "passengers",
    "wheel_speed_mean",
]
ROLLING_WINDOWS = (10, HISTORY_SECONDS)


def canonicalize_mission(data: pd.DataFrame) -> pd.DataFrame:
    """Rename cleaned CSV fields to the names used by live MQTT snapshots."""
    required = ["timestamp", "heading_deg", *SOURCE_TO_CANONICAL]
    missing = sorted(set(required) - set(data.columns))
    if missing:
        raise ValueError(f"mission is missing required columns: {', '.join(missing)}")

    canonical = data[list(SOURCE_TO_CANONICAL)].rename(columns=SOURCE_TO_CANONICAL)
    canonical.insert(0, "heading_deg", data["heading_deg"])
    canonical.insert(0, "timestamp", data["timestamp"])
    return canonical


def engineer_features(canonical: pd.DataFrame) -> pd.DataFrame:
    """Create past-only model features from canonical telemetry snapshots."""
    missing = sorted(set(CANONICAL_INPUT_COLUMNS) - set(canonical.columns))
    if missing:
        raise ValueError(f"telemetry is missing required fields: {', '.join(missing)}")

    features = canonical[list(SOURCE_TO_CANONICAL.values())].copy()
    for column in features:
        features[column] = pd.to_numeric(features[column], errors="coerce")

    wheel_columns = [column for column in features if column.startswith("wheel_speed_")]
    features["wheel_speed_mean"] = features[wheel_columns].mean(axis=1)
    features["wheel_speed_spread"] = (
        features[wheel_columns].max(axis=1) - features[wheel_columns].min(axis=1)
    )
    features["acceleration_kmh_s"] = features["speed_kmh"].diff()
    features["altitude_change_10s"] = features["altitude_m"].diff(10)

    heading_radians = np.deg2rad(
        pd.to_numeric(canonical["heading_deg"], errors="coerce")
    )
    features["heading_sin"] = np.sin(heading_radians)
    features["heading_cos"] = np.cos(heading_radians)

    timestamps = pd.to_datetime(canonical["timestamp"])
    seconds_of_day = (
        timestamps.dt.hour * 3600 + timestamps.dt.minute * 60 + timestamps.dt.second
    )
    features["time_sin"] = np.sin(2 * np.pi * seconds_of_day / 86400)
    features["time_cos"] = np.cos(2 * np.pi * seconds_of_day / 86400)

    for signal in ROLLING_SIGNALS:
        for window in ROLLING_WINDOWS:
            rolling = features[signal].rolling(window=window, min_periods=window)
            prefix = f"{signal}__"
            suffix = f"_{window}s"
            features[f"{prefix}mean{suffix}"] = rolling.mean()
            features[f"{prefix}std{suffix}"] = rolling.std()
            features[f"{prefix}min{suffix}"] = rolling.min()
            features[f"{prefix}max{suffix}"] = rolling.max()
            features[f"{prefix}change{suffix}"] = (
                features[signal] - features[signal].shift(window - 1)
            )
    return features


def _validate_time_axis(data: pd.DataFrame, path: Path) -> None:
    timestamps = pd.to_datetime(data["timestamp"])
    differences = timestamps.diff().dropna().dt.total_seconds()
    if not differences.eq(1.0).all():
        raise ValueError(f"{path.name} is not sampled continuously at 1 Hz")


def build_mission_examples(path: Path) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    """Build past-only features and future targets for one complete mission."""
    data = pd.read_csv(path)
    _validate_time_axis(data, path)
    canonical = canonicalize_mission(data)
    features = engineer_features(canonical)

    future_power = pd.concat(
        [canonical["power_kw"].shift(-step) for step in range(1, FORECAST_SECONDS + 1)],
        axis=1,
    )
    target = future_power.mean(axis=1, skipna=False).rename(TARGET)

    valid = pd.Series(False, index=data.index)
    valid.iloc[HISTORY_SECONDS - 1 : -FORECAST_SECONDS] = True
    valid &= target.notna()

    metadata = pd.DataFrame({
        "timestamp": pd.to_datetime(canonical["timestamp"]),
        "current_power_kw": features["power_kw"],
        "recent_mean_power_kw": features["power_kw__mean_10s"],
    })
    return (
        features.loc[valid].reset_index(drop=True),
        target.loc[valid].reset_index(drop=True),
        metadata.loc[valid].reset_index(drop=True),
    )
