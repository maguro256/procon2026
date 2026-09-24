# -*- coding: utf-8 -*-
"""
音声でのタスク登録（E-2）の待ち時間の予算を見張る。

**諦める順番が逆転すると、登録できているのに「登録できませんでした」と出る。**
モジュールが先に諦めても、サーバーは処理を続けてタスクを作るため、作業者は
録り直して二重登録になる。数字を1つ動かしただけで壊れるので、順序を固定する。

    python Test/test_voice_budget.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app
from voice import intent

ok = True


def check(label, cond, detail=""):
    """detail は崩れたときだけ出す。通っているのに不等式が並ぶと読みにくい"""
    global ok
    if not cond:
        ok = False
    print(f"{'OK  ' if cond else 'NG  '}{label}" + ("" if cond else f"  → {detail}"))


# サーバーが1回の /api/voice で使いうる最大。両方が上限まで粘るのが最悪ケース
process = app.VOICE_TIMEOUT + intent.TIMEOUT
# モジュールが取りうる最長。押下を待って、目一杯録って、送信の応答を待つ
module_max = app.RECORD_WAIT_SEC + app.RECORD_MAX_SEC + app.RECORD_UPLOAD_SEC
# サーバーが応答を待つ時間。_request_answer が timeout + 2 待つ
server_wait = app.RECORD_TOTAL_SEC + 2

print(f"文字起こし {app.VOICE_TIMEOUT}秒 + 意図分析 {intent.TIMEOUT}秒 = 処理 {process}秒")
print(f"モジュールの送信待ち {app.RECORD_UPLOAD_SEC}秒 / モジュール最長 {module_max}秒")
print(f"サーバーの応答待ち {server_wait}秒\n")

check("処理の合計が RECORD_PROCESS_SEC と一致する",
      app.RECORD_PROCESS_SEC == process,
      f"{app.RECORD_PROCESS_SEC} != {process}")

check("サーバーの処理 < モジュールの送信待ち",
      process < app.RECORD_UPLOAD_SEC,
      f"{process} < {app.RECORD_UPLOAD_SEC}")

check("モジュールの最長 < サーバーの応答待ち",
      module_max < server_wait,
      f"{module_max} < {server_wait}")

# 定数が正しくても、実際に送る値がそれを使っていなければ意味がない。
# 元の不具合は upload_timeout に別式（RECORD_TOTAL_SEC - 30）を渡していたこと
sent = {}


def _fake_request_answer(device_id, cmd, text, timeout, want_payload=False, **fields):
    sent.update(dict(fields, cmd=cmd, timeout=timeout))
    return None


app._request_answer = _fake_request_answer
app.request_record("pi-test", "http://example/api/voice")

check("送信する upload_timeout が RECORD_UPLOAD_SEC",
      sent.get("upload_timeout") == app.RECORD_UPLOAD_SEC,
      f"{sent.get('upload_timeout')} != {app.RECORD_UPLOAD_SEC}")
check("送信する wait_sec / max_sec も定数どおり",
      sent.get("wait_sec") == app.RECORD_WAIT_SEC
      and sent.get("max_sec") == app.RECORD_MAX_SEC,
      f"wait={sent.get('wait_sec')} max={sent.get('max_sec')}")
check("応答待ちに RECORD_TOTAL_SEC を渡している",
      sent.get("timeout") == app.RECORD_TOTAL_SEC,
      f"{sent.get('timeout')} != {app.RECORD_TOTAL_SEC}")

print("\n" + ("すべてOK" if ok else "NG あり"))
sys.exit(0 if ok else 1)
