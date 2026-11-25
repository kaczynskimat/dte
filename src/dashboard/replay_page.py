"""Streamlit page for controlling the MQTT/Redis sensor replay."""

from __future__ import annotations

import csv
import html
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import streamlit as st


PROJECT_DIR = Path(__file__).resolve().parents[2]
CONTROL_KEY = "ztbus:replay:control"
MESSAGE_STREAM = "ztbus:replay:messages"
TOPICS = ("location", "kinematics", "energy", "cabin", "odometry", "brakes")
# Same env var and defaults as src/replay/producer.py so topics and the bridge's state key line up.
BUS_ID = os.getenv("REPLAY_BUS_ID", "B208")
TOPIC_PREFIX = f"ztbus/{BUS_ID}"
# Hash written by src/state_bridge/bridge.py: the merged latest value of every replayed sensor field (the twin state).
STATE_KEY = f"bus:{BUS_ID}:state"
PRODUCER_LOG_PATH = Path(tempfile.gettempdir()) / "dte-replay.log"
DATASETS = (
    ("clean_replay_mission.csv", "Replay MQTT e rota da página Fleet"),
    ("train_mission.csv", "Missão de treino (ainda sem consumidor no dashboard)"),
    ("mock_state.json", "Métricas estáticas da página Fleet"),
)


def _load_refresh_helper() -> Any | None:
    try:
        from streamlit_autorefresh import st_autorefresh

        return st_autorefresh
    except ImportError:
        return None


def _get_redis() -> Any | None:
    try:
        import redis
        from redis.backoff import NoBackoff
        from redis.retry import Retry

        return redis.Redis(
            host=os.getenv("REPLAY_REDIS_HOST", "127.0.0.1"),
            port=int(os.getenv("REPLAY_REDIS_PORT", "6379")),
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
            # redis-py's default retries with exponential backoff turn one failed ping into ~3.6 s,
            # leaving the whole page blank while Redis is down. One immediate retry is enough locally.
            retry=Retry(NoBackoff(), 1),
        )
    except (ImportError, ValueError):
        return None


def _redis_is_live(redis_client: Any | None) -> bool:
    if redis_client is None:
        return False
    try:
        redis_client.ping()
        return True
    except Exception:
        return False


@st.cache_data(show_spinner=False)
def _dataset_count() -> int:
    path = PROJECT_DIR / "data" / "clean_replay_mission.csv"
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8", newline="") as source:
        return max(0, sum(1 for _ in csv.DictReader(source)))


def _compose(*arguments: str, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *arguments],
        cwd=PROJECT_DIR,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _wait_for_services(redis_client: Any, timeout: float = 25.0, require_bridge: bool = True) -> bool:
    deadline = time.monotonic() + timeout
    mqtt_host = os.getenv("REPLAY_MQTT_HOST", "127.0.0.1")
    mqtt_port = int(os.getenv("REPLAY_MQTT_PORT", "1883"))
    while time.monotonic() < deadline:
        try:
            redis_client.ping()
            with socket.create_connection((mqtt_host, mqtt_port), timeout=0.5):
                if not require_bridge or redis_client.get("ztbus:replay:bridge_status") == "ready":
                    return True
        except Exception:
            pass
        # Sleep on every miss, not only on errors, so waiting for the bridge does not busy-loop on Redis.
        time.sleep(0.4)
    return False


def _start_replay(redis_client: Any, speed: float) -> None:
    with st.spinner("A iniciar o broker, Redis e a bridge…"):
        try:
            result = _compose("up", "-d", "mqtt-broker", "redis-cache", "state-bridge")
        except (FileNotFoundError, subprocess.TimeoutExpired) as error:
            st.error(f"Não foi possível iniciar o Docker Compose: {error}")
            return

        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "Falha ao iniciar os serviços."
            st.error(detail)
            return

        if not _wait_for_services(redis_client, require_bridge=False):
            st.error("O Redis ou o broker MQTT não respondeu a tempo. Verifica se as portas 6379 e 1883 estão livres.")
            return

        redis_client.delete("ztbus:replay:bridge_status")
        try:
            bridge_result = _compose("restart", "state-bridge", timeout=45)
        except (FileNotFoundError, subprocess.TimeoutExpired) as error:
            st.error(f"Não foi possível preparar a bridge MQTT: {error}")
            return
        if bridge_result.returncode != 0:
            st.error(bridge_result.stderr.strip() or "Não foi possível reiniciar a bridge MQTT.")
            return

        if not _wait_for_services(redis_client):
            st.error("O Redis ou o broker MQTT não respondeu a tempo. Verifica se as portas 6379 e 1883 estão livres.")
            return

        for key in redis_client.scan_iter(match="ztbus:replay:topic_latest:*"):
            redis_client.delete(key)
        redis_client.delete(MESSAGE_STREAM, "ztbus:replay:topic_counts", STATE_KEY)

        total = _dataset_count()
        redis_client.hset(
            CONTROL_KEY,
            mapping={
                "status": "running",
                "speed": speed,
                "index": 0,
                "total": total,
                "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
        )

        environment = os.environ.copy()
        environment.update(
            {
                "REPLAY_REDIS_HOST": os.getenv("REPLAY_REDIS_HOST", "127.0.0.1"),
                "REPLAY_REDIS_PORT": os.getenv("REPLAY_REDIS_PORT", "6379"),
                "REPLAY_MQTT_HOST": os.getenv("REPLAY_MQTT_HOST", "127.0.0.1"),
                "REPLAY_MQTT_PORT": os.getenv("REPLAY_MQTT_PORT", "1883"),
            }
        )
        log_path = PRODUCER_LOG_PATH
        redis_client.hdel(CONTROL_KEY, "error", "pid")
        try:
            with log_path.open("w", encoding="utf-8") as log_file:
                process = subprocess.Popen(
                    [sys.executable, str(PROJECT_DIR / "src" / "replay" / "producer.py")],
                    cwd=PROJECT_DIR,
                    env=environment,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            # Remember the producer so the page can tell a crashed replay from a running one.
            redis_client.hset(CONTROL_KEY, mapping={"pid": process.pid})
            time.sleep(0.25)
            if process.poll() is not None:
                try:
                    detail = log_path.read_text(encoding="utf-8")[-1200:]
                except OSError:
                    detail = "O processo de reprodução terminou ao iniciar."
                redis_client.hset(CONTROL_KEY, mapping={"status": "error", "error": detail})
                st.error(detail)
        except OSError as error:
            redis_client.hset(CONTROL_KEY, mapping={"status": "error"})
            st.error(f"Não foi possível iniciar o produtor MQTT: {error}")


def _producer_alive(pid_text: str | None) -> bool:
    """False only when the producer this dashboard launched is known to have exited."""
    try:
        pid = int(pid_text or "")
    except ValueError:
        return True  # Started outside the dashboard (no pid recorded): nothing to check.
    try:
        # Reap it if it is our child; an unreaped zombie would still pass os.kill(pid, 0).
        finished_pid, _ = os.waitpid(pid, os.WNOHANG)
        return finished_pid != pid
    except ChildProcessError:
        pass  # Not our child, e.g. Streamlit was restarted while the replay kept running.
    except AttributeError:
        return True  # os.WNOHANG is unavailable on Windows.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _mark_dead_producer(redis_client: Any, control: dict[str, str]) -> dict[str, str]:
    if control.get("status") not in {"running", "paused"} or _producer_alive(control.get("pid")):
        return control
    detail = "O produtor do replay terminou sem atualizar o estado."
    try:
        log_tail = PRODUCER_LOG_PATH.read_text(encoding="utf-8").strip()[-800:]
    except OSError:
        log_tail = ""
    if log_tail:
        detail = f"{detail}\n\n{log_tail}"
    redis_client.hset(CONTROL_KEY, mapping={"status": "error", "error": detail})
    return {**control, "status": "error", "error": detail}


def _replay_speed_from(control: dict[str, str]) -> float:
    try:
        speed = float(control.get("speed", 1.0))
    except (TypeError, ValueError):
        speed = 1.0
    # Same bounds as the producer, snapped to the slider's 0.25 step.
    return round(min(10.0, max(0.25, speed)) * 4) / 4


def _push_replay_speed(redis_client: Any) -> None:
    """Slider callback: only an actual change by this viewer is written to Redis."""
    try:
        if redis_client.hget(CONTROL_KEY, "status") in {"running", "paused"}:
            redis_client.hset(CONTROL_KEY, mapping={"speed": st.session_state["replay_speed"]})
    except Exception:
        pass


def _latest_payload(redis_client: Any, topic: str) -> dict[str, Any] | None:
    raw = redis_client.get(f"ztbus:replay:topic_latest:{TOPIC_PREFIX}/{topic}")
    if not raw:
        return None
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {"value": raw}
    return decoded if isinstance(decoded, dict) else {"value": decoded}


def _recent_messages(redis_client: Any, count: int = 24) -> list[dict[str, Any]]:
    entries = redis_client.xrevrange(MESSAGE_STREAM, max="+", min="-", count=count)
    messages = []
    for message_id, fields in entries:
        try:
            payload = json.loads(fields.get("payload", "{}"))
        except json.JSONDecodeError:
            payload = fields.get("payload", "")
        messages.append(
            {
                "Recebida (UTC)": fields.get("received_at", ""),
                "Tópico": fields.get("topic", ""),
                "Payload": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            }
        )
    return messages


def _render_topic_card(topic: str, payload: dict[str, Any] | None, count: int) -> None:
    full_topic = f"{TOPIC_PREFIX}/{topic}"
    active = payload is not None
    state_label = "A receber" if active else "À espera"
    payload_text = json.dumps(payload, ensure_ascii=False, indent=2) if payload else "Ainda sem mensagens neste tópico."
    st.markdown(
        f"""
        <article class="replay-topic-card">
          <div class="replay-topic-head">
            <span class="replay-topic-indicator {'is-live' if active else ''}" aria-hidden="true"></span>
            <code>{html.escape(full_topic)}</code>
            <span class="replay-topic-state">{state_label}</span>
          </div>
          <div class="replay-topic-foot"><span>{count:,} mensagens nesta execução</span></div>
          <details><summary>Último payload</summary><pre>{html.escape(payload_text)}</pre></details>
        </article>
        """,
        unsafe_allow_html=True,
    )


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_number(value: Any, decimals: int, unit: str = "") -> str:
    number = _as_float(value)
    if number is None:
        return "—"
    text = f"{number:,.{decimals}f}"
    return f"{text} {unit}".strip()


def _format_flag(value: Any, on_label: str, off_label: str) -> str:
    number = _as_float(value)
    if number is None:
        return "—"
    return on_label if number else off_label


def _twin_metrics(state: dict[str, str]) -> list[tuple[str, str]]:
    latitude, longitude = _as_float(state.get("latitude")), _as_float(state.get("longitude"))
    position = f"{latitude:.5f}, {longitude:.5f}" if latitude is not None and longitude is not None else "—"
    return [
        ("Velocidade", _format_number(state.get("speed_kmh"), 1, "km/h")),
        ("Potência", _format_number(state.get("power_kw"), 1, "kW")),
        ("Passageiros", _format_number(state.get("passengers"), 0)),
        ("Portas", _format_flag(state.get("doors_open"), "Abertas", "Fechadas")),
        ("Temperatura", _format_number(state.get("temp_celsius"), 1, "°C")),
        ("Travão de paragem", _format_flag(state.get("halt_brake"), "Ativo", "Inativo")),
        ("Rede elétrica", _format_flag(state.get("grid_available"), "Disponível", "Indisponível")),
        ("Posição", position),
    ]


def _render_twin_state(redis_client: Any, connected: bool) -> None:
    st.subheader("Estado do gémeo digital")
    state = redis_client.hgetall(STATE_KEY) if connected else {}
    if not state:
        st.markdown(
            f'<div class="replay-empty">A bridge ainda não escreveu <code>{html.escape(STATE_KEY)}</code> no Redis. '
            "Inicia o replay para ver o estado agregado do autocarro.</div>",
            unsafe_allow_html=True,
        )
        return
    tiles = "".join(
        f'<div class="flow-metric"><span>{html.escape(label)}</span><strong class="twin-value">{html.escape(value)}</strong></div>'
        for label, value in _twin_metrics(state)
    )
    st.markdown(f'<div class="flow-metrics twin-metrics">{tiles}</div>', unsafe_allow_html=True)
    sample_time = state.get("timestamp") or "—"
    st.markdown(
        f'<p class="flow-caption">Hash Redis <code>{html.escape(STATE_KEY)}</code> escrito pela state bridge · '
        f"{len(state)} campos · amostra de {html.escape(sample_time)}</p>",
        unsafe_allow_html=True,
    )
    ordered = {key: state[key] for key in sorted(state)}
    st.markdown(
        f'<details class="twin-raw"><summary>Todos os campos</summary><pre>{html.escape(json.dumps(ordered, ensure_ascii=False, indent=2))}</pre></details>',
        unsafe_allow_html=True,
    )


@st.cache_data(show_spinner=False)
def _dataset_summary(name: str, modified: float) -> dict[str, Any]:
    """Row count and covered time range of one prepared dataset (modified only busts the cache)."""
    path = PROJECT_DIR / "data" / name
    first = last = ""
    rows = 0
    if path.suffix == ".json":
        with path.open("r", encoding="utf-8") as source:
            payload = json.load(source)
        records = [record for record in payload if isinstance(record, dict)] if isinstance(payload, list) else []
        rows = len(records)
        stamps = [_as_float(record.get("time_unix")) for record in records]
        stamps = [stamp for stamp in stamps if stamp is not None]
        if stamps:
            first, last = (
                datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                for stamp in (min(stamps), max(stamps))
            )
    else:
        with path.open("r", encoding="utf-8", newline="") as source:
            for row in csv.DictReader(source):
                stamp = row.get("timestamp", "")
                if stamp:
                    first = first or stamp
                    last = stamp
                rows += 1
    return {"rows": rows, "first": first, "last": last}


def _render_datasets() -> None:
    st.subheader("Dados preparados")
    table = []
    for name, used_by in DATASETS:
        path = PROJECT_DIR / "data" / name
        if not path.exists():
            table.append({"Ficheiro": f"data/{name}", "Registos": None, "Início (UTC)": "", "Fim (UTC)": "", "Usado em": "Ficheiro em falta"})
            continue
        try:
            summary = _dataset_summary(name, path.stat().st_mtime)
        except (OSError, ValueError) as error:
            table.append({"Ficheiro": f"data/{name}", "Registos": None, "Início (UTC)": "", "Fim (UTC)": "", "Usado em": f"Erro a ler: {error}"})
            continue
        table.append(
            {
                "Ficheiro": f"data/{name}",
                "Registos": summary["rows"],
                "Início (UTC)": summary["first"],
                "Fim (UTC)": summary["last"],
                "Usado em": used_by,
            }
        )
    st.dataframe(table, hide_index=True, width="stretch")
    st.markdown(
        '<p class="flow-caption">Gerados por <code>data_preparation.ipynb</code> a partir dos ficheiros ZTBus do autocarro B208.</p>',
        unsafe_allow_html=True,
    )


def _render_styles() -> None:
    st.markdown(
        """
        <style>
        .stApp, [data-testid="stAppViewContainer"] { --primary-color: #5a796d; --primaryColor: #5a796d; background: #f0f3f1; color: #36434a; }
        [data-testid="stMainBlockContainer"] { max-width: 1200px !important; margin-left: auto !important; margin-right: auto !important; padding: 78px 28px 36px !important; }
        [data-testid="stMainBlockContainer"] > div { gap: 16px; }
        [data-testid="stVerticalBlock"] { gap: 12px; }
        h1, h2, h3, p { color: #36434a; }
        h1 { text-align: center; letter-spacing: -.025em; }
        h2, h3 { color: #3f4e54; }
        .replay-eyebrow { margin: 0 0 5px; color: #5a7168; font-size: 10px; font-weight: 700; letter-spacing: .14em; text-align: center; text-transform: uppercase; }
        .replay-subtitle { margin: 0; color: #59686d; font-size: 14px; line-height: 1.5; text-align: center; }
        [data-testid="stVerticalBlockBorderWrapper"] { border-color: #d9e0dc !important; border-radius: 14px !important; background: #fff !important; box-shadow: 0 3px 12px rgba(38, 50, 44, .035); }
        .replay-topic-card { min-height: 142px; padding: 15px 16px; border: 1px solid #d9e0dc; border-radius: 12px; background: #fff; box-shadow: 0 2px 10px rgba(38, 50, 44, .035); }
        .replay-topic-head { display: flex; align-items: center; gap: 8px; min-width: 0; }
        .replay-topic-head code { overflow: hidden; color: #405c50; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
        .replay-topic-indicator { width: 8px; height: 8px; flex: 0 0 8px; border-radius: 50%; background: #8b9891; }
        .replay-topic-indicator.is-live { background: #4c805f; box-shadow: 0 0 0 3px #e7f0e9; }
        .replay-topic-state { margin-left: auto; color: #59686d; font-size: 11px; white-space: nowrap; }
        .replay-topic-foot { margin: 11px 0 7px; color: #59686d; font-size: 11px; font-variant-numeric: tabular-nums; }
        .replay-topic-card details { border-top: 1px solid #e7ebe8; padding-top: 7px; }
        .replay-topic-card summary { color: #53635c; cursor: pointer; font-size: 11px; }
        .replay-topic-card pre { max-height: 180px; margin: 9px 0 0; padding: 10px; overflow: auto; border-radius: 7px; color: #39484e; background: #f3f5f4; font: 11px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }
        .flow-metrics { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; width: 100%; box-sizing: border-box; }
        .flow-metric { min-width: 0; padding: 12px 8px; border: 1px solid #d9e0dc; border-radius: 11px; background: #fff; text-align: center; }
        .flow-metric span { display: block; color: #59686d; font-size: 12px; line-height: 1.35; }
        .flow-metric strong { display: block; margin-top: 5px; color: #3e4c52; font-size: 26px; font-weight: 600; font-variant-numeric: tabular-nums; }
        .flow-caption { margin: 12px 0 0; color: #59686d; text-align: center; font-size: 12px; }
        .twin-metrics { grid-template-columns: repeat(4, minmax(0, 1fr)); }
        .flow-metric strong.twin-value { font-size: 18px; overflow-wrap: anywhere; }
        .twin-raw { margin-top: 10px; padding: 10px 14px; border: 1px solid #d9e0dc; border-radius: 10px; background: #fff; }
        .twin-raw summary { color: #53635c; cursor: pointer; font-size: 12px; }
        .twin-raw pre { max-height: 260px; margin: 9px 0 0; padding: 10px; overflow: auto; border-radius: 7px; color: #39484e; background: #f3f5f4; font: 11px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }
        .replay-progress { display: block; width: 100%; height: 8px; margin: 8px 0 2px; appearance: none; border: 0; border-radius: 999px; background: #dce4df; }
        .replay-progress::-webkit-progress-bar { border-radius: 999px; background: #dce4df; }
        .replay-progress::-webkit-progress-value { border-radius: 999px; background: #668578; }
        .replay-progress::-moz-progress-bar { border-radius: 999px; background: #668578; }
        div[data-testid="stDataFrame"] { border: 1px solid #d9e0dc; border-radius: 10px; overflow: hidden; }
        button[kind="primary"], button[data-testid="stBaseButton-primary"] { background: #5a796d !important; border-color: #5a796d !important; color: #fff !important; }
        button[kind="primary"] *, button[data-testid="stBaseButton-primary"] * { color: #fff !important; }
        button[kind="primary"]:hover, button[data-testid="stBaseButton-primary"]:hover { background: #4b6b5e !important; border-color: #4b6b5e !important; }
        button[kind="primary"]:disabled, button[data-testid="stBaseButton-primary"]:disabled { background: #dfe6e1 !important; border-color: #dfe6e1 !important; color: #596960 !important; }
        button[kind="secondary"], button[data-testid="stBaseButton-secondary"] { border-color: #d3dcd6 !important; color: #465950 !important; background: #fff !important; }
        div[data-testid="stAlert"], div[role="alert"] { border: 1px solid #d6dfd9 !important; border-radius: 10px !important; background: #e8eeea !important; color: #43554c !important; }
        div[data-testid="stAlert"] *, div[role="alert"] * { background-color: transparent !important; color: #43554c !important; }
        button { min-height: 40px; border-radius: 10px !important; }
        button:focus-visible, input:focus-visible, [role="slider"]:focus-visible { outline: 3px solid #819b8e !important; outline-offset: 2px !important; }
        .replay-status { display: flex; width: fit-content; align-items: center; gap: 8px; margin: 14px auto 0; padding: 7px 12px; border: 1px solid #d9e0dc; border-radius: 999px; background: #fff; color: #4d5d55; font-size: 12px; }
        .replay-connection-note, .replay-empty { margin: 18px 0; padding: 12px 16px; border: 1px solid #d6dfd9; border-radius: 10px; background: #e8eeea; color: #43554c; text-align: center; font-size: 13px; }
        [data-testid="stSlider"] [data-baseweb="slider"] div { accent-color: #5a796d; }
        [data-testid="stSlider"] [role="slider"] { background: #5a796d !important; border-color: #5a796d !important; }
        [data-testid="stSlider"] [data-baseweb="slider"] > div:first-child { background: #dce4df !important; }
        [data-testid="stSlider"] [data-baseweb="slider"] > div:first-child > div { background: #668578 !important; }
        [data-testid="stProgress"] [role="progressbar"] { border-radius: 999px; background: #dce4df !important; }
        [data-testid="stProgress"] [role="progressbar"] > div { border-radius: 999px; background: #668578 !important; }
        @media (max-width: 700px) {
          [data-testid="stMainBlockContainer"] { padding: 72px 14px 24px !important; }
          .replay-topic-head code { font-size: 11px; }
          .twin-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _render_controls_card(redis_client: Any, connected: bool, status: str, control: dict[str, str]) -> float:
    with st.container(border=True):
        st.subheader("Reprodução")
        # While a replay is live, Redis owns the speed: a new tab or reload must show it, not reset it to 1×.
        if "replay_speed" not in st.session_state or (connected and status in {"running", "paused"}):
            st.session_state["replay_speed"] = _replay_speed_from(control)
        speed = st.slider(
            "Velocidade do dataset",
            min_value=0.25,
            max_value=10.0,
            step=0.25,
            format="%.2fx",
            key="replay_speed",
            on_change=_push_replay_speed,
            args=(redis_client,),
            disabled=redis_client is None,
            help="1× segue o tempo original do dataset; valores maiores aceleram e menores desaceleram.",
        )
        saved_total = control.get("total")
        total = int(saved_total) if saved_total else _dataset_count()
        index = int(control.get("index", 0) or 0)
        progress_value = min(index, total) if total else 0
        st.markdown(
            f'<progress class="replay-progress" max="{max(total, 1)}" value="{progress_value}" aria-label="Progresso do replay"></progress><p class="replay-progress-label">{index:,} / {total:,} amostras</p>',
            unsafe_allow_html=True,
        )

        start_col, pause_col = st.columns(2, gap="small")
        with start_col:
            start_clicked = st.button(
                "Iniciar replay",
                type="primary",
                width="stretch",
                disabled=redis_client is None or status in {"starting", "running", "paused"},
            )
        with pause_col:
            pause_label = "Retomar" if status == "paused" else "Pausar"
            pause_clicked = st.button(
                pause_label,
                width="stretch",
                disabled=redis_client is None or status not in {"running", "paused"},
            )
        stop_col, services_col = st.columns(2, gap="small")
        with stop_col:
            stop_clicked = st.button(
                "Parar replay",
                width="stretch",
                disabled=redis_client is None or status not in {"starting", "running", "paused"},
            )
        with services_col:
            stop_services_clicked = st.button(
                "Parar broker e Redis",
                width="stretch",
                help="Desliga os serviços Docker depois de parar a reprodução.",
            )

        if start_clicked and redis_client is not None:
            _start_replay(redis_client, speed)
            try:
                started = redis_client.hget(CONTROL_KEY, "status") == "running"
            except Exception:
                started = False
            if started:
                st.rerun()
        if pause_clicked and redis_client is not None:
            redis_client.hset(CONTROL_KEY, mapping={"status": "running" if status == "paused" else "paused"})
            st.rerun()
        if stop_clicked and redis_client is not None:
            redis_client.hset(CONTROL_KEY, mapping={"status": "stopped"})
            st.rerun()

        if stop_services_clicked:
            if connected and status in {"starting", "running", "paused"}:
                redis_client.hset(CONTROL_KEY, mapping={"status": "stopped"})
            with st.spinner("A desligar os serviços…"):
                try:
                    result = _compose("stop", "state-bridge", "mqtt-broker", "redis-cache")
                except (FileNotFoundError, subprocess.TimeoutExpired) as error:
                    st.error(f"Não foi possível parar o Docker Compose: {error}")
                else:
                    if result.returncode != 0:
                        st.error(result.stderr.strip() or "Não foi possível parar os serviços.")
                    else:
                        st.success("Broker, Redis e bridge parados.")
            st.rerun()
    return speed


def _render_flow_card(redis_client: Any, connected: bool, status: str, speed: float, refresh: Any | None) -> None:
    with st.container(border=True):
        st.subheader("Estado do fluxo")
        topic_counts = redis_client.hgetall("ztbus:replay:topic_counts") if connected else {}
        received = sum(int(value) for value in topic_counts.values()) if topic_counts else 0
        active_topics = sum(1 for topic in TOPICS if _latest_payload(redis_client, topic)) if connected else 0
        st.markdown(
            f'<div class="flow-metrics"><div class="flow-metric"><span>Mensagens recebidas</span><strong>{received:,}</strong></div><div class="flow-metric"><span>Tópicos ativos</span><strong>{active_topics} / 6</strong></div></div>',
            unsafe_allow_html=True,
        )
        if refresh is None:
            caption = "Instala as dependências do projeto para atualizar a atividade automaticamente."
        elif status in {"running", "paused"}:
            caption = f"A atualizar a cada segundo · velocidade atual {speed:g}×"
        else:
            caption = f"A vista acompanha a execução do replay · velocidade {speed:g}×"
        st.markdown(f'<p class="flow-caption">{html.escape(caption)}</p>', unsafe_allow_html=True)


def render_replay_page() -> None:
    refresh = _load_refresh_helper()

    _render_styles()
    redis_client = _get_redis()
    connected = _redis_is_live(redis_client)

    control = redis_client.hgetall(CONTROL_KEY) if connected else {}
    if connected:
        control = _mark_dead_producer(redis_client, control)
    status = control.get("status", "idle")
    status_labels = {
        "idle": "Pronto a iniciar",
        "starting": "A iniciar",
        "running": "Em execução",
        "paused": "Em pausa",
        "stopped": "Parado",
        "completed": "Concluído",
        "error": "Erro",
    }
    status_dot = "#4c805f" if status == "running" else "#9a7440" if status == "paused" else "#89958e"
    st.markdown('<p class="replay-eyebrow">DiTEC · simulação de sensores</p>', unsafe_allow_html=True)
    st.title("Replay control")
    st.markdown(
        '<p class="replay-subtitle">Reproduz o dataset como telemetria MQTT e acompanha o que chega a cada tópico.</p>',
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<div class="replay-status"><span aria-hidden="true" style="width:8px;height:8px;border-radius:50%;background:{status_dot}"></span>{html.escape(status_labels.get(status, status.title()))}</div>',
        unsafe_allow_html=True,
    )
    if status == "error" and control.get("error"):
        st.error(control["error"])

    if redis_client is None:
        missing_dependency = False
        try:
            import redis  # noqa: F401
        except ImportError:
            missing_dependency = True
        if missing_dependency:
            st.error("Falta o cliente Redis nesta instalação. Instala as dependências com `pip install -r requirements.txt`.")
    elif not connected:
        st.markdown('<div class="replay-connection-note">O Redis ainda não está ligado. Usa «Iniciar replay» para arrancar o broker, Redis e bridge.</div>', unsafe_allow_html=True)

    left, right = st.columns([1, 1])
    with left:
        speed = _render_controls_card(redis_client, connected, status, control)
    with right:
        _render_flow_card(redis_client, connected, status, speed, refresh)

    st.divider()
    _render_twin_state(redis_client, connected)

    st.divider()
    st.subheader("Tópicos MQTT")
    topic_columns = st.columns(2)
    topic_counts = redis_client.hgetall("ztbus:replay:topic_counts") if connected else {}
    for position, topic in enumerate(TOPICS):
        payload = _latest_payload(redis_client, topic) if connected else None
        count = int(topic_counts.get(f"{TOPIC_PREFIX}/{topic}", 0))
        with topic_columns[position % 2]:
            _render_topic_card(topic, payload, count)

    st.divider()
    st.subheader("Mensagens recentes")
    messages = _recent_messages(redis_client) if connected else []
    if messages:
        st.dataframe(messages, hide_index=True, width="stretch", height=370)
    else:
        st.markdown('<div class="replay-empty">Ainda não chegaram mensagens. Inicia o replay para ver a atividade MQTT aqui.</div>', unsafe_allow_html=True)

    st.divider()
    _render_datasets()

    if refresh is not None and status in {"running", "paused"}:
        refresh(interval=1000, key="replay_live_refresh")
