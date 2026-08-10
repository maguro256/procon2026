import nfc
import paho.mqtt.client as mqtt
import json
import time

BROKER_HOST = "192.168.0.115"  # このPC(app.py + Mosquitto)のIP
BROKER_PORT = 1883
DEVICE_ID = "pi01"

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=DEVICE_ID)
client.connect(BROKER_HOST, BROKER_PORT, 60)
client.loop_start()

def waitTouch():
    result = {}

    def on_tag_connect(tag):
        result['tag'] = tag
        return True  

    with nfc.ContactlessFrontend('usb') as clf:
        clf.connect(rdwr={'on-connect': on_tag_connect})

    return result.get('tag')

def send_to_host_tag_id(tag_id):
    topic = f"pi/{DEVICE_ID}/data"
    payload = json.dumps({
        "device_id": DEVICE_ID,
        "tag_id": tag_id,
        "timestamp": time.time(),
    })
    info = client.publish(topic, payload, qos=1)
    info.wait_for_publish()

def main():
    last_tag_id = None
    while True:
        tag = waitTouch()
        if tag:
            tag_id = tag.identifier.hex()
            if tag_id != last_tag_id:
                print(f"Tag ID: {tag_id}")
                send_to_host_tag_id(tag_id)
            last_tag_id = tag_id
        else:
            last_tag_id = None

if __name__ == "__main__":
    main()