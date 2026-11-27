"""The mission the dashboard replays, shared by the Fleet page, the twin feed and the Replay page.

The dashboard starts the producer with this file and time window, and draws the route and stops
from the same rows, so the map and the 3D scene line up with what is being replayed.
"""

from __future__ import annotations

import csv
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parents[2]
BUS_ID = os.getenv("REPLAY_BUS_ID", "B208")
ROUTE = os.getenv("REPLAY_ROUTE", "83")
MISSION_PATH = PROJECT_DIR / os.getenv("REPLAY_FILE", "data/test_mission_2021-10-05.csv")
# Same variable names as docker-compose.yml. An empty value leaves that end of the window open.
REPLAY_START = os.getenv("REPLAY_START", "2021-10-05 07:00:00").strip()
REPLAY_END = os.getenv("REPLAY_END", "2021-10-05 09:00:00").strip()

# One MQTT topic per sensor (docs/sensor_streams.md), plus the replay clock and status topics.
SENSOR_TOPICS = (
    "latitude", "longitude", "altitude_m", "heading_deg", "speed_kmh", "power_kw",
    "grid_available", "doors_open", "passengers", "temp_celsius",
    "steering_angle_rad", "articulation_angle_rad",
    "wheel_speed_fl", "wheel_speed_fr", "wheel_speed_ml", "wheel_speed_mr", "wheel_speed_rl", "wheel_speed_rr",
    "halt_brake", "park_brake", "brake_bar", "traction_force_n",
    "itcs_busRoute", "itcs_stopName",
)
TOPICS = (*(f"sensors/{name}" for name in SENSOR_TOPICS), "replay/tick", "replay")


def in_window(timestamp: str) -> bool:
    """The producer's --start-time/--end-time filter (inclusive); timestamps sort as text."""
    return (not REPLAY_START or timestamp >= REPLAY_START) and (not REPLAY_END or timestamp <= REPLAY_END)


@lru_cache(maxsize=2)
def _rows(path: Path, modified: float) -> tuple[dict[str, Any], ...]:
    with path.open("r", encoding="utf-8", newline="") as source:
        return tuple(row for row in csv.DictReader(source) if in_window(row.get("timestamp", "")))


def mission_rows() -> tuple[dict[str, Any], ...]:
    """Rows of the replayed window, in order (empty when the mission file is missing)."""
    if not MISSION_PATH.exists():
        return ()
    return _rows(MISSION_PATH, MISSION_PATH.stat().st_mtime)


def producer_arguments() -> list[str]:
    """Arguments that make src.replay.producer replay exactly mission_rows()."""
    arguments = ["--file", str(MISSION_PATH), "--bus-id", BUS_ID, "--expected-route", ROUTE]
    if REPLAY_START:
        arguments += ["--start-time", REPLAY_START]
    if REPLAY_END:
        arguments += ["--end-time", REPLAY_END]
    return arguments
