# DTE: ZTBus trolleybus digital twin

A digital twin of Zurich trolleybus B208 on route 83, built from the ZTBus dataset. A recorded
mission is replayed as live MQTT telemetry, one topic per sensor. A bridge keeps the current state
of the bus in Redis and derives its operational state. An ML worker forecasts the power demand of
the next 10 s and flags anomalies. A Streamlit dashboard follows the bus in 3D and on a map of
Zurich.

## Architecture

```
data/test_mission_2021-10-05.csv
        │
        ▼
 replay producer ──MQTT──▶ Mosquitto ──▶ state bridge ──▶ Redis ◀──▶ prediction worker
 src/replay/producer.py  ztbus/B208/     src/state_bridge  │      src/ml/predictor.py
        ▲                sensors/* +     + state machine   │      (XGBoost, src/ml/models/model.pkl)
        │                replay/tick                       │  bus:B208:state · ztbus:replay:messages
        │ start / pause / speed                            ▼
        └──────────────── Streamlit dashboard ◀── twin feed (src/dashboard/twin_feed.py)
                          dashboard.py: Fleet (3D twin + map + state + ML) · Replay (control)
```

| Component | Path | What it does |
|---|---|---|
| Data preparation | `data_preparation.ipynb` | Cleans raw ZTBus CSVs into the mission files in `data/` |
| Replay producer | `src/replay/producer.py` | Publishes each second of a mission as one MQTT message per sensor plus a `replay/tick`, timed by `t0 + (T - T0) / speed`. The dashboard can pause, stop or change its speed (`--control-redis-host`) |
| MQTT broker / Redis | `docker-compose.yml` | Eclipse Mosquitto 2 on `1883`, Redis 7 on `6379` |
| State bridge | `src/state_bridge/bridge.py` | Merges every message into `bus:<bus>:state` and derives `operational_state` on each tick |
| State machine | `src/state_machine/inference.py` | `infer_state(telemetry) -> str`: Boarding / Cruising / Regenerating / Idling plus 10 more dimensions |
| ML | `src/ml/` | `train.py` trains the 10 s power forecast offline; `predictor.py` runs it on every tick and writes `ml_*` fields |
| Dashboard | `dashboard.py`, `src/dashboard/` | **Fleet**: 3D bus animated by the live state at the replay speed, operational state, ML forecast and anomaly flag, live map. **Replay**: start/pause/stop, speed, per-topic messages |

Documentation:

- [`docs/sensor_streams.md`](docs/sensor_streams.md): MQTT topics.
- [`docs/representation_model.md`](docs/representation_model.md): representation model.
- [`docs/telemetry_schema.md`](docs/telemetry_schema.md): CSV columns, Redis keys and ML fields.

## Running it

Every component has its own Docker image (`src/<component>/Dockerfile`, Python 3.13 with pinned
requirements). `docker-compose.yml` has two profiles:

| Profile | Starts | Use it for |
|---|---|---|
| `dashboard` | broker, Redis, state bridge, prediction worker, dashboard | Watching and controlling the twin in the browser |
| `demo` | broker, Redis, state bridge, prediction worker, replay producer | A headless replay of the whole mission |

Do not start both profiles together: the dashboard runs its own producer.

### With the dashboard (Docker)

Requirements: Docker with Compose.

```bash
docker compose --profile dashboard up -d --build
```

Open <http://localhost:8501>, go to the **Replay** page and press *Start replay*. Then switch to
**Fleet** to watch the twin. The speed slider changes the replay speed while it runs. Port 8503 is
the live feed the Fleet page polls, so it must stay published.

To stop everything:

```bash
docker compose --profile dashboard down
```

The dashboard replays `data/test_mission_2021-10-05.csv` from 07:00 to 09:00 by default. Set
`REPLAY_START` / `REPLAY_END` before `up` (for example `REPLAY_START="2021-10-05 12:00:00"`) to
pick another window, or set both to empty strings for the whole day.

### With the dashboard (local Python, for development)

Requirements: Docker with Compose, and Python 3.11+.

```bash
pip install -r requirements.txt
```

```bash
streamlit run dashboard.py
```

Here the Replay page starts the broker, Redis, the bridge and the prediction worker itself with
Docker Compose, and runs the producer locally. Code changes show up on the next page reload,
without rebuilding an image.

### Without the dashboard

```bash
REPLAY_SPEED=10 docker compose --profile demo up --build
```

This replays the whole mission headless. Follow the state with
`docker compose exec redis-cache redis-cli HGETALL bus:B208:state`.

### Configuration

| Variable | Default | Used by |
|---|---|---|
| `MQTT_HOST`, `MQTT_PORT` | `localhost`, `1883` | state bridge (`mqtt-broker` inside Compose) |
| `REDIS_HOST`, `REDIS_PORT` | `localhost`, `6379` | state bridge (`redis-cache` inside Compose) |
| `REPLAY_MQTT_HOST`, `REPLAY_REDIS_HOST` (+ `_PORT`) | `127.0.0.1` (`mqtt-broker` / `redis-cache` in Compose) | dashboard and the producer it starts |
| `REPLAY_BUS_ID` | `B208` | dashboard |
| `REPLAY_FILE` | `data/test_mission_2021-10-05.csv` | dashboard |
| `REPLAY_START`, `REPLAY_END` | `2021-10-05 07:00:00`, `2021-10-05 09:00:00` in the dashboard; whole mission in Compose | dashboard, Compose |
| `REPLAY_SPEED` | `1` | Compose |
| `TWIN_FEED_PORT` | `8503` | local JSON feed polled by the Fleet page |
| `TWIN_FEED_HOST` | `127.0.0.1` (`0.0.0.0` in the dashboard image) | interface the feed listens on |
| `DASHBOARD_MANAGES_SERVICES` | `1` (`0` in the dashboard image) | whether the Replay page starts and stops the Compose services itself |

## Tests

```bash
pip install -r src/ml/requirements.txt
```

```bash
python -m unittest discover -s tests
```

`tests/test_state_machine.py` and `tests/test_state_bridge.py` only need the standard library,
`paho-mqtt` and `redis`. The ML tests also load the model and the mission files.

## Status

| Issue | Status |
|---|---|
| #1 Data contract · #2 Data cleaning · #3 Broker + Redis · #4 Replay producer | Done |
| #5 Redis state bridge · #6 State machine | Done |
| #7 Offline ML model · #8 Real-time ML inference service | Done (#17) |
| #9 Dashboard layout · #10 Live map · #11 Dashboard on live Redis state | Done (#16) |
