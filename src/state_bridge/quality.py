"""Evaluate whether a completed bus snapshot is usable and up to date."""

from __future__ import annotations

import json
import math
from typing import Any, Mapping


# Route and stop are intentionally absent: they are event-based and retain their
# last known value. Every field below is expected once per replay tick.
PERIODIC_FIELDS = (
    "latitude",
    "longitude",
    "altitude_m",
    "heading_deg",
    "speed_kmh",
    "power_kw",
    "grid_available",
    "doors_open",
    "passengers",
    "temp_celsius",
    "steering_angle_rad",
    "articulation_angle_rad",
    "wheel_speed_fl",
    "wheel_speed_fr",
    "wheel_speed_ml",
    "wheel_speed_mr",
    "wheel_speed_rl",
    "wheel_speed_rr",
    "halt_brake",
    "park_brake",
    "brake_bar",
    "traction_force_n",
)

BOOLEAN_FIELDS = {
    "grid_available",
    "doors_open",
    "halt_brake",
    "park_brake",
}

# Conservative physical limits. They detect corrupted telemetry without treating
# ordinary variation in the ZTBus missions as an error.
PHYSICAL_RANGES = {
    "latitude": (-90.0, 90.0),
    "longitude": (-180.0, 180.0),
    "altitude_m": (-500.0, 5000.0),
    "heading_deg": (0.0, 360.0),
    "speed_kmh": (-5.0, 130.0),
    "power_kw": (-1000.0, 1000.0),
    "passengers": (0.0, 250.0),
    "temp_celsius": (-50.0, 60.0),
    "steering_angle_rad": (-3.2, 3.2),
    "articulation_angle_rad": (-3.2, 3.2),
    "wheel_speed_fl": (-50.0, 50.0),
    "wheel_speed_fr": (-50.0, 50.0),
    "wheel_speed_ml": (-50.0, 50.0),
    "wheel_speed_mr": (-50.0, 50.0),
    "wheel_speed_rl": (-50.0, 50.0),
    "wheel_speed_rr": (-50.0, 50.0),
    "brake_bar": (0.0, 20.0),
    "traction_force_n": (-100000.0, 100000.0),
}


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _same_time(sensor_time: Any, tick_time: Any) -> bool:
    try:
        return float(sensor_time) == float(tick_time)
    except (TypeError, ValueError):
        return str(sensor_time).strip() == str(tick_time).strip()


def _is_valid_boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    return str(value).strip().lower() in {"0", "1", "false", "true"}


def _is_within_range(field: str, value: Any) -> bool:
    if field in BOOLEAN_FIELDS:
        return _is_valid_boolean(value)
    limits = PHYSICAL_RANGES.get(field)
    if limits is None:
        return True
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and limits[0] <= number <= limits[1]


def evaluate_quality(
    telemetry: Mapping[str, Any],
    sensor_times: Mapping[str, Any],
    tick_time: Any,
) -> dict[str, Any]:
    """Return Redis-ready quality fields for one completed replay tick."""
    missing: list[str] = []
    stale: list[str] = []
    invalid: list[str] = []

    for field in PERIODIC_FIELDS:
        value = telemetry.get(field)
        if _is_missing(value):
            missing.append(field)
            continue
        if field not in sensor_times or not _same_time(sensor_times[field], tick_time):
            stale.append(field)
        if not _is_within_range(field, value):
            invalid.append(field)

    flags = [
        *(f"missing:{field}" for field in missing),
        *(f"stale:{field}" for field in stale),
        *(f"out_of_range:{field}" for field in invalid),
    ]
    if missing or invalid:
        status = "invalid"
    elif stale:
        status = "warning"
    else:
        status = "valid"

    return {
        "quality_status": status,
        "quality_flags": json.dumps(flags, separators=(",", ":")),
        "quality_missing_count": len(missing),
        "quality_stale_count": len(stale),
        "quality_invalid_count": len(invalid),
        "quality_checked_timestamp": "" if tick_time is None else tick_time,
    }
