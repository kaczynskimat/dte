import paho.mqtt.client as mqtt
import redis
import json
import os

from src.state_machine import infer_state
from src.state_bridge.quality import evaluate_quality


r: redis.Redis

def on_message(client, userdata, msg):
    topic = msg.topic

    try:
        payload = msg.payload.decode('utf-8')  # decode the payload to a string
        print(f"Received message on topic {topic}: {payload}")
        # r.set(topic, payload)  # store the message in Redis with the topic as the key
        data = json.loads(payload)
        # clean_data = {k: ("" if v is None else v) for k, v in data.items()} # replace None values with empty strings

        if not isinstance(data, dict):
            print(f"Ignoring non-object JSON payload on topic: {topic}")
            return

        clean_data = {}
        for k, v in data.items():
            if isinstance(v, dict):
                clean_data[k] = json.dumps(v) # convert nested dict to string
            elif v is None:
                clean_data[k] = "" # handle None/null
            else:
                clean_data[k] = v # keep ints, floats, strings as-is

        topic_parts = topic.split('/')
        bus_id = topic_parts[1]  # grabs "B208"
        key = f"bus:{bus_id}:state"
        sensor_times_key = f"bus:{bus_id}:sensor:timestamps"

        # A new replay must not inherit sensor readings from the previous run.
        if topic.endswith("/replay") and data.get("replay_status") == "running":
            r.delete(key, sensor_times_key)

        r.hset(key, mapping=clean_data)
        if len(topic_parts) >= 4 and topic_parts[2] == "sensors":
            sensor_name = topic_parts[3]
            sensor_time = data.get("time_unix", data.get("timestamp"))
            if sensor_time is not None:
                r.hset(sensor_times_key, sensor_name, sensor_time)
        if topic.endswith("/replay/tick"):
            telemetry = r.hgetall(key)
            sensor_times = r.hgetall(sensor_times_key)
            quality = evaluate_quality(
                telemetry,
                sensor_times,
                data.get("time_unix", data.get("timestamp")),
            )
            r.hset(key, mapping={
                "operational_state": infer_state(telemetry),
                "representation_timestamp": data.get("timestamp", ""),
                "representation_sequence": data.get("snapshot_sequence", ""),
                **quality,
            })
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        IndexError,
        redis.RedisError,
    ) as error:
        # Invalid messages must not stop the MQTT loop.
        print(f"Ignoring message on {topic}: {error}")


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        client.subscribe("ztbus/#")  # subscribe to all topics
        print("Connected to MQTT broker and subscribed to ztbus/#")
    else:
        print(f"MQTT connection failed: {reason_code}")


def main() -> None:
    global r
    r = redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        decode_responses=True,
    )
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect_async(
        os.getenv("MQTT_HOST", "localhost"),
        int(os.getenv("MQTT_PORT", "1883")),
        60,
    )
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    main()
