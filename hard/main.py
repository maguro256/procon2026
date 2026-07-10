import json
import os
import sys
import paho.mqtt.client as mqtt

def load_devices():
    #登録済みの端末読み込み
    devices_file = os.path.join(os.path.dirname(__file__), 'devices.json')
    with open(devices_file, 'r') as f:
        devices = json.load(f)
    return devices

def load_clients():
    #登録済みのクライアント読み込み
    clients_file = os.path.join(os.path.dirname(__file__), 'clients.json')
    with open(clients_file, 'r') as f:
        clients = json.load(f)

    id_to_name = {}
    for info in clients.values():
        tag_id = info['ID']
        name = info['name']
        id_to_name[tag_id] = name
    return id_to_name

def main():
    devices = load_devices()
    clients = load_clients()
    print("loaded devices")
    print("loaded clients")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message
    client.user_data_set({'devices': devices, 'clients': clients})
    client.connect("localhost", 1883, 60)
    client.loop_forever()

def on_connect(client,userdata,flags,reason_code,properties):
    print(f"Userdata: {userdata}")
    print(f"Flags: {flags}")
    print(f"Reason Code: {reason_code}")
    print(f"Properties: {properties}")

    client.subscribe("pi/+/data")

def on_message(client, userdata, msg):
    devices = userdata['devices']
    clients = userdata['clients']

    topic_parts = msg.topic.split("/")
    device_id = topic_parts[1]

    if device_id in devices:
        print("Device Name: ", device_id)
    else:
        print("Unknown device: ", device_id)
        return
    
    msg_payload = msg.payload.decode("utf-8")
    msg_json = json.loads(msg_payload)
    tag_id = msg_json.get("tag_id")
    if tag_id in clients:
        client_name = clients[tag_id]
        print(f"Client Name: {client_name}")
    else :
        print(f"Unknown client with tag ID: {tag_id}")

main()