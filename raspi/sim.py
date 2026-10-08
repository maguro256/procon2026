"""
sim.py - カードリーダー無しでモジュールを動かす擬似クライアント

raspi.py をそのまま import して NFC 読み取りだけをキーボード入力に差し替える。
通信・死活・下りコマンドの処理は実機と同じコードが走るので、ここで通れば
実機で疑うのはリーダー周りだけになる。ターミナルを複数開けば複数台になる。

    python raspi/sim.py              # pi01 として起動
    python raspi/sim.py pi02         # 2台目（GEMMBA_DEVICE_ID=pi02 と同じ）

コンソール:
    <タグID>   その社員証をタッチしたことにする（例: 0123456789abcdef）
    @<番号>    list で出した番号の作業者のタグをタッチする（例: @1）
    list       DB(gemmba.db) に登録済みの作業者タグと機材のモジュールIDを出す
    y / n      サーバーからの確認に答える（選択肢は番号で答える）
    q          終了（offline を通知してから抜ける）

録音の指示(record)が来たら、マイクの代わりに GEMMBA_SIM_WAV のファイル
（既定: Test/tts_sample.wav）を送る。
"""
import json
import os
import sqlite3
import sys

# DEVICE_ID は raspi の import 時に決まるので、引数はそれより先に環境変数へ移す
if len(sys.argv) > 1:
    os.environ["GEMMBA_DEVICE_ID"] = sys.argv[1]

import raspi  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "gemmba.db")
SIM_WAV = os.environ.get("GEMMBA_SIM_WAV", os.path.join(ROOT, "Test", "tts_sample.wav"))


def sim_record(max_sec=raspi.REC_MAX_SEC, wait_sec=30):
    """ボタンもマイクも無いので、用意した WAV をそのまま録音結果として返す"""
    if not os.path.exists(SIM_WAV):
        print(f"[sim] 録音の代わりに送る WAV がありません: {SIM_WAV}")
        return None
    print(f"[sim] 録音の代わりに {SIM_WAV} を送ります")
    return SIM_WAV


raspi.record_while_held = sim_record


def load_tags():
    """作業者のタグ一覧。読むだけなので app.py の稼働中でも問題ない"""
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        workers = conn.execute(
            "SELECT name, nfc_tag_id FROM workers WHERE nfc_tag_id IS NOT NULL AND nfc_tag_id != '' "
            "ORDER BY id").fetchall()
        equipment = conn.execute(
            "SELECT name, COALESCE(NULLIF(hostname, ''), module_id) FROM equipment ORDER BY id").fetchall()
        conn.close()
        return workers, equipment
    except sqlite3.Error as e:
        print(f"[sim] {DB_PATH} を読めません: {e}")
        return [], []


def print_list(workers, equipment):
    print("[sim] 作業者のタグ（@番号 でタッチ）")
    for i, (name, tag) in enumerate(workers, 1):
        print(f"  @{i:<3} {tag:<20} {name}")
    print("[sim] 機材のモジュールID（python raspi/sim.py <ID> で起動）")
    for name, device_id in equipment:
        mark = " ← このターミナル" if device_id == raspi.DEVICE_ID else ""
        print(f"  {device_id or '(未設定)':<20} {name}{mark}")


def publish_offline():
    """Ctrl-C ではなく q で抜けたときに、LWT を待たず即座にオフラインにする"""
    raspi.client.publish(
        raspi.STATUS_TOPIC,
        json.dumps({"device_id": raspi.DEVICE_ID, "online": False}),
        qos=1, retain=True,
    ).wait_for_publish(timeout=5)


def touch(tag_id):
    raspi.send_to_host_tag_id(tag_id)
    print(f"[sim] タッチを送信: {tag_id}")


def main():
    raspi.render_display([f"Gemmba {raspi.DEVICE_ID}", "ブローカーを探しています…"])
    raspi.connect_forever()
    raspi.client.loop_start()
    raspi.threading.Thread(target=raspi.supervisor, daemon=True).start()
    raspi.threading.Thread(target=raspi.heartbeat, daemon=True).start()

    print(f"[sim] device_id={raspi.DEVICE_ID} で待機中。タグIDを入力するとタッチになります。")
    print("[sim] list で登録済みタグ一覧、確認・選択には y/n/番号、q で終了。")
    workers = []
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            # 確認・選択の表示中は、入力を raspi の console 入力キューへ渡す。
            # 答え方（y/n/番号）の解釈は実機の console モードと同じコードが行う。
            if raspi._screen["choice"]:
                raspi._stdin_lines.put(line)
                continue
            if line in ("q", "quit", "exit"):
                break
            if line == "list":
                workers, equipment = load_tags()
                print_list(workers, equipment)
                continue
            if line.startswith("@"):
                if not workers:
                    workers, _ = load_tags()
                n = line[1:]
                if not n.isdigit() or not 1 <= int(n) <= len(workers):
                    print("[sim] 番号が範囲外です。list で確認してください")
                    continue
                name, tag = workers[int(n) - 1]
                print(f"[sim] {name} のタグ")
                touch(tag)
                continue
            touch(line)
    except KeyboardInterrupt:
        pass

    publish_offline()
    raspi.client.loop_stop()
    raspi.client.disconnect()
    print("[sim] 終了しました。")


if __name__ == "__main__":
    main()
