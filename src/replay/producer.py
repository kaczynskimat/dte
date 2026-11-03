import paho.mqtt.client as mqtt
import time
import pandas as pd
import json

client = mqtt.Client()
client.connect("localhost", 1883, 60)
# start the network loop in a separate thread
client.loop_start()
# publish a test string to "ztbus/test" topic
client.publish("ztbus/test", "Hello, MQTT!")
client.publish("ztbus/test2", "Another test!")

time.sleep(1)  # wait for the message to be sent

df = pd.read_csv("../../data/clean_replay_mission.csv")

prefix = "ztbus/B208"
speed = 1 # speed factor
t_start = df['time_unix'].iloc[0] # dataset start time
t0_real = time.time() # real world start time
for i in range(len(df)):
    row = df.iloc[i]
    t = row['time_unix'] # current time in dataset

    # Formula from the requirements
    t_target = t0_real + (t - t_start) / speed
    sleep_duration = t_target - time.time()
    if sleep_duration > 0:
        time.sleep(sleep_duration)

    # print(f"Replaying row {i} at timestamp {time.time()}.")

    # timestamp data
    meta = {
        "timestamp": str(row['timestamp']),
        "time_unix": int(row['time_unix'])
    }

    # location topic
    location_payload = {
        **meta,
        "latitude": row['latitude'],
        "longitude": row['longitude'],
        "altitude": row['gnss_altitude']
    }
    client.publish(f"{prefix}/location", json.dumps(location_payload))

    # kinematics topic
    kinematics_payload = {
        **meta,
        "speed_kmh": row['speed_kmh'],
        "heading_deg": row['heading_deg']
    }
    client.publish(f"{prefix}/kinematics", json.dumps(kinematics_payload))

    # energy topic
    energy_payload = {
        **meta,
        "power_kw": row['power_kw'],
        "grid_available": int(row['status_gridIsAvailable'])
    }
    client.publish(f"{prefix}/energy", json.dumps(energy_payload))

    # cabin topic
    cabin_payload = {
        **meta,
        "doors_open": int(row['status_doorIsOpen']),
        "passengers": int(row['itcs_numberOfPassengers']),
        "temp_celsius": row['temp_celsius']
    }
    client.publish(f"{prefix}/cabin", json.dumps(cabin_payload))


    # odometry & steering (Wheel encoders & bus joint angle)
    odometry_payload = {
        **meta,
        "steering_angle_rad": row['odometry_steeringAngle'],
        "articulation_angle_rad": row['odometry_articulationAngle'],
        "wheel_speed_fl": row['odometry_wheelSpeed_fl'],
        "wheel_speed_fr": row['odometry_wheelSpeed_fr'],
        "wheel_speed_ml": row['odometry_wheelSpeed_ml'],
        "wheel_speed_mr": row['odometry_wheelSpeed_mr'],
        "wheel_speed_rl": row['odometry_wheelSpeed_rl'],
        "wheel_speed_rr": row['odometry_wheelSpeed_rr']
    }
    client.publish(f"{prefix}/odometry", json.dumps(odometry_payload))

    # brakes & mechanical traction topic
    brakes_payload = {
        **meta,
        "halt_brake": int(row['status_haltBrakeIsActive']),
        "park_brake": int(row['status_parkBrakeIsActive']),
        "brake_bar": row['brake_bar'],
        "traction_force_n": row['traction_tractionForce']
    }
    client.publish(f"{prefix}/brakes", json.dumps(brakes_payload))


    if i % 50 == 0:
        print(f"🚌 [{row['timestamp']}] Replaying row {i}/{len(df)} | Speed: {row['speed_kmh']} km/h")






    