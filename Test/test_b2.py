# -*- coding: utf-8 -*-
"""
B-2（3ボタン）と難易度フィードバックの検証。DBは一時ファイル、MQTTは差し替えて
実機なしで回す。

    C:\\Users\\owner\\AppData\\Local\\Programs\\Python\\Python38\\python.exe Test\\test_b2.py
"""
import os, sys, tempfile, threading
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


def seed():
    """作業中のタスクを1件仕込む。完了APIが work_logs を作れる状態にする"""
    c = db.get_db()
    c.execute("DELETE FROM work_logs")
    c.execute("UPDATE equipment SET current_task_id = NULL")
    c.execute("DELETE FROM tasks")
    c.execute("DELETE FROM equipment")
    c.execute("DELETE FROM workers")
    c.execute("INSERT INTO workers (id,name,years_of_service,nfc_tag_id) VALUES (1,'田中 太郎',5,'TAG-1')")
    c.execute("INSERT INTO equipment (id,name,module_id,hostname,status,online) "
              "VALUES (1,'旋盤A','MOD-A-01','pi01','working',1)")
    c.execute("INSERT INTO tasks (id,title,difficulty,priority,status,assigned_worker_id,"
              "equipment_id,started_at) VALUES "
              "(1,'製品A 組立',3,'normal','in_progress',1,1,datetime('now','localtime','-10 minutes'))")
    c.commit()
    c.close()


# ---------------------------------------------------------------- 完了API

seed()
res = client.post("/api/tasks/1/complete")
body = res.get_json()
check("完了APIが work_log_id を返す", isinstance(body.get("work_log_id"), int), True)
check("完了APIがタスク名を返す", body.get("title"), "製品A 組立")
log_id = body["work_log_id"]

c = db.get_db()
row = c.execute("SELECT * FROM work_logs WHERE id = ?", (log_id,)).fetchone()
c.close()
check("実績が1件できる", row is not None, True)
check("答える前は未記入", row["felt_difficulty"], None)

# ---------------------------------------------------------------- フィードバックAPI

res = client.post(f"/api/work_logs/{log_id}/feedback", json={"felt_difficulty": "hard"})
check("フィードバックを受け付ける", res.status_code, 200)

c = db.get_db()
row = c.execute("SELECT felt_difficulty FROM work_logs WHERE id = ?", (log_id,)).fetchone()
c.close()
check("実績に書かれる", row["felt_difficulty"], "hard")

res = client.post(f"/api/work_logs/{log_id}/feedback", json={"felt_difficulty": "とても難しい"})
check("知らない値は弾く", res.status_code, 400)

res = client.post("/api/work_logs/9999/feedback", json={"felt_difficulty": "easy"})
check("無い実績は404", res.status_code, 404)

# tasks.difficulty を書き換えていないこと。書き換えると ai_stub が work_logs を
# 再生するときに過去のログの文脈まで変わってしまう（TODO.md の D-1）。
c = db.get_db()
row = c.execute("SELECT difficulty FROM tasks WHERE id = 1").fetchone()
c.close()
check("tasks.difficulty は変えない", row["difficulty"], 3)

# ---------------------------------------------------------------- 下り通信の組み立て
# request_choice / request_confirm を実際に通す。ここを差し替えたままにすると、
# 引数の取り違えのような組み立て側の誤りが素通りしてしまう（実際に一度見落とした）。

sent = []


def _fake_send_cmd(device_id, cmd, **fields):
    sent.append((device_id, cmd, fields))
    # モジュールの代わりに即答する。answer は choice=添字 / confirm=真偽
    rid = fields["request_id"]
    answer = 2 if cmd == "choice" else True
    threading.Thread(target=app._handle_reply,
                     args=(device_id, {"request_id": rid, "answer": answer}),
                     daemon=True).start()
    return True


_real_send_cmd = app.send_cmd
app.send_cmd = _fake_send_cmd

got = app.request_choice("pi01", "この作業の難易度は？", ["簡単", "普通", "難しい"],
                         timeout=2, lines=["製品A 組立"], default=1)
check("request_choice が添字を返す", got, 2)
check("choice コマンドを送る", sent[-1][1], "choice")
check("選択肢を載せる", sent[-1][2]["options"], ["簡単", "普通", "難しい"])
check("既定の選択位置を載せる", sent[-1][2]["default"], 1)

got = app.request_confirm("pi01", "着手しますか？", timeout=2,
                          lines=["製品A 組立"], badge="1/3")
check("request_confirm は真偽を返す", got, True)
check("confirm コマンドを送る", sent[-1][1], "confirm")
check("バッジを載せる", sent[-1][2]["badge"], "1/3")
check("Yes/No には選択肢を載せない", "options" in sent[-1][2], False)

app.send_cmd = _real_send_cmd

# ---------------------------------------------------------------- 完了後の問いかけ

posted = []


class _FakeResponse:
    ok = True

    def json(self):
        return {"ok": True}


def _fake_post(url, json=None, timeout=None):
    posted.append((url, json))
    return _FakeResponse()


app.requests.post = _fake_post
EQ = {"id": 1, "name": "旋盤A"}
WORKER = {"id": 1, "name": "田中 太郎"}

app.request_choice = lambda *a, **k: 2          # 「難しい」を選ぶ
posted.clear()
app._ask_felt_difficulty("pi01", "MOD-A-01", WORKER, EQ, log_id, "製品A 組立")
check("選んだ値を送る", posted, [(f"{app.SELF_URL}/api/work_logs/{log_id}/feedback",
                                  {"felt_difficulty": "hard"})])

app.request_choice = lambda *a, **k: 0          # 「簡単」
posted.clear()
app._ask_felt_difficulty("pi01", "MOD-A-01", WORKER, EQ, log_id, "製品A 組立")
check("添字と値の対応", posted[0][1], {"felt_difficulty": "easy"})

app.request_choice = lambda *a, **k: None       # 無応答
posted.clear()
app._ask_felt_difficulty("pi01", "MOD-A-01", WORKER, EQ, log_id, "製品A 組立")
check("無応答なら何も書かない", posted, [])

# ---------------------------------------------------------------- 管理画面からの代替応答

def _pending(options):
    """request_choice / request_confirm が作るのと同じ待ち受けを1件置く"""
    rid = "test1234"
    slot = {"event": threading.Event(), "answer": None, "device_id": "pi01",
            "text": "この作業の難易度は？", "lines": [], "options": options,
            "equipment_name": "旋盤A", "worker_name": "田中 太郎"}
    with app._replies_lock:
        app._pending_replies[rid] = slot
    return rid, slot


rid, slot = _pending(["簡単", "普通", "難しい"])
res = client.get("/api/pending")
confirms = res.get_json()["confirms"]
check("待ち受けに選択肢が出る", confirms[0]["options"], ["簡単", "普通", "難しい"])

res = client.post(f"/api/confirm/{rid}", json={"index": 1})
check("管理画面から選べる", (res.status_code, slot["answer"]), (200, 1))

rid, slot = _pending(["簡単", "普通", "難しい"])
res = client.post(f"/api/confirm/{rid}", json={"index": 5})
check("範囲外の添字は弾く", res.status_code, 400)
res = client.post(f"/api/confirm/{rid}", json={"answer": True})
check("選択肢に Yes/No で答えたら弾く", res.status_code, 400)

rid, slot = _pending(None)          # options が無ければ従来の Yes/No
res = client.post(f"/api/confirm/{rid}", json={"answer": True})
check("Yes/No は従来どおり", (res.status_code, slot["answer"]), (200, True))

with app._replies_lock:
    app._pending_replies.clear()

print()
print("すべてOK" if ok else "NG あり")
sys.exit(0 if ok else 1)
