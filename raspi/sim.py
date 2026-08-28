"""
sim.py - FeliCaリーダー無しでモジュールを動かす擬似クライアント

raspi.py をそのまま import して NFC 読み取りだけをキーボード入力に差し替える。
通信・死活・下りコマンドの処理は実機と同じコードが走るので、ここで通れば
実機で疑うのはリーダー周りだけになる。

    python raspi/sim.py
    GEMMBA_DEVICE_ID=pi02 python raspi/sim.py    # 2台目として起動

コンソール:
    <タグID>   その社員証をタッチしたことにする（例: 0123456789abcdef）
    y / n      サーバーからの確認に答える
    q          終了（offline を通知してから抜ける）
"""
import json
import queue
import sys
import threading

import raspi

# raspi.ask_yes_no を差し替える。実機ではここが物理ボタン(B-2)になる。
# 下のメインループと stdin を取り合わないよう、答えはキュー越しに受け渡す。
_confirm = {"active": False, "answer": queue.Queue(maxsize=1)}


def sim_ask(text, lines, timeout):
    raspi.render_display(list(lines) + [text, "[y] はい  [n] いいえ  ← ここに入力"])
    # 前回の答えが残っていると即座に返ってしまう
    while not _confirm["answer"].empty():
        _confirm["answer"].get_nowait()
    _confirm["active"] = True
    try:
        return _confirm["answer"].get(timeout=timeout)
    except queue.Empty:
        return None
    finally:
        _confirm["active"] = False


raspi.ask_yes_no = sim_ask


def publish_offline():
    """Ctrl-C ではなく q で抜けたときに、LWT を待たず即座にオフラインにする"""
    raspi.client.publish(
        raspi.STATUS_TOPIC,
        json.dumps({"device_id": raspi.DEVICE_ID, "online": False}),
        qos=1, retain=True,
    ).wait_for_publish()


def main():
    raspi.connect_forever()
    raspi.client.loop_start()
    threading.Thread(target=raspi.supervisor, daemon=True).start()
    threading.Thread(target=raspi.heartbeat, daemon=True).start()

    print(f"[sim] device_id={raspi.DEVICE_ID} で待機中。タグIDを入力するとタッチになります。")
    print("[sim] y/n で確認に応答、q で終了。")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if line in ("q", "quit", "exit"):
            break
        if _confirm["active"] and line.lower() in ("y", "yes", "n", "no"):
            _confirm["answer"].put(line.lower() in ("y", "yes"))
            continue
        raspi.send_to_host_tag_id(line)
        print(f"[sim] タッチを送信: {line}")

    publish_offline()
    raspi.client.loop_stop()
    raspi.client.disconnect()
    print("[sim] 終了しました。")


if __name__ == "__main__":
    main()
