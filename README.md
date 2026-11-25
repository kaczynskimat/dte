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

Requirements: Docker (with Compose) and Python 3.11+.

### With the dashboard

```bash
pip install -r requirements.txt
```

```bash
streamlit run dashboard.py
```

Open the **Replay** page and press *Iniciar replay*. The dashboard starts the broker, Redis, the
state bridge and the prediction worker with Docker Compose (the first time builds the images). It
then runs the producer locally. Switch to **Fleet** to watch the twin. The speed slider changes the
replay speed while it runs.

The dashboard replays `data/test_mission_2021-10-05.csv` from 07:00 to 09:00 by default. Set
`REPLAY_START` / `REPLAY_END` (for example `REPLAY_START="2021-10-05 12:00:00"`) to pick another
window, or set both to empty strings for the whole day.

### Without the dashboard

```bash
REPLAY_SPEED=10 docker compose --profile demo up --build
```

This runs every service, including the producer, in Docker. Follow the state with
`redis-cli HGETALL bus:B208:state`.

### Configuration

| Variable | Default | Used by |
|---|---|---|
| `MQTT_HOST`, `MQTT_PORT` | `localhost`, `1883` | state bridge (`mqtt-broker` inside Compose) |
| `REDIS_HOST`, `REDIS_PORT` | `localhost`, `6379` | state bridge (`redis-cache` inside Compose) |
| `REPLAY_MQTT_HOST`, `REPLAY_REDIS_HOST` (+ `_PORT`) | `127.0.0.1` | dashboard and the producer it starts |
| `REPLAY_BUS_ID` | `B208` | dashboard |
| `REPLAY_FILE` | `data/test_mission_2021-10-05.csv` | dashboard |
| `REPLAY_START`, `REPLAY_END` | `2021-10-05 07:00:00`, `2021-10-05 09:00:00` in the dashboard; whole mission in Compose | dashboard, Compose |
| `REPLAY_SPEED` | `1` | Compose |
| `TWIN_FEED_PORT` | `8503` | local JSON feed polled by the Fleet page |

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
