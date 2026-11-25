"""Tiny local HTTP feed that lets the Fleet page follow the digital twin in Redis.

The Fleet page is a static iframe (re-sending it would reload the 3D scene), so instead of
re-rendering it, its JavaScript polls ``GET /twin?after=<stream id>``. Every reply carries the
replay control hash and the MQTT messages the state bridge appended to the Redis stream after
``after``, in order, so the browser applies each message to its twin and never misses short
events (a stop name is only present for one sample). A first request, or one whose cursor fell
out of the capped stream, also receives the merged ``bus:<id>:state`` hash to resynchronise.
"""

from __future__ import annotations

import csv
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

REPLAY_PATH = Path(__file__).resolve().parents[2] / "data" / "clean_replay_mission.csv"
CONTROL_KEY = "ztbus:replay:control"
MESSAGE_STREAM = "ztbus:replay:messages"
BUS_ID = os.getenv("REPLAY_BUS_ID", "B208")
STATE_KEY = f"bus:{BUS_ID}:state"
FEED_HOST = os.getenv("TWIN_FEED_HOST", "127.0.0.1")
FEED_PORT = int(os.getenv("TWIN_FEED_PORT", "8503"))
MAX_MESSAGES = 600
# Written into the state hash by the ML inference service (issue #8); forwarded on every poll.
ML_FIELDS = ("ml_prediction", "anomaly_score", "ml_anomaly")


def _redis_client() -> Any | None:
    try:
        import redis
        from redis.backoff import NoBackoff
        from redis.retry import Retry
    except ImportError:
        return None
    return redis.Redis(
        host=os.getenv("REPLAY_REDIS_HOST", "127.0.0.1"),
        port=int(os.getenv("REPLAY_REDIS_PORT", "6379")),
        decode_responses=True,
        socket_connect_timeout=0.3,
        socket_timeout=0.5,
        retry=Retry(NoBackoff(), 1),
    )


def _stream_id(value: str) -> tuple[int, int] | None:
    try:
        milliseconds, _, sequence = value.partition("-")
        return int(milliseconds), int(sequence or 0)
    except ValueError:
        return None


def _decode(raw: str | None) -> Any:
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return raw


_itcs_lock = threading.Lock()
_itcs_by_time: dict[int, tuple[str | None, str | None]] | None = None


def _itcs_index() -> dict[int, tuple[str | None, str | None]]:
    """time_unix -> (stop name, route) from the replayed dataset.

    The producer publishes ``stop_name`` / ``bus_route`` in each ``cabin`` message; for messages
    without them (e.g. an older producer) the feed fills them in from the same CSV row, matched by
    ``time_unix``. Values the producer publishes itself always win.
    """
    global _itcs_by_time
    with _itcs_lock:
        if _itcs_by_time is None:
            index: dict[int, tuple[str | None, str | None]] = {}
            try:
                with REPLAY_PATH.open("r", encoding="utf-8", newline="") as source:
                    for row in csv.DictReader(source):
                        try:
                            stamp = int(float(row.get("time_unix") or ""))
                        except ValueError:
                            continue
                        stop = (row.get("itcs_stopName") or "").strip()
                        route = (row.get("itcs_busRoute") or "").strip()
                        index[stamp] = (None if stop in {"", "-"} else stop, None if route in {"", "-"} else route)
            except OSError:
                pass
            _itcs_by_time = index
        return _itcs_by_time


def _with_itcs(topic: str, payload: Any) -> Any:
    if not topic.endswith("/cabin") or not isinstance(payload, dict):
        return payload
    try:
        stamp = int(float(payload.get("time_unix")))
    except (TypeError, ValueError):
        return payload
    stop, route = _itcs_index().get(stamp, (None, None))
    return {"stop_name": stop, "bus_route": route, **payload}


def build_snapshot(client: Any, after: str | None) -> dict[str, Any]:
    """Everything the page needs for one poll; never raises for Redis being down."""
    if client is None:
        return {"online": False, "reason": "redis-client-missing"}
    try:
        control = client.hgetall(CONTROL_KEY)
        cursor = _stream_id(after) if after else None
        first = client.xrange(MESSAGE_STREAM, count=1)
        # The stream is capped, so a cursor older than its first entry means messages were dropped.
        gap = cursor is None or (bool(first) and _stream_id(first[0][0]) > cursor)
        if gap:
            # Resynchronise from the merged hash; replaying older messages on top would rewind the twin.
            entries = []
            latest = client.xrevrange(MESSAGE_STREAM, count=1)
            last_id = latest[0][0] if latest else "0-0"
        else:
            entries = client.xrange(MESSAGE_STREAM, min=f"({after}", count=MAX_MESSAGES)
            last_id = entries[-1][0] if entries else after
        snapshot: dict[str, Any] = {
            "online": True,
            "control": control,
            "messages": [
                {
                    "id": entry_id,
                    "topic": fields.get("topic", ""),
                    "payload": _with_itcs(fields.get("topic", ""), _decode(fields.get("payload"))),
                }
                for entry_id, fields in entries
            ],
            "last_id": last_id,
            "gap": gap,
            "ml": dict(zip(ML_FIELDS, client.hmget(STATE_KEY, ML_FIELDS))),
        }
        if gap:
            state = client.hgetall(STATE_KEY)
            snapshot["state"] = _with_itcs("/cabin", state) if state else state
        return snapshot
    except Exception as error:  # Redis stopped or restarting: the page falls back to sample data.
        return {"online": False, "reason": type(error).__name__}


class _Handler(BaseHTTPRequestHandler):
    client: Any = None

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        url = urlparse(self.path)
        if url.path != "/twin":
            self.send_error(404)
            return
        after = (parse_qs(url.query).get("after") or [None])[0]
        body = json.dumps(build_snapshot(self.client, after), separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        # The Fleet iframe is served by Streamlit on another port.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: Any) -> None:  # Keep the Streamlit log readable.
        pass


def start_feed() -> str:
    """Start the feed once per process; returns its port (another process may already own it)."""
    _Handler.client = _redis_client()
    try:
        server = ThreadingHTTPServer((FEED_HOST, FEED_PORT), _Handler)
    except OSError:
        return str(FEED_PORT)  # Port taken, e.g. by a second Streamlit instance serving the same feed.
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="twin-feed", daemon=True).start()
    return str(FEED_PORT)
