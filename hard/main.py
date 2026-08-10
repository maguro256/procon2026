"""
hard/main.py - MQTT⇔アプリ本体 連携ブリッジ

各機材のモジュール(ESP32/Pi)は NFCタッチを検知すると
  topic: pi/<module_id>/data
  payload: {"tag_id": "<社員証のICタグID>"}
を publish する。本スクリプトはこれを購読し、app.py が公開する
HTTP API (/api/...) を叩いて実際のタスク割当・機材ロックを行う。
（app.py 側のコメントにある通り「ESP32側からはHTTPで叩く」想定の実装）

タッチの意味は状態遷移で決まる（概要の運用フロー通り）:
  機材が idle                        → 開始タッチ（タスク着手 or フリー利用でロック）
  機材が working かつ同じ作業者      → 終了タッチ（タスク完了 or ロック解除）
  機材が working かつ別の作業者      → 使用中なので拒否
  機材が stopped/maintenance         → 利用不可

デバイスID→機材(module_id)の対応は devices.json、
社員証タグID→表示名のローカルキャッシュは clients.json に持つ。
実際の作業者・機材の正・タスク割当ロジックはアプリ本体(DB)側が真実源。
"""
import json
import os

import paho.mqtt.client as mqtt
import requests

APP_BASE_URL = os.environ.get("GEMMBA_APP_URL", "http://localhost:5000")
MQTT_HOST = os.environ.get("GEMMBA_MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("GEMMBA_MQTT_PORT", "1883"))
HTTP_TIMEOUT = 5


def main():
    print(f"app server: {APP_BASE_URL}")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.loop_forever()


def on_connect(client, userdata, flags, reason_code, properties):
    print(f"Connected to MQTT broker (reason={reason_code})")
    client.subscribe("pi/+/data")


def on_message(client, userdata, msg):
    #pi/<module_id>/data
    module_id = msg.topic.split("/")[1]

    try:
        msg_json = json.loads(msg.payload.decode("utf-8"))
    except ValueError:
        print(f"[{module_id}] invalid JSON payload: {msg.payload!r}")
        return

    tag_id = msg_json.get("tag_id")
    if not tag_id:
        print(f"[{module_id}] payload missing tag_id")
        return

    handle_touch(module_id, tag_id)


def handle_touch(module_id, tag_id):
    """1回のNFCタッチを、機材の現在状態に応じて開始/終了/拒否に振り分けてAPIを叩く"""
    try:
        w_resp = requests.get(f"{APP_BASE_URL}/api/workers/{tag_id}/next_task", timeout=HTTP_TIMEOUT)
        eq_resp = requests.get(f"{APP_BASE_URL}/api/equipment/{module_id}/status", timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        print(f"[{module_id}] app server unreachable: {e}")
        return

    if w_resp.status_code == 404:
        print(f"[{module_id}] unknown NFC tag: {tag_id} → notifying app server")
        try:
            requests.post(
                f"{APP_BASE_URL}/api/unknown_tag",
                json={"nfc_tag_id": tag_id, "module_id": module_id},
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException as e:
            print(f"[{module_id}] failed to notify app server: {e}")
        return
    if eq_resp.status_code == 404:
        print(f"[{module_id}] module_id not registered as equipment: {module_id}")
        return
    w_resp.raise_for_status()
    eq_resp.raise_for_status()

    worker = w_resp.json()["worker"]
    tasks = w_resp.json()["tasks"]
    equipment = eq_resp.json()

    if equipment["status"] in ("stopped", "maintenance"):
        print(f"[{module_id}] {worker['name']}: equipment unavailable ({equipment['status']})")
        return

    if equipment["status"] == "working":
        if equipment["current_worker_id"] == worker["id"]:
            end_session(module_id, module_id, worker, equipment)
        else:
            print(f"[{module_id}] equipment locked by another worker; rejecting {worker['name']}'s touch")
        return

    start_session(module_id, tag_id, worker, equipment, tasks)


def start_session(module_id, tag_id, worker, equipment, tasks):
    """機材が空きの状態でのタッチ = 開始。担当タスクがあれば着手、無ければフリー利用でロック"""
    candidate = next(
        (t for t in tasks if t.get("equipment_id") in (None, equipment["id"])),
        None,
    )

    if candidate:
        requests.post(
            f"{APP_BASE_URL}/api/tasks/{candidate['id']}/start",
            json={"nfc_tag_id": tag_id, "module_id": module_id},
            timeout=HTTP_TIMEOUT,
        )
        print(f"[{module_id}] {worker['name']} started task: {candidate['title']}")
    else:
        requests.post(
            f"{APP_BASE_URL}/api/equipment/{module_id}/status",
            json={"status": "working", "nfc_tag_id": tag_id},
            timeout=HTTP_TIMEOUT,
        )
        print(f"[{module_id}] {worker['name']} started free-use (no task assigned)")

    # 誘導機能: 他の特定機材に紐づいた至急/高優先タスクがあれば知らせる
    elsewhere = next(
        (t for t in tasks
         if t.get("equipment_id") not in (None, equipment["id"]) and t["priority"] in ("urgent", "high")),
        None,
    )
    if elsewhere:
        print(f"[{module_id}] NOTE: {worker['name']} has a higher-priority task at another equipment: {elsewhere['title']}")


def end_session(device_id, module_id, worker, equipment):
    """機材を使用中の本人が再タッチ = 終了。タスク中なら完了記録、フリー利用ならロック解除のみ"""
    task_id = equipment.get("current_task_id")
    if task_id:
        requests.post(f"{APP_BASE_URL}/api/tasks/{task_id}/complete", timeout=HTTP_TIMEOUT)
        print(f"[{module_id}] {worker['name']} completed task #{task_id}")
    else:
        print(f"[{module_id}] {worker['name']} ended free-use")

    requests.post(
        f"{APP_BASE_URL}/api/equipment/{module_id}/status",
        json={"status": "idle"},
        timeout=HTTP_TIMEOUT,
    )


if __name__ == "__main__":
    main()
