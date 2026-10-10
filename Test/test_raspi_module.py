# -*- coding: utf-8 -*-
"""
ラズパイのモジュール（raspi/raspi.py, raspi/rc522.py）の不安定さにつながる箇所の確認。
ハードもブローカーも要らない。ボタン・MQTT・RC522・arecord は差し替える。

  1. 問い合わせ中に display が来ても落ちず、その問い合わせは時間切れ(None)で閉じる
  2. 問い合わせ中に次の問い合わせが来たら、古い方は閉じ、新しい方の画面を消さない
  3. 切断中のタッチ・長押しは送らない（繋がり直した後にまとめて届かない）
  4. 壊れた指示で on_message が例外を漏らさない。数値が壊れていても返事は返す
  5. RC522 が自分でリセットしたら（設定が初期値に戻ったら）入れ直す
  6. 録音で arecord がすぐ落ちたら、前回の録音を送らない。録音中の録音指示は busy
  7. 切断したら「再接続しています」を出す

    python Test/test_raspi_module.py
"""
import json
import os
import queue
import sys
import tempfile
import threading
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "raspi"))

# rc522 は spidev を import する。PCには無いので空の代わりを置く
sys.modules.setdefault("spidev", types.ModuleType("spidev"))
import raspi  # noqa: E402
import rc522  # noqa: E402

ok = True


def check(label, got, want):
    global ok
    mark = "OK " if got == want else "NG "
    if got != want:
        ok = False
    print(f"{mark}{label}: {got!r}" + ("" if got == want else f"  期待 {want!r}"))


# ---- 差し替え: MQTT（送ったものを記録する）と物理ボタン
sent = []
connected = {"v": True}


class Info:
    def wait_for_publish(self, timeout=None):
        pass


raspi.client.publish = lambda topic, payload, qos=0, retain=False: sent.append((topic, json.loads(payload))) or Info()
raspi.client.is_connected = lambda: connected["v"]
raspi.INPUT_MODE = "buttons"
raspi.init_buttons = lambda: True
raspi._print_screen = lambda: None


def replies():
    return [p for t, p in sent if t == raspi.REPLY_TOPIC]


def press(name):
    raspi._button_events.put(name)


def ask_in_thread(cmd, **fields):
    payload = dict(fields, cmd=cmd)
    msg = types.SimpleNamespace(payload=json.dumps(payload).encode())
    raspi.on_message(None, None, msg)


def wait_until(cond, sec=3.0):
    end = time.time() + sec
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


print("-- 1. 問い合わせ中の display --")
ask_in_thread("choice", request_id="A", text="どうしますか？", options=["実行", "登録", "終了"], timeout=10)
wait_until(lambda: raspi._screen["choice"] is not None)
ask_in_thread("display", lines=["別の表示"])
check("display で問い合わせが閉じて None が返る",
      wait_until(lambda: any(r["request_id"] == "A" for r in replies())) and
      [r["answer"] for r in replies() if r["request_id"] == "A"], [None])
press("ok")   # 以前はここで TypeError（choice が None）
time.sleep(0.4)
check("その後の決定で何も落ちない（返事は増えない）", len(replies()), 1)
check("画面は display のまま", raspi._screen["lines"], ["別の表示"])

print("\n-- 2. 問い合わせ中に次の問い合わせ --")
sent.clear()
while not raspi._button_events.empty():
    raspi._button_events.get_nowait()
ask_in_thread("choice", request_id="B", text="1つ目", options=["a", "b"], timeout=10)
wait_until(lambda: raspi._screen["choice"] and raspi._screen["choice"]["text"] == "1つ目")
ask_in_thread("confirm", request_id="C", text="2つ目", timeout=10)
check("古い方は None で閉じる", wait_until(lambda: any(r["request_id"] == "B" for r in replies())) and
      [r["answer"] for r in replies() if r["request_id"] == "B"], [None])
time.sleep(0.3)
check("新しい方の選択肢が画面に残る", (raspi._screen["choice"] or {}).get("text"), "2つ目")
press("right")
time.sleep(0.3)
press("ok")
check("新しい方に決定が届く（いいえ）", wait_until(lambda: any(r["request_id"] == "C" for r in replies())) and
      [r["answer"] for r in replies() if r["request_id"] == "C"], [False])
check("答えた後は選択肢を畳む", raspi._screen["choice"], None)

print("\n-- 3. 切断中のタッチ・長押し --")
sent.clear()
connected["v"] = False
raspi.send_to_host_tag_id("04aa")
check("切断中のタッチは送らない", [t for t, _ in sent], [])
check("繋ぎ直していることを出す", raspi._screen["lines"][1:2], ["サーバーに再接続しています…"])
raspi._long_press["armed"] = True
raspi._on_ok_held()
check("切断中の長押しは送らない", [t for t, _ in sent], [])
connected["v"] = True
raspi.send_to_host_tag_id("04aa")
check("繋がっていればタッチを送る", [(t, p["tag_id"]) for t, p in sent], [(raspi.DATA_TOPIC, "04aa")])

print("\n-- 4. 壊れた指示 --")
sent.clear()
for bad in (b"[1, 2]", b"\xff\xfe", b'{"cmd": "display", "lines": null}', b'"text"'):
    try:
        raspi.on_message(None, None, types.SimpleNamespace(payload=bad))
        survived = True
    except Exception as e:   # noqa: BLE001
        survived = repr(e)
    check(f"on_message が例外を漏らさない {bad[:20]!r}", survived, True)
ask_in_thread("choice", request_id="D", text="?", options=["x", "y"], timeout="abc", default="zz")
wait_until(lambda: raspi._screen["choice"] is not None)
press("ok")
check("数値が壊れていても返事を返す", wait_until(lambda: any(r["request_id"] == "D" for r in replies())) and
      [r["answer"] for r in replies() if r["request_id"] == "D"], [0])

print("\n-- 5. RC522 の自己リセットからの復帰 --")


class FakeSpi:
    """RC522 のレジスタだけを真似る。reset() で電源のゆらぎによるリセットを起こす"""
    def __init__(self):
        self.regs = {rc522._VersionReg: 0x92}

    def xfer2(self, data):
        addr = (data[0] >> 1) & 0x3F
        if data[0] & 0x80:
            return [0, self.regs.get(addr, 0)]
        if addr == rc522._CommandReg and data[1] == rc522._PCD_RESETPHASE:
            self.reset()
            return [0, 0]
        self.regs[addr] = data[1]
        return [0, 0]

    def reset(self):
        self.regs = {rc522._VersionReg: 0x92, rc522._TxControlReg: 0x80}


reader = rc522.RC522.__new__(rc522.RC522)
reader._spi, reader._rst = FakeSpi(), None
reader._init_chip()
check("初期化した直後は正常", reader.healthy(), True)
reader._spi.reset()   # チップが勝手にリセットした
check("リセット後は異常と分かる", reader.healthy(), False)
reader.reinit()
check("入れ直すと正常に戻る", reader.healthy(), True)

# touch_loop が定期的に確かめて入れ直すこと（読めない状態のまま続けない）
calls = {"reinit": 0, "reads": 0}


class FakeReader:
    version = 0x92
    broken = True

    def healthy(self):
        return not self.broken

    def reinit(self):
        calls["reinit"] += 1
        self.broken = False

    def read_uid(self):
        calls["reads"] += 1
        if calls["reads"] > 40:
            raise KeyboardInterrupt   # 試験を終える
        return None

    def close(self):
        pass


fake = types.ModuleType("rc522")
fake.open_reader = lambda: FakeReader()
sys.modules["rc522"] = fake
raspi.NFC_HEALTH_SEC, raspi.TOUCH_POLL_SEC = 0.05, 0.01
try:
    raspi.touch_loop(lambda uid: None)
except KeyboardInterrupt:
    pass
sys.modules["rc522"] = rc522
check("touch_loop がリセットに気づいて入れ直す", calls["reinit"], 1)

print("\n-- 6. 録音 --")
tmpdir = tempfile.mkdtemp()
raspi.REC_PATH = os.path.join(tmpdir, "rec.wav")
with open(raspi.REC_PATH, "wb") as f:
    f.write(b"x" * 5000)   # 前回の録音が残っている


class FakeButton:
    is_pressed = False

    def wait_for_press(self, timeout=None):
        return True

    def wait_for_release(self, timeout=None):
        time.sleep(1.0)
        return True


class DeadArecord:
    """マイクが他で使用中などで、起動してすぐ落ちる arecord"""
    returncode = 1

    def __init__(self, cmd, stdout=None, stderr=None):
        stderr.write(b"arecord: main:850: audio open error: Device or resource busy")

    def poll(self):
        return 1


import subprocess  # noqa: E402
real_popen = subprocess.Popen
subprocess.Popen = DeadArecord
raspi._buttons["ok"] = FakeButton()
check("arecord がすぐ落ちたら None（前回の録音を送らない）", raspi.record_while_held(5, 5), None)
check("前回の録音は消してある", os.path.exists(raspi.REC_PATH), False)
subprocess.Popen = real_popen

sent.clear()
raspi._record_lock.acquire()   # 録音中
raspi._handle_record({"request_id": "R", "url": "http://x/api/voice"})
raspi._record_lock.release()
check("録音中の録音指示は busy", [(r["request_id"], r.get("error")) for r in replies()], [("R", "busy")])

print("\n-- 7. 切断の表示 --")
raspi.on_disconnect(None, None, None, "test", None)
check("切断したら再接続中の表示", (raspi._screen["state"], raspi._screen["lines"][1:]),
      ("offline", ["サーバーに再接続しています…"]))

print("\nすべてOK" if ok else "\nNG があります")
sys.exit(0 if ok else 1)
