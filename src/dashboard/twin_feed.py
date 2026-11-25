"""Tiny local HTTP feed that lets the Fleet page follow the digital twin in Redis.

The Fleet page is a static iframe (re-sending it would reload the 3D scene), so instead of
re-rendering it, its JavaScript polls ``GET /twin?after=<stream id>``. Every reply carries the
replay control hash and the MQTT messages the state bridge appended to the Redis stream after
``after``, in order, so the browser applies each message to its twin and never misses short
events (a stop name is only present for one sample). A first request, or one whose cursor fell
out of the capped stream, also receives the merged ``bus:<id>:state`` hash to resynchronise.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from src.dashboard.mission import BUS_ID

CONTROL_KEY = "ztbus:replay:control"
MESSAGE_STREAM = "ztbus:replay:messages"
STATE_KEY = f"bus:{BUS_ID}:state"
FEED_HOST = os.getenv("TWIN_FEED_HOST", "127.0.0.1")
FEED_PORT = int(os.getenv("TWIN_FEED_PORT", "8503"))
MAX_MESSAGES = 600
# Written into the state hash by the prediction worker (src/ml/predictor.py); forwarded on every poll.
ML_FIELDS = (
    "ml_status",
    "ml_samples_collected",
    "ml_samples_required",
    "ml_predicted_mean_power_kw_next_10s",
    "ml_absolute_error_kw",
    "ml_anomaly_threshold_kw",
    "ml_is_anomaly",
)


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
                    "payload": _decode(fields.get("payload")),
                }
                for entry_id, fields in entries
            ],
            "last_id": last_id,
            "gap": gap,
            "ml": dict(zip(ML_FIELDS, client.hmget(STATE_KEY, ML_FIELDS))),
        }
        if gap:
            snapshot["state"] = client.hgetall(STATE_KEY)
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
