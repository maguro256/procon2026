# -*- coding: utf-8 -*-
"""
タッチ後のメニュー（タスク実行 / タスク登録 / 作業終了）と、音声でのタスク登録の検証。
DBは一時ファイル、MQTTと文字起こしは差し替えて実機なしで回す。

    C:\\Users\\owner\\AppData\\Local\\Programs\\Python\\Python38\\python.exe Test\\test_menu.py
"""
import os, sys, tempfile
from pathlib import Path

sys.path.insert(0, os.getcwd())
import db

tmp = Path(tempfile.mkdtemp()) / "t.db"
db.DB_PATH = tmp
conn = db.get_db()
conn.executescript(db.SCHEMA)
conn.commit()
conn.close()

import app

client = app.app.test_client()
ok = True


def check(label, got, want):
    global ok
    mark = "OK " if got == want else "NG "
    if got != want:
        ok = False
    print(f"{mark}{label}: {got!r}" + ("" if got == want else f"  期待 {want!r}"))


WORKER = {"id": 1, "name": "田中 太郎"}
IDLE_EQ = {"id": 1, "name": "旋盤A", "status": "idle", "current_worker_id": None, "ip": "192.168.0.9"}
BUSY_EQ = {"id": 1, "name": "旋盤A", "status": "working", "current_worker_id": 1, "ip": "192.168.0.9"}

calls = []
menu_seen = []


def _fake_choice(device_id, text, options, timeout=None, **kw):
    """メニューに出た内容を記録して、用意した答えを順に返す"""
    menu_seen.append({"text": text, "options": list(options), "default": kw.get("default")})
    return _answers.pop(0) if _answers else None


def run_menu(answers, equipment):
    """メニューを1回通す。answers は選ぶ添字の並び"""
    global _answers
    _answers = list(answers)
    calls.clear()
    menu_seen.clear()
    app._show_menu("pi01", "MOD-A-01", "TAG-1", WORKER, dict(equipment), [])


app.request_choice = _fake_choice
app._notify = lambda *a, **k: None
app._notify_briefly = lambda *a, **k: None
app._notify_pause = lambda *a, **k: calls.append(("pause", a[1][0]))
app._start_session = lambda *a, **k: calls.append(("start", None))
app._end_session = lambda *a, **k: calls.append(("end", None))
app._register_by_voice = lambda *a, **k: calls.append(("voice", None))

# ---------------------------------------------------------------- メニューの中身

run_menu([0], IDLE_EQ)
check("メニューの項目", menu_seen[0]["options"], ["タスク実行", "タスク登録", "作業終了"])
check("空きのときの初期位置", menu_seen[0]["default"], 0)

run_menu([2], BUSY_EQ)
check("作業中のときの初期位置", menu_seen[0]["default"], 2)

# ---------------------------------------------------------------- 分岐

run_menu([0], IDLE_EQ)
check("空き + タスク実行 → 着手", calls, [("start", None)])

run_menu([1], IDLE_EQ)
check("タスク登録 → 音声へ", calls, [("voice", None)])

run_menu([2], BUSY_EQ)
check("作業中 + 作業終了 → 終了", calls, [("end", None)])

run_menu([1], BUSY_EQ)
check("作業中でもタスク登録はできる", calls, [("voice", None)])

# ---------------------------------------------------------------- 選べない項目

run_menu([0, 2], BUSY_EQ)
check("作業中にタスク実行 → 戻して終了を選び直せる",
      calls, [("pause", "すでに作業中です"), ("end", None)])
check("  メニューを出し直している", len(menu_seen), 2)

run_menu([2, 0], IDLE_EQ)
check("空きで作業終了 → 戻して実行を選び直せる",
      calls, [("pause", "作業中のタスクがありません"), ("start", None)])

# ---------------------------------------------------------------- 無応答

run_menu([], IDLE_EQ)
check("無応答なら何もしない", calls, [])

# ---------------------------------------------------------------- タッチしただけでは終了しない

ended = []
app._show_menu = lambda *a, **k: ended.append("menu")
app._end_session = lambda *a, **k: ended.append("end")


class _Resp:
    def __init__(self, code, body):
        self.status_code = code
        self._body = body

    def json(self):
        return self._body


def _fake_get(url, timeout=None):
    if "next_task" in url:
        return _Resp(200, {"worker": WORKER, "tasks": []})
    return _Resp(200, dict(BUSY_EQ))


app.requests.get = _fake_get
ended.clear()
app._handle_touch("pi01", "MOD-A-01", "TAG-1")
check("作業中に本人がタッチしてもメニュー（自動終了しない）", ended, ["menu"])

# 他人が使用中なら、メニューを出す前に断る
OTHER = {"id": 9, "name": "佐藤 花子"}


def _fake_get_other(url, timeout=None):
    if "next_task" in url:
        return _Resp(200, {"worker": OTHER, "tasks": []})
    return _Resp(200, dict(BUSY_EQ))


app.requests.get = _fake_get_other
ended.clear()
app._handle_touch("pi01", "MOD-A-01", "TAG-9")
check("他人が使用中ならメニューも出さない", ended, [])

# ---------------------------------------------------------------- 音声でのタスク登録

# 意図分析(E-2)は fallback に固定する。Ollama が動いていると本物の Gemma が走り、
# 題名を要約して優先度も推定するため、この節の期待値（1文目がタスク名・既定値）が
# **環境によって揺れる**。ここで見たいのは /api/voice の配管なので、LLM は外す。
# 意図分析そのものの検証は Test/test_e2_intent.py が持っている。
app.intent.analyze = lambda text, today=None: app.intent.fallback(text)

app._transcribe = lambda path: "旋盤の切粉清掃を至急お願いします。数量は30個です"

c = db.get_db()
c.execute("INSERT INTO workers (id,name,years_of_service,nfc_tag_id) VALUES (1,'田中 太郎',5,'TAG-1')")
c.commit()
c.close()

res = client.post("/api/voice?tag_id=TAG-1&module_id=MOD-A-01", data=b"x" * 2000,
                  content_type="audio/wav")
body = res.get_json()
check("音声からタスクを作る", body.get("ok"), True)
check("  1文目がタスク名", body.get("text"), "旋盤の切粉清掃を至急お願いします")

c = db.get_db()
t = c.execute("SELECT * FROM tasks WHERE id = ?", (body["task_id"],)).fetchone()
c.close()
check("  全文を補足に残す", t["description"], "旋盤の切粉清掃を至急お願いします。数量は30個です")
check("  既定の優先度", t["priority"], "normal")
check("  未着手で入る", t["status"], "todo")

res = client.post("/api/voice?tag_id=TAG-1", data=b"x" * 10, content_type="audio/wav")
check("短すぎる音声は弾く", res.status_code, 400)

app._transcribe = lambda path: ""
res = client.post("/api/voice?tag_id=TAG-1", data=b"x" * 2000, content_type="audio/wav")
check("聞き取れなければ登録しない", res.get_json().get("ok"), False)

# 長い読み上げでもタスク名が伸びきらない
app._transcribe = lambda path: "あ" * 100
res = client.post("/api/voice?tag_id=TAG-1", data=b"x" * 2000, content_type="audio/wav")
check("長すぎるタスク名は詰める", len(res.get_json()["text"]), app.intent.TITLE_MAX + 1)

print()
print("すべてOK" if ok else "NG あり")
sys.exit(0 if ok else 1)
