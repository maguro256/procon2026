import nfc
import paho.mqtt.client as mqtt
import json
import os
import socket
import threading
import time

# 機材との対応付けに使う名前。管理画面の「デバイスID」と一致させること。
# モジュールの同一性はこのIDで決まるので、IPが変わっても影響しない。
DEVICE_ID = os.environ.get("GEMMBA_DEVICE_ID", "pi01")

# ブローカーはUDPブロードキャストで自動探索する。環境変数を指定した場合はそちらを優先。
#   GEMMBA_BROKER_HOST=192.168.0.114 python raspi.py
BROKER_HOST_ENV = os.environ.get("GEMMBA_BROKER_HOST")
BROKER_PORT_ENV = int(os.environ.get("GEMMBA_BROKER_PORT", "1883"))
DISCOVERY_PORT = int(os.environ.get("GEMMBA_DISCOVERY_PORT", "50505"))
DISCOVERY_REQUEST = b"GEMMBA_DISCOVER_V1"
DISCOVERY_TIMEOUT = 3.0

CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".broker_cache.json")
STATUS_TOPIC = f"pi/{DEVICE_ID}/status"
DATA_TOPIC = f"pi/{DEVICE_ID}/data"
KEEPALIVE = 30          # この2倍ほど無応答だとブローカーがLWTを配信する
HEARTBEAT_SEC = 30
RETRY_SEC = 5


# ------------------------------------------------------------ ブローカー探索

def discover_broker():
    """LANへブロードキャストし、app.py から自分のIPを教えてもらう"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(DISCOVERY_TIMEOUT)
    try:
        sock.sendto(DISCOVERY_REQUEST, ("255.255.255.255", DISCOVERY_PORT))
        deadline = time.time() + DISCOVERY_TIMEOUT
        while time.time() < deadline:
            try:
                data, _ = sock.recvfrom(1024)
            except socket.timeout:
                break
            try:
                info = json.loads(data.decode("utf-8"))
            except ValueError:
                continue
            if info.get("host"):
                return info["host"], int(info.get("port", 1883))
    except OSError as e:
        print(f"[discovery] ブロードキャストに失敗: {e}")
    finally:
        sock.close()
    return None


def load_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            info = json.load(f)
        return info["host"], int(info.get("port", 1883))
    except (OSError, ValueError, KeyError):
        return None


def save_cache(host, port):
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({"host": host, "port": port}, f)
    except OSError as e:
        print(f"[discovery] キャッシュ保存に失敗: {e}")


def resolve_broker():
    """環境変数 → 自動探索 → 前回成功したIP の順で候補を決める"""
    if BROKER_HOST_ENV:
        return BROKER_HOST_ENV, BROKER_PORT_ENV
    found = discover_broker()
    if found:
        return found
    cached = load_cache()
    if cached:
        print(f"[discovery] 応答なし。前回のブローカー {cached[0]}:{cached[1]} を試します。")
        return cached
    return None


def local_ip_toward(host):
    """ブローカーから見た自分のIP。管理画面に表示するだけで、識別には使わない"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((host, 9))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


# ------------------------------------------------------------ MQTT

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=DEVICE_ID)
# 電源断やネット切断で予告なく落ちた場合、ブローカーが代理で offline を配信する
client.will_set(
    STATUS_TOPIC,
    json.dumps({"device_id": DEVICE_ID, "online": False}),
    qos=1, retain=True,
)

_broker = {"host": None, "port": None}


def publish_online():
    payload = json.dumps({
        "device_id": DEVICE_ID,
        "online": True,
        "ip": local_ip_toward(_broker["host"]) if _broker["host"] else None,
    })
    # retain=True にすると、app.py が後から起動しても現在の状態を受け取れる
    client.publish(STATUS_TOPIC, payload, qos=1, retain=True)


def on_connect(client, userdata, flags, reason_code, properties):
    print(f"[MQTT] connected to {_broker['host']}:{_broker['port']} as {DEVICE_ID}")
    publish_online()


def on_disconnect(client, userdata, flags, reason_code, properties):
    print(f"[MQTT] disconnected (reason={reason_code})")


client.on_connect = on_connect
client.on_disconnect = on_disconnect


def connect_forever():
    """繋がるまで探索と接続を繰り返す。PCより先に起動しても落ちない"""
    while True:
        target = resolve_broker()
        if target:
            host, port = target
            try:
                client.connect(host, port, KEEPALIVE)
                _broker["host"], _broker["port"] = host, port
                save_cache(host, port)
                return
            except OSError as e:
                print(f"[MQTT] {host}:{port} へ接続できません ({e})")
        else:
            print("[discovery] ブローカーが見つかりません（PC側の app.py は起動していますか？）")
        time.sleep(RETRY_SEC)


def supervisor():
    """
    切断されたら再探索して繋ぎ直す。paho の自動再接続は同じIPを見続けるため、
    PC側のIPが変わったケースはここで拾う必要がある。
    """
    while True:
        time.sleep(RETRY_SEC)
        if client.is_connected():
            continue
        print("[MQTT] 切断を検知。ブローカーを再探索します。")
        client.loop_stop()
        connect_forever()
        client.loop_start()


def heartbeat():
    """定期的に生存を通知する。誰も機材に触らなくてもオンラインだと分かる"""
    while True:
        time.sleep(HEARTBEAT_SEC)
        if client.is_connected():
            publish_online()


# ------------------------------------------------------------ NFC

def waitTouch():
    result = {}

    def on_tag_connect(tag):
        result['tag'] = tag
        return True

    with nfc.ContactlessFrontend('usb') as clf:
        clf.connect(rdwr={'on-connect': on_tag_connect})

    return result.get('tag')


def send_to_host_tag_id(tag_id):
    payload = json.dumps({
        "device_id": DEVICE_ID,
        "tag_id": tag_id,
        "timestamp": time.time(),
    })
    info = client.publish(DATA_TOPIC, payload, qos=1)
    info.wait_for_publish()


def main():
    connect_forever()
    client.loop_start()
    threading.Thread(target=supervisor, daemon=True).start()
    threading.Thread(target=heartbeat, daemon=True).start()

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
