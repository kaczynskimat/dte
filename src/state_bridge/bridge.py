import paho.mqtt.client as mqtt
import redis
import json

def on_message(client, userdata, msg):
    topic = msg.topic
    payload = msg.payload.decode('utf-8')  # decode the payload to a string
    print(f"Received message on topic {topic}: {payload}")
    # r.set(topic, payload)  # store the message in Redis with the topic as the key
    data = json.loads(payload)
    # clean_data = {k: ("" if v is None else v) for k, v in data.items()} # replace None values with empty strings

    clean_data = {}
    for k, v in data.items():
        if isinstance(v, dict):
            clean_data[k] = json.dumps(v) # convert nested dict to string
        elif v is None:
            clean_data[k] = "" # handle None/null
        else:
            clean_data[k] = v # keep ints, floats, strings as-is

    bus_id = topic.split('/')[1]  # grabs "B208"    
    key = f"bus:{bus_id}:state"


    r.hset(key, mapping=clean_data)




r = redis.Redis(host='localhost', port=6379, decode_responses=True) # decode responses returns strings instead of raw byte strings
client = mqtt.Client()
client.connect("localhost", 1883, 60)
client.on_message = on_message
client.subscribe("ztbus/#")  # subscribe to all topics
client.loop_forever()  # start the MQTT client loop



