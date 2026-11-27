# Fidelity and telemetry-quality validation

The bridge evaluates every completed replay tick. It checks that all 22
periodic sensors published a reading for the current source timestamp and that
their values are inside conservative physical limits. Route and stop are not
checked for staleness because they are event-based readings.

The current result is stored in `bus:B208:state`:

| Field | Meaning |
|---|---|
| `quality_status` | `valid`, `warning` (stale values), or `invalid` (missing or physically invalid values) |
| `quality_flags` | JSON list such as `["missing:latitude"]` or `["out_of_range:heading_deg"]` |
| `quality_missing_count` | Number of absent or empty periodic readings |
| `quality_stale_count` | Number of readings whose source time differs from the tick |
| `quality_invalid_count` | Number of readings outside their physical range |
| `quality_checked_timestamp` | Source time evaluated by the check |

Per-sensor source times are kept separately in
`bus:B208:sensor:timestamps`. Starting a new replay clears the previous state
and these timestamps, so a new mission cannot silently inherit old readings.

## End-to-end measurement

Install the two lightweight validation dependencies:

```bash
python -m pip install -r src/validation/requirements.txt
```

Start the broker, Redis and bridge:

```bash
docker compose --profile demo up -d --build mqtt-broker redis-cache state-bridge
```

Run the observer in one terminal before starting the replay:

```bash
python -m src.validation.fidelity --samples 60 --timeout 30
```

In a second terminal, replay the same number of samples:

```bash
docker compose --profile demo run --rm replay-producer \
  python -m src.replay.producer \
  --file /app/data/test_mission_2021-10-05.csv \
  --broker-host mqtt-broker \
  --bus-id B208 \
  --expected-route 83 \
  --speed 10 \
  --startup-delay 1 \
  --start-time "2021-10-05 07:00:00" \
  --max-rows 60
```

The observer matches each MQTT tick to its source CSV row, waits for the same
sequence in Redis, and reports field match rate, missing and mismatched values,
invalid snapshots, and mean/maximum bridge latency. It exits with a non-zero
status if any Redis value differs from the source.
