"""Full-screen spatial view of a ZTBus replay mission."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

import streamlit as st


PROJECT_DIR = Path(__file__).resolve().parent
REPLAY_PATH = PROJECT_DIR / "data" / "clean_replay_mission.csv"
MOCK_STATE_PATH = PROJECT_DIR / "data" / "mock_state.json"
ASSETS_DIR = PROJECT_DIR / "src" / "dashboard" / "assets"


def _to_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value or value in {"-", "NaN", "nan", "null", "None"}:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _flatten_record(record: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}

    def visit(source: dict[str, Any], prefix: tuple[str, ...] = ()) -> None:
        for key, child in source.items():
            path = (*prefix, str(key))
            if isinstance(child, dict):
                visit(child, path)
            else:
                values[".".join(path)] = child
                values.setdefault(str(key), child)

    visit(record)
    return values


def _position_from_record(record: dict[str, Any]) -> list[float] | None:
    values = _flatten_record(record)
    latitude = longitude = None
    latitude_key = longitude_key = None

    for key in ("latitude", "location.latitude", "gnss_latitude", "location.gnss_latitude"):
        latitude = _to_number(values.get(key))
        if latitude is not None:
            latitude_key = key
            break

    for key in ("longitude", "location.longitude", "gnss_longitude", "location.gnss_longitude"):
        longitude = _to_number(values.get(key))
        if longitude is not None:
            longitude_key = key
            break

    if latitude is None or longitude is None:
        return None
    if latitude_key and "gnss_latitude" in latitude_key:
        latitude = math.degrees(latitude)
    if longitude_key and "gnss_longitude" in longitude_key:
        longitude = math.degrees(longitude)

    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return [longitude, latitude]


def _odometer(rows: list[dict[str, Any]]) -> list[float]:
    """Metres travelled at each row, integrated from the wheel-based vehicle speed (no GNSS jitter)."""
    distances: list[float] = []
    travelled = 0.0
    previous_time: float | None = None
    for row in rows:
        stamp = _to_number(row.get("time_unix"))
        speed = _to_number(row.get("odometry_vehicleSpeed")) or 0.0
        if previous_time is not None and stamp is not None:
            travelled += max(0.0, speed) * max(0.0, min(stamp - previous_time, 5.0))
        if stamp is not None:
            previous_time = stamp
        distances.append(travelled)
    return distances


def _read_csv_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8", newline="") as data_file:
        return list(csv.DictReader(data_file))


def _read_csv_route(path: Path) -> list[list[float]]:
    rows = _read_csv_rows(path)
    distances = _odometer(rows)
    route: list[list[float]] = []
    for row, distance in zip(rows, distances):
        position = _position_from_record(row)
        if position is not None:
            # [lon, lat, time_unix, metres travelled]: time and distance let the live twin draw the
            # trail already travelled and place the 3D bus and stops at their real distances.
            stamp = _to_number(row.get("time_unix"))
            route.append(position if stamp is None else [*position, int(stamp), round(distance, 1)])
    return route


def _load_stop_visits() -> list[dict[str, Any]]:
    """Every stop of the replayed mission, in route order, where the bus stood at it (or passed it).

    The ITCS reports a stop name for one sample as the bus leaves or passes the stop. Looking back from
    that sample for the dwell with open doors gives where the bus really stood; with no dwell within
    60 m the bus passed without stopping.
    """
    if not REPLAY_PATH.exists():
        return []
    rows = _read_csv_rows(REPLAY_PATH)
    distances = _odometer(rows)
    visits: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        name = (row.get("itcs_stopName") or "").strip()
        if name in {"", "-"}:
            continue
        if index and (rows[index - 1].get("itcs_stopName") or "").strip() == name:
            continue
        dwell = None
        for back in range(index, max(-1, index - 120), -1):
            if distances[index] - distances[back] > 60:
                break
            if _to_number(rows[back].get("status_doorIsOpen")) == 1 and (_to_number(rows[back].get("speed_kmh")) or 0.0) < 2:
                dwell = back
                break
        at = dwell if dwell is not None else index
        position = _position_from_record(rows[at]) or _position_from_record(row)
        if position is None:
            continue
        visits.append(
            {
                "name": name,
                "lon": round(position[0], 6),
                "lat": round(position[1], 6),
                "d": round(distances[at], 1),
                "served": dwell is not None,
            }
        )
    return visits


def _read_mock_route(path: Path) -> list[list[float]]:
    with path.open("r", encoding="utf-8") as data_file:
        payload = json.load(data_file)

    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        records = next(
            (
                payload[key]
                for key in ("samples", "records", "data")
                if isinstance(payload.get(key), list)
            ),
            [payload],
        )
    else:
        records = []

    return [
        position
        for record in records
        if isinstance(record, dict)
        for position in [_position_from_record(record)]
        if position is not None
    ]


def _load_route() -> list[list[float]]:
    if REPLAY_PATH.exists():
        route = _read_csv_route(REPLAY_PATH)
        if len(route) >= 2:
            return route

    if MOCK_STATE_PATH.exists():
        route = _read_mock_route(MOCK_STATE_PATH)
        if len(route) >= 2:
            return route

    return []


def _load_mock_metrics() -> dict[str, Any]:
    if not MOCK_STATE_PATH.exists():
        return {}

    with MOCK_STATE_PATH.open("r", encoding="utf-8") as data_file:
        payload = json.load(data_file)

    if isinstance(payload, list):
        records = [record for record in payload if isinstance(record, dict)]
        return records[-1] if records else {}
    if isinstance(payload, dict):
        for key in ("samples", "records", "data"):
            records = payload.get(key)
            if isinstance(records, list):
                valid_records = [record for record in records if isinstance(record, dict)]
                return valid_records[-1] if valid_records else {}
        return payload
    return {}


def _sample_state(metrics: dict[str, Any]) -> str | None:
    """State-machine output for the sample record shown before a replay starts."""
    if not metrics:
        return None
    from src.state_machine import infer_state

    return infer_state(metrics)


@st.cache_resource(show_spinner=False)
def _twin_feed_port() -> str:
    """Start (once per process) the local JSON feed the Fleet page polls for live twin state."""
    from src.dashboard.twin_feed import start_feed

    return start_feed()


def _dashboard_html(route: list[list[float]], metrics: dict[str, Any], feed_port: str = "") -> str:
    route_json = json.dumps(route, separators=(",", ":"))
    metrics_json = json.dumps(metrics, separators=(",", ":"))
    return r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
        integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
  <style>
    * { box-sizing: border-box; }
    html, body, #screen { width: 100%; height: 100%; margin: 0; overflow: hidden; }
    body { font-family: system-ui, sans-serif; }
    #screen {
      position: relative;
      overflow: hidden;
    }
    #map-pane {
      position: absolute;
      z-index: 10;
      left: var(--map-pip-x, 18px);
      top: var(--map-pip-y, calc(100% - var(--map-pip-height, 260px) - 18px));
      width: var(--map-pip-width, 360px);
      height: var(--map-pip-height, 260px);
      overflow: hidden;
      border: 2px solid rgba(255, 255, 255, 0.96);
      border-radius: 10px;
      background: #ffffff;
      box-shadow: 0 8px 28px rgba(15, 35, 55, 0.3);
    }
    #map { width: 100%; height: 100%; min-width: 0; }
    #model-pane {
      position: absolute;
      z-index: 0;
      inset: 0;
      background: radial-gradient(ellipse at center, #ffffff 0%, #edf3f9 78%);
      overflow: hidden;
      perspective: 1000px;
    }
    .map-resize-handle {
      position: absolute;
      z-index: 1100;
      padding: 0;
      border: 0;
      background: transparent;
      touch-action: none;
    }
    .map-resize-handle:focus-visible { outline: 2px solid #0878bd; outline-offset: -2px; }
    .map-resize-n, .map-resize-s { left: 14px; right: 14px; height: 14px; cursor: ns-resize; }
    .map-resize-n { top: 0; }
    .map-resize-s { bottom: 0; }
    .map-resize-w, .map-resize-e { top: 14px; bottom: 14px; width: 14px; cursor: ew-resize; }
    .map-resize-w { left: 0; }
    .map-resize-e { right: 0; }
    .map-resize-nw, .map-resize-ne, .map-resize-sw, .map-resize-se { width: 22px; height: 22px; }
    .map-resize-nw { top: 0; left: 0; cursor: nwse-resize; }
    .map-resize-ne { top: 0; right: 0; cursor: nesw-resize; }
    .map-resize-sw { bottom: 0; left: 0; cursor: nesw-resize; }
    .map-resize-se { bottom: 0; right: 0; cursor: nwse-resize; }
    .map-resize-n::after, .map-resize-s::after, .map-resize-e::after, .map-resize-w::after,
    .map-resize-nw::after, .map-resize-ne::after, .map-resize-sw::after, .map-resize-se::after {
      content: "";
      position: absolute;
      background: transparent;
      opacity: 0;
      transition: opacity 120ms ease;
    }
    .map-resize-n::after, .map-resize-s::after { left: 42%; right: 42%; height: 2px; background: #0878bd; }
    .map-resize-n::after { top: 3px; }
    .map-resize-s::after { bottom: 3px; }
    .map-resize-e::after, .map-resize-w::after { top: 42%; bottom: 42%; width: 2px; background: #0878bd; }
    .map-resize-e::after { right: 3px; }
    .map-resize-w::after { left: 3px; }
    .map-resize-nw::after, .map-resize-se::after { width: 11px; height: 11px; border: 0; }
    .map-resize-nw::after { top: 4px; left: 4px; border-top: 2px solid #0878bd; border-left: 2px solid #0878bd; }
    .map-resize-se::after { right: 4px; bottom: 4px; border-right: 2px solid #0878bd; border-bottom: 2px solid #0878bd; }
    .map-resize-ne::after, .map-resize-sw::after { width: 11px; height: 11px; border: 0; }
    .map-resize-ne::after { top: 4px; right: 4px; border-top: 2px solid #0878bd; border-right: 2px solid #0878bd; }
    .map-resize-sw::after { bottom: 4px; left: 4px; border-bottom: 2px solid #0878bd; border-left: 2px solid #0878bd; }
    .map-resize-nw::after, .map-resize-ne::after, .map-resize-sw::after, .map-resize-se::after { opacity: .55; }
    .map-resize-handle:hover::after, .map-resize-handle:focus-visible::after { opacity: 1; }
    #map-pane.resizing { user-select: none; }
    .bus-model-view {
      inset: 0;
      position: absolute;
      height: 100%;
      min-height: 0;
      overflow: hidden;
      width: 100%;
    }
    .bus-model-view[hidden] { display: none; }
    .bus-model-view canvas { display: block; height: 100%; width: 100%; }
    #passenger-controls {
      position: absolute;
      z-index: 5;
      right: 18px;
      bottom: 18px;
      display: flex;
      align-items: center;
      gap: 10px;
    }
    #passenger-count-label { color: #273540; font: 600 12px/1 system-ui, sans-serif; }
    #passenger-count {
      width: 52px;
      height: 38px;
      padding: 0 4px;
      border: 1px solid #cad4dc;
      border-radius: 10px;
      background: #ffffff;
      color: #273540;
      font: 600 14px system-ui, sans-serif;
      text-align: center;
    }
    .passenger-button {
      border: 0;
      border-radius: 999px;
      padding: 11px 17px;
      color: #ffffff;
      background: #0878bd;
      box-shadow: 0 4px 14px rgba(15, 35, 55, 0.24);
      font: 600 14px/1 system-ui, sans-serif;
      cursor: pointer;
    }
    #add-passenger { background: #0878bd; }
    #add-passenger:hover { background: #066aa7; }
    #remove-passenger { background: #3f5668; }
    #remove-passenger:hover { background: #304553; }
    .passenger-button:disabled { cursor: default; opacity: 0.72; }
    #trip-toggle {
      position: absolute;
      z-index: 5;
      right: 18px;
      bottom: 72px;
      border: 0;
      border-radius: 999px;
      padding: 11px 17px;
      color: #ffffff;
      background: #16854b;
      box-shadow: 0 4px 14px rgba(15, 35, 55, 0.24);
      font: 600 14px/1 system-ui, sans-serif;
      cursor: pointer;
    }
    #trip-toggle:hover { background: #116d3d; }
    #trip-toggle:disabled { cursor: default; opacity: 0.72; }
    .leaflet-control-attribution { font-size: 9px !important; }
    #telemetry-overlay {
      position: absolute;
      z-index: 7;
      inset: 0;
      overflow: hidden;
      pointer-events: none;
      color: #f5f8fb;
      font-family: system-ui, sans-serif;
    }
    #topview-open {
      position: absolute;
      z-index: 6;
      top: 12px;
      left: 12px;
      width: 118px;
      height: 76px;
      padding: 3px;
      border: 0;
      background: transparent;
      cursor: pointer;
      pointer-events: auto;
    }
    #topview-open svg { display: block; width: 100%; height: 100%; overflow: visible; }
    #topview-open:focus-visible { outline: 2px solid #0878bd; outline-offset: 2px; border-radius: 5px; }
    #topview-modal {
      position: absolute;
      z-index: 30;
      inset: 0;
      display: grid;
      place-items: center;
      padding: 22px;
      background: rgba(21, 32, 42, 0.42);
      font-family: system-ui, sans-serif;
    }
    #topview-modal[hidden] { display: none; }
    #topview-dialog {
      width: min(1360px, calc(100vw - 24px));
      height: min(92vh, calc(100vh - 24px));
      display: flex;
      flex-direction: column;
      overflow: hidden;
      border-radius: 16px;
      background: #ffffff;
      box-shadow: 0 24px 80px rgba(10, 24, 36, 0.34);
    }
    #topview-dialog-header {
      min-height: 62px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 10px 16px;
      border-bottom: 1px solid #dfe4e7;
    }
    #inspection-title h2 { margin: 0; color: #28343a; font-size: 17px; line-height: 1.25; font-weight: 700; }
    #inspection-title p { margin: 3px 0 0; color: #78848a; font-size: 10px; letter-spacing: .1em; text-transform: uppercase; }
    #inspection-actions { display: flex; align-items: center; gap: 9px; }
    #json-toggle {
      min-height: 34px;
      padding: 0 13px;
      border: 1px solid #d4dfe5;
      border-radius: 18px;
      color: #22516b;
      background: #f4f8fa;
      font: 600 12px system-ui, sans-serif;
      cursor: pointer;
    }
    #json-toggle:hover { background: #e8f2f7; }
    #topview-close {
      width: 34px;
      height: 34px;
      border: 0;
      border-radius: 50%;
      color: #33434f;
      background: #e8edf1;
      font: 22px/1 system-ui, sans-serif;
      cursor: pointer;
    }
    #topview-close:hover { background: #dce5eb; }
    #captured-json-view {
      flex: 1;
      min-height: 0;
      display: flex;
      padding: 16px 18px 18px;
      overflow: hidden;
      background: #f5f7f8;
    }
    #captured-json-view[hidden] { display: none; }
    #captured-json {
      flex: 1;
      min-height: 0;
      margin: 0;
      padding: 17px;
      overflow: auto;
      border: 1px solid #dce2e5;
      border-radius: 9px;
      color: #263640;
      background: #ffffff;
      font: 12px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      tab-size: 2;
      white-space: pre;
    }
    .json-line { display: block; min-height: 1.55em; }
    .json-line-highlight { margin-inline: -17px; padding-inline: 17px; background: #fff2c2; box-shadow: inset 3px 0 #d69c00; }
    #topview-diagram {
      flex: 1;
      min-height: 0;
      display: grid;
      grid-template-columns: minmax(0, 1.12fr) minmax(330px, .88fr);
      gap: 18px;
      padding: 16px 18px 18px;
      overflow: hidden;
    }
    #topview-diagram[hidden] { display: none; }
    #inspection-drawing {
      position: relative;
      min-width: 0;
      min-height: 0;
      display: flex;
      border: 1px solid #dce2e5;
      background: #ffffff;
      overflow: hidden;
    }
    /* Zoomable drawing: it only ever grows up to the panel height, so it scrolls sideways, never vertically. */
    #inspection-scroller {
      flex: 1;
      min-width: 0;
      display: flex;
      align-items: center;
      overflow-x: auto;
      overflow-y: hidden;
      overscroll-behavior-x: contain;
      scrollbar-gutter: stable;
    }
    #inspection-scroller.is-zoomed { cursor: grab; }
    #inspection-scroller.is-panning { cursor: grabbing; user-select: none; }
    #inspection-scroller > svg { display: block; flex: 0 0 auto; width: 100%; height: auto; margin: 0 auto; }
    #inspection-zoom {
      position: absolute; z-index: 2; top: 10px; right: 10px;
      display: flex; align-items: center; gap: 2px; padding: 3px;
      border: 1px solid #d4dfe5; border-radius: 999px; background: rgba(255, 255, 255, 0.94);
      box-shadow: 0 2px 8px rgba(15, 35, 55, 0.1);
    }
    #inspection-zoom button {
      min-width: 30px; height: 28px; padding: 0 8px; border: 0; border-radius: 999px;
      color: #22516b; background: transparent; font: 650 15px/1 system-ui, sans-serif; cursor: pointer;
    }
    #inspection-zoom button:hover:not(:disabled) { background: #e8f2f7; }
    #inspection-zoom button:disabled { color: #a9b6bd; cursor: default; }
    #inspection-zoom-level { min-width: 54px !important; font-size: 11px !important; font-variant-numeric: tabular-nums; }
    #inspection-zoom-hint { position: absolute; z-index: 2; left: 12px; bottom: 18px; color: #8a959b; font: 10px/1.2 system-ui, sans-serif; pointer-events: none; }
    #inspection-drawing text { font-family: system-ui, sans-serif; }
    .inspection-outline { fill: #ffffff; stroke: #465158; stroke-width: 2.2; stroke-linecap: round; stroke-linejoin: round; }
    .inspection-detail { fill: none; stroke: #788288; stroke-width: 1.2; stroke-linecap: round; stroke-linejoin: round; }
    .inspection-strong-detail { fill: none; stroke: #465158; stroke-width: 1.6; stroke-linecap: round; stroke-linejoin: round; }
    .inspection-leader { fill: none; stroke: #0878bd; stroke-width: 1.4; stroke-linecap: round; stroke-linejoin: round; }
    .inspection-marker { fill: #ffffff; stroke: #0878bd; stroke-width: 1.8; }
    .inspection-marker-text { fill: #0878bd; font-size: 12px; font-weight: 750; text-anchor: middle; dominant-baseline: central; }
    .inspection-view-label { fill: #5e696f; font-size: 11px; font-weight: 700; letter-spacing: .12em; }
    #inspection-checklist {
      min-width: 0;
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      grid-template-rows: auto auto auto auto;
      align-content: start;
      column-gap: 16px;
      overflow: hidden;
      padding: 0 4px 0 0;
      color: #354149;
    }
    #inspection-checklist > .inspection-meta { grid-column: 1 / -1; grid-row: 1; }
    #inspection-checklist > .inspection-group:nth-of-type(1) { grid-column: 1; grid-row: 2; }
    #inspection-checklist > .inspection-group:nth-of-type(2) { grid-column: 2; grid-row: 2; }
    #inspection-checklist > .inspection-group:nth-of-type(3) { grid-column: 1; grid-row: 3; }
    #inspection-checklist > .inspection-group:nth-of-type(4) { grid-column: 2; grid-row: 3; }
    #inspection-checklist > .inspection-group:nth-of-type(5) { grid-column: 2; grid-row: 4; }
    .inspection-meta { display: flex; justify-content: space-between; gap: 12px; padding: 0 0 10px; border-bottom: 1px solid #dfe4e7; color: #737f85; font-size: 11px; }
    .inspection-group { min-width: 0; padding: 6px 0 5px; border-bottom: 1px solid #e4e8ea; }
    .inspection-group-title { display: flex; align-items: center; gap: 7px; margin: 0 0 4px; color: #39474e; font-size: 11px; font-weight: 750; }
    .inspection-group-number { width: 19px; height: 19px; flex: 0 0 19px; display: inline-grid; place-items: center; border: 1px solid #0878bd; border-radius: 50%; color: #0878bd; font-size: 9px; }
    .inspection-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 6px; padding: 2px 0; border-bottom: 1px dotted #e6eaec; font-size: 10px; line-height: 1.2; }
    .inspection-row:last-child { border-bottom: 0; }
    .inspection-row span { color: #738087; }
    .inspection-row strong span { color: inherit; }
    .inspection-row strong { color: #303b40; text-align: right; font-size: 10px; font-weight: 650; font-variant-numeric: tabular-nums; }
    .inspection-row, .inspection-group { transition: background-color 120ms ease, box-shadow 120ms ease; }
    .inspection-row.is-highlighted { padding-inline: 5px; border-radius: 4px; background: #eaf5fa; box-shadow: inset 3px 0 #0878bd; }
    .inspection-group.is-highlighted { border-radius: 6px; background: #f7fbfd; }
    .inspection-meta.is-highlighted { border-radius: 5px; background: #eaf5fa; box-shadow: inset 3px 0 #0878bd; }
    .inspection-target { transition: fill 120ms ease, stroke 120ms ease, opacity 120ms ease; }
    rect.inspection-target.is-highlighted { fill: #dff3fb; stroke: #0878bd; stroke-width: 4; }
    path.inspection-target.is-highlighted.inspection-outline { fill: #f1f9fc; stroke: #0878bd; stroke-width: 3.5; }
    path.inspection-target.is-highlighted.inspection-detail { stroke: #0878bd; stroke-width: 2.6; }
    .inspection-target.is-highlighted .inspection-leader { stroke: #d48600; stroke-width: 3; }
    .inspection-target.is-highlighted .inspection-marker { fill: #0878bd; stroke: #0878bd; stroke-width: 2.8; }
    .inspection-target.is-highlighted .inspection-marker-text { fill: #ffffff; }
    .inspection-label { display: inline-flex; align-items: center; gap: 6px; }
    .parameter-info-button {
      width: 17px;
      height: 17px;
      flex: 0 0 17px;
      display: inline-grid;
      place-items: center;
      padding: 0;
      border: 1px solid #91adbb;
      border-radius: 50%;
      color: #39728e;
      background: #ffffff;
      font: 700 10px/1 system-ui, sans-serif;
      cursor: pointer;
    }
    .parameter-info-button:hover, .parameter-info-button:focus-visible { border-color: #0878bd; color: #0878bd; background: #eaf5fa; }
    #parameter-info-tooltip {
      position: fixed;
      z-index: 60;
      width: min(350px, calc(100vw - 24px));
      max-height: min(42vh, 300px);
      padding: 13px 14px 11px;
      overflow: auto;
      border: 1px solid #d9e1e5;
      border-radius: 11px;
      color: #354149;
      background: #ffffff;
      box-shadow: 0 10px 30px rgba(10, 24, 36, 0.22);
      font-family: system-ui, sans-serif;
    }
    #parameter-info-tooltip[hidden] { display: none; }
    #parameter-info-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin: 0 0 9px; }
    #parameter-info-header p { margin: 0 0 3px; color: #78848a; font-size: 8px; font-weight: 700; letter-spacing: .11em; text-transform: uppercase; }
    #parameter-info-header h3 { margin: 0; color: #28343a; font-size: 13px; line-height: 1.3; }
    #parameter-info-close { width: 24px; height: 24px; flex: 0 0 24px; border: 0; border-radius: 50%; color: #33434f; background: #e8edf1; font: 18px/1 system-ui, sans-serif; cursor: pointer; }
    #parameter-info-key { display: inline-block; margin: 0 0 8px; padding: 4px 7px; border: 1px solid #dce8ed; border-radius: 6px; color: #174d68; background: #eff6f9; font: 11px/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; cursor: pointer; }
    #parameter-info-key::after { content: ' ↗'; color: #39728e; }
    #parameter-info-key:hover, #parameter-info-key:focus-visible { border-color: #78a8bf; background: #e4f1f6; }
    #parameter-info-description { margin: 0 0 7px; color: #45545b; font-size: 11px; line-height: 1.45; }
    #parameter-info-mapping { margin: 0 0 7px; color: #65747b; font-size: 10px; line-height: 1.4; }
    #parameter-info-source { margin: 0; padding-top: 7px; border-top: 1px solid #e8ecee; color: #78848a; font-size: 9px; }
    @media (max-width: 700px) {
      #topview-open { width: 88px; height: 58px; top: 8px; left: 8px; }
      #topview-modal { padding: 8px; }
      #topview-dialog { width: calc(100vw - 16px); height: calc(100vh - 16px); border-radius: 10px; }
      #topview-diagram { display: block; overflow: auto; }
      #inspection-drawing { height: 410px; margin-bottom: 16px; }
      #inspection-zoom-hint { display: none; }
      #inspection-checklist { overflow: visible; }
      #inspection-title h2 { font-size: 15px; }
    }
    @media (min-width: 701px) and (max-width: 980px) {
      #topview-diagram { grid-template-columns: minmax(250px, .82fr) minmax(384px, 1.18fr); grid-template-rows: minmax(0, 1fr); gap: 12px; padding: 12px; overflow: hidden; }
      #inspection-drawing { height: auto; }
      #inspection-checklist { overflow: hidden; }
    }
    #telemetry-hud {
      position: absolute;
      z-index: 5;
      top: 10px;
      right: 10px;
      width: min(142px, calc(50% - 14px));
      color: #263640;
      text-align: center;
      text-shadow: 0 1px 2px rgba(255, 255, 255, 0.78);
      pointer-events: none;
    }
    .telemetry-instrument {
      width: 100%;
      padding: 5px 6px;
      border-radius: 9px;
      background: transparent;
      cursor: default;
      pointer-events: none;
    }
    #speed-gauge { display: block; width: 100%; height: 71px; overflow: visible; }
    #speed-gauge .gauge-track { fill: none; stroke: rgba(179, 202, 216, 0.28); stroke-width: 8; stroke-linecap: round; }
    #speed-gauge .gauge-progress { fill: none; stroke: #23c6d8; stroke-width: 8; stroke-linecap: round; }
    #speed-gauge .gauge-needle { stroke: #f8c15c; stroke-width: 2.6; stroke-linecap: round; }
    #speed-gauge .gauge-hub { fill: #f8c15c; }
    #speed-gauge .gauge-tick { fill: #60717b; font: 8px system-ui, sans-serif; }
    .gauge-reading { margin-top: -3px; font-variant-numeric: tabular-nums; }
    #speed-value { color: #1e2b34; font-size: 22px; font-weight: 750; letter-spacing: -0.04em; }
    .gauge-unit { margin-left: 4px; color: #53646f; font-size: 9px; }
    .gauge-caption { color: #596a75; font-size: 8px; letter-spacing: 0.07em; text-transform: uppercase; }
    #power-reading {
      margin-top: 3px;
      font-variant-numeric: tabular-nums;
    }
    #power-value { display: block; margin-top: 2px; color: #1e2b34; font-size: 15px; font-weight: 750; }
    #power-raw-value { display: block; margin-top: 1px; color: #596a75; font-size: 8px; }
    /* Live digital twin: feed status, stop banner, state chips, map marker and debug-only controls. */
    #twin-status {
      position: absolute; z-index: 6; top: 94px; left: 14px;
      display: flex; align-items: center; gap: 7px; max-width: calc(100% - 200px);
      padding: 5px 11px 5px 9px; border: 1px solid #d6e0e6; border-radius: 999px;
      background: rgba(255, 255, 255, 0.92); color: #3c4d57;
      box-shadow: 0 3px 10px rgba(15, 35, 55, 0.08);
      font: 600 11px/1.2 system-ui, sans-serif; white-space: nowrap; pointer-events: none;
    }
    #twin-status-text { overflow: hidden; text-overflow: ellipsis; }
    .twin-dot { width: 8px; height: 8px; flex: 0 0 8px; border-radius: 50%; background: #9aa7af; }
    #twin-status.is-live .twin-dot { background: #16a34a; animation: twin-pulse 1.6s ease-in-out infinite; }
    #twin-status.is-paused .twin-dot { background: #d97706; }
    @keyframes twin-pulse { 0% { box-shadow: 0 0 0 0 rgba(22, 163, 74, 0.45); } 100% { box-shadow: 0 0 0 7px rgba(22, 163, 74, 0); } }
    #stop-banner {
      position: absolute; z-index: 6; top: 64px; left: 50%;
      display: flex; align-items: center; gap: 10px; max-width: min(520px, calc(100% - 320px));
      padding: 8px 15px 8px 9px; border-radius: 12px;
      background: #13314a; color: #ffffff; box-shadow: 0 8px 22px rgba(10, 30, 48, 0.28);
      font: 600 13px/1.25 system-ui, sans-serif; pointer-events: none;
      transform: translate(-50%, 0); transition: opacity 260ms ease, transform 260ms ease;
    }
    #stop-banner.is-hidden { opacity: 0; transform: translate(-50%, -10px); }
    #stop-banner-icon {
      width: 26px; height: 26px; flex: 0 0 26px; display: grid; place-items: center;
      border-radius: 50%; background: #f3c331; color: #13314a; font: 800 13px/1 system-ui, sans-serif;
    }
    #stop-banner small { display: block; color: #a9c4d8; font-size: 9px; font-weight: 700; letter-spacing: .1em; text-transform: uppercase; }
    #stop-banner-name { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    #twin-chips { display: grid; gap: 3px; margin-top: 7px; text-align: left; text-shadow: none; }
    .twin-chip {
      display: flex; justify-content: space-between; gap: 6px; padding: 3px 7px;
      border-radius: 7px; background: rgba(255, 255, 255, 0.82); color: #53646f;
      font: 600 9px/1.25 system-ui, sans-serif; transition: background-color 200ms ease, color 200ms ease;
    }
    .twin-chip strong { color: #1e2b34; font-weight: 750; }
    .twin-chip.is-active { background: #fff3d6; color: #8a5a00; }
    .twin-chip.is-active strong { color: #6d4700; }
    .twin-chip.is-alert { background: #fde4e4; color: #9b1c1c; }
    .twin-chip.is-alert strong { color: #7f1414; }
    .twin-chip.flash { animation: chip-flash 700ms ease; }
    @keyframes chip-flash { 0% { background: #c9ecff; } 100% { background: rgba(255, 255, 255, 0.82); } }
    /* Inferred operating state (rule-based state machine in the state bridge) and the ML slot. */
    #twin-state-card {
      --state-color: #8b99a2;
      position: absolute; z-index: 6; top: 126px; left: 14px; width: 236px; box-sizing: border-box;
      padding: 9px 11px 9px 12px; border: 1px solid #d6e0e6; border-left: 5px solid var(--state-color);
      border-radius: 12px; background: rgba(255, 255, 255, 0.94); color: #3c4d57;
      box-shadow: 0 6px 18px rgba(15, 35, 55, 0.10); font: 600 10px/1.3 system-ui, sans-serif;
      pointer-events: none; transition: border-color 260ms ease;
    }
    #twin-state-card small { display: block; color: #6b7a85; font-size: 8px; font-weight: 700; letter-spacing: .09em; text-transform: uppercase; }
    #twin-state-main { display: flex; align-items: center; gap: 8px; margin-top: 4px; }
    .twin-state-dot { width: 12px; height: 12px; flex: 0 0 12px; border-radius: 50%; background: var(--state-color); }
    #twin-state-card.is-live .twin-state-dot { animation: state-pulse 1.6s ease-in-out infinite; }
    @keyframes state-pulse { 0% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--state-color) 55%, transparent); } 100% { box-shadow: 0 0 0 8px transparent; } }
    #twin-state-value { color: #1e2b34; font-size: 21px; font-weight: 780; letter-spacing: -0.02em; }
    #twin-state-since { margin-left: auto; color: #6b7a85; font-size: 9px; font-variant-numeric: tabular-nums; white-space: nowrap; }
    #twin-state-tags { display: flex; flex-wrap: wrap; gap: 3px; margin-top: 6px; }
    #twin-state-tags span { padding: 2px 6px; border-radius: 999px; background: #eef3f6; color: #46525a; font-size: 9px; white-space: nowrap; }
    #twin-ml-row { display: flex; justify-content: space-between; gap: 6px; margin-top: 7px; padding-top: 6px; border-top: 1px dashed #d6e0e6; font-size: 9px; }
    #twin-ml-row span { color: #6b7a85; }
    #twin-ml-value { color: #8b99a2; font-weight: 700; }
    #twin-ml-row.is-ready #twin-ml-value { color: #1e2b34; }
    #twin-ml-row.is-alert #twin-ml-value { color: #b91c1c; }
    #twin-state-card.state-boarding { --state-color: #d97706; }
    #twin-state-card.state-cruising { --state-color: #0878bd; }
    #twin-state-card.state-regenerating { --state-color: #16a34a; }
    #twin-state-card.state-idling { --state-color: #6b7a85; }
    #twin-state-card.flash { animation: state-flash 900ms ease; }
    @keyframes state-flash { 0% { transform: scale(1.04); box-shadow: 0 0 0 4px color-mix(in srgb, var(--state-color) 35%, transparent); } 100% { transform: scale(1); } }
    .bus-marker { background: transparent; border: 0; }
    .stop-legend {
      display: flex; gap: 8px; padding: 3px 7px; border-radius: 7px;
      background: rgba(255, 255, 255, 0.9); color: #3c4d57; font: 600 9px/1.3 system-ui, sans-serif;
      box-shadow: 0 1px 5px rgba(15, 35, 55, 0.15);
    }
    .stop-legend span { display: inline-flex; align-items: center; gap: 4px; white-space: nowrap; }
    .stop-legend i { width: 8px; height: 8px; box-sizing: border-box; border: 2px solid #6b7a85; border-radius: 50%; background: #fff; }
    .stop-legend i.is-served { border-color: #16a34a; background: #16a34a; }
    .stop-legend i.is-skipped { border-color: #dc2626; }
    .bus-marker-body {
      width: 26px; height: 26px; display: grid; place-items: center; box-sizing: border-box;
      border: 3px solid #ffffff; border-radius: 50%; background: #0878bd;
      box-shadow: 0 2px 9px rgba(10, 30, 48, 0.45);
    }
    .bus-marker-body::before {
      content: ""; width: 0; height: 0; margin-top: -2px;
      border-left: 5px solid transparent; border-right: 5px solid transparent; border-bottom: 9px solid #ffffff;
    }
    .bus-marker.is-stopped .bus-marker-body { background: #d97706; }
    body:not(.debug-mode) .debug-only { display: none !important; }
    #debug-toggle {
      min-height: 34px; display: inline-flex; align-items: center; gap: 7px; padding: 0 12px 0 9px;
      border: 1px solid #d4dfe5; border-radius: 18px; color: #3c4d57; background: #ffffff;
      font: 600 12px system-ui, sans-serif; cursor: pointer;
    }
    #debug-toggle::before { content: ""; width: 9px; height: 9px; flex: 0 0 9px; border-radius: 50%; background: #c3cdd3; transition: background-color 160ms ease, box-shadow 160ms ease; }
    #debug-toggle[aria-pressed="true"] { border-color: #e0b44a; background: #fff8e6; color: #6d4700; }
    #debug-toggle[aria-pressed="true"]::before { background: #d48600; box-shadow: 0 0 0 3px rgba(212, 134, 0, 0.2); }
    #debug-toggle:hover { background: #f4f8fa; }
    #debug-extra { position: absolute; z-index: 5; right: 18px; bottom: 126px; display: flex; gap: 10px; }
    #debug-extra .passenger-button { background: #5b6b78; }
    #debug-extra .passenger-button:hover { background: #47555f; }
    #debug-note { position: absolute; z-index: 5; right: 18px; bottom: 176px; padding: 4px 9px; border-radius: 7px; background: #fff3d6; color: #6d4700; font: 600 10px/1.3 system-ui, sans-serif; }
    @media (max-width: 700px) {
      #twin-status { top: 70px; left: 8px; }
      #twin-state-card { top: 100px; left: 8px; width: 180px; }
      #stop-banner { top: 112px; max-width: calc(100% - 24px); }
    }
    @media (max-height: 600px) {
      #telemetry-hud { top: 8px; right: 8px; }
      #speed-gauge { height: 58px; }
    }
  </style>
  <script type="importmap">
    {"imports":{"three":"https://cdn.jsdelivr.net/npm/three@0.186.0/build/three.module.js","three/addons/":"https://cdn.jsdelivr.net/npm/three@0.186.0/examples/jsm/"}}
  </script>
</head>
<body>
  <main id="screen">
    <section id="map-pane" aria-label="Map picture-in-picture">
      <div id="map"></div>
      <button class="map-resize-handle map-resize-n" data-edge="n" type="button" tabindex="-1" aria-hidden="true"></button>
      <button class="map-resize-handle map-resize-s" data-edge="s" type="button" tabindex="-1" aria-hidden="true"></button>
      <button class="map-resize-handle map-resize-e" data-edge="e" type="button" tabindex="-1" aria-hidden="true"></button>
      <button class="map-resize-handle map-resize-w" data-edge="w" type="button" tabindex="-1" aria-hidden="true"></button>
      <button class="map-resize-handle map-resize-nw" data-edge="nw" type="button" aria-label="Resize map from the top-left corner" title="Drag to resize"></button>
      <button class="map-resize-handle map-resize-ne" data-edge="ne" type="button" aria-label="Resize map from the top-right corner" title="Drag to resize"></button>
      <button class="map-resize-handle map-resize-sw" data-edge="sw" type="button" aria-label="Resize map from the bottom-left corner" title="Drag to resize"></button>
      <button class="map-resize-handle map-resize-se" data-edge="se" type="button" aria-label="Resize map from the bottom-right corner" title="Drag to resize"></button>
    </section>
    <section id="model-pane">
      <div id="bus-model-static" class="bus-model-view"></div>
      <div id="bus-model-animation" class="bus-model-view" hidden></div>
      <div id="telemetry-overlay">
        <button id="topview-open" type="button" aria-label="Open bus top view and telemetry" title="View components and telemetry">
          __TOPVIEW_BUTTON_SVG__
        </button>
        <div id="telemetry-hud" aria-label="Speed and power">
          <div id="speedometer-widget" class="telemetry-instrument" aria-label="Speedometer">
            <div class="gauge-caption">Speed</div>
            <svg id="speed-gauge" viewBox="0 0 160 100" role="img" aria-label="Speedometer up to 60 kilometres per hour">
              <path class="gauge-track" d="M 19 79 A 61 61 0 0 1 141 79"></path>
              <path id="gauge-progress" class="gauge-progress" d="M 19 79 A 61 61 0 0 1 141 79"></path>
              <text class="gauge-tick" x="12" y="94">0</text>
              <text class="gauge-tick" x="137" y="94">60</text>
              <line id="gauge-needle" class="gauge-needle" x1="80" y1="79" x2="80" y2="29"></line>
              <circle class="gauge-hub" cx="80" cy="79" r="4"></circle>
            </svg>
            <div class="gauge-reading"><strong id="speed-value">—</strong><span class="gauge-unit">km/h</span></div>
          </div>
          <div id="power-reading" class="telemetry-instrument" aria-label="Power consumption">
            <div class="gauge-caption">Power consumption</div>
            <strong id="power-value">—</strong>
            <small id="power-raw-value"></small>
          </div>
          <div id="twin-chips" aria-label="Bus state">
            <div class="twin-chip" id="chip-passengers"><span>Passengers</span><strong>—</strong></div>
            <div class="twin-chip" id="chip-doors"><span>Doors</span><strong>—</strong></div>
            <div class="twin-chip" id="chip-brake"><span>Halt brake</span><strong>—</strong></div>
            <div class="twin-chip" id="chip-grid"><span>Overhead grid</span><strong>—</strong></div>
          </div>
        </div>
        <div id="twin-status" role="status"><span class="twin-dot" aria-hidden="true"></span><span id="twin-status-text">Connecting to the replay…</span></div>
        <div id="twin-state-card" role="status" aria-live="polite" aria-label="Inferred operating state">
          <small id="twin-state-source">State machine</small>
          <div id="twin-state-main"><span class="twin-state-dot" aria-hidden="true"></span><strong id="twin-state-value">—</strong><span id="twin-state-since"></span></div>
          <div id="twin-state-tags"></div>
          <div id="twin-ml-row"><span>ML prediction</span><strong id="twin-ml-value">Model not connected</strong></div>
        </div>
        <div id="stop-banner" class="is-hidden" role="status" aria-live="polite">
          <span id="stop-banner-icon" aria-hidden="true">H</span>
          <span><small id="stop-banner-label">Bus stop</small><span id="stop-banner-name"></span></span>
        </div>
      </div>
      <div id="debug-note" class="debug-only">Debug mode: buttons inject test messages into the twin</div>
      <div id="debug-extra" class="debug-only">
        <button id="debug-doors" class="passenger-button" type="button">Toggle doors</button>
        <button id="debug-grid" class="passenger-button" type="button">Toggle grid</button>
      </div>
      <button id="trip-toggle" class="debug-only" type="button">Start trip</button>
      <div id="passenger-controls" class="debug-only">
        <label id="passenger-count-label" for="passenger-count">Passengers</label>
        <input id="passenger-count" type="number" min="1" max="10" step="1" value="1">
        <button id="add-passenger" class="passenger-button" type="button">+1 passenger</button>
        <button id="remove-passenger" class="passenger-button" type="button">−1 passenger</button>
      </div>
    </section>
    <div id="topview-modal" hidden aria-hidden="true">
      <section id="topview-dialog" role="dialog" aria-modal="true" aria-label="Bus inspection sheet">
        <header id="topview-dialog-header">
          <div id="inspection-title">
            <p>DiTEC · reference reading</p>
            <h2>Bus inspection sheet</h2>
          </div>
          <div id="inspection-actions">
            <button id="debug-toggle" type="button" aria-pressed="false" title="Show the manual animation buttons">Debug mode</button>
            <button id="json-toggle" type="button" aria-label="View captured JSON">View JSON</button>
            <button id="topview-close" type="button" aria-label="Close">×</button>
          </div>
        </header>
        <div id="topview-diagram">
          <div id="inspection-drawing">
            <div id="inspection-scroller">
              __TOPVIEW_INSPECTION_SVG__
            </div>
            <div id="inspection-zoom" role="group" aria-label="Drawing zoom">
              <button id="inspection-zoom-out" type="button" aria-label="Zoom out" title="Zoom out">−</button>
              <button id="inspection-zoom-level" type="button" aria-label="Reset zoom to fit" title="Fit to panel">100%</button>
              <button id="inspection-zoom-in" type="button" aria-label="Zoom in" title="Zoom in">+</button>
            </div>
            <div id="inspection-zoom-hint">Ctrl/⌘ + scroll or pinch to zoom · drag to pan</div>
          </div>
          <aside id="inspection-checklist" aria-label="Component readings">
            <div class="inspection-meta" data-inspection-target="record-time"><span class="inspection-label" data-info-key="time_unix">Record</span><strong id="modal-time">—</strong></div>
            <section class="inspection-group" data-inspection-targets="wheel-rear-left wheel-rear-right wheel-middle-left wheel-middle-right wheel-front-left wheel-front-right">
              <h3 class="inspection-group-title"><span class="inspection-group-number">1–6</span> Wheels</h3>
              <div class="inspection-row" data-inspection-target="wheel-rear-left"><span class="inspection-label" data-info-key="odometry_wheelSpeed_rl">Rear left</span><strong id="modal-wheel-rl">—</strong></div>
              <div class="inspection-row" data-inspection-target="wheel-rear-right"><span class="inspection-label" data-info-key="odometry_wheelSpeed_rr">Rear right</span><strong id="modal-wheel-rr">—</strong></div>
              <div class="inspection-row" data-inspection-target="wheel-middle-left"><span class="inspection-label" data-info-key="odometry_wheelSpeed_ml">Middle left</span><strong id="modal-wheel-ml">—</strong></div>
              <div class="inspection-row" data-inspection-target="wheel-middle-right"><span class="inspection-label" data-info-key="odometry_wheelSpeed_mr">Middle right</span><strong id="modal-wheel-mr">—</strong></div>
              <div class="inspection-row" data-inspection-target="wheel-front-left"><span class="inspection-label" data-info-key="odometry_wheelSpeed_fl">Front left</span><strong id="modal-wheel-fl">—</strong></div>
              <div class="inspection-row" data-inspection-target="wheel-front-right"><span class="inspection-label" data-info-key="odometry_wheelSpeed_fr">Front right</span><strong id="modal-wheel-fr">—</strong></div>
            </section>
            <section class="inspection-group" data-inspection-targets="steering-angle vehicle-speed articulation-angle">
              <h3 class="inspection-group-title"><span class="inspection-group-number">7</span> Axles and motion</h3>
              <div class="inspection-row" data-inspection-target="steering-angle"><span class="inspection-label" data-info-key="odometry_steeringAngle">Steering angle</span><strong id="modal-steering">—</strong></div>
              <div class="inspection-row" data-inspection-target="vehicle-speed"><span class="inspection-label" data-info-key="odometry_vehicleSpeed">Vehicle speed</span><strong id="modal-vehicle-speed">—</strong></div>
              <div class="inspection-row" data-inspection-target="articulation-angle"><span class="inspection-label" data-info-key="odometry_articulationAngle">Articulation angle</span><strong id="modal-articulation">—</strong></div>
            </section>
            <section class="inspection-group" data-inspection-targets="brake-pressure brake-line-pressure holding-brake parking-brake traction-force">
              <h3 class="inspection-group-title"><span class="inspection-group-number">8</span> Braking</h3>
              <div class="inspection-row" data-inspection-target="brake-pressure"><span class="inspection-label" data-info-key="traction_brakePressure">Brake pressure</span><strong id="modal-brake-pressure">—</strong></div>
              <div class="inspection-row" data-inspection-target="brake-line-pressure"><span class="inspection-label" data-info-key="brake_bar">Brake line pressure</span><strong id="modal-brake-bar">—</strong></div>
              <div class="inspection-row" data-inspection-target="holding-brake"><span class="inspection-label" data-info-key="status_haltBrakeIsActive">Holding brake</span><strong id="modal-halt-brake">—</strong></div>
              <div class="inspection-row" data-inspection-target="parking-brake"><span class="inspection-label" data-info-key="status_parkBrakeIsActive">Parking brake</span><strong id="modal-park-brake">—</strong></div>
              <div class="inspection-row" data-inspection-target="traction-force"><span class="inspection-label" data-info-key="traction_tractionForce">Traction force</span><strong id="modal-traction">—</strong></div>
            </section>
            <section class="inspection-group" data-inspection-targets="door-status passengers route stop">
              <h3 class="inspection-group-title"><span class="inspection-group-number">9</span> Doors and cabin</h3>
              <div class="inspection-row" data-inspection-target="door-status"><span class="inspection-label" data-info-key="status_doorIsOpen">Door status</span><strong id="modal-door">—</strong></div>
              <div class="inspection-row" data-inspection-target="passengers"><span class="inspection-label" data-info-key="itcs_numberOfPassengers">Passengers</span><strong id="modal-passengers">—</strong></div>
              <div class="inspection-row" data-inspection-target="route"><span class="inspection-label" data-info-key="itcs_busRoute">Route</span><strong id="modal-route">—</strong></div>
              <div class="inspection-row" data-inspection-target="stop"><span class="inspection-label" data-info-key="itcs_stopName">Stop</span><strong id="modal-stop">—</strong></div>
            </section>
            <section class="inspection-group" data-inspection-targets="gnss-altitude latitude longitude heading ambient-temperature grid-connection">
              <h3 class="inspection-group-title"><span class="inspection-group-number">10</span> Roof and location</h3>
              <div class="inspection-row" data-inspection-target="gnss-altitude"><span class="inspection-label" data-info-key="gnss_altitude">GNSS altitude</span><strong id="modal-altitude">—</strong></div>
              <div class="inspection-row" data-inspection-target="latitude"><span class="inspection-label" data-info-key="latitude">Latitude</span><strong id="modal-latitude">—</strong></div>
              <div class="inspection-row" data-inspection-target="longitude"><span class="inspection-label" data-info-key="longitude">Longitude</span><strong id="modal-longitude">—</strong></div>
              <div class="inspection-row" data-inspection-target="heading"><span class="inspection-label" data-info-key="heading_deg">Heading</span><strong id="modal-heading">—</strong></div>
              <div class="inspection-row" data-inspection-target="ambient-temperature"><span class="inspection-label" data-info-key="temp_celsius">Ambient temperature</span><strong id="modal-temperature">—</strong></div>
              <div class="inspection-row" data-inspection-target="grid-connection"><span class="inspection-label" data-info-key="status_gridIsAvailable">Grid connection</span><strong id="modal-grid">—</strong></div>
            </section>
          </aside>
        </div>
        <section id="captured-json-view" hidden aria-label="Captured JSON">
          <pre id="captured-json" tabindex="0"></pre>
        </section>
      </section>
      <div id="parameter-info-tooltip" hidden role="group" aria-labelledby="parameter-info-title">
        <header id="parameter-info-header">
          <div>
            <p>DATASET PARAMETER</p>
            <h3 id="parameter-info-title"></h3>
          </div>
          <button id="parameter-info-close" type="button" aria-label="Close parameter information">×</button>
        </header>
        <button id="parameter-info-key" type="button" aria-label="Show this field in captured JSON"></button>
        <p id="parameter-info-description"></p>
        <p id="parameter-info-mapping" hidden></p>
        <p id="parameter-info-source"></p>
      </div>
    </div>
  </main>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
          integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
  <script>
    const route = __ROUTE_JSON__;
    const telemetry = __TELEMETRY_JSON__;
    // Stops of the replayed mission in route order: {name, lat, lon, d (metres along the route), served}.
    const stopVisits = __STOPS_JSON__;
    const latLngs = route.map(([longitude, latitude]) => [latitude, longitude]);
    const routeBounds = L.latLngBounds(latLngs);
    const allowedBounds = routeBounds.pad(0.12);
    const map = L.map('map', {
      zoomControl: false,
      scrollWheelZoom: true,
      maxBounds: allowedBounds,
      maxBoundsViscosity: 1,
      maxZoom: 19
    });
    map.attributionControl.setPrefix(false);
    map.attributionControl.setPosition('bottomleft');
    L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}', {
      maxZoom: 19,
      maxNativeZoom: 16,
      attribution: 'Tiles &copy; Esri, HERE, Garmin, &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, and the GIS user community'
    }).addTo(map);
    L.polyline(latLngs, { color: '#0878bd', weight: 6, opacity: 0.96, lineCap: 'round', lineJoin: 'round' }).addTo(map);
    L.circleMarker(latLngs[0], { radius: 6, color: '#ffffff', weight: 2, fillColor: '#0d9488', fillOpacity: 1 }).addTo(map);
    L.circleMarker(latLngs[latLngs.length - 1], { radius: 6, color: '#ffffff', weight: 2, fillColor: '#ea580c', fillOpacity: 1 }).addTo(map);
    map.fitBounds(routeBounds, { padding: [32, 32] });
    map.setMinZoom(map.getZoom());
    map.setMaxBounds(allowedBounds);
    requestAnimationFrame(() => map.invalidateSize({ pan: false }));

    // Live bus marker: the twin sets a target position, and the marker glides there every frame.
    const busMarkerIcon = L.divIcon({ className: 'bus-marker', html: '<div class="bus-marker-body"></div>', iconSize: [26, 26], iconAnchor: [13, 13] });
    const busMarker = L.marker(latLngs[0], { icon: busMarkerIcon, keyboard: false, interactive: false, zIndexOffset: 1000 });
    const busTrail = L.polyline([], { color: '#0b3f63', weight: 4, opacity: 0.9, lineCap: 'round', lineJoin: 'round' });
    const mapTwin = { target: null, shown: null, heading: 0, stopped: false };
    window.twinMap = {
      reset() {
        busTrail.setLatLngs([]);
        mapTwin.target = null;
        mapTwin.shown = null;
        busMarker.remove();
        busTrail.remove();
      },
      showHistory(timeUnix) {
        // Everything the bus already drove in this replay, from the timed route points.
        if (!Number.isFinite(timeUnix)) return;
        const travelled = route.filter(point => point.length > 2 && point[2] <= timeUnix).map(([longitude, latitude]) => [latitude, longitude]);
        if (!travelled.length) return;
        busTrail.setLatLngs(travelled);
        if (!map.hasLayer(busTrail)) busTrail.addTo(map);
      },
      setStopStatus(visitIndex, status) {
        const visit = stopVisits[visitIndex];
        const mapStop = visit && mapStops[visit.mapStop];
        if (!mapStop) return;
        mapStop.marker.setStyle(stopStyles[status] || stopStyles.ahead);
        const label = status === 'served' ? 'bus stopped' : status === 'skipped' ? 'bus did not stop' : 'ahead';
        mapStop.marker.setTooltipContent(`${mapStop.name} · ${label}`);
      },
      setPosition(latitude, longitude, heading, stopped) {
        if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return;
        mapTwin.target = [latitude, longitude];
        if (Number.isFinite(heading)) mapTwin.heading = heading;
        mapTwin.stopped = Boolean(stopped);
        if (!mapTwin.shown) {
          mapTwin.shown = [latitude, longitude];
          busTrail.addTo(map);
          busMarker.setLatLng(mapTwin.shown).addTo(map);
        }
        const trail = busTrail.getLatLngs();
        const last = trail[trail.length - 1];
        if (!last || last.lat !== latitude || last.lng !== longitude) busTrail.addLatLng([latitude, longitude]);
      },
      step(amount) {
        if (!mapTwin.target || !mapTwin.shown) return;
        const [targetLat, targetLng] = mapTwin.target;
        const jump = Math.abs(targetLat - mapTwin.shown[0]) + Math.abs(targetLng - mapTwin.shown[1]);
        const blend = jump > 0.01 ? 1 : amount;  // Teleport after a resync instead of sliding across the city.
        mapTwin.shown = [mapTwin.shown[0] + (targetLat - mapTwin.shown[0]) * blend, mapTwin.shown[1] + (targetLng - mapTwin.shown[1]) * blend];
        busMarker.setLatLng(mapTwin.shown);
        const element = busMarker.getElement();
        if (element) {
          element.classList.toggle('is-stopped', mapTwin.stopped);
          const body = element.firstElementChild;
          if (body) body.style.transform = `rotate(${mapTwin.heading}deg)`;
        }
        if (!map.getBounds().pad(-0.15).contains(mapTwin.shown)) map.panTo(mapTwin.shown, { animate: true, duration: 0.6 });
      }
    };

    // Bus stops: grey until the bus reaches them, then green if it stopped there or red if it did not.
    const stopPane = map.createPane('stops');
    stopPane.style.zIndex = 450;  // above the route and trail, below the bus marker
    const stopStyles = {
      ahead: { radius: 4.5, weight: 2, color: '#6b7a85', fillColor: '#ffffff', fillOpacity: 1 },
      served: { radius: 5, weight: 2, color: '#ffffff', fillColor: '#16a34a', fillOpacity: 1 },
      skipped: { radius: 5, weight: 2.5, color: '#dc2626', fillColor: '#ffffff', fillOpacity: 1 }
    };
    const mapStops = [];
    stopVisits.forEach(visit => {
      // Both directions of the line share stop names; visits within 80 m of each other are one marker.
      let index = mapStops.findIndex(stop => stop.name === visit.name && map.distance([stop.lat, stop.lon], [visit.lat, visit.lon]) < 80);
      if (index < 0) {
        const marker = L.circleMarker([visit.lat, visit.lon], { ...stopStyles.ahead, pane: 'stops' })
          .bindTooltip(`${visit.name} · ahead`, { direction: 'top', offset: [0, -4] })
          .addTo(map);
        mapStops.push({ name: visit.name, lat: visit.lat, lon: visit.lon, marker });
        index = mapStops.length - 1;
      }
      visit.mapStop = index;
    });
    const stopLegend = L.control({ position: 'topright' });
    stopLegend.onAdd = () => {
      const legend = L.DomUtil.create('div', 'stop-legend');
      legend.innerHTML = '<span><i class="is-served"></i>Stopped</span><span><i class="is-skipped"></i>Not stopped</span><span><i></i>Ahead</span>';
      return legend;
    };
    if (stopVisits.length) stopLegend.addTo(map);
  </script>
  <script type="module">
    import * as THREE from 'three';
    import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

    const staticHost = document.getElementById('bus-model-static');
    const animationHost = document.getElementById('bus-model-animation');
    const scene = new THREE.Scene();
    const animationScene = new THREE.Scene();

    function addEnvironment(targetScene) {
      targetScene.add(new THREE.HemisphereLight(0xeaf4ff, 0x697584, 2.05));
      const keyLight = new THREE.DirectionalLight(0xfff7ed, 3.1);
      keyLight.position.set(7, 13, 9);
      keyLight.castShadow = true;
      keyLight.shadow.mapSize.set(2048, 2048);
      keyLight.shadow.camera.left = -19;
      keyLight.shadow.camera.right = 15;
      keyLight.shadow.camera.top = 12;
      keyLight.shadow.camera.bottom = -8;
      keyLight.shadow.bias = -0.0002;
      targetScene.add(keyLight);
      const fillLight = new THREE.DirectionalLight(0xc6e3ff, 1.3);
      fillLight.position.set(-9, 6, -7);
      targetScene.add(fillLight);

      const floor = new THREE.Mesh(
        new THREE.PlaneGeometry(100, 100),
        new THREE.MeshStandardMaterial({ color: 0xeaf0f6, roughness: 0.94 })
      );
      floor.rotation.x = -Math.PI / 2;
      floor.position.y = -0.035;
      floor.receiveShadow = true;
      floor.name = 'ground-floor';
      targetScene.add(floor);
    }

    addEnvironment(scene);
    addEnvironment(animationScene);

    const bus = new THREE.Group();
    scene.add(bus);
    bus.position.z = 1.7;

    const paintBlue = new THREE.MeshPhysicalMaterial({ color: 0x1685ed, roughness: 0.33, metalness: 0.08, clearcoat: 0.3 });
    const paintWhite = new THREE.MeshPhysicalMaterial({ color: 0xf5f6f4, roughness: 0.38, metalness: 0.02, clearcoat: 0.2 });
    const roofBlack = new THREE.MeshStandardMaterial({ color: 0x15191d, roughness: 0.3, metalness: 0.18 });
    const windowBlack = new THREE.MeshStandardMaterial({ color: 0x111b24, roughness: 0.21, metalness: 0.24 });
    const tintedGlass = new THREE.MeshPhysicalMaterial({ color: 0x263d50, roughness: 0.17, metalness: 0.22, transparent: true, opacity: 0.48, clearcoat: 0.8, side: THREE.DoubleSide, depthWrite: false });
    const rubber = new THREE.MeshStandardMaterial({ color: 0x17191b, roughness: 0.87 });
    const rimMetal = new THREE.MeshStandardMaterial({ color: 0xaeb5ba, roughness: 0.31, metalness: 0.72 });
    const darkMetal = new THREE.MeshStandardMaterial({ color: 0x30373b, roughness: 0.47, metalness: 0.58 });
    const yellow = new THREE.MeshStandardMaterial({ color: 0xf3c331, roughness: 0.55 });
    const seatBlue = new THREE.MeshStandardMaterial({ color: 0x1768aa, roughness: 0.72 });
    const passengerCoat = new THREE.MeshStandardMaterial({ color: 0xe58a36, roughness: 0.72 });
    const passengerPants = new THREE.MeshStandardMaterial({ color: 0x26333e, roughness: 0.82 });
    const passengerSkin = new THREE.MeshStandardMaterial({ color: 0xc58d6c, roughness: 0.85 });
    const passengerHair = new THREE.MeshStandardMaterial({ color: 0x342a24, roughness: 0.9 });
    const passengerShoes = new THREE.MeshStandardMaterial({ color: 0x17191b, roughness: 0.9 });
    const asphalt = new THREE.MeshStandardMaterial({ color: 0x24282d, roughness: 0.96 });
    const roadPaint = new THREE.MeshStandardMaterial({ color: 0xe4e5df, roughness: 0.82 });
    const shelterGlass = new THREE.MeshPhysicalMaterial({ color: 0x9edbe9, roughness: 0.16, metalness: 0.05, transparent: true, opacity: 0.28, clearcoat: 0.45, side: THREE.DoubleSide, depthWrite: false });
    const shelterFrame = new THREE.MeshStandardMaterial({ color: 0x27333a, roughness: 0.48, metalness: 0.55 });
    const shelterRoof = new THREE.MeshStandardMaterial({ color: 0x313b42, roughness: 0.42, metalness: 0.35 });
    const benchWood = new THREE.MeshStandardMaterial({ color: 0x97643c, roughness: 0.76 });
    const lightMaterial = new THREE.MeshStandardMaterial({ color: 0xf4f5f2, emissive: 0x8da3b4, emissiveIntensity: 0.25, roughness: 0.24 });
    const redLight = new THREE.MeshStandardMaterial({ color: 0xd92531, emissive: 0x701018, emissiveIntensity: 0.3, roughness: 0.28 });

    function addMesh(parent, geometry, material, position, options = {}) {
      const mesh = new THREE.Mesh(geometry, material);
      mesh.position.set(position[0], position[1], position[2]);
      mesh.castShadow = options.castShadow !== false;
      mesh.receiveShadow = options.receiveShadow !== false;
      if (options.rotation) mesh.rotation.set(...options.rotation);
      parent.add(mesh);
      return mesh;
    }

    function box(parent, size, position, material, options = {}) {
      return addMesh(parent, new THREE.BoxGeometry(size[0], size[1], size[2]), material, position, options);
    }

    function rodBetween(parent, start, end, radius, material) {
      const a = new THREE.Vector3(...start);
      const b = new THREE.Vector3(...end);
      const direction = new THREE.Vector3().subVectors(b, a);
      const mesh = new THREE.Mesh(new THREE.CylinderGeometry(radius, radius, direction.length(), 10), material);
      mesh.position.copy(a).add(b).multiplyScalar(0.5);
      mesh.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), direction.normalize());
      mesh.castShadow = true;
      parent.add(mesh);
      return mesh;
    }

    const roadSegmentLength = 14;
    // Trolleybus overhead line: two contact wires above the bus lane (bus.position.z = 1.7).
    const overheadWireHeight = 5.7;
    const overheadWireRadius = 0.012;
    const overheadWireSpacing = 0.64;
    const overheadWireMaterial = new THREE.MeshStandardMaterial({ color: 0x4a3a2c, roughness: 0.42, metalness: 0.7 });
    let overheadLinePowered = true;  // new road segments get wires only while the twin has grid power
    function createRoadSegment(targetScene, centerX) {
      const segment = new THREE.Group();
      segment.position.x = centerX;
      const road = new THREE.Mesh(new THREE.PlaneGeometry(roadSegmentLength, 6.8), asphalt);
      road.rotation.x = -Math.PI / 2;
      road.position.y = -0.025;
      road.receiveShadow = true;
      segment.add(road);
      for (let i = 0; i < 5; i += 1) {
        box(segment, [1.15, 0.006, 0.12], [-5.6 + i * 2.8, -0.02, 0], roadPaint, { castShadow: false, receiveShadow: false });
      }
      const laneZ = 1.7;
      // Wires, mast and brackets form one group the twin hides where the bus has no overhead power.
      const overhead = new THREE.Group();
      overhead.name = 'overhead-line';
      overhead.visible = overheadLinePowered;
      segment.add(overhead);
      for (const offset of [-1, 1]) {
        const wire = new THREE.Mesh(new THREE.CylinderGeometry(overheadWireRadius, overheadWireRadius, roadSegmentLength, 5), overheadWireMaterial);
        wire.rotation.z = Math.PI / 2;
        wire.position.set(0, overheadWireHeight, laneZ + offset * overheadWireSpacing / 2);
        overhead.add(wire);
      }
      // One side-pole bracket per segment carries both wires across the lane.
      const mastX = -5.5;
      const mastZ = -3.85;
      const armY = overheadWireHeight + 0.55;
      addMesh(overhead, new THREE.CylinderGeometry(0.075, 0.11, armY + 0.4, 10), shelterFrame, [mastX, (armY + 0.4) / 2 - 0.03, mastZ]);
      rodBetween(overhead, [mastX, armY, mastZ], [mastX, armY, laneZ + 0.75], 0.035, shelterFrame);
      rodBetween(overhead, [mastX, armY - 0.8, mastZ], [mastX, armY, mastZ + 2.6], 0.022, shelterFrame);
      for (const offset of [-1, 1]) {
        const wireZ = laneZ + offset * overheadWireSpacing / 2;
        rodBetween(overhead, [mastX, armY, wireZ], [mastX, overheadWireHeight, wireZ], 0.01, shelterFrame);
        addMesh(overhead, new THREE.CylinderGeometry(0.03, 0.03, 0.16, 8), shelterRoof, [mastX, armY - 0.14, wireZ], { castShadow: false });
      }
      targetScene.add(segment);
      return segment;
    }

    function addRoadAndBusStop(targetScene) {
      const roadSegments = [-14, 0, 14].map(centerX => createRoadSegment(targetScene, centerX));
      const shelter = new THREE.Group();
      // Keep the shelter centered between both doors while leaving a clear aisle beside the bus.
      shelter.position.set(2.065, 0, 0.75);
      targetScene.add(shelter);
      const glassZ = 4.25;
      for (const x of [-1.065, 0, 1.065]) {
        box(shelter, [0.99, 1.84, 0.035], [x, 0.99, glassZ], shelterGlass, { castShadow: false });
      }
      for (const xOffset of [-1.62, -0.53, 0.53, 1.62]) {
        box(shelter, [0.055, 1.9, 0.055], [xOffset, 0.925, glassZ], shelterFrame);
      }
      for (const xOffset of [-1.62, 1.62]) {
        box(shelter, [0.055, 1.9, 0.055], [xOffset, 0.925, 3.45], shelterFrame);
      }
      box(shelter, [3.7, 0.14, 1.85], [0, 2.02, 3.82], shelterRoof);

      // A plain slatted bench facing the passenger doors.
      box(shelter, [2.25, 0.09, 0.44], [0, 0.56, 3.55], benchWood);
      box(shelter, [2.25, 0.42, 0.09], [0, 0.91, 3.77], benchWood);
      for (const xOffset of [-0.82, 0.82]) {
        box(shelter, [0.07, 0.53, 0.07], [xOffset, 0.245, 3.55], shelterFrame);
        box(shelter, [0.06, 0.44, 0.06], [xOffset, 0.76, 3.72], shelterFrame);
      }
      return { roadSegments, shelter };
    }

    const staticEnvironment = addRoadAndBusStop(scene);
    const animationEnvironment = addRoadAndBusStop(animationScene);
    const animationRoadSegments = animationEnvironment.roadSegments;
    const movingShelter = animationEnvironment.shelter;

    // ------------------------------------------------------------------------
    // Articulated trolleybus (Hess lighTram 19-style, VBZ bus B208, route 33).
    // Local frame: front points to +X, Y is up, passenger doors face +Z, ground
    // at y = 0. The front section hangs directly off `bus`; the trailer, its
    // wheels and doors hang off `articulation-rear`, whose origin is the joint
    // centre, so rotating it about Y bends the bus.
    // ------------------------------------------------------------------------
    const roofTelemetryUnit = new THREE.Group();
    {
      const wheelRadius = 0.5;
      const halfWidth = 1.25;
      const sillY = 0.3;
      const wallTopY = 2.78;
      const roofTopY = 3.1;
      const floorY = 0.42;
      const archRadius = 0.64;
      const wheelTrack = 1.045;
      const doorWidth = 1.25;
      const doorBottomY = 0.36;
      const doorTopY = 2.58;
      const frontX = 5.5;
      const rearX = -13.2;
      const noseRadius = 0.45;
      const tailRadius = 0.3;
      const frontAxleX = 2.7;
      const middleAxleX = -3.2;
      const rearAxleX = -10.3;
      const jointX = -4.75;
      const frontSectionEndX = -4.3;
      const trailerStartX = -5.2;
      const frontDoorX = 4.28;
      const alightDoorX = -0.15;

      bus.userData.length = Number((frontX - rearX).toFixed(2));
      bus.userData.wheelRadius = wheelRadius;
      bus.userData.frontDoorX = frontDoorX;
      bus.userData.alightDoorX = alightDoorX;
      bus.userData.floorHeight = floorY;
      bus.userData.doorWidth = doorWidth;
      bus.userData.frontX = frontX;
      bus.userData.rearX = rearX;
      bus.userData.articulationX = jointX;
      bus.userData.axleX = { front: frontAxleX, middle: middleAxleX, rear: rearAxleX };

      const roofEquipment = new THREE.MeshStandardMaterial({ color: 0xdde2e6, roughness: 0.46, metalness: 0.12 });
      const ventDark = new THREE.MeshStandardMaterial({ color: 0x3a4248, roughness: 0.6, metalness: 0.35 });
      const bellowsCore = new THREE.MeshStandardMaterial({ color: 0x2a2f34, roughness: 0.88 });
      const bellowsRib = new THREE.MeshStandardMaterial({ color: 0x3e454b, roughness: 0.78 });
      const wheelWell = new THREE.MeshStandardMaterial({ color: 0x14171a, roughness: 0.92, side: THREE.DoubleSide });
      const cabinFloor = new THREE.MeshStandardMaterial({ color: 0x59616a, roughness: 0.86 });
      const poleMaterial = new THREE.MeshStandardMaterial({ color: 0x23282d, roughness: 0.4, metalness: 0.35 });
      const insulatorMaterial = new THREE.MeshStandardMaterial({ color: 0x8c3f2b, roughness: 0.45 });
      const carbonMaterial = new THREE.MeshStandardMaterial({ color: 0x0f1113, roughness: 0.65 });
      const plateMaterial = new THREE.MeshStandardMaterial({ color: 0xf7f7f2, roughness: 0.5 });
      const glassHighlight = new THREE.MeshBasicMaterial({ color: 0x9cc7e6, transparent: true, opacity: 0.2 });
      const amberLamp = intensity => new THREE.MeshStandardMaterial({ color: 0xffa21f, emissive: 0xff7a00, emissiveIntensity: intensity, roughness: 0.3 });
      // One material per side so `indicator-left` lights its front, side and rear lamps together.
      const indicatorMaterials = { left: amberLamp(0.12), right: amberLamp(0.12) };
      const sideMarker = amberLamp(0.35);

      // Vertical stripes give the articulation fold a pleated look.
      const bellowsCanvas = document.createElement('canvas');
      bellowsCanvas.width = 32;
      bellowsCanvas.height = 2;
      const bellowsContext = bellowsCanvas.getContext('2d');
      bellowsContext.fillStyle = '#25292e';
      bellowsContext.fillRect(0, 0, 32, 2);
      bellowsContext.fillStyle = '#495057';
      bellowsContext.fillRect(0, 0, 12, 2);
      const bellowsTexture = new THREE.CanvasTexture(bellowsCanvas);
      bellowsTexture.wrapS = THREE.RepeatWrapping;
      bellowsTexture.repeat.set(84, 1);
      bellowsTexture.colorSpace = THREE.SRGBColorSpace;
      const bellowsFold = new THREE.MeshStandardMaterial({ map: bellowsTexture, roughness: 0.85 });

      function destinationMaterial(label, width, height) {
        const canvas = document.createElement('canvas');
        canvas.width = width;
        canvas.height = height;
        const context = canvas.getContext('2d');
        context.fillStyle = '#07090b';
        context.fillRect(0, 0, width, height);
        context.fillStyle = '#ffb21e';
        context.font = `700 ${Math.round(height * 0.74)}px "Helvetica Neue", Arial, sans-serif`;
        context.textAlign = 'center';
        context.textBaseline = 'middle';
        context.fillText(label, width / 2, height * 0.54);
        context.fillStyle = 'rgba(7, 9, 11, 0.5)';
        for (let x = 0; x < width; x += 5) context.fillRect(x, 0, 1.5, height);
        for (let y = 0; y < height; y += 5) context.fillRect(0, y, width, 1.5);
        const texture = new THREE.CanvasTexture(canvas);
        texture.colorSpace = THREE.SRGBColorSpace;
        texture.anisotropy = 4;
        return new THREE.MeshBasicMaterial({ map: texture });
      }

      function subtractRanges(start, end, cuts) {
        const result = [];
        let cursor = start;
        for (const [a, b] of [...cuts].sort((p, q) => p[0] - q[0])) {
          if (b <= cursor || a >= end) continue;
          if (a > cursor) result.push([cursor, Math.min(a, end)]);
          cursor = Math.max(cursor, b);
        }
        if (cursor < end) result.push([cursor, end]);
        return result.filter(([a, b]) => b - a > 0.02);
      }

      // Plan-view outline (X along the bus, second axis across it) with rounded nose/tail corners.
      function planOutline(x0, x1, halfSpan, frontCorner, rearCorner) {
        const shape = new THREE.Shape();
        shape.moveTo(x0 + rearCorner, -halfSpan);
        shape.lineTo(x1 - frontCorner, -halfSpan);
        if (frontCorner > 0) shape.absarc(x1 - frontCorner, -halfSpan + frontCorner, frontCorner, -Math.PI / 2, 0, false);
        shape.lineTo(x1, halfSpan - frontCorner);
        if (frontCorner > 0) shape.absarc(x1 - frontCorner, halfSpan - frontCorner, frontCorner, 0, Math.PI / 2, false);
        shape.lineTo(x0 + rearCorner, halfSpan);
        if (rearCorner > 0) shape.absarc(x0 + rearCorner, halfSpan - rearCorner, rearCorner, Math.PI / 2, Math.PI, false);
        shape.lineTo(x0, -halfSpan + rearCorner);
        if (rearCorner > 0) shape.absarc(x0 + rearCorner, -halfSpan + rearCorner, rearCorner, Math.PI, Math.PI * 1.5, false);
        shape.closePath();
        return shape;
      }

      // Extrudes a plan outline upwards from y0 to y1; `bevel` rounds the top (and hidden bottom) edges.
      function planExtrude(parent, shape, y0, y1, material, options = {}) {
        const bevel = options.bevel || 0;
        const depth = Math.max(0.001, y1 - y0 - 2 * bevel);
        const geometry = new THREE.ExtrudeGeometry(shape, bevel > 0
          ? { depth, bevelEnabled: true, bevelThickness: bevel, bevelSize: bevel, bevelOffset: -bevel, bevelSegments: 4, curveSegments: options.curveSegments || 10 }
          : { depth, bevelEnabled: false, curveSegments: options.curveSegments || 10 });
        geometry.rotateX(-Math.PI / 2);
        geometry.translate(0, y0 + bevel, 0);
        return addMesh(parent, geometry, material, [0, 0, 0], options);
      }

      // Cross-section (Z across, Y up) with rounded top corners, extruded along X.
      function crossSection(halfSpan, y0, y1, topRadius, bottomRadius) {
        const shape = new THREE.Shape();
        shape.moveTo(-halfSpan + bottomRadius, y0);
        shape.lineTo(halfSpan - bottomRadius, y0);
        shape.quadraticCurveTo(halfSpan, y0, halfSpan, y0 + bottomRadius);
        shape.lineTo(halfSpan, y1 - topRadius);
        shape.quadraticCurveTo(halfSpan, y1, halfSpan - topRadius, y1);
        shape.lineTo(-halfSpan + topRadius, y1);
        shape.quadraticCurveTo(-halfSpan, y1, -halfSpan, y1 - topRadius);
        shape.lineTo(-halfSpan, y0 + bottomRadius);
        shape.quadraticCurveTo(-halfSpan, y0, -halfSpan + bottomRadius, y0);
        return shape;
      }

      function sectionExtrude(parent, shape, x0, x1, material, options = {}) {
        const geometry = new THREE.ExtrudeGeometry(shape, { depth: x1 - x0, bevelEnabled: false, curveSegments: 6 });
        geometry.rotateY(Math.PI / 2);
        geometry.translate(x0, 0, 0);
        return addMesh(parent, geometry, material, [0, 0, 0], options);
      }

      // Side-wall profile with wheel arches cut from the sill and optional door holes.
      function sideWallShape(x0, x1, arches, doors) {
        const shape = new THREE.Shape();
        shape.moveTo(x0, sillY);
        for (const x of [...arches].sort((a, b) => a - b)) {
          shape.lineTo(x - archRadius, sillY);
          shape.lineTo(x - archRadius, wheelRadius);
          shape.absarc(x, wheelRadius, archRadius, Math.PI, 0, true);
          shape.lineTo(x + archRadius, sillY);
        }
        shape.lineTo(x1, sillY);
        shape.lineTo(x1, wallTopY);
        shape.lineTo(x0, wallTopY);
        shape.closePath();
        for (const [a, b] of doors) {
          const hole = new THREE.Path();
          hole.moveTo(a, doorBottomY);
          hole.lineTo(b, doorBottomY);
          hole.lineTo(b, doorTopY);
          hole.lineTo(a, doorTopY);
          hole.closePath();
          shape.holes.push(hole);
        }
        return shape;
      }

      function roundedRectShape(width, height, radius) {
        const halfW = width / 2;
        const halfH = height / 2;
        const shape = new THREE.Shape();
        shape.moveTo(-halfW + radius, -halfH);
        shape.lineTo(halfW - radius, -halfH);
        shape.quadraticCurveTo(halfW, -halfH, halfW, -halfH + radius);
        shape.lineTo(halfW, halfH - radius);
        shape.quadraticCurveTo(halfW, halfH, halfW - radius, halfH);
        shape.lineTo(-halfW + radius, halfH);
        shape.quadraticCurveTo(-halfW, halfH, -halfW, halfH - radius);
        shape.lineTo(-halfW, -halfH + radius);
        shape.quadraticCurveTo(-halfW, -halfH, -halfW + radius, -halfH);
        return shape;
      }

      // Rounded roof pod (HVAC, converter, battery) sitting on the roof at y0.
      function roofPod(parent, x0, x1, width, y0, height, material) {
        const pod = planExtrude(parent, planOutline(x0, x1, width / 2, Math.min(0.35, width / 4), Math.min(0.35, width / 4)), y0, y0 + height, material, { bevel: 0.07, curveSegments: 8 });
        return pod;
      }

      function addDoor(parent, spec) {
        const x = spec.x;
        const doorHeight = doorTopY - doorBottomY;
        const midY = (doorTopY + doorBottomY) / 2;
        // Dark interior shown while the leaves are shut; hidden while the doors are open.
        const opening = box(parent, [doorWidth - 0.02, doorHeight - 0.02, 0.03], [x, midY, halfWidth - 0.035], roofBlack, { castShadow: false });
        opening.name = spec.opening;
        const leafWidth = doorWidth / 2 - 0.02;
        const frameHeight = doorHeight - 0.04;
        for (const leftLeaf of [true, false]) {
          const towardsCentre = leftLeaf ? 1 : -1;
          // The group origin is the hinge, so rotation.y swings the leaf outwards.
          const leaf = new THREE.Group();
          leaf.name = leftLeaf ? spec.left : spec.right;
          leaf.position.set(x - towardsCentre * (doorWidth / 2 - 0.015), midY, halfWidth);
          parent.add(leaf);
          const c = towardsCentre * leafWidth / 2;
          box(leaf, [leafWidth - 0.05, frameHeight - 0.3, 0.01], [c, 0.11, 0.016], tintedGlass, { castShadow: false });
          for (const edge of [-1, 1]) {
            box(leaf, [0.04, frameHeight, 0.026], [c + edge * (leafWidth / 2 - 0.02), 0, 0.016], roofBlack, { castShadow: false });
          }
          box(leaf, [leafWidth, 0.05, 0.026], [c, frameHeight / 2 - 0.025, 0.016], roofBlack, { castShadow: false });
          box(leaf, [leafWidth, 0.26, 0.022], [c, -frameHeight / 2 + 0.13, 0.016], roofBlack, { castShadow: false });
          box(leaf, [leafWidth - 0.07, 0.026, 0.02], [c, -0.18, 0.022], darkMetal, { castShadow: false });
          box(leaf, [0.022, 0.8, 0.02], [towardsCentre * (leafWidth - 0.075), 0.02, 0.034], rimMetal, { castShadow: false });
        }
        for (const edge of [-1, 1]) {
          box(parent, [0.05, doorHeight + 0.05, 0.012], [x + edge * (doorWidth / 2 + 0.025), midY + 0.025, halfWidth + 0.006], roofBlack, { castShadow: false });
        }
        box(parent, [doorWidth + 0.1, 0.05, 0.012], [x, doorTopY + 0.025, halfWidth + 0.006], roofBlack, { castShadow: false });
        box(parent, [doorWidth - 0.04, 0.04, 0.13], [x, floorY - 0.02, halfWidth - 0.065], yellow, { castShadow: false });
      }

      // Shared wheel geometry: rounded-shoulder tyre, steel hub and polished outer rim.
      const tyreProfile = [[0.34, -0.14], [0.43, -0.146], [0.474, -0.132], [0.495, -0.1], [0.5, -0.05], [0.5, 0.05], [0.495, 0.1], [0.474, 0.132], [0.43, 0.146], [0.34, 0.14]]
        .map(([r, y]) => new THREE.Vector2(r * wheelRadius / 0.5, y));
      const tyreGeometry = new THREE.LatheGeometry(tyreProfile, 36).rotateX(Math.PI / 2);
      const hubGeometry = new THREE.CylinderGeometry(0.345, 0.345, 0.27, 28).rotateX(Math.PI / 2);
      const rimDiscGeometry = new THREE.CylinderGeometry(0.3, 0.3, 0.03, 28).rotateX(Math.PI / 2);
      const rimRingGeometry = new THREE.TorusGeometry(0.3, 0.02, 8, 28);
      const hubCapGeometry = new THREE.CylinderGeometry(0.11, 0.11, 0.06, 20).rotateX(Math.PI / 2);
      const boltGeometry = new THREE.CylinderGeometry(0.024, 0.024, 0.03, 8).rotateX(Math.PI / 2);
      const rimHoleGeometry = new THREE.CylinderGeometry(0.038, 0.038, 0.034, 10).rotateX(Math.PI / 2);

      function createWheel(side, twin) {
        // Y up, Z is the axle: rotating the assembly about local Z rolls the wheel.
        const assembly = new THREE.Group();
        assembly.userData.rollingWheel = true;
        for (let i = 0; i < (twin ? 2 : 1); i += 1) {
          const offset = -side * i * 0.3;
          addMesh(assembly, tyreGeometry, rubber, [0, 0, offset]);
          addMesh(assembly, hubGeometry, darkMetal, [0, 0, offset], { castShadow: false });
        }
        addMesh(assembly, rimDiscGeometry, rimMetal, [0, 0, side * 0.13], { castShadow: false });
        addMesh(assembly, rimRingGeometry, rimMetal, [0, 0, side * 0.145], { castShadow: false });
        addMesh(assembly, hubCapGeometry, darkMetal, [0, 0, side * 0.15], { castShadow: false });
        for (let bolt = 0; bolt < 10; bolt += 1) {
          const angle = bolt * Math.PI / 5;
          addMesh(assembly, boltGeometry, rimMetal, [Math.cos(angle) * 0.17, Math.sin(angle) * 0.17, side * 0.155], { castShadow: false });
        }
        for (let hole = 0; hole < 6; hole += 1) {
          const angle = hole * Math.PI / 3 + Math.PI / 6;
          addMesh(assembly, rimHoleGeometry, darkMetal, [Math.cos(angle) * 0.245, Math.sin(angle) * 0.245, side * 0.135], { castShadow: false });
        }
        return assembly;
      }

      const wheelWellGeometry = new THREE.CylinderGeometry(archRadius, archRadius, 2 * halfWidth - 0.1, 20, 1, true, Math.PI / 2, Math.PI).rotateX(Math.PI / 2);
      const archTrimGeometry = new THREE.TorusGeometry(archRadius + 0.012, 0.028, 6, 24, Math.PI);

      function addSeatRow(parent, x, side) {
        const z = side * 0.6;
        box(parent, [0.44, 0.09, 0.86], [x, 0.86, z], seatBlue, { castShadow: false });
        box(parent, [0.08, 0.5, 0.86], [x - 0.2, 1.14, z], seatBlue, { castShadow: false });
        box(parent, [0.06, 0.42, 0.06], [x, 0.63, z], darkMetal, { castShadow: false });
      }

      // Builds one rigid body section: thin side walls (doors only on +Z), rounded roof lid,
      // nose/tail caps, wrapped glazing, livery, arches, floor, doors and seats.
      function buildSection(parent, cfg) {
        const { x0, x1, nose, tail, arches, doors } = cfg;
        const wallStart = x0 + tail;
        const wallEnd = x1 - nose;
        const doorRanges = doors.map(door => [door.x - doorWidth / 2, door.x + doorWidth / 2]);
        const archRanges = arches.map(x => [x - archRadius - 0.03, x + archRadius + 0.03]);

        for (const side of [-1, 1]) {
          const geometry = new THREE.ExtrudeGeometry(
            sideWallShape(wallStart, wallEnd, arches, side === 1 ? doorRanges : []),
            { depth: 0.05, bevelEnabled: false, curveSegments: 16 }
          );
          geometry.translate(0, 0, side === 1 ? halfWidth - 0.05 : -halfWidth);
          addMesh(parent, geometry, paintWhite, [0, 0, 0]);
        }
        if (nose > 0) planExtrude(parent, planOutline(wallEnd - 0.02, x1, halfWidth, nose, 0), sillY, wallTopY, paintWhite, { curveSegments: 12 });
        if (tail > 0) planExtrude(parent, planOutline(x0, wallStart + 0.02, halfWidth, 0, tail), sillY, wallTopY, paintWhite, { curveSegments: 12 });
        planExtrude(parent, planOutline(x0, x1, halfWidth, Math.max(nose, 0.05), Math.max(tail, 0.05)), wallTopY - 0.24, roofTopY, paintWhite, { bevel: 0.24, curveSegments: 12 });

        // Closed end plates at the articulation so the hollow shell never shows.
        const endPlate = crossSection(halfWidth - 0.015, sillY + 0.02, roofTopY - 0.04, 0.3, 0.02);
        if (nose === 0) sectionExtrude(parent, endPlate, x1 - 0.02, x1, bellowsCore, { castShadow: false });
        if (tail === 0) sectionExtrude(parent, endPlate, x0, x0 + 0.02, bellowsCore, { castShadow: false });

        function wrapEnd(end, outset, y0, y1, material, options = {}) {
          const shape = end === 'nose'
            ? planOutline(wallEnd - 0.005, x1 + outset, halfWidth + outset, nose + outset, 0)
            : planOutline(x0 - outset, wallStart + 0.005, halfWidth + outset, 0, tail + outset);
          return planExtrude(parent, shape, y0, y1, material, { castShadow: false, curveSegments: 12, ...options });
        }
        for (const end of ['nose', 'tail']) {
          if ((end === 'nose' ? nose : tail) === 0) continue;
          wrapEnd(end, 0.012, end === 'nose' ? 1.05 : 1.22, 2.64, windowBlack);
          wrapEnd(end, 0.007, 2.665, 2.755, paintBlue);
          wrapEnd(end, 0.03, 0.27, 0.56, paintBlue, { castShadow: true });
          wrapEnd(end, 0.045, 0.3, 0.36, roofBlack);
        }

        const glazeStart = tail > 0 ? wallStart : x0 + 0.1;
        const glazeEnd = nose > 0 ? wallEnd : x1 - 0.1;
        for (const side of [-1, 1]) {
          const z = side * (halfWidth + 0.006);
          const cuts = side === 1 ? doorRanges.map(([a, b]) => [a - 0.07, b + 0.07]) : [];
          for (const [a, b] of subtractRanges(glazeStart, glazeEnd, cuts).filter(([a, b]) => b - a > 0.3)) {
            const length = b - a;
            const centre = (a + b) / 2;
            box(parent, [length, 1.42, 0.012], [centre, 1.93, z], windowBlack, { castShadow: false });
            const panes = Math.max(1, Math.round(length / 1.5));
            const paneLength = length / panes;
            for (let i = 0; i < panes; i += 1) {
              const px = a + paneLength * (i + 0.5);
              box(parent, [paneLength - 0.08, 1.3, 0.006], [px, 1.93, z + side * 0.008], tintedGlass, { castShadow: false });
              if (paneLength > 0.8) box(parent, [paneLength - 0.12, 0.016, 0.006], [px, 2.33, z + side * 0.012], darkMetal, { castShadow: false });
            }
            box(parent, [length - 0.1, 0.022, 0.004], [centre, 2.5, z + side * 0.014], glassHighlight, { castShadow: false });
          }
          // Blue roofline stripe runs unbroken above the doors; blue skirt stops at arches and doors.
          box(parent, [wallEnd - wallStart, 0.09, 0.012], [(wallStart + wallEnd) / 2, 2.71, z], paintBlue, { castShadow: false });
          for (const [a, b] of subtractRanges(wallStart, wallEnd, archRanges.concat(side === 1 ? doorRanges : []))) {
            box(parent, [b - a, 0.26, 0.018], [(a + b) / 2, 0.43, side * (halfWidth + 0.009)], paintBlue);
            box(parent, [b - a, 0.06, 0.03], [(a + b) / 2, 0.33, side * (halfWidth + 0.02)], roofBlack, { castShadow: false });
          }
        }

        for (const x of arches) {
          addMesh(parent, wheelWellGeometry, wheelWell, [x, wheelRadius, 0], { castShadow: false });
          for (const side of [-1, 1]) {
            addMesh(parent, archTrimGeometry, roofBlack, [x, wheelRadius, side * (halfWidth + 0.012)], { castShadow: false });
          }
        }
        for (const [a, b] of subtractRanges(x0 + 0.02, x1 - 0.02, archRanges)) {
          box(parent, [b - a, 0.06, 2 * halfWidth - 0.1], [(a + b) / 2, floorY - 0.03, 0], cabinFloor, { castShadow: false });
        }
        for (const door of doors) {
          addDoor(parent, door);
          for (const dx of [-0.72, 0.72]) rodBetween(parent, [door.x + dx, floorY, 0.95], [door.x + dx, 2.5, 0.95], 0.022, yellow);
        }
        for (const z of [-0.55, 0.55]) rodBetween(parent, [x0 + 0.3, 2.45, z], [x1 - 0.5, 2.45, z], 0.018, yellow);
        for (const x of cfg.seats) {
          for (const side of [-1, 1]) addSeatRow(parent, x, side);
        }
      }

      // ---- Front section (fixed) --------------------------------------------
      buildSection(bus, {
        x0: frontSectionEndX,
        x1: frontX,
        nose: noseRadius,
        tail: 0,
        arches: [frontAxleX, middleAxleX],
        doors: [
          { x: frontDoorX, opening: 'front-entry-opening', left: 'front-entry-left-leaf', right: 'front-entry-right-leaf' },
          { x: alightDoorX, opening: 'rear-exit-opening', left: 'middle-door-left-leaf', right: 'middle-door-right-leaf' }
        ],
        seats: [-2.35, -1.55, 1.15, 1.95]
      });
      box(bus, [0.05, 1.15, 0.95], [3.75, 1.0, -0.72], darkMetal, { castShadow: false });

      // ---- Trailer section (bends about the articulation joint) -------------
      const articulationRear = new THREE.Group();
      articulationRear.name = 'articulation-rear';
      articulationRear.position.set(jointX, 0, 0);
      bus.add(articulationRear);
      // Inner group cancels the joint offset so trailer parts are authored in bus coordinates.
      const trailer = new THREE.Group();
      trailer.position.x = -jointX;
      articulationRear.add(trailer);
      buildSection(trailer, {
        x0: rearX,
        x1: trailerStartX,
        nose: 0,
        tail: tailRadius,
        arches: [rearAxleX],
        doors: [
          { x: -6.3, opening: 'door-3-opening', left: 'door-3-left-leaf', right: 'door-3-right-leaf' },
          { x: -11.95, opening: 'door-4-opening', left: 'door-4-left-leaf', right: 'door-4-right-leaf' }
        ],
        seats: [-7.65, -8.45, -9.25]
      });
      bus.userData.trailerDoorX = [-6.3, -11.95];

      // ---- Articulation: ribbed bellows halves plus a round fold over the turntable.
      const bellowsSection = crossSection(1.16, 0.38, 3.0, 0.3, 0.04);
      sectionExtrude(bus, bellowsSection, jointX, frontSectionEndX + 0.03, bellowsCore);
      sectionExtrude(trailer, bellowsSection, trailerStartX - 0.03, jointX, bellowsCore);
      const ribGeometry = new THREE.ExtrudeGeometry(crossSection(1.2, 0.35, 3.03, 0.32, 0.05), { depth: 0.045, bevelEnabled: false, curveSegments: 6 }).rotateY(Math.PI / 2);
      for (let i = 0; i < 4; i += 1) {
        addMesh(bus, ribGeometry, bellowsRib, [frontSectionEndX - 0.07 - i * 0.105, 0, 0]);
        addMesh(trailer, ribGeometry, bellowsRib, [trailerStartX + 0.03 + i * 0.105, 0, 0]);
      }
      // Rotationally symmetric fold: fills the outside of the bend when the trailer swings.
      // Kept just inside the bellows halves so it only shows on the outside of a bend.
      const foldProfile = [new THREE.Vector2(0.001, 0.4), new THREE.Vector2(1.1, 0.4), new THREE.Vector2(1.15, 0.44), new THREE.Vector2(1.15, 2.67)];
      for (let i = 1; i <= 6; i += 1) {
        const angle = (i / 6) * Math.PI / 2;
        foldProfile.push(new THREE.Vector2(0.86 + 0.29 * Math.cos(angle), 2.67 + 0.29 * Math.sin(angle)));
      }
      foldProfile.push(new THREE.Vector2(0.001, 2.96));
      addMesh(bus, new THREE.LatheGeometry(foldProfile, 64), bellowsFold, [jointX, 0, 0], { castShadow: false });
      addMesh(bus, new THREE.CylinderGeometry(1.0, 1.0, 0.06, 32), darkMetal, [jointX, 0.33, 0], { castShadow: false });

      // ---- Running gear: steered front axle, driven middle axle, trailer axle.
      for (const side of [-1, 1]) {
        const steer = new THREE.Group();
        steer.name = side < 0 ? 'steer-front-left' : 'steer-front-right';
        steer.position.set(frontAxleX, wheelRadius, side * wheelTrack);
        bus.add(steer);
        steer.add(createWheel(side, false));
        const middle = createWheel(side, true);
        middle.position.set(middleAxleX, wheelRadius, side * wheelTrack);
        bus.add(middle);
        const rear = createWheel(side, true);
        rear.position.set(rearAxleX, wheelRadius, side * wheelTrack);
        trailer.add(rear);
      }

      // ---- Nose: windscreen band, destination display, lamps, wipers, mirrors.
      const frontDisplay = new THREE.Mesh(new THREE.PlaneGeometry(1.25, 0.24), destinationMaterial('33', 320, 62));
      frontDisplay.position.set(frontX + 0.016, 2.47, 0);
      frontDisplay.rotation.y = Math.PI / 2;
      bus.add(frontDisplay);
      const sideDisplay = new THREE.Mesh(new THREE.PlaneGeometry(0.62, 0.2), destinationMaterial('33', 186, 60));
      sideDisplay.position.set(2.55, 2.44, halfWidth + 0.018);
      bus.add(sideDisplay);
      box(bus, [0.025, 0.11, 0.5], [frontX + 0.045, 0.43, 0], plateMaterial, { castShadow: false });
      for (const z of [-0.36, 0.36]) {
        box(bus, [0.025, 0.028, 0.8], [frontX + 0.028, 1.14, z], roofBlack, { castShadow: false, rotation: [z > 0 ? 0.2 : -0.2, 0, 0] });
      }
      for (const side of [-1, 1]) {
        const label = side < 0 ? 'left' : 'right';
        box(bus, [0.03, 0.2, 0.47], [frontX + 0.012, 0.8, side * 0.565], roofBlack, { castShadow: false });
        const low = box(bus, [0.03, 0.11, 0.14], [frontX + 0.027, 0.8, side * 0.42], lightMaterial.clone(), { castShadow: false });
        low.name = `headlight-${label}`;
        const high = box(bus, [0.03, 0.11, 0.14], [frontX + 0.027, 0.8, side * 0.59], lightMaterial.clone(), { castShadow: false });
        high.name = `headlight-${label}-high`;
        const drl = box(bus, [0.02, 0.026, 0.46], [frontX + 0.03, 0.925, side * 0.565], lightMaterial.clone(), { castShadow: false });
        drl.name = `headlight-${label}-drl`;
        const indicator = box(bus, [0.03, 0.11, 0.07], [frontX + 0.027, 0.8, side * 0.74], indicatorMaterials[label], { castShadow: false });
        indicator.name = `indicator-${label}`;
        const repeater = box(bus, [0.14, 0.05, 0.02], [3.48, 0.98, side * (halfWidth + 0.01)], indicatorMaterials[label], { castShadow: false });
        repeater.name = `indicator-${label}-side`;
        for (const x of [1.25, -1.9]) box(bus, [0.09, 0.045, 0.02], [x, 0.62, side * (halfWidth + 0.012)], sideMarker, { castShadow: false });
        for (const x of [-7.7, -9.3]) box(trailer, [0.09, 0.045, 0.02], [x, 0.62, side * (halfWidth + 0.012)], sideMarker, { castShadow: false });

        const mirrorArmCurve = new THREE.CatmullRomCurve3([
          new THREE.Vector3(frontX - 0.25, 2.74, side * 1.12),
          new THREE.Vector3(frontX - 0.02, 2.8, side * 1.3),
          new THREE.Vector3(frontX + 0.14, 2.72, side * 1.46),
          new THREE.Vector3(frontX + 0.17, 2.56, side * 1.54)
        ]);
        addMesh(bus, new THREE.TubeGeometry(mirrorArmCurve, 20, 0.032, 8, false), roofBlack, [0, 0, 0]);
        const mirrorHousingGeometry = new THREE.ExtrudeGeometry(roundedRectShape(0.23, 0.48, 0.045), {
          depth: 0.12, bevelEnabled: true, bevelSegments: 2, bevelSize: 0.012, bevelThickness: 0.012, curveSegments: 6
        });
        mirrorHousingGeometry.translate(0, 0, -0.06);
        addMesh(bus, mirrorHousingGeometry, roofBlack, [frontX + 0.22, 2.33, side * 1.6], { rotation: [0, -Math.PI / 2, 0] });
        const mirrorGlass = new THREE.Mesh(
          new THREE.ShapeGeometry(roundedRectShape(0.16, 0.36, 0.026)),
          new THREE.MeshStandardMaterial({ color: 0x8295a4, metalness: 0.58, roughness: 0.22, side: THREE.DoubleSide })
        );
        mirrorGlass.rotation.y = -Math.PI / 2;
        mirrorGlass.position.set(frontX + 0.138, 2.34, side * 1.6);
        bus.add(mirrorGlass);
      }

      // ---- Tail: lamp clusters (brake lamps own their materials), rear display, plate.
      for (const side of [-1, 1]) {
        const label = side < 0 ? 'left' : 'right';
        box(trailer, [0.03, 0.62, 0.28], [rearX - 0.012, 0.9, side * 0.78], roofBlack, { castShadow: false });
        const brake = box(trailer, [0.03, 0.24, 0.22], [rearX - 0.027, 1.05, side * 0.78], redLight.clone(), { castShadow: false });
        brake.name = `brake-light-${label}`;
        const rearIndicator = box(trailer, [0.03, 0.12, 0.22], [rearX - 0.027, 0.85, side * 0.78], indicatorMaterials[label], { castShadow: false });
        rearIndicator.name = `indicator-${label}-rear`;
        box(trailer, [0.03, 0.08, 0.22], [rearX - 0.027, 0.71, side * 0.78], lightMaterial, { castShadow: false });
        box(trailer, [0.03, 0.06, 0.16], [rearX - 0.016, 2.71, side * 0.8], redLight, { castShadow: false });
      }
      const highBrake = box(trailer, [0.03, 0.05, 0.62], [rearX - 0.022, 2.58, 0], redLight.clone(), { castShadow: false });
      highBrake.name = 'brake-light-high';
      const rearDisplay = new THREE.Mesh(new THREE.PlaneGeometry(0.56, 0.2), destinationMaterial('33', 168, 60));
      rearDisplay.position.set(rearX - 0.018, 2.38, 0);
      rearDisplay.rotation.y = -Math.PI / 2;
      trailer.add(rearDisplay);
      box(trailer, [0.025, 0.11, 0.5], [rearX - 0.045, 0.43, 0], plateMaterial, { castShadow: false });

      // ---- Roof equipment: front HVAC, traction converter, pole base; trailer HVAC and battery.
      roofPod(bus, 2.55, 4.75, 2.0, roofTopY - 0.04, 0.34, roofEquipment);
      for (const x of [3.12, 4.18]) {
        addMesh(bus, new THREE.CylinderGeometry(0.27, 0.27, 0.02, 24), ventDark, [x, roofTopY + 0.305, 0], { castShadow: false });
        addMesh(bus, new THREE.TorusGeometry(0.27, 0.018, 6, 24), rimMetal, [x, roofTopY + 0.315, 0], { castShadow: false, rotation: [Math.PI / 2, 0, 0] });
      }
      roofPod(bus, -0.25, 1.95, 1.85, roofTopY - 0.04, 0.36, roofEquipment);
      for (const side of [-1, 1]) {
        for (let i = 0; i < 7; i += 1) {
          box(bus, [0.05, 0.18, 0.01], [0.15 + i * 0.25, roofTopY + 0.15, side * 0.93], ventDark, { castShadow: false });
        }
      }
      roofPod(bus, -3.7, -0.6, 1.9, roofTopY - 0.04, 0.3, roofEquipment);
      box(bus, [1.3, 0.05, 1.15], [-2.2, roofTopY + 0.285, 0], darkMetal);

      // Trolley poles: pivot groups at the bases; rotation.z lifts the tips to the overhead wire.
      const poleBase = new THREE.Vector3(-2.3, 3.62, 0);
      const poleLength = 6.1;
      const shoeLift = 0.11;
      const raisedAngle = -Math.asin((overheadWireHeight - overheadWireRadius - shoeLift - poleBase.y) / poleLength);
      const loweredAngle = 0.016;
      for (const side of [-1, 1]) {
        const z = side * overheadWireSpacing / 2;
        for (const dx of [-0.16, 0.16]) {
          addMesh(bus, new THREE.CylinderGeometry(0.045, 0.045, 0.1, 10), insulatorMaterial, [poleBase.x + 0.1 + dx, roofTopY + 0.36, z], { castShadow: false });
        }
        box(bus, [0.5, 0.09, 0.22], [poleBase.x + 0.1, poleBase.y - 0.12, z], darkMetal);
        rodBetween(bus, [poleBase.x + 0.32, poleBase.y - 0.1, z], [0.0, roofTopY + 0.2, side * 0.55], 0.022, roofBlack);

        const pole = new THREE.Group();
        pole.name = side < 0 ? 'trolley-pole-left' : 'trolley-pole-right';
        pole.position.set(poleBase.x, poleBase.y, z);
        pole.userData.axis = 'z';
        pole.userData.raisedAngle = Number(raisedAngle.toFixed(4));
        pole.userData.loweredAngle = loweredAngle;
        pole.rotation.z = pole.userData.raisedAngle;
        bus.add(pole);
        addMesh(pole, new THREE.CylinderGeometry(0.085, 0.085, 0.17, 16).rotateX(Math.PI / 2), darkMetal, [0, 0, 0]);
        rodBetween(pole, [0.14, 0, 0], [-0.95, 0, 0], 0.055, darkMetal);
        rodBetween(pole, [-0.95, 0, 0], [-(poleLength - 0.4), 0, 0], 0.03, poleMaterial);
        rodBetween(pole, [-(poleLength - 0.4), 0, 0], [-poleLength, 0, 0], 0.036, insulatorMaterial);
        // Current collector head; counter-rotated so the shoe is level on the wire when raised.
        const head = new THREE.Group();
        head.name = side < 0 ? 'trolley-head-left' : 'trolley-head-right';
        head.position.set(-poleLength, 0, 0);
        head.rotation.z = -pole.userData.raisedAngle;
        pole.add(head);
        box(head, [0.09, 0.08, 0.06], [0, 0.02, 0], darkMetal);
        box(head, [0.4, 0.045, 0.07], [0, 0.0755, 0], darkMetal);
        box(head, [0.3, 0.012, 0.045], [0, 0.104, 0], carbonMaterial, { castShadow: false });
        for (const end of [-1, 1]) {
          box(head, [0.06, 0.03, 0.07], [end * 0.215, 0.09, 0], darkMetal, { rotation: [0, 0, end * 0.5], castShadow: false });
        }
      }
      bus.userData.overheadWireHeight = overheadWireHeight;

      // Trailer roof: HVAC, pole retaining hooks and the traction battery.
      roofPod(trailer, -7.35, -5.6, 1.9, roofTopY - 0.04, 0.3, roofEquipment);
      for (const x of [-6.9, -6.05]) {
        addMesh(trailer, new THREE.CylinderGeometry(0.24, 0.24, 0.02, 24), ventDark, [x, roofTopY + 0.265, 0], { castShadow: false });
      }
      const hookGeometry = new THREE.TorusGeometry(0.055, 0.014, 6, 12, Math.PI).rotateZ(Math.PI).rotateY(Math.PI / 2);
      for (const side of [-1, 1]) {
        const z = side * overheadWireSpacing / 2;
        rodBetween(trailer, [-8.1, roofTopY - 0.02, z], [-8.1, 3.46, z], 0.022, darkMetal);
        addMesh(trailer, hookGeometry, darkMetal, [-8.1, 3.52, z], { castShadow: false });
      }
      roofPod(trailer, -12.35, -8.75, 2.0, roofTopY - 0.04, 0.28, roofEquipment);
      for (let i = 0; i < 6; i += 1) {
        box(trailer, [0.34, 0.012, 1.4], [-12.0 + i * 0.6, roofTopY + 0.245, 0], ventDark, { castShadow: false });
      }

      // Telemetry gateway with GNSS dome and LTE whip on the rear of the front section.
      bus.add(roofTelemetryUnit);
      roofTelemetryUnit.position.set(-4.0, roofTopY, 0);
      box(roofTelemetryUnit, [0.34, 0.08, 0.6], [0, 0.04, 0], darkMetal);
      addMesh(roofTelemetryUnit, new THREE.SphereGeometry(0.09, 16, 8, 0, Math.PI * 2, 0, Math.PI / 2), roofEquipment, [0.02, 0.08, -0.15]);
      rodBetween(roofTelemetryUnit, [0, 0.08, 0.17], [0, 0.42, 0.17], 0.01, roofBlack);
    }

    const animatedBus = bus.clone(true);
    animationScene.add(animatedBus);
    const frontLeftDoor = animatedBus.getObjectByName('front-entry-left-leaf');
    const frontRightDoor = animatedBus.getObjectByName('front-entry-right-leaf');
    const rearLeftDoor = animatedBus.getObjectByName('middle-door-left-leaf');
    const rearRightDoor = animatedBus.getObjectByName('middle-door-right-leaf');
    const frontOpening = animatedBus.getObjectByName('front-entry-opening');
    const rearOpening = animatedBus.getObjectByName('rear-exit-opening');
    const animatedWheelMeshes = [];
    animatedBus.traverse(object => {
      if (object.userData.rollingWheel) animatedWheelMeshes.push(object);
    });

    const boardingPerson = new THREE.Group();
    boardingPerson.position.set(4.28, 0, 2.35);
    animatedBus.add(boardingPerson);
    addMesh(boardingPerson, new THREE.CapsuleGeometry(0.17, 0.48, 5, 10), passengerCoat, [0, 1.12, 0]);
    const head = addMesh(boardingPerson, new THREE.SphereGeometry(0.14, 20, 16), passengerSkin, [0, 1.62, 0]);
    head.castShadow = true;
    const hair = addMesh(boardingPerson, new THREE.SphereGeometry(0.145, 18, 12), passengerHair, [0, 1.69, 0]);
    hair.scale.set(1, 0.5, 1);
    for (const eyeX of [-0.05, 0.05]) {
      addMesh(boardingPerson, new THREE.SphereGeometry(0.022, 8, 8), passengerHair, [eyeX, 1.645, 0.12]);
    }
    addMesh(boardingPerson, new THREE.SphereGeometry(0.026, 8, 8), passengerSkin, [0, 1.59, 0.15]);

    const legPivots = [];
    for (const x of [-0.105, 0.105]) {
      const leg = new THREE.Group();
      leg.name = x < 0 ? 'passenger-leg-left' : 'passenger-leg-right';
      leg.position.set(x, 0.78, 0);
      boardingPerson.add(leg);
      addMesh(leg, new THREE.CapsuleGeometry(0.07, 0.46, 4, 8), passengerPants, [0, -0.29, 0]);
      box(leg, [0.15, 0.1, 0.24], [0, -0.58, 0.08], passengerShoes);
      legPivots.push(leg);
    }
    const armPivots = [];
    for (const x of [-0.22, 0.22]) {
      const arm = new THREE.Group();
      arm.name = x < 0 ? 'passenger-arm-left' : 'passenger-arm-right';
      arm.position.set(x, 1.38, 0);
      boardingPerson.add(arm);
      addMesh(arm, new THREE.CapsuleGeometry(0.055, 0.32, 4, 8), passengerCoat, [0, -0.22, 0]);
      addMesh(arm, new THREE.SphereGeometry(0.06, 12, 10), passengerSkin, [0, -0.43, -0.015]);
      armPivots.push(arm);
    }

    function smoothStep(value) {
      const t = Math.max(0, Math.min(1, value));
      return t * t * (3 - 2 * t);
    }

    function mix(start, end, amount) {
      return start + (end - start) * amount;
    }

    const animationClock = new THREE.Clock();
    let boardingActive = false;
    let boardingElapsed = 0;
    let passengerChange = 'board';
    let animationPassengerCount = 1;
    const passengerQueueSpacing = 0.72;
    const passengerFlowSpeed = 1.3;
    const passengerInsideZ = 0.55;
    // Queue on the pavement between the kerb and the shelter glass (world z ≈ 4.6 < glass at 5.0).
    const passengerQueueStartZ = 2.9;
    const passengerCountInput = document.getElementById('passenger-count');
    const passengerAddButton = document.getElementById('add-passenger');
    const passengerRemoveButton = document.getElementById('remove-passenger');
    const tripToggleButton = document.getElementById('trip-toggle');
    // Door angles requested by the passenger walk; the render loop combines them with the twin's door state.
    const passengerDoorAngles = { front: 0, rear: 0 };
    const legacyDoorX = { front: 4.28, alight: -0.15 };
    function busDoorX(which) {
      const value = Number(animatedBus.userData[which === 'front' ? 'frontDoorX' : 'alightDoorX']);
      return Number.isFinite(value) ? value : legacyDoorX[which];
    }

    function passengerModelFor(group) {
      return {
        group,
        legs: [group.getObjectByName('passenger-leg-left'), group.getObjectByName('passenger-leg-right')],
        arms: [group.getObjectByName('passenger-arm-left'), group.getObjectByName('passenger-arm-right')]
      };
    }

    const passengerModels = [passengerModelFor(boardingPerson)];
    boardingPerson.visible = false;

    function ensurePassengerModels(count) {
      while (passengerModels.length < count) {
        const group = boardingPerson.clone(true);
        group.visible = false;
        animatedBus.add(group);
        passengerModels.push(passengerModelFor(group));
      }
    }

    function removeExtraPassengerModels() {
      for (const model of passengerModels.slice(1)) {
        animatedBus.remove(model.group);
      }
      passengerModels.splice(1);
    }

    function readPassengerCount() {
      const requested = Math.round(Number(passengerCountInput.value) || 1);
      const count = Math.max(1, Math.min(10, requested));
      passengerCountInput.value = String(count);
      const noun = count === 1 ? 'passenger' : 'passengers';
      passengerAddButton.textContent = `+${count} ${noun}`;
      passengerRemoveButton.textContent = `−${count} ${noun}`;
      return count;
    }

    function updatePassengerAnimation(elapsed) {
      if (!boardingActive) return;
      const time = elapsed;
      const openingDuration = 0.9;
      const closingDuration = 1.0;
      const queueLength = passengerQueueSpacing * (animationPassengerCount - 1);
      const pathDepth = passengerQueueStartZ - passengerInsideZ;
      const flowDistanceTotal = pathDepth + queueLength;
      const flowDuration = flowDistanceTotal / passengerFlowSpeed;
      const closingStartsAt = openingDuration + flowDuration;
      const animationDuration = closingStartsAt + closingDuration;

      if (time >= animationDuration) {
        // The door controller in the render loop closes the doors unless the twin says they are open.
        passengerDoorAngles.front = 0;
        passengerDoorAngles.rear = 0;
        for (const model of passengerModels) model.group.visible = false;
        removeExtraPassengerModels();
        finishPassengerAnimation();
        return;
      }

      const boarding = passengerChange === 'board';
      const doorX = busDoorX(boarding ? 'front' : 'alight');
      let doorAngle = 0;
      if (time < openingDuration) {
        doorAngle = 1.12 * smoothStep(time / openingDuration);
      } else if (time < closingStartsAt) {
        doorAngle = 1.12;
      } else {
        doorAngle = 1.12 * (1 - smoothStep((time - closingStartsAt) / closingDuration));
      }
      passengerDoorAngles[boarding ? 'front' : 'rear'] = doorAngle;

      const flowElapsed = Math.max(0, time - openingDuration);
      const flowDistance = Math.min(flowDistanceTotal, flowElapsed * passengerFlowSpeed);
      const flowActive = time >= openingDuration && time < closingStartsAt;
      for (const model of passengerModels) model.group.visible = false;

      for (let index = 0; index < animationPassengerCount; index += 1) {
        const model = passengerModels[index];
        const progressAlongQueue = flowDistance - index * passengerQueueSpacing;
        let x;
        let y;
        let z;
        let facing;

        if (boarding) {
          if (progressAlongQueue > pathDepth) continue;
          if (progressAlongQueue < 0) {
            x = doorX + progressAlongQueue;
            y = 0;
            z = passengerQueueStartZ;
            facing = Math.PI / 2;
          } else {
            x = doorX;
            z = passengerQueueStartZ - progressAlongQueue;
            y = 0.42 * smoothStep((1.35 - z) / 0.8);
            const turn = smoothStep(progressAlongQueue / 0.48);
            // The passenger model faces +Z by default; turn it toward -Z to walk into the bus.
            facing = index === 0 ? mix(0, Math.PI, turn) : mix(Math.PI / 2, Math.PI, turn);
          }
          if (index === 0 && flowDistance === 0) facing = Math.PI * 2;
        } else {
          if (progressAlongQueue < 0) {
            x = doorX - progressAlongQueue;
            y = 0.42;
            z = passengerInsideZ;
            const turn = smoothStep((progressAlongQueue + 0.24) / 0.48);
            facing = mix(-Math.PI / 2, 0, turn);
          } else if (progressAlongQueue <= pathDepth) {
            x = doorX;
            z = passengerInsideZ + progressAlongQueue;
            y = 0.42 * (1 - smoothStep(progressAlongQueue / pathDepth));
            if (progressAlongQueue < 0.24) {
              facing = mix(-Math.PI / 2, 0, smoothStep(progressAlongQueue / 0.24));
            } else if (progressAlongQueue > pathDepth - 0.24) {
              facing = mix(0, Math.PI / 2, smoothStep((progressAlongQueue - (pathDepth - 0.24)) / 0.24));
            } else {
              facing = 0;
            }
          } else {
            x = doorX + progressAlongQueue - pathDepth;
            y = 0;
            z = passengerQueueStartZ;
            facing = Math.PI / 2;
          }
          if (index === 0 && flowDistance === 0) facing = 0;
        }

        model.group.visible = true;
        model.group.position.set(x, y, z);
        model.group.rotation.y = facing;
        const passengerStride = Math.sin(flowElapsed * 10 - index * 0.55) * 0.42 * (flowActive ? 1 : 0);
        model.legs[0].rotation.x = passengerStride;
        model.legs[1].rotation.x = -passengerStride;
        model.arms[0].rotation.x = -passengerStride * 0.55;
        model.arms[1].rotation.x = passengerStride * 0.55;
      }
    }

    function createModelView(host, targetScene) {
      const camera = new THREE.PerspectiveCamera(34, 1, 0.1, 100);
      camera.position.set(20.3, 15.6, 34.7);
      const renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true });
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      renderer.setClearColor(0x000000, 0);
      renderer.outputColorSpace = THREE.SRGBColorSpace;
      renderer.toneMapping = THREE.ACESFilmicToneMapping;
      renderer.toneMappingExposure = 1.12;
      renderer.shadowMap.enabled = true;
      renderer.shadowMap.type = THREE.PCFSoftShadowMap;
      host.appendChild(renderer.domElement);

      const controls = new OrbitControls(camera, renderer.domElement);
      controls.target.set(-3.85, 2.3, 1.7);
      controls.enableDamping = true;
      controls.dampingFactor = 0.06;
      controls.enablePan = false;
      controls.minDistance = 30;
      controls.maxDistance = 62;
      controls.minAzimuthAngle = -0.72;
      controls.maxAzimuthAngle = 0.72;
      controls.minPolarAngle = 0.95;
      controls.maxPolarAngle = 1.42;
      controls.update();

      function resize() {
        const width = Math.max(host.clientWidth, 1);
        const height = Math.max(host.clientHeight, 1);
        renderer.setSize(width, height, false);
        camera.aspect = width / height;
        camera.zoom = Math.min(1, camera.aspect / 0.6);
        camera.updateProjectionMatrix();
      }
      resize();
      return { camera, controls, renderer, resize, scene: targetScene };
    }

    const staticView = createModelView(staticHost, scene);
    const animationView = createModelView(animationHost, animationScene);
    function formatTelemetryValue(key) {
      const value = telemetry[key];
      if (value === undefined || value === null || value === '') return '—';
      const number = Number(value);
      const fixed = (amount, digits, unit) => `${amount.toLocaleString('en-GB', { minimumFractionDigits: digits, maximumFractionDigits: digits })}${unit ? ` ${unit}` : ''}`;
      const angle = amount => fixed(number * 180 / Math.PI, 1, '°');
      switch (key) {
        case 'time_unix':
          return new Date(number * 1000).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit', timeZone: 'UTC' });
        case 'timestamp':
          return new Date(number).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit', timeZone: 'UTC' });
        case 'gnss_altitude': return fixed(number, 1, 'm');
        case 'latitude': return fixed(number, 5, '°');
        case 'longitude': return fixed(number, 5, '°');
        case 'heading_deg': return fixed(number, 1, '°');
        case 'temp_celsius': return fixed(number, 1, '°C');
        case 'odometry_articulationAngle':
        case 'odometry_steeringAngle': return angle(number);
        case 'odometry_vehicleSpeed': return fixed(number, 2, 'm/s');
        case 'odometry_wheelSpeed_fl':
        case 'odometry_wheelSpeed_fr':
        case 'odometry_wheelSpeed_ml':
        case 'odometry_wheelSpeed_mr':
        case 'odometry_wheelSpeed_rl':
        case 'odometry_wheelSpeed_rr': return fixed(number, 2, 'm/s');
        case 'itcs_numberOfPassengers': return fixed(number, 0, 'passengers');
        case 'status_doorIsOpen': return number ? 'Open' : 'Closed';
        case 'status_gridIsAvailable': return number ? 'Available' : 'Unavailable';
        case 'status_haltBrakeIsActive':
        case 'status_parkBrakeIsActive': return number ? 'Active' : 'Inactive';
        case 'traction_brakePressure': return fixed(number / 1000, 1, 'kPa');
        case 'traction_tractionForce': return fixed(number / 1000, 2, 'kN');
        case 'brake_bar': return fixed(number, 2, 'bar');
        case 'itcs_busRoute':
        case 'itcs_stopName': return String(value) === '-' ? '—' : String(value);
        default: return Number.isFinite(number) ? fixed(number, 1, '') : String(value);
      }
    }

    const modalValueMap = {
      'modal-altitude': 'gnss_altitude',
      'modal-latitude': 'latitude',
      'modal-longitude': 'longitude',
      'modal-heading': 'heading_deg',
      'modal-temperature': 'temp_celsius',
      'modal-time': 'time_unix',
      'modal-wheel-fl': 'odometry_wheelSpeed_fl',
      'modal-wheel-fr': 'odometry_wheelSpeed_fr',
      'modal-wheel-ml': 'odometry_wheelSpeed_ml',
      'modal-wheel-mr': 'odometry_wheelSpeed_mr',
      'modal-wheel-rl': 'odometry_wheelSpeed_rl',
      'modal-wheel-rr': 'odometry_wheelSpeed_rr',
      'modal-brake-pressure': 'traction_brakePressure',
      'modal-brake-bar': 'brake_bar',
      'modal-halt-brake': 'status_haltBrakeIsActive',
      'modal-park-brake': 'status_parkBrakeIsActive',
      'modal-steering': 'odometry_steeringAngle',
      'modal-vehicle-speed': 'odometry_vehicleSpeed',
      'modal-articulation': 'odometry_articulationAngle',
      'modal-door': 'status_doorIsOpen',
      'modal-passengers': 'itcs_numberOfPassengers',
      'modal-route': 'itcs_busRoute',
      'modal-stop': 'itcs_stopName',
      'modal-grid': 'status_gridIsAvailable',
      'modal-traction': 'traction_tractionForce'
    };
    const modalValueElements = Object.entries(modalValueMap).map(([elementId, metricKey]) => [document.getElementById(elementId), metricKey]);
    function renderInspectionValues() {
      for (const [element, metricKey] of modalValueElements) {
        const text = formatTelemetryValue(metricKey);
        if (element.textContent !== text) element.textContent = text;
      }
    }
    renderInspectionValues();

    const wheelPositions = {
      fl: 'Front left', fr: 'Front right',
      ml: 'Middle left', mr: 'Middle right',
      rl: 'Rear left', rr: 'Rear right'
    };
    const wheelParameterInfo = Object.fromEntries(Object.entries(wheelPositions).map(([positionCode, positionName]) => {
      const jsonKey = `odometry_wheelSpeed_${positionCode}`;
      return [jsonKey, {
        title: `${positionName} wheel speed`,
        jsonKey,
        description: 'Wheel speed measured with a wheel encoder. The paper notes that this measurement is not reliable below approximately 1.5 m/s.',
        source: 'ZTBus paper, Table 1'
      }];
    }));
    const parameterInfo = {
      time_unix: {
        title: 'Record time', jsonKey: 'time_unix',
        description: 'Unix timestamp in seconds since 00:00 UTC on 1 January 1970.',
        mapping: 'The mock JSON also includes timestamp in milliseconds for the same instant. The paper describes time_iso as the absolute UTC time in ISO 8601 format.',
        source: 'ZTBus paper, Table 1'
      },
      ...wheelParameterInfo,
      odometry_steeringAngle: {
        title: 'Steering angle', jsonKey: 'odometry_steeringAngle',
        description: 'Angle of the front wheels relative to the vehicle body. A positive angle means a left turn.',
        source: 'ZTBus paper, Table 1'
      },
      odometry_vehicleSpeed: {
        title: 'Vehicle speed', jsonKey: 'odometry_vehicleSpeed',
        description: 'Vehicle speed calculated from the rotational velocity of the middle axle drive shaft and a compound transmission ratio.',
        source: 'ZTBus paper, Table 1; Methods → Processing, Eq. (1)'
      },
      odometry_articulationAngle: {
        title: 'Articulation angle', jsonKey: 'odometry_articulationAngle',
        description: 'Angle of the bus pivot joint. A positive angle means a right turn.',
        source: 'ZTBus paper, Table 1'
      },
      traction_brakePressure: {
        title: 'Brake pressure', jsonKey: 'traction_brakePressure',
        description: 'Mean pressure in the friction braking lines. The dataset reports this signal in pascals; the sheet displays it in kilopascals.',
        source: 'ZTBus paper, Table 1'
      },
      brake_bar: {
        title: 'Brake line pressure', jsonKey: 'brake_bar',
        description: 'Mean pressure in the friction braking lines, shown in bar.',
        mapping: 'brake_bar is an app-side unit-converted field. The paper’s source column is traction_brakePressure in pascals.',
        source: 'ZTBus paper, Table 1; app-side conversion'
      },
      status_haltBrakeIsActive: {
        title: 'Holding brake', jsonKey: 'status_haltBrakeIsActive',
        description: 'Boolean flag showing whether the halt brake is active. The paper says this brake activates automatically whenever the bus is stationary.',
        source: 'ZTBus paper, Table 1'
      },
      status_parkBrakeIsActive: {
        title: 'Parking brake', jsonKey: 'status_parkBrakeIsActive',
        description: 'Boolean flag for the fail-safe parking brake. It works when the vehicle is unpowered and can be activated manually by the driver, for example during a longer stop.',
        source: 'ZTBus paper, Table 1'
      },
      traction_tractionForce: {
        title: 'Traction force', jsonKey: 'traction_tractionForce',
        description: 'Estimate of the total traction force from both traction motors, based on motor torque and the compound transmission ratio.',
        source: 'ZTBus paper, Table 1; Methods → Processing, Eq. (2)'
      },
      status_doorIsOpen: {
        title: 'Door status', jsonKey: 'status_doorIsOpen',
        description: 'Boolean flag that is true when at least one bus door is open.',
        source: 'ZTBus paper, Table 1'
      },
      itcs_numberOfPassengers: {
        title: 'Passengers', jsonKey: 'itcs_numberOfPassengers',
        description: 'Estimated number of passengers on board, measured by the infrared passenger-counting system.',
        source: 'ZTBus paper, Table 1; Methods → Data collection'
      },
      itcs_busRoute: {
        title: 'Route', jsonKey: 'itcs_busRoute',
        description: 'VBZ bus route name supplied by the intermodal transport control system (ITCS). A dash means the value is unavailable.',
        source: 'ZTBus paper, Table 1'
      },
      itcs_stopName: {
        title: 'Stop', jsonKey: 'itcs_stopName',
        description: 'Name of the bus stop served, supplied by the ITCS. A dash means the value is unavailable.',
        source: 'ZTBus paper, Table 1'
      },
      gnss_altitude: {
        title: 'GNSS altitude', jsonKey: 'gnss_altitude',
        description: 'Altitude above sea level measured by the GNSS sensor.',
        source: 'ZTBus paper, Table 1'
      },
      latitude: {
        title: 'Latitude', jsonKey: 'latitude',
        description: 'WGS 84 latitude measured by the GNSS sensor.',
        mapping: 'The paper’s column is gnss_latitude in radians. This mock JSON stores the equivalent coordinate in degrees under latitude.',
        source: 'ZTBus paper, Table 1; app-side unit conversion'
      },
      longitude: {
        title: 'Longitude', jsonKey: 'longitude',
        description: 'WGS 84 longitude measured by the GNSS sensor.',
        mapping: 'The paper’s column is gnss_longitude in radians. This mock JSON stores the equivalent coordinate in degrees under longitude.',
        source: 'ZTBus paper, Table 1; app-side unit conversion'
      },
      heading_deg: {
        title: 'Heading', jsonKey: 'heading_deg',
        description: 'Direction of travel supplied by the GNSS sensor. North is 0° (or 360°) and east is 90°.',
        mapping: 'This mock JSON key is a degree-converted alias for the paper’s gnss_course column, which is in radians. At a standstill, the paper notes that the value is held or set to zero depending on the bus.',
        source: 'ZTBus paper, Table 1; app-side unit conversion'
      },
      temp_celsius: {
        title: 'Ambient temperature', jsonKey: 'temp_celsius',
        description: 'Ambient temperature measured by a sensor behind the front bumper. The paper notes a sensor resolution of 1 K.',
        mapping: 'The paper’s source column is temperature_ambient in kelvin; this mock JSON provides the value in degrees Celsius.',
        source: 'ZTBus paper, Table 1; app-side unit conversion'
      },
      status_gridIsAvailable: {
        title: 'Grid connection', jsonKey: 'status_gridIsAvailable',
        description: 'Boolean flag indicating whether the trolleybus current collector is connected to the overhead power grid.',
        source: 'ZTBus paper, Table 1'
      }
    };
    const parameterInfoTooltip = document.getElementById('parameter-info-tooltip');
    const parameterInfoTitle = document.getElementById('parameter-info-title');
    const parameterInfoKey = document.getElementById('parameter-info-key');
    const parameterInfoDescription = document.getElementById('parameter-info-description');
    const parameterInfoMapping = document.getElementById('parameter-info-mapping');
    const parameterInfoSource = document.getElementById('parameter-info-source');
    const parameterInfoCloseButton = document.getElementById('parameter-info-close');
    let activeParameterInfoButton = null;
    let activeParameterInfoKey = null;

    function openParameterInfo(key, button) {
      const info = parameterInfo[key];
      if (!info) return;
      if (!parameterInfoTooltip.hidden && activeParameterInfoKey === key) {
        closeParameterInfoTooltip(false);
        return;
      }
      activeParameterInfoButton = button;
      activeParameterInfoKey = key;
      parameterInfoTitle.textContent = info.title;
      parameterInfoKey.textContent = info.jsonKey;
      parameterInfoKey.setAttribute('aria-label', `Show ${info.jsonKey} in captured JSON`);
      parameterInfoDescription.textContent = info.description;
      parameterInfoMapping.textContent = info.mapping || '';
      parameterInfoMapping.hidden = !info.mapping;
      parameterInfoSource.textContent = `Dataset reference: ${info.source}`;
      parameterInfoTooltip.hidden = false;
      requestAnimationFrame(() => {
        positionParameterInfoTooltip(button);
        parameterInfoKey.focus();
      });
    }

    function positionParameterInfoTooltip(button) {
      if (parameterInfoTooltip.hidden || !button?.isConnected) return;
      const anchor = button.getBoundingClientRect();
      const margin = 12;
      const width = parameterInfoTooltip.offsetWidth;
      const height = parameterInfoTooltip.offsetHeight;
      let left = Math.max(margin, Math.min(anchor.left, window.innerWidth - width - margin));
      let top = anchor.bottom + 8;
      if (top + height > window.innerHeight - margin) top = anchor.top - height - 8;
      top = Math.max(margin, Math.min(top, window.innerHeight - height - margin));
      left = Math.max(margin, left);
      parameterInfoTooltip.style.left = `${left}px`;
      parameterInfoTooltip.style.top = `${top}px`;
    }

    function closeParameterInfoTooltip(restoreFocus = false) {
      if (parameterInfoTooltip.hidden) return;
      parameterInfoTooltip.hidden = true;
      const trigger = activeParameterInfoButton;
      activeParameterInfoButton = null;
      activeParameterInfoKey = null;
      if (restoreFocus && trigger?.isConnected) trigger.focus();
    }

    document.querySelectorAll('.inspection-label[data-info-key]').forEach(label => {
      const key = label.dataset.infoKey;
      const info = parameterInfo[key];
      if (!info) return;
      const button = document.createElement('button');
      button.className = 'parameter-info-button';
      button.type = 'button';
      button.textContent = 'i';
      button.setAttribute('aria-label', `More information about ${info.title}`);
      button.title = `More information about ${info.title}`;
      button.addEventListener('click', () => openParameterInfo(key, button));
      label.appendChild(button);
    });
    parameterInfoCloseButton.addEventListener('click', () => closeParameterInfoTooltip(true));
    parameterInfoKey.addEventListener('click', () => {
      const key = activeParameterInfoKey;
      closeParameterInfoTooltip(false);
      if (key) showJsonParameter(key);
    });
    document.addEventListener('click', event => {
      if (parameterInfoTooltip.hidden || parameterInfoTooltip.contains(event.target)) return;
      if (event.target.closest?.('.parameter-info-button')) return;
      closeParameterInfoTooltip(false);
    });
    document.addEventListener('keydown', event => {
      if (event.key !== 'Escape' || parameterInfoTooltip.hidden) return;
      event.preventDefault();
      event.stopPropagation();
      closeParameterInfoTooltip(true);
    });
    window.addEventListener('resize', () => {
      if (!parameterInfoTooltip.hidden) positionParameterInfoTooltip(activeParameterInfoButton);
    });
    document.getElementById('inspection-checklist').addEventListener('scroll', () => {
      if (!parameterInfoTooltip.hidden) closeParameterInfoTooltip(false);
    }, { passive: true });

    const topviewOpenButton = document.getElementById('topview-open');
    const topviewModal = document.getElementById('topview-modal');
    const topviewCloseButton = document.getElementById('topview-close');
    const topviewDialog = document.getElementById('topview-dialog');
    const topviewDiagram = document.getElementById('topview-diagram');
    const inspectionChecklist = document.getElementById('inspection-checklist');
    const inspectionDrawing = document.getElementById('inspection-drawing');
    const capturedJsonView = document.getElementById('captured-json-view');
    const capturedJson = document.getElementById('captured-json');
    const jsonToggleButton = document.getElementById('json-toggle');
    const inspectionTitleLabel = document.querySelector('#inspection-title p');
    const inspectionTitleHeading = document.querySelector('#inspection-title h2');

    const inspectionTargetSelector = '[data-inspection-target], [data-inspection-targets]';
    const inspectionTargets = [...document.querySelectorAll(inspectionTargetSelector)];
    function closestInspectionTarget(element) {
      return element?.closest?.(inspectionTargetSelector) || null;
    }
    function targetKeys(element) {
      if (element.dataset.inspectionTarget) return [element.dataset.inspectionTarget];
      return (element.dataset.inspectionTargets || '').split(/\s+/).filter(Boolean);
    }
    function highlightInspectionTarget(targetElement) {
      const activeKeys = new Set(targetKeys(targetElement));
      inspectionTargets.forEach(element => {
        const isMatch = targetKeys(element).some(key => activeKeys.has(key));
        element.classList.toggle('is-highlighted', isMatch);
      });
    }
    function clearInspectionHighlight() {
      inspectionTargets.forEach(element => element.classList.remove('is-highlighted'));
    }
    function bindInspectionHighlighting(surface) {
      surface.addEventListener('pointerover', event => {
        const target = closestInspectionTarget(event.target);
        if (target) highlightInspectionTarget(target);
      });
      surface.addEventListener('pointerout', event => {
        const fromTarget = closestInspectionTarget(event.target);
        const toTarget = closestInspectionTarget(event.relatedTarget);
        if (fromTarget === toTarget) return;
        if (toTarget) highlightInspectionTarget(toTarget);
        else clearInspectionHighlight();
      });
      surface.addEventListener('focusin', event => {
        const target = closestInspectionTarget(event.target);
        if (target) highlightInspectionTarget(target);
      });
      surface.addEventListener('focusout', event => {
        const toTarget = closestInspectionTarget(event.relatedTarget);
        if (toTarget) highlightInspectionTarget(toTarget);
        else clearInspectionHighlight();
      });
    }
    bindInspectionHighlighting(inspectionChecklist);
    bindInspectionHighlighting(inspectionDrawing);

    // ---- Inspection drawing zoom: grows sideways only, up to the panel height, and pans horizontally.
    const inspectionScroller = document.getElementById('inspection-scroller');
    const inspectionSvg = inspectionScroller.querySelector('svg');
    const zoomInButton = document.getElementById('inspection-zoom-in');
    const zoomOutButton = document.getElementById('inspection-zoom-out');
    const zoomLevelButton = document.getElementById('inspection-zoom-level');
    const ZOOM_STEP = 1.25;
    let inspectionZoom = 1;
    let inspectionViewBoxCropped = false;
    let inspectionPan = null;

    function cropInspectionViewBox() {
      // Trim the empty margins of the 1400×620 artboard so 100% already shows the bus as large as fits.
      if (inspectionViewBoxCropped) return;
      try {
        const box = inspectionSvg.getBBox();
        if (box.width > 0 && box.height > 0) {
          const pad = 22;
          inspectionSvg.setAttribute('viewBox', `${box.x - pad} ${box.y - pad} ${box.width + pad * 2} ${box.height + pad * 2}`);
          inspectionViewBoxCropped = true;
        }
      } catch (_) {}
    }
    function baseInspectionWidth() {
      return Math.max(inspectionScroller.clientWidth, window.innerWidth <= 700 ? 700 : 0);
    }
    function maxInspectionZoom() {
      const viewBox = inspectionSvg.viewBox.baseVal;
      const base = baseInspectionWidth();
      if (!viewBox || !viewBox.width || !base) return 1;
      const heightAtFit = base * viewBox.height / viewBox.width;
      // Leave room for a horizontal scrollbar so zooming in never creates a vertical scroll.
      const fitHeight = (inspectionDrawing.clientHeight - 20) / heightAtFit;
      return Math.max(1, Math.min(6, fitHeight));
    }
    function setInspectionZoom(requested, anchorClientX = null) {
      if (topviewModal.hidden || topviewDiagram.hidden) return;
      const maximum = maxInspectionZoom();
      const zoom = Math.max(1, Math.min(maximum, requested));
      const scrollerRect = inspectionScroller.getBoundingClientRect();
      const anchorX = anchorClientX === null ? scrollerRect.width / 2 : anchorClientX - scrollerRect.left;
      const oldWidth = inspectionSvg.getBoundingClientRect().width || 1;
      const anchorFraction = (inspectionScroller.scrollLeft + anchorX) / oldWidth;
      inspectionZoom = zoom;
      inspectionSvg.style.width = `${Math.round(baseInspectionWidth() * zoom)}px`;
      // Keep the point under the cursor (or the centre) in place while the drawing grows or shrinks.
      inspectionScroller.scrollLeft = anchorFraction * inspectionSvg.getBoundingClientRect().width - anchorX;
      zoomLevelButton.textContent = `${Math.round(zoom * 100)}%`;
      zoomOutButton.disabled = zoom <= 1.001;
      zoomInButton.disabled = zoom >= maximum - 0.001;
      inspectionScroller.classList.toggle('is-zoomed', inspectionScroller.scrollWidth > inspectionScroller.clientWidth + 1);
    }
    function resetInspectionZoom() {
      cropInspectionViewBox();
      setInspectionZoom(1);
      inspectionScroller.scrollLeft = 0;
    }
    zoomInButton.addEventListener('click', () => setInspectionZoom(inspectionZoom * ZOOM_STEP));
    zoomOutButton.addEventListener('click', () => setInspectionZoom(inspectionZoom / ZOOM_STEP));
    zoomLevelButton.addEventListener('click', resetInspectionZoom);
    inspectionScroller.addEventListener('wheel', event => {
      if (event.ctrlKey || event.metaKey) {
        // Trackpad pinch arrives as ctrl+wheel; ctrl/⌘ + mouse wheel zooms too.
        event.preventDefault();
        setInspectionZoom(inspectionZoom * Math.exp(-event.deltaY * 0.004), event.clientX);
        return;
      }
      // There is nothing to scroll vertically, so a vertical wheel moves the drawing sideways.
      if (Math.abs(event.deltaY) > Math.abs(event.deltaX) && inspectionScroller.scrollWidth > inspectionScroller.clientWidth) {
        event.preventDefault();
        inspectionScroller.scrollLeft += event.deltaY;
      }
    }, { passive: false });
    inspectionScroller.addEventListener('pointerdown', event => {
      if (event.pointerType !== 'mouse' || event.button !== 0) return;  // touch already pans natively
      if (inspectionScroller.scrollWidth <= inspectionScroller.clientWidth) return;
      inspectionPan = { id: event.pointerId, x: event.clientX, scroll: inspectionScroller.scrollLeft, moved: false };
    });
    inspectionScroller.addEventListener('pointermove', event => {
      if (!inspectionPan || event.pointerId !== inspectionPan.id) return;
      const deltaX = event.clientX - inspectionPan.x;
      if (!inspectionPan.moved) {
        if (Math.abs(deltaX) < 4) return;
        inspectionPan.moved = true;
        inspectionScroller.setPointerCapture(event.pointerId);
        inspectionScroller.classList.add('is-panning');
      }
      inspectionScroller.scrollLeft = inspectionPan.scroll - deltaX;
    });
    function endInspectionPan(event) {
      if (!inspectionPan || event.pointerId !== inspectionPan.id) return;
      if (inspectionScroller.hasPointerCapture(event.pointerId)) inspectionScroller.releasePointerCapture(event.pointerId);
      inspectionScroller.classList.remove('is-panning');
      inspectionPan = null;
    }
    inspectionScroller.addEventListener('pointerup', endInspectionPan);
    inspectionScroller.addEventListener('pointercancel', endInspectionPan);
    window.addEventListener('keydown', event => {
      if (topviewModal.hidden || topviewDiagram.hidden || event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.target.closest?.('input, textarea, select')) return;
      if (event.key === '+' || event.key === '=') setInspectionZoom(inspectionZoom * ZOOM_STEP);
      else if (event.key === '-' || event.key === '_') setInspectionZoom(inspectionZoom / ZOOM_STEP);
      else if (event.key === '0') resetInspectionZoom();
      else return;
      event.preventDefault();
    });
    window.addEventListener('resize', () => setInspectionZoom(inspectionZoom));

    function renderCapturedJson(highlightKey = null) {
      capturedJson.replaceChildren();
      let highlightedLine = null;
      for (const lineText of JSON.stringify(telemetry, null, 2).split('\n')) {
        const line = document.createElement('span');
        line.className = 'json-line';
        line.textContent = lineText;
        const keyMatch = lineText.match(/^\s*("(?:\\.|[^"\\])*")\s*:/);
        if (keyMatch) {
          try {
            const jsonKey = JSON.parse(keyMatch[1]);
            line.dataset.jsonKey = jsonKey;
            if (jsonKey === highlightKey) {
              line.classList.add('json-line-highlight');
              highlightedLine = line;
            }
          } catch (_) {}
        }
        capturedJson.appendChild(line);
      }
      return highlightedLine;
    }

    function setInspectionMode(showJson, highlightKey = null) {
      topviewDiagram.hidden = showJson;
      capturedJsonView.hidden = !showJson;
      const highlightedLine = renderCapturedJson(showJson ? highlightKey : null);
      jsonToggleButton.textContent = showJson ? 'Back to inspection' : 'View JSON';
      jsonToggleButton.setAttribute('aria-label', showJson ? 'Back to the inspection sheet' : 'View captured JSON');
      inspectionTitleLabel.textContent = showJson ? 'DiTEC · captured record' : twin.live ? 'DiTEC · live twin reading' : 'DiTEC · reference reading';
      inspectionTitleHeading.textContent = showJson ? 'Captured JSON' : 'Bus inspection sheet';
      topviewDialog.setAttribute('aria-label', showJson ? 'Captured bus JSON' : 'Bus inspection sheet');
      if (highlightedLine) requestAnimationFrame(() => {
        capturedJson.focus({ preventScroll: true });
        highlightedLine.scrollIntoView({ block: 'center', behavior: 'smooth' });
      });
    }

    function showJsonParameter(key) {
      setInspectionMode(true, key);
    }

    function openTopviewModal() {
      setInspectionMode(false);
      topviewModal.hidden = false;
      topviewModal.setAttribute('aria-hidden', 'false');
      topviewCloseButton.focus();
      requestAnimationFrame(resetInspectionZoom);  // needs layout: the drawing is measured once visible
    }
    function closeTopviewModal() {
      if (topviewModal.hidden) return;
      topviewModal.hidden = true;
      topviewModal.setAttribute('aria-hidden', 'true');
      topviewOpenButton.focus();
    }
    topviewOpenButton.addEventListener('click', openTopviewModal);
    jsonToggleButton.addEventListener('click', () => setInspectionMode(capturedJsonView.hidden));
    topviewCloseButton.addEventListener('click', closeTopviewModal);
    topviewModal.addEventListener('click', event => {
      if (event.target === topviewModal) closeTopviewModal();
    });
    window.addEventListener('keydown', event => {
      if (event.key === 'Escape') closeTopviewModal();
    });

    const gaugeArcLength = Math.PI * 61;
    const gaugeProgress = document.getElementById('gauge-progress');
    const gaugeNeedle = document.getElementById('gauge-needle');
    const speedValueElement = document.getElementById('speed-value');
    const powerValueElement = document.getElementById('power-value');
    const powerRawElement = document.getElementById('power-raw-value');
    gaugeProgress.style.strokeDasharray = `${gaugeArcLength} ${gaugeArcLength}`;
    function setGaugeSpeed(speedKmh) {
      const clamped = Math.max(0, Math.min(60, speedKmh));
      gaugeProgress.style.strokeDashoffset = `${gaugeArcLength * (1 - clamped / 60)}`;
      gaugeNeedle.setAttribute('transform', `rotate(${-90 + clamped * 3} 80 79)`);
      speedValueElement.textContent = Math.max(0, speedKmh).toLocaleString('en-GB', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
    }
    function renderPowerReading() {
      const powerKw = Number(telemetry.power_kw) || 0;
      const rawPower = Number(telemetry.electric_powerDemand) || powerKw * 1000;
      powerValueElement.textContent = `${powerKw.toLocaleString('en-GB', { minimumFractionDigits: 1, maximumFractionDigits: 1 })} kW`;
      powerRawElement.textContent = `${rawPower.toLocaleString('en-GB', { maximumFractionDigits: 0 })} W demand`;
    }
    setGaugeSpeed(Number(telemetry.speed_kmh) || 0);
    renderPowerReading();

    const passengerButtons = document.querySelectorAll('.passenger-button');
    const layoutScreen = document.getElementById('screen');
    const mapPane = document.getElementById('map-pane');
    const mapResizeHandles = [...document.querySelectorAll('.map-resize-handle')];
    let tripBusX = 0;
    let cameraFollowX = 0;
    let newRoadSegmentsSinceCleanup = 0;
    let mapPipRect = {
      x: 18,
      y: Math.max(18, layoutScreen.clientHeight - 278),
      width: 360,
      height: 260
    };
    let mapResizeStart = null;
    let mapInvalidatePending = false;

    function clamp(value, minimum, maximum) {
      return Math.max(minimum, Math.min(maximum, value));
    }

    function scheduleMapInvalidate() {
      if (mapInvalidatePending) return;
      mapInvalidatePending = true;
      requestAnimationFrame(() => {
        mapInvalidatePending = false;
        map.invalidateSize({ pan: false });
      });
    }

    function setMapPipRect(requestedRect) {
      const bounds = layoutScreen.getBoundingClientRect();
      const maximumWidth = Math.max(1, Math.min(900, bounds.width - 36));
      const maximumHeight = Math.max(1, Math.min(720, bounds.height - 36));
      const minimumWidth = Math.min(190, maximumWidth);
      const minimumHeight = Math.min(150, maximumHeight);
      const width = clamp(Number(requestedRect.width) || 360, minimumWidth, maximumWidth);
      const height = clamp(Number(requestedRect.height) || 260, minimumHeight, maximumHeight);
      const x = clamp(Number(requestedRect.x) || 18, 18, Math.max(18, bounds.width - 18 - width));
      const y = clamp(Number(requestedRect.y) || 18, 18, Math.max(18, bounds.height - 18 - height));
      mapPipRect = { x, y, width, height };
      layoutScreen.style.setProperty('--map-pip-x', `${x}px`);
      layoutScreen.style.setProperty('--map-pip-y', `${y}px`);
      layoutScreen.style.setProperty('--map-pip-width', `${width}px`);
      layoutScreen.style.setProperty('--map-pip-height', `${height}px`);
      scheduleMapInvalidate();
    }

    function resizeMapFromPointer(event, handle) {
      if (!mapResizeStart || mapResizeStart.pointerId !== event.pointerId || !handle.hasPointerCapture(event.pointerId)) return;
      const start = mapResizeStart;
      const edge = start.edge;
      const deltaX = event.clientX - start.pointerX;
      const deltaY = event.clientY - start.pointerY;
      const bounds = layoutScreen.getBoundingClientRect();
      let { x, y, width, height } = start.rect;
      const availableWidth = Math.max(1, Math.min(900, bounds.width - 36));
      const availableHeight = Math.max(1, Math.min(720, bounds.height - 36));
      const minimumWidth = Math.min(190, availableWidth);
      const minimumHeight = Math.min(150, availableHeight);

      if (edge.includes('e')) {
        const maximum = Math.min(availableWidth, bounds.width - 18 - start.rect.x);
        width = clamp(start.rect.width + deltaX, minimumWidth, Math.max(minimumWidth, maximum));
      }
      if (edge.includes('w')) {
        const right = start.rect.x + start.rect.width;
        const maximum = Math.min(availableWidth, right - 18);
        width = clamp(start.rect.width - deltaX, minimumWidth, Math.max(minimumWidth, maximum));
        x = right - width;
      }
      if (edge.includes('s')) {
        const maximum = Math.min(availableHeight, bounds.height - 18 - start.rect.y);
        height = clamp(start.rect.height + deltaY, minimumHeight, Math.max(minimumHeight, maximum));
      }
      if (edge.includes('n')) {
        const bottom = start.rect.y + start.rect.height;
        const maximum = Math.min(availableHeight, bottom - 18);
        height = clamp(start.rect.height - deltaY, minimumHeight, Math.max(minimumHeight, maximum));
        y = bottom - height;
      }
      setMapPipRect({ x, y, width, height });
    }

    function finishMapResize(event, handle) {
      mapPane.classList.remove('resizing');
      mapResizeStart = null;
      if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId);
    }

    for (const handle of mapResizeHandles) {
      handle.addEventListener('pointerdown', event => {
        event.preventDefault();
        mapPane.classList.add('resizing');
        handle.setPointerCapture(event.pointerId);
        mapResizeStart = {
          edge: handle.dataset.edge,
          pointerId: event.pointerId,
          pointerX: event.clientX,
          pointerY: event.clientY,
          rect: { ...mapPipRect }
        };
      });
      handle.addEventListener('pointermove', event => resizeMapFromPointer(event, handle));
      handle.addEventListener('pointerup', event => finishMapResize(event, handle));
      handle.addEventListener('pointercancel', event => finishMapResize(event, handle));
      handle.addEventListener('keydown', event => {
        if (!['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home'].includes(event.key)) return;
        event.preventDefault();
        if (event.key === 'Home') {
          const bounds = layoutScreen.getBoundingClientRect();
          setMapPipRect({ x: 18, y: bounds.height - 278, width: 360, height: 260 });
          return;
        }
        const edge = handle.dataset.edge;
        const deltaX = event.key === 'ArrowRight' ? 24 : event.key === 'ArrowLeft' ? -24 : 0;
        const deltaY = event.key === 'ArrowDown' ? 24 : event.key === 'ArrowUp' ? -24 : 0;
        const next = { ...mapPipRect };
        if (edge.includes('e')) next.width += deltaX;
        if (edge.includes('w')) { next.x += deltaX; next.width -= deltaX; }
        if (edge.includes('s')) next.height += deltaY;
        if (edge.includes('n')) { next.y += deltaY; next.height -= deltaY; }
        setMapPipRect(next);
      });
    }
    setMapPipRect(mapPipRect);

    function disposeRoadSegment(segment) {
      animationScene.remove(segment);
      segment.traverse(object => {
        if (object.isMesh && object.geometry) object.geometry.dispose();
      });
      segment.clear();
    }

    function removeOldRoadSegments() {
      const keepBehindBus = tripBusX - 21;
      for (let index = animationRoadSegments.length - 1; index >= 0; index -= 1) {
        const segment = animationRoadSegments[index];
        if (segment.position.x + roadSegmentLength / 2 < keepBehindBus) {
          disposeRoadSegment(segment);
          animationRoadSegments.splice(index, 1);
        }
      }
    }

    function ensureRoadAhead() {
      let furthestCenter = Math.max(...animationRoadSegments.map(segment => segment.position.x));
      while (furthestCenter + roadSegmentLength / 2 < tripBusX + 25) {
        furthestCenter += roadSegmentLength;
        animationRoadSegments.push(createRoadSegment(animationScene, furthestCenter));
        newRoadSegmentsSinceCleanup += 1;
        if (newRoadSegmentsSinceCleanup >= 2) {
          newRoadSegmentsSinceCleanup = 0;
          removeOldRoadSegments();
        }
      }
    }

    const wheelRadius = Number(animatedBus.userData.wheelRadius) || 0.55;
    // The shadow-casting key light travels with the bus, or shadows vanish after ~15 m of driving.
    const followLights = [];
    animationScene.traverse(object => { if (object.isDirectionalLight && object.castShadow) followLights.push(object); });
    // The finite ground plane travels with the bus too, so the horizon never shows its edge.
    const groundFloor = animationScene.getObjectByName('ground-floor');
    function moveBusForward(distance) {
      if (distance <= 0) return;
      tripBusX += distance;
      animatedBus.position.x = tripBusX;
      const cameraDelta = tripBusX - cameraFollowX;
      animationView.camera.position.x += cameraDelta;
      animationView.controls.target.x += cameraDelta;
      for (const light of followLights) {
        light.position.x += cameraDelta;
        light.target.position.x += cameraDelta;
        light.target.updateMatrixWorld();
      }
      if (groundFloor) groundFloor.position.x += cameraDelta;
      cameraFollowX = tripBusX;
      // Forward travel along +X rolls the wheel clockwise from the passenger side.
      const wheelSpin = -distance / wheelRadius;
      for (const wheel of animatedWheelMeshes) {
        wheel.rotation.z = (wheel.rotation.z + wheelSpin) % (Math.PI * 2);
      }
      ensureRoadAhead();
    }

    function startPassengerAnimation(mode, count) {
      if (boardingActive) return false;
      animationPassengerCount = Math.max(1, Math.min(8, Math.round(count) || 1));
      ensurePassengerModels(animationPassengerCount);
      boardingActive = true;
      passengerChange = mode;
      boardingElapsed = 0;
      const doorX = busDoorX(mode === 'board' ? 'front' : 'alight');
      passengerModels.forEach((model, index) => {
        model.group.visible = index < animationPassengerCount;
        if (mode === 'board') {
          model.group.position.set(doorX - passengerQueueSpacing * index, 0, passengerQueueStartZ);
          model.group.rotation.y = index === 0 ? Math.PI * 2 : Math.PI / 2;
        } else {
          model.group.position.set(doorX + passengerQueueSpacing * index, 0.42, passengerInsideZ);
          model.group.rotation.y = index === 0 ? 0 : -Math.PI / 2;
        }
      });
      passengerButtons.forEach(button => { button.disabled = true; });
      passengerCountInput.disabled = true;
      return true;
    }

    function finishPassengerAnimation() {
      boardingActive = false;
      passengerButtons.forEach(button => { button.disabled = false; });
      passengerCountInput.disabled = false;
    }

    // ---------------------------------------------------------------------------------------------
    // Digital twin. Every MQTT message the state bridge stored in Redis is applied, in order, to
    // `telemetry` (the same object the inspection sheet and HUD read), and the changes drive the
    // scene: speed moves the bus and the map, doors open, passenger deltas walk people in or out,
    // a stop name or open doors while stationary bring up a stop, the halt brake lights the brake
    // lamps, grid availability raises or lowers the trolley poles, steering/articulation pose the bus.
    // ---------------------------------------------------------------------------------------------
    const FEED_PORT = __TWIN_FEED_PORT__;
    const feedHost = (() => {
      // The feed binds IPv4 only, so avoid a refused ::1 attempt when the page is on "localhost".
      try {
        const host = window.parent.location.hostname;
        return !host || host === 'localhost' ? '127.0.0.1' : host;
      } catch (_) { return '127.0.0.1'; }
    })();
    const feedUrl = FEED_PORT ? `http://${feedHost}:${FEED_PORT}/twin` : '';
    const MOTION_SCALE = 0.55;          // scene units per metre travelled, so city speeds read calmly
    const STOPPED_KMH = 1.5;
    const DEPART_KMH = 4;
    const fieldMap = {
      time_unix: 'time_unix',
      latitude: 'latitude', longitude: 'longitude', altitude: 'gnss_altitude',
      speed_kmh: 'speed_kmh', heading_deg: 'heading_deg',
      power_kw: 'power_kw', grid_available: 'status_gridIsAvailable',
      doors_open: 'status_doorIsOpen', passengers: 'itcs_numberOfPassengers', temp_celsius: 'temp_celsius',
      stop_name: 'itcs_stopName', bus_route: 'itcs_busRoute',
      steering_angle_rad: 'odometry_steeringAngle', articulation_angle_rad: 'odometry_articulationAngle',
      wheel_speed_fl: 'odometry_wheelSpeed_fl', wheel_speed_fr: 'odometry_wheelSpeed_fr',
      wheel_speed_ml: 'odometry_wheelSpeed_ml', wheel_speed_mr: 'odometry_wheelSpeed_mr',
      wheel_speed_rl: 'odometry_wheelSpeed_rl', wheel_speed_rr: 'odometry_wheelSpeed_rr',
      halt_brake: 'status_haltBrakeIsActive', park_brake: 'status_parkBrakeIsActive',
      brake_bar: 'brake_bar', traction_force_n: 'traction_tractionForce'
    };
    const textFields = new Set(['itcs_stopName', 'itcs_busRoute']);

    const twin = {
      live: false,              // telemetry holds replayed state rather than the sample record
      online: false,            // the feed answered and Redis is reachable
      status: 'offline',        // replay control status from Redis
      replaySpeed: 1,
      startedAt: null,
      lastMessageAt: 0,
      syncing: false,
      atStop: true,             // the initial scene shows the bus beside the shelter
      stopName: null,
      visualSpeed: 0,
      gaugeSpeed: Number(telemetry.speed_kmh) || 0,
      doorAngle: 0,
      poleBlend: 1,
      brakeGlow: 0,
      shelterPop: 1,
      needsSnap: false,         // jump the scene to the current state on the next frame
      sceneOrigin: null,        // scene x of the route start (metre 0), set when the twin snaps
      awaitingFirstState: false,
      lastFix: null,            // previous GNSS fix, to estimate speed if the speed signal is missing
      gpsSpeedKmh: null
    };
    // Output of the rule-based state machine (src/state_machine), computed by the state bridge and
    // streamed as ztbus/<bus>/state messages. Kept apart from `telemetry`, which mirrors raw sensors.
    const twinState = { inferred: __TWIN_STATE_JSON__, since: '', ml: null, changed: false };
    const pendingPassengerChanges = [];
    let panelsDirty = true;
    let lastPanelRender = 0;
    let debugTimers = [];

    const trolleyPoles = ['trolley-pole-left', 'trolley-pole-right']
      .map(name => animatedBus.getObjectByName(name))
      .filter(pole => pole && Number.isFinite(pole.userData.raisedAngle) && Number.isFinite(pole.userData.loweredAngle));
    for (const pole of trolleyPoles) {
      // userData is copied by JSON on clone, so attach the head object after the lookup, not in the model.
      Object.defineProperty(pole.userData, 'head', { value: animatedBus.getObjectByName(pole.name.replace('pole', 'head')), enumerable: false });
    }
    const extraDoorLeaves = [];
    const brakeLightMaterials = new Set();
    animatedBus.traverse(object => {
      const match = /^door-\d+-(left|right)-leaf$/.exec(object.name || '');
      if (match) extraDoorLeaves.push({ leaf: object, sign: match[1] === 'left' ? -1 : 1 });
      if ((object.name || '').startsWith('brake-light') && object.material) {
        for (const material of [].concat(object.material)) {
          if (material.emissive) brakeLightMaterials.add(material);
        }
      }
    });
    for (const material of brakeLightMaterials) material.userData.baseEmissive = material.emissiveIntensity;
    const shelterOffsetFromBus = movingShelter.position.x - animatedBus.position.x;
    const shelterBaseScale = movingShelter.scale.clone();

    const twinStatus = document.getElementById('twin-status');
    const twinStatusText = document.getElementById('twin-status-text');
    const stopBanner = document.getElementById('stop-banner');
    const stopBannerLabel = document.getElementById('stop-banner-label');
    const stopBannerName = document.getElementById('stop-banner-name');
    let stopBannerTimer = null;

    function setOverheadLinePowered(powered) {
      if (powered === overheadLinePowered) return;
      overheadLinePowered = powered;
      // Change the line from the segment under the bus onwards; what is behind keeps its wires.
      for (const segment of animationRoadSegments) {
        if (segment.position.x + roadSegmentLength / 2 < tripBusX - 2) continue;
        const overhead = segment.getObjectByName('overhead-line');
        if (overhead) overhead.visible = powered;
      }
    }

    function toNumber(value) {
      if (value === null || value === undefined || value === '') return null;
      const number = Number(value);
      return Number.isFinite(number) ? number : null;
    }
    function liveSpeedKmh() {
      const speed = toNumber(telemetry.speed_kmh);
      return speed ?? twin.gpsSpeedKmh ?? 0;
    }
    function isOn(key) { return toNumber(telemetry[key]) === 1; }

    function showStopBanner(label, name) {
      clearTimeout(stopBannerTimer);
      stopBannerLabel.textContent = label;
      stopBannerName.textContent = name;
      stopBanner.classList.remove('is-hidden');
    }
    function hideStopBanner(afterMs = 0) {
      clearTimeout(stopBannerTimer);
      stopBannerTimer = setTimeout(() => stopBanner.classList.add('is-hidden'), afterMs);
    }

    function flashChip(id) {
      if (twin.syncing) return;  // only real changes flash, not the initial resync
      const chip = document.getElementById(id);
      chip.classList.remove('flash');
      void chip.offsetWidth;
      chip.classList.add('flash');
    }

    // ---- Stops along the route -------------------------------------------------------------------
    // The 3D road is the route unrolled: scene x = sceneOrigin + metres travelled × MOTION_SCALE. Each
    // stop gets a shelter at its real distance, so the bus either pulls up beside it or drives past.
    const STOP_MATCH_METRES = 40;
    const stopShelters = new Map();
    const stopSignTextures = new Map();
    let nextStopVisit = 0;

    function odometerAt(timeUnix) {
      let low = 0;
      let high = route.length - 1;
      if (high < 0 || route[0].length < 4) return null;
      while (low < high) {
        const middle = (low + high + 1) >> 1;
        if (route[middle][2] <= timeUnix) low = middle;
        else high = middle - 1;
      }
      return route[low][3];
    }
    function currentOdometer() {
      const stamp = toNumber(telemetry.time_unix);
      return twin.live && stamp !== null ? odometerAt(stamp) : null;
    }
    function nearestStopVisit(distance) {
      let best = null;
      stopVisits.forEach((visit, index) => {
        const gap = Math.abs(visit.d - distance);
        if (gap <= STOP_MATCH_METRES && (!best || gap < best.gap)) best = { visit, index, gap };
      });
      return best;
    }
    function setStopVisitStatus(index, status) {
      const visit = stopVisits[index];
      if (!visit || visit.status === status) return;
      visit.status = status;
      window.twinMap?.setStopStatus(index, status);
    }
    function resetStopVisits(distance) {
      // Stops already behind the bus take their outcome from the replayed data; the rest wait for the twin.
      stopVisits.forEach((visit, index) => {
        visit.liveServed = false;
        const passed = distance !== null && visit.d < distance - STOP_MATCH_METRES;
        setStopVisitStatus(index, passed ? (visit.served ? 'served' : 'skipped') : 'ahead');
      });
      nextStopVisit = stopVisits.findIndex(visit => visit.status === 'ahead');
      if (nextStopVisit < 0) nextStopVisit = stopVisits.length;
    }
    function updateStopVisits(distance) {
      if (distance === null) return;
      if (twin.atStop) {
        const nearest = nearestStopVisit(distance);
        if (nearest) nearest.visit.liveServed = true;
      }
      while (nextStopVisit < stopVisits.length && distance > stopVisits[nextStopVisit].d + STOP_MATCH_METRES) {
        const visit = stopVisits[nextStopVisit];
        const served = visit.liveServed;
        setStopVisitStatus(nextStopVisit, served ? 'served' : 'skipped');
        if (!served && !twin.atStop) {
          showStopBanner('Did not stop at', visit.name);
          hideStopBanner(3500);
        }
        nextStopVisit += 1;
      }
    }
    function stopSignTexture(name) {
      if (stopSignTextures.has(name)) return stopSignTextures.get(name);
      const label = name.replace(/^Zürich,\s*/, '');
      const canvas = document.createElement('canvas');
      const context = canvas.getContext('2d');
      context.font = '600 44px system-ui, sans-serif';
      canvas.width = Math.min(1024, Math.ceil(context.measureText(label).width) + 110);
      canvas.height = 84;
      context.fillStyle = '#13314a';
      context.beginPath();
      context.roundRect(0, 0, canvas.width, canvas.height, 18);
      context.fill();
      context.fillStyle = '#f3c331';
      context.beginPath();
      context.arc(42, 42, 24, 0, Math.PI * 2);
      context.fill();
      context.fillStyle = '#13314a';
      context.font = '800 30px system-ui, sans-serif';
      context.textAlign = 'center';
      context.textBaseline = 'middle';
      context.fillText('H', 42, 44);
      context.fillStyle = '#ffffff';
      context.font = '600 44px system-ui, sans-serif';
      context.textAlign = 'left';
      context.fillText(label, 80, 45);
      const texture = new THREE.CanvasTexture(canvas);
      texture.colorSpace = THREE.SRGBColorSpace;
      texture.userData.aspect = canvas.width / canvas.height;
      stopSignTextures.set(name, texture);
      return texture;
    }
    function createStopShelter(visit) {
      const shelter = movingShelter.clone(true);
      shelter.scale.copy(shelterBaseScale);
      shelter.visible = true;
      const texture = stopSignTexture(visit.name);
      const sign = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, depthWrite: false }));
      const height = 0.62;
      sign.scale.set(height * texture.userData.aspect, height, 1);
      sign.position.set(0, 3.55, 0);
      shelter.add(sign);
      animationScene.add(shelter);
      return shelter;
    }
    function updateStopShelters() {
      const visible = twin.live && twin.sceneOrigin !== null;
      stopVisits.forEach((visit, index) => {
        const x = visible ? twin.sceneOrigin + visit.d * MOTION_SCALE + shelterOffsetFromBus : NaN;
        const inRange = visible && x > tripBusX - 45 && x < tripBusX + 75;
        let shelter = stopShelters.get(index);
        if (inRange) {
          if (!shelter) {
            shelter = createStopShelter(visit);
            stopShelters.set(index, shelter);
          }
          shelter.position.x = x;
        } else if (shelter) {
          animationScene.remove(shelter);
          shelter.children.filter(child => child.isSprite).forEach(sprite => sprite.material.dispose());
          stopShelters.delete(index);
        }
      });
    }

    function arriveAtStop() {
      twin.atStop = true;
      const known = nearestStopVisit(currentOdometer() ?? -1e9);
      if (known && twin.sceneOrigin !== null) {
        // The route's own shelter is already beside the bus; just name the stop.
        twin.stopName = known.visit.name;
        known.visit.liveServed = true;
        showStopBanner('Stopped at', known.visit.name);
        return;
      }
      const shelterX = tripBusX + shelterOffsetFromBus;
      if (!movingShelter.visible || Math.abs(movingShelter.position.x - shelterX) > 0.5) {
        movingShelter.position.x = shelterX;
        movingShelter.visible = true;
        twin.shelterPop = 0;
      }
      showStopBanner('Stopped at a bus stop', twin.stopName || 'Waiting for the ITCS stop name…');
    }

    function departFromStop() {
      twin.atStop = false;
      if (twin.stopName) {
        showStopBanner('Departed from', twin.stopName);
        hideStopBanner(2600);
      } else {
        hideStopBanner(0);
      }
      twin.stopName = null;
    }

    function onStopName(name) {
      twin.stopName = name;
      const speed = liveSpeedKmh();
      if (twin.atStop) {
        showStopBanner('Stopped at', name);
      } else if (speed < DEPART_KMH + 1) {
        arriveAtStop();
      } else {
        // With the route's stop list, passing a stop without stopping is reported by updateStopVisits.
        if (!stopVisits.length) {
          showStopBanner('Passing stop', name);
          hideStopBanner(3000);
        }
        twin.stopName = null;
      }
    }

    function queuePassengerChange(delta) {
      if (!delta) return;
      const last = pendingPassengerChanges[pendingPassengerChanges.length - 1];
      if (last && Math.sign(last) === Math.sign(delta)) pendingPassengerChanges[pendingPassengerChanges.length - 1] += delta;
      else pendingPassengerChanges.push(delta);
      if (pendingPassengerChanges.length > 3) pendingPassengerChanges.splice(0, pendingPassengerChanges.length - 3);
    }

    function processPassengerQueue() {
      if (boardingActive || !pendingPassengerChanges.length) return;
      if (twin.visualSpeed > 0.15) return;   // the bus is held (see updateTwinScene) until it stands still
      const delta = pendingPassengerChanges.shift();
      startPassengerAnimation(delta > 0 ? 'board' : 'alight', Math.abs(delta));
    }

    function onTwinChanges(changes) {
      if (changes.latitude || changes.longitude || changes.heading_deg) {
        const latitude = toNumber(telemetry.latitude);
        const longitude = toNumber(telemetry.longitude);
        const stamp = toNumber(telemetry.time_unix);
        if (latitude !== null && longitude !== null) {
          if (twin.lastFix && stamp !== null && stamp > twin.lastFix.stamp) {
            const metres = map.distance([twin.lastFix.latitude, twin.lastFix.longitude], [latitude, longitude]);
            twin.gpsSpeedKmh = metres / (stamp - twin.lastFix.stamp) * 3.6;
          }
          twin.lastFix = { latitude, longitude, stamp: stamp ?? 0 };
          window.twinMap?.setPosition(latitude, longitude, toNumber(telemetry.heading_deg), liveSpeedKmh() < STOPPED_KMH);
        }
      }
      if (changes.itcs_numberOfPassengers) {
        const { from, to } = changes.itcs_numberOfPassengers;
        if (!twin.syncing && Number.isFinite(from) && Number.isFinite(to)) queuePassengerChange(to - from);
        flashChip('chip-passengers');
      }
      if (changes.itcs_stopName && telemetry.itcs_stopName !== '-' && !twin.syncing) onStopName(telemetry.itcs_stopName);
      if (changes.status_doorIsOpen) flashChip('chip-doors');
      if (changes.status_haltBrakeIsActive) flashChip('chip-brake');
      if (changes.status_gridIsAvailable) flashChip('chip-grid');
      panelsDirty = true;
    }

    function applyInferredState(inferred) {
      const next = typeof inferred === 'string' && inferred.trim() ? inferred.trim() : null;
      if (next === twinState.inferred) return;
      const previousOperation = (twinState.inferred || '').split(' | ')[0];
      twinState.inferred = next;
      if (next && next.split(' | ')[0] !== previousOperation) {
        twinState.since = replayClock();
        twinState.changed = !twin.syncing;
      }
      panelsDirty = true;
    }

    function applyFields(fields) {
      if (!fields || typeof fields !== 'object') return;
      if ('inferred_state' in fields) applyInferredState(fields.inferred_state);
      const changes = {};
      for (const [field, value] of Object.entries(fields)) {
        const key = fieldMap[field];
        if (!key) continue;
        let next;
        if (textFields.has(key)) {
          next = value === null || value === undefined || value === '' ? '-' : String(value);
        } else {
          next = toNumber(value);
          if (next === null) continue;  // a missing sensor value keeps the last known reading
        }
        if (telemetry[key] !== next) {
          changes[key] = { from: telemetry[key], to: next };
          telemetry[key] = next;
        }
      }
      if (changes.speed_kmh) telemetry.odometry_vehicleSpeed = telemetry.speed_kmh / 3.6;
      if (changes.power_kw) telemetry.electric_powerDemand = telemetry.power_kw * 1000;
      if (changes.brake_bar) telemetry.traction_brakePressure = telemetry.brake_bar * 1e5;
      if (changes.time_unix) telemetry.timestamp = telemetry.time_unix * 1000;
      if (Object.keys(changes).length) onTwinChanges(changes);
    }

    function clearTwin() {
      // Replayed state replaces the sample record; a new replay run starts from an empty twin.
      for (const key of Object.keys(telemetry)) delete telemetry[key];
      window.twinMap?.reset();
      twin.lastFix = null;
      twin.gpsSpeedKmh = null;
      twin.stopName = null;
      twinState.inferred = null;
      twinState.since = '';
      pendingPassengerChanges.length = 0;
      twin.live = true;
      twin.awaitingFirstState = true;
      twin.needsSnap = true;
      twin.sceneOrigin = null;
      resetStopVisits(null);
      panelsDirty = true;
    }

    function resyncFromState(state) {
      const firstLiveState = !twin.live || twin.awaitingFirstState;
      if (!twin.live) clearTwin();
      twin.syncing = true;  // a resync sets values; it must not replay them as passenger or stop events
      applyFields(state);
      twin.syncing = false;
      twin.awaitingFirstState = false;
      window.twinMap?.showHistory(toNumber(telemetry.time_unix));
      // The scene has no "beginning": it opens on the bus exactly as the data has it right now.
      if (firstLiveState) twin.needsSnap = true;
      panelsDirty = true;
    }

    function snapSceneToTwin() {
      // Jump straight to the current state (first data, or back after the view was hidden) instead of
      // replaying an arrival, a pile of queued passengers or a slow acceleration from zero.
      pendingPassengerChanges.length = 0;
      if (boardingActive) {
        boardingActive = false;
        boardingElapsed = 0;
        for (const model of passengerModels) model.group.visible = false;
        removeExtraPassengerModels();
        finishPassengerAnimation();
      }
      passengerDoorAngles.front = 0;
      passengerDoorAngles.rear = 0;
      const speedKmh = liveSpeedKmh();
      const doorsOpen = isOn('status_doorIsOpen');
      const atStopNow = speedKmh < STOPPED_KMH && doorsOpen;
      const allowed = motionAllowed();
      twin.visualSpeed = allowed && !atStopNow ? (speedKmh / 3.6) * MOTION_SCALE : 0;
      twin.gaugeSpeed = allowed || !twin.live ? speedKmh : 0;
      setGaugeSpeed(twin.gaugeSpeed);
      twin.doorAngle = doorsOpen ? 1.12 : 0;
      const gridOn = toNumber(telemetry.status_gridIsAvailable) !== 0;
      twin.poleBlend = gridOn ? 1 : 0;
      overheadLinePowered = gridOn;
      for (const segment of animationRoadSegments) {
        const overhead = segment.getObjectByName('overhead-line');
        if (overhead) overhead.visible = gridOn;
      }
      twin.brakeGlow = isOn('status_haltBrakeIsActive') ? 1 : 0;
      twin.shelterPop = 1;
      movingShelter.scale.copy(shelterBaseScale);
      clearTimeout(stopBannerTimer);
      const distance = currentOdometer();
      twin.sceneOrigin = distance === null ? null : tripBusX - distance * MOTION_SCALE;
      resetStopVisits(distance);
      const knownStop = atStopNow && distance !== null ? nearestStopVisit(distance) : null;
      if (twin.sceneOrigin !== null) movingShelter.visible = false;  // the route's own shelters take over
      if (knownStop) {
        twin.atStop = true;
        twin.stopName = knownStop.visit.name;
        knownStop.visit.liveServed = true;
        showStopBanner('Stopped at', knownStop.visit.name);
      } else if (atStopNow) {
        twin.atStop = true;
        movingShelter.position.x = tripBusX + shelterOffsetFromBus;
        movingShelter.visible = true;
        showStopBanner('Stopped at a bus stop', twin.stopName || 'Waiting for the ITCS stop name…');
      } else {
        twin.atStop = false;
        twin.stopName = null;
        // A shelter still beside the bus would claim a stop that is not in the data.
        if (Math.abs(movingShelter.position.x - (tripBusX + shelterOffsetFromBus)) < 12) movingShelter.visible = false;
        stopBanner.classList.add('is-hidden');
      }
      window.twinMap?.step(1);
      panelsDirty = true;
    }

    function replayClock() {
      const stamp = toNumber(telemetry.time_unix);
      if (stamp === null) return '';
      return new Date(stamp * 1000).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit', timeZone: 'UTC' });
    }

    function renderTwinStatus() {
      const clock = replayClock();
      let text;
      let mode = '';
      if (!twin.online) {
        text = twin.live ? `Replay offline · last known state ${clock}` : 'Sample data · start the replay to see the live twin';
      } else if (!twin.live) {
        text = 'Replay ready · start it on the Replay page';
      } else if (twin.status === 'running') {
        const stale = performance.now() - twin.lastMessageAt > 4000;
        text = stale ? `Waiting for messages · ${clock}` : `Live twin · ${clock} UTC · ${twin.replaySpeed.toLocaleString('en-GB')}×`;
        mode = stale ? 'is-paused' : 'is-live';
      } else if (twin.status === 'paused') {
        text = `Replay paused · ${clock} UTC`;
        mode = 'is-paused';
      } else if (twin.status === 'completed') {
        text = `Replay finished · ${clock} UTC`;
      } else {
        text = `Replay ${twin.status} · ${clock} UTC`;
      }
      twinStatus.classList.toggle('is-live', mode === 'is-live');
      twinStatus.classList.toggle('is-paused', mode === 'is-paused');
      if (twinStatusText.textContent !== text) twinStatusText.textContent = text;
    }

    function setChip(id, value, state) {
      const chip = document.getElementById(id);
      const strong = chip.querySelector('strong');
      if (strong.textContent !== value) strong.textContent = value;
      chip.classList.toggle('is-active', state === 'active');
      chip.classList.toggle('is-alert', state === 'alert');
    }

    const stateCard = document.getElementById('twin-state-card');
    const stateValue = document.getElementById('twin-state-value');
    const stateSince = document.getElementById('twin-state-since');
    const stateTags = document.getElementById('twin-state-tags');
    const stateSource = document.getElementById('twin-state-source');
    const mlRow = document.getElementById('twin-ml-row');
    const mlValue = document.getElementById('twin-ml-value');
    const stateTagLabels = {
      Forward: 'Forward', Reverse: 'Reverse', Stopped: 'Stopped',
      Straight: 'Straight', TurningLeft: 'Turning left', TurningRight: 'Turning right',
      ParkBrake: 'Park brake', HaltBrake: 'Halt brake', Braking: 'Braking', Released: 'Brakes released',
      Regenerating: 'Recovering energy', Consuming: 'Drawing power', Neutral: 'No power flow',
      AtStop: 'At stop', BetweenStops: 'Between stops'
    };

    function renderTwinState() {
      const parts = twinState.inferred ? twinState.inferred.split(' | ') : [];
      const operation = parts[0] || '—';
      for (const name of ['boarding', 'cruising', 'regenerating', 'idling']) {
        stateCard.classList.toggle(`state-${name}`, operation.toLowerCase() === name);
      }
      stateCard.classList.toggle('is-live', twin.live && twin.online && twin.status === 'running');
      if (stateValue.textContent !== operation) stateValue.textContent = operation;
      const since = twin.live && twinState.since ? `since ${twinState.since}` : '';
      if (stateSince.textContent !== since) stateSince.textContent = since;
      // [operation, motion, direction, braking, energy, doors, grid, stop, service, occupancy, heading]
      const tags = [parts[1], parts[2], parts[3], parts[4], parts[7]]
        .filter(part => part && part !== 'Stationary')
        .map(part => stateTagLabels[part] || part);
      if (parts[9]) tags.push(parts[9] === 'Empty' ? 'Empty' : `${parts[9]} load`);
      const tagsHtml = tags.map(tag => `<span>${tag}</span>`).join('');
      if (stateTags.innerHTML !== tagsHtml) stateTags.innerHTML = tagsHtml;
      const source = twin.live ? 'State machine · live' : 'State machine · sample record';
      if (stateSource.textContent !== source) stateSource.textContent = source;
      if (twinState.changed) {
        twinState.changed = false;
        stateCard.classList.remove('flash');
        void stateCard.offsetWidth;
        stateCard.classList.add('flash');
      }
      // Placeholder for the ML inference service (issue #8): it writes ml_prediction / anomaly_score
      // into bus:<id>:state and the feed forwards them here.
      const ml = twinState.ml;
      let mlText = 'Model not connected';
      let mlState = '';
      if (ml) {
        const score = toNumber(ml.anomaly_score);
        const prediction = ml.ml_prediction;
        mlText = [prediction, score === null ? null : `anomaly ${score.toFixed(2)}`].filter(Boolean).join(' · ') || '—';
        mlState = ml.ml_anomaly === '1' || ml.ml_anomaly === 1 ? 'alert' : 'ready';
      }
      if (mlValue.textContent !== mlText) mlValue.textContent = mlText;
      mlRow.classList.toggle('is-ready', mlState === 'ready');
      mlRow.classList.toggle('is-alert', mlState === 'alert');
    }

    function renderTwinPanels() {
      renderInspectionValues();
      renderPowerReading();
      const passengers = toNumber(telemetry.itcs_numberOfPassengers);
      setChip('chip-passengers', passengers === null ? '—' : String(passengers), '');
      setChip('chip-doors', toNumber(telemetry.status_doorIsOpen) === null ? '—' : isOn('status_doorIsOpen') ? 'Open' : 'Closed', isOn('status_doorIsOpen') ? 'active' : '');
      setChip('chip-brake', toNumber(telemetry.status_haltBrakeIsActive) === null ? '—' : isOn('status_haltBrakeIsActive') ? 'Applied' : 'Released', isOn('status_haltBrakeIsActive') ? 'active' : '');
      const grid = toNumber(telemetry.status_gridIsAvailable);
      setChip('chip-grid', grid === null ? '—' : grid === 1 ? 'Connected' : 'Poles down', grid === 0 ? 'alert' : '');
      if (!capturedJsonView.hidden) renderCapturedJson();
      renderTwinStatus();
      renderTwinState();
    }

    function motionAllowed() {
      const replayRunning = twin.live && twin.online && twin.status === 'running';
      // Debug messages may drive the bus whenever the replay is not the one driving it.
      if (document.body.classList.contains('debug-mode') && !replayRunning) return true;
      if (!replayRunning) return false;
      return performance.now() - twin.lastMessageAt < 4000;
    }

    function twinTimeScale() {
      // While the replay drives the twin, scene time runs at the replay speed: at 5× the bus covers
      // 5× the distance per second and doors, brakes, poles and passengers move 5× faster, so the
      // animation keeps pace with the data instead of lagging and re-anchoring.
      const replayDriving = twin.live && twin.online && twin.status === 'running';
      return replayDriving ? Math.min(10, Math.max(0.25, twin.replaySpeed || 1)) : 1;
    }

    function updateTwinScene(delta) {
      const speedKmh = liveSpeedKmh();
      const doorsOpen = isOn('status_doorIsOpen');
      const stationary = speedKmh < STOPPED_KMH;
      const allowed = motionAllowed();

      if (allowed) {
        if (!twin.atStop && stationary && doorsOpen) arriveAtStop();
        if (twin.atStop && speedKmh > DEPART_KMH && !doorsOpen && !boardingActive && !pendingPassengerChanges.length) departFromStop();
      }

      // Speed: the bus follows the real distance travelled (so it meets each stop where the data does),
      // easing toward it; it is held while people walk in or out and catches up afterwards.
      const hold = !allowed || boardingActive || pendingPassengerChanges.length > 0 || (twin.atStop && doorsOpen);
      const distance = currentOdometer();
      let targetSpeed = hold ? 0 : (speedKmh / 3.6) * MOTION_SCALE;
      // Only while the replay drives the bus; debug trips on a paused replay move freely and the road
      // re-anchors when the replay resumes.
      if (!hold && distance !== null && twin.sceneOrigin !== null && twin.status === 'running') {
        const sinceMessage = Math.min(1.2, (performance.now() - twin.lastMessageAt) / 1000 * twin.replaySpeed);
        const targetX = twin.sceneOrigin + (distance + (speedKmh / 3.6) * sinceMessage) * MOTION_SCALE;
        let error = targetX - tripBusX;
        if (error > 60 || error < -30) {
          // Far off (e.g. after a long hold): re-anchor the road instead of racing or reversing.
          twin.sceneOrigin -= error;
          error = 0;
        }
        targetSpeed = Math.max(0, targetSpeed + error * 0.6);
      }
      const rate = targetSpeed > twin.visualSpeed ? 2.2 : 4.5;
      const step = rate * delta;
      twin.visualSpeed += Math.max(-step, Math.min(step, targetSpeed - twin.visualSpeed));
      if (twin.visualSpeed < 0.005 && targetSpeed === 0) twin.visualSpeed = 0;
      moveBusForward(twin.visualSpeed * delta);

      const shownKmh = allowed || !twin.live ? speedKmh : 0;
      const gaugeBefore = twin.gaugeSpeed;
      twin.gaugeSpeed += (shownKmh - twin.gaugeSpeed) * Math.min(1, delta * 5);
      if (Math.abs(twin.gaugeSpeed - gaugeBefore) > 0.02) setGaugeSpeed(twin.gaugeSpeed);

      // Doors: the twin's door flag, combined with whatever the passenger walk needs.
      twin.doorAngle += ((doorsOpen ? 1.12 : 0) - twin.doorAngle) * Math.min(1, delta * 4);
      const frontAngle = Math.max(twin.doorAngle, passengerDoorAngles.front);
      const rearAngle = Math.max(twin.doorAngle, passengerDoorAngles.rear);
      frontLeftDoor.rotation.y = -frontAngle;
      frontRightDoor.rotation.y = frontAngle;
      rearLeftDoor.rotation.y = -rearAngle;
      rearRightDoor.rotation.y = rearAngle;
      for (const { leaf, sign } of extraDoorLeaves) leaf.rotation.y = sign * twin.doorAngle;
      frontOpening.visible = frontAngle < 0.05;
      rearOpening.visible = rearAngle < 0.05;

      // Halt brake / brake pressure light the brake lamps.
      const braking = isOn('status_haltBrakeIsActive') || (toNumber(telemetry.brake_bar) ?? 0) > 0.35;
      twin.brakeGlow += ((braking ? 1 : 0) - twin.brakeGlow) * Math.min(1, delta * 8);
      for (const material of brakeLightMaterials) {
        material.emissiveIntensity = (material.userData.baseEmissive ?? 0.3) + twin.brakeGlow * 2.6;
      }

      // Overhead grid: raise or lower the trolley poles.
      const gridFlag = toNumber(telemetry.status_gridIsAvailable);
      const poleTarget = gridFlag === 0 ? 0 : 1;
      setOverheadLinePowered(gridFlag !== 0);
      twin.poleBlend += (poleTarget - twin.poleBlend) * Math.min(1, delta * 1.6);
      for (const pole of trolleyPoles) {
        const axis = ['x', 'y', 'z'].includes(pole.userData.axis) ? pole.userData.axis : 'z';
        pole.rotation[axis] = mix(pole.userData.loweredAngle, pole.userData.raisedAngle, twin.poleBlend);
        // Keep the collector shoe level whether the pole is up on the wire or down on its hook.
        if (pole.userData.head) pole.userData.head.rotation[axis] = -pole.rotation[axis];
      }

      // The scene is a straight road, so the bus always drives straight: steering and articulation
      // angles stay in the inspection sheet but do not bend the trailer or turn the front wheels.

      // A shelter that just appeared grows out of the pavement.
      if (twin.shelterPop < 1) {
        twin.shelterPop = Math.min(1, twin.shelterPop + delta / 0.45);
        const t = twin.shelterPop;
        const overshoot = 1 + 2.2 * Math.pow(t - 1, 3) + 1.2 * Math.pow(t - 1, 2);
        movingShelter.scale.set(shelterBaseScale.x, shelterBaseScale.y * Math.max(0.02, overshoot), shelterBaseScale.z);
      }
    }

    // ---- Feed polling --------------------------------------------------------------------------
    let feedCursor = null;
    async function pollFeed() {
      let data = { online: false };
      if (feedUrl) {
        try {
          const url = feedCursor ? `${feedUrl}?after=${encodeURIComponent(feedCursor)}` : feedUrl;
          const response = await fetch(url, { cache: 'no-store' });
          if (response.ok) data = await response.json();
        } catch (_) {}
      }
      try {
        handleFeed(data);
      } catch (error) {
        console.error('Digital twin update failed', error);
      }
      setTimeout(pollFeed, document.hidden ? 1000 : 250);
    }

    function handleFeed(data) {
      twin.online = Boolean(data.online);
      if (!twin.online) {
        feedCursor = null;
        panelsDirty = true;
        return;
      }
      const control = data.control || {};
      twin.status = control.status || 'idle';
      twin.replaySpeed = Number(control.speed) || 1;
      // started_at changes on every "Iniciar replay"; a page opened before it must forget the old run.
      if (control.started_at && control.started_at !== twin.startedAt) {
        twin.startedAt = control.started_at;
        clearTwin();
      }
      twinState.ml = data.ml && Object.values(data.ml).some(value => value !== null && value !== '') ? data.ml : null;
      if (data.state && Object.keys(data.state).length) resyncFromState(data.state);
      const messages = data.messages || [];
      for (const message of messages) applyFields(message.payload);
      if (messages.length) twin.lastMessageAt = performance.now();
      feedCursor = data.last_id || feedCursor;
      panelsDirty = true;
    }

    // ---- Debug mode: the old manual buttons now inject test messages into the same twin -------
    const debugToggle = document.getElementById('debug-toggle');
    function setDebugMode(enabled) {
      document.body.classList.toggle('debug-mode', enabled);
      debugToggle.setAttribute('aria-pressed', String(enabled));
      try { localStorage.setItem('ditec-twin-debug', enabled ? '1' : '0'); } catch (_) {}
      if (!enabled) {
        debugTimers.forEach(clearTimeout);
        debugTimers = [];
      }
      renderTripButton();
    }
    debugToggle.addEventListener('click', () => setDebugMode(!document.body.classList.contains('debug-mode')));
    let storedDebug = false;
    try { storedDebug = localStorage.getItem('ditec-twin-debug') === '1'; } catch (_) {}

    function injectDebug(fields, afterMs = 0) {
      debugTimers.push(setTimeout(() => {
        applyFields(fields);
        renderTripButton();
      }, afterMs));
    }

    function renderTripButton() {
      const moving = liveSpeedKmh() >= STOPPED_KMH;
      tripToggleButton.textContent = moving ? 'Stop at next stop' : 'Start trip';
    }

    tripToggleButton.addEventListener('click', () => {
      debugTimers.forEach(clearTimeout);
      debugTimers = [];
      const speed = liveSpeedKmh();
      if (speed < STOPPED_KMH) {
        injectDebug({ doors_open: 0, halt_brake: 0, stop_name: null });
        injectDebug({ speed_kmh: 12 }, 700);
        injectDebug({ speed_kmh: 24 }, 1500);
        injectDebug({ speed_kmh: 30 }, 2300);
      } else {
        const steps = 10;
        for (let index = 1; index <= steps; index += 1) {
          injectDebug({ speed_kmh: Math.max(0, speed * (1 - index / steps)) }, index * 300);
        }
        injectDebug({ speed_kmh: 0, halt_brake: 1 }, steps * 300 + 100);
        injectDebug({ doors_open: 1 }, steps * 300 + 700);
        injectDebug({ stop_name: 'Debug stop' }, steps * 300 + 900);
      }
    });

    function changePassengers(sign) {
      const count = readPassengerCount();
      const current = toNumber(telemetry.itcs_numberOfPassengers) ?? 0;
      const next = Math.max(0, current + sign * count);
      if (next !== current) applyFields({ passengers: next });
    }

    passengerCountInput.addEventListener('input', readPassengerCount);
    passengerCountInput.addEventListener('change', readPassengerCount);
    readPassengerCount();
    document.getElementById('debug-doors').addEventListener('click', () => {
      applyFields({ doors_open: isOn('status_doorIsOpen') ? 0 : 1 });
    });
    document.getElementById('debug-grid').addEventListener('click', () => {
      applyFields({ grid_available: toNumber(telemetry.status_gridIsAvailable) === 0 ? 1 : 0 });
    });
    passengerAddButton.addEventListener('click', () => changePassengers(1));
    passengerRemoveButton.addEventListener('click', () => changePassengers(-1));

    window.addEventListener('resize', () => {
      setMapPipRect(mapPipRect);
      staticView.resize();
      animationView.resize();
    });

    // The twin always renders the travelling scene: road, moving shelter, following camera.
    staticHost.hidden = true;
    animationHost.hidden = false;
    animationView.resize();
    setDebugMode(storedDebug);
    renderTwinPanels();
    pollFeed();

    function renderModel() {
      requestAnimationFrame(renderModel);
      // Simulate in small fixed steps so the twin keeps replay time (real time × replay speed) even when
      // the browser throttles frames (background tab, hidden pane); after more than 2 s away it resumes.
      const rawDelta = animationClock.getDelta();
      // Frames stop while the Fleet view is hidden (Replay page, background tab); on return, catch up
      // by jumping to the current state rather than animating everything that happened meanwhile.
      if (rawDelta > 2.5 && twin.live) twin.needsSnap = true;
      if (twin.needsSnap) {
        twin.needsSnap = false;
        snapSceneToTwin();
      }
      const frameDelta = Math.min(rawDelta, 2);
      const timeScale = twinTimeScale();
      let remaining = frameDelta * timeScale;
      while (remaining > 1e-4) {
        const delta = Math.min(remaining, 0.05);
        remaining -= delta;
        if (boardingActive) {
          boardingElapsed += delta;
          updatePassengerAnimation(boardingElapsed);
        } else {
          processPassengerQueue();
        }
        updateTwinScene(delta);
      }
      updateStopVisits(currentOdometer());
      updateStopShelters();
      animationView.controls.update();
      animationView.renderer.render(animationView.scene, animationView.camera);
      window.twinMap?.step(Math.min(1, frameDelta * timeScale * 3));
      const now = performance.now();
      if (panelsDirty && now - lastPanelRender > 200) {
        panelsDirty = false;
        lastPanelRender = now;
        renderTwinPanels();
        if (document.body.classList.contains('debug-mode')) renderTripButton();
      }
    }
    renderModel();
  </script>
</body>
</html>""".replace("__ROUTE_JSON__", route_json).replace("__TELEMETRY_JSON__", metrics_json).replace(
        "__TWIN_FEED_PORT__", json.dumps(feed_port)
    ).replace(
        # Top-view drawings of the articulated trolleybus (inspection sheet and its opening button).
        "__TOPVIEW_INSPECTION_SVG__", (ASSETS_DIR / "topview_inspection.svg").read_text(encoding="utf-8").strip()
    ).replace(
        "__TOPVIEW_BUTTON_SVG__", (ASSETS_DIR / "topview_button.svg").read_text(encoding="utf-8").strip()
    ).replace(
        "__STOPS_JSON__", json.dumps(_load_stop_visits(), ensure_ascii=False, separators=(",", ":"))
    ).replace(
        "__TWIN_STATE_JSON__", json.dumps(_sample_state(metrics))
    )


def _load_styles() -> None:
    st.markdown(
        """
        <style>
        html, body, #root { width: 100%; height: 100%; margin: 0; padding: 0; }
        body { overflow: hidden; overscroll-behavior: none; }
        .stApp, [data-testid="stAppViewContainer"] { width: 100%; height: 100%; overflow: hidden; }
        [data-testid="stMain"] { position: fixed !important; inset: 0 !important; width: 100vw !important; height: 100vh !important; overflow: auto !important; }
        [data-testid="stMainBlockContainer"] { max-width: none !important; min-height: 100vh !important; margin: 0 !important; padding: 72px 24px 28px !important; overflow: visible !important; }
        header[data-testid="stHeader"],
        [data-testid="stToolbar"],
        [data-testid="stDecoration"],
        [data-testid="stStatusWidget"],
        footer,
        #MainMenu,
        .stDeployButton {
            display: none !important;
        }
        iframe[title="st.iframe"] {
            position: fixed !important;
            inset: 0 !important;
            height: 100vh !important;
            width: 100vw !important;
            max-width: none !important;
            margin: 0 !important;
            padding: 0 !important;
            border: 0 !important;
            overflow: hidden !important;
            z-index: 1 !important;
        }
        div[data-testid="stRadio"] {
            position: fixed !important;
            z-index: 10000 !important;
            top: 10px !important;
            left: 50% !important;
            width: max-content !important;
            margin: 0 !important;
            padding: 0 !important;
            transform: translateX(-50%) !important;
        }
        div[data-testid="stRadio"] > label { display: none !important; }
        div[data-testid="stRadio"] [role="radiogroup"] {
            display: flex !important;
            gap: 2px !important;
            padding: 3px !important;
            border: 1px solid #e7eaed !important;
            border-radius: 999px !important;
            background: rgba(248, 249, 250, .94) !important;
            box-shadow: 0 4px 16px rgba(38, 50, 58, .07) !important;
            backdrop-filter: blur(12px);
        }
        div[data-testid="stRadio"] [role="radiogroup"] label {
            min-height: 32px !important;
            margin: 0 !important;
            padding: 6px 13px !important;
            border-radius: 999px !important;
            color: #68757d !important;
            cursor: pointer !important;
            font-size: 12px !important;
            line-height: 20px !important;
            transition: background-color .18s ease, color .18s ease, box-shadow .18s ease !important;
        }
        div[data-testid="stRadio"] [role="radiogroup"] label > div:first-child { display: none !important; }
        div[data-testid="stRadio"] [role="radiogroup"] label > div:has(input[type="radio"]) {
            position: absolute !important;
            width: 1px !important;
            height: 1px !important;
            overflow: hidden !important;
            clip-path: inset(50%) !important;
            white-space: nowrap !important;
        }
        div[data-testid="stRadio"] [role="radiogroup"] label:not(:has(input:checked)),
        div[data-testid="stRadio"] [role="radiogroup"] label:not(:has(input:checked)) * {
            color: #68757d !important;
            opacity: 1 !important;
        }
        div[data-testid="stRadio"] [role="radiogroup"] label:has(input:checked),
        div[data-testid="stRadio"] [role="radiogroup"] label:has(input:checked) * {
            color: #46525a !important;
        }
        div[data-testid="stRadio"] [role="radiogroup"] label:has(input:checked) {
            background: #ffffff !important;
            box-shadow: 0 2px 7px rgba(38, 50, 58, .10) !important;
        }
        div[data-testid="stRadio"] [role="radiogroup"] label:focus-within {
            outline: 2px solid #a8b7b0 !important;
            outline-offset: 2px !important;
        }
        @media (max-width: 520px) {
            [data-testid="stMainBlockContainer"] { padding: 66px 12px 24px !important; }
            div[data-testid="stRadio"] [role="radiogroup"] label { padding: 6px 11px !important; font-size: 11px !important; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


@st.cache_data(show_spinner=False, max_entries=4)
def _fleet_page_html(feed_port: str, source_stamps: tuple[float, ...]) -> str | None:
    """The Fleet page HTML; source_stamps (file mtimes) only invalidate the cache when inputs change."""
    route = _load_route()
    if len(route) < 2:
        return None
    return _dashboard_html(route, _load_mock_metrics(), feed_port)


def _fleet_source_stamps() -> tuple[float, ...]:
    paths = (
        Path(__file__),
        REPLAY_PATH,
        MOCK_STATE_PATH,
        ASSETS_DIR / "topview_inspection.svg",
        ASSETS_DIR / "topview_button.svg",
        PROJECT_DIR / "src" / "state_machine" / "inference.py",
    )
    return tuple(path.stat().st_mtime if path.exists() else 0.0 for path in paths)


def main() -> None:
    st.set_page_config(page_title="Zurich Transit Monitor", layout="wide", initial_sidebar_state="collapsed")
    _load_styles()

    page = st.radio(
        "Páginas da dashboard",
        ("Fleet", "Replay"),
        horizontal=True,
        label_visibility="collapsed",
        key="dashboard_page",
    )
    # The Fleet view is rendered on both pages as the same element in the same place, so Streamlit
    # keeps its iframe alive: switching to Replay and back never restarts the twin, the map or the
    # camera. The Replay page only hides it.
    fleet_html = _fleet_page_html(_twin_feed_port(), _fleet_source_stamps())
    if fleet_html is not None:
        st.iframe(fleet_html, height=900)
    if page == "Fleet":
        if fleet_html is None:
            st.error("Não encontrei pontos suficientes para desenhar a rota do autocarro.")
    else:
        st.markdown(
            '<style>div[data-testid="stElementContainer"]:has(> iframe[data-testid="stIFrame"]) { display: none !important; }</style>',
            unsafe_allow_html=True,
        )
        from src.dashboard.replay_page import render_replay_page

        render_replay_page()


if __name__ == "__main__":
    main()

