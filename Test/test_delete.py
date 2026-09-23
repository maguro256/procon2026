# -*- coding: utf-8 -*-
"""
機材・作業者・タスクの削除の検証（2026-09-15 の 500 の再発防止）。

実績(work_logs)から参照されている行を削除すると外部キー制約で落ちていた。
実績は消さずに残す設計なので、参照だけを外して削除できること、
そして**実績の件数が減らないこと**を確かめる。

    C:\\Users\\owner\\AppData\\Local\\Programs\\Python\\Python38\\python.exe Test\\test_delete.py
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


def seed():
    c = db.get_db()
    c.execute("DELETE FROM work_logs")
    c.execute("UPDATE equipment SET current_task_id = NULL, current_worker_id = NULL")
    c.execute("DELETE FROM tasks")
    c.execute("DELETE FROM equipment")
    c.execute("DELETE FROM workers")
    c.execute("INSERT INTO equipment (id,name,module_id) VALUES (1,'旋盤A','MOD-001')")
    c.execute("INSERT INTO workers (id,name,years_of_service) VALUES (1,'田中 太郎',5)")
    c.execute("INSERT INTO tasks (id,title,status,equipment_id,assigned_worker_id) "
              "VALUES (1,'製品A 組立','done',1,1)")
    c.execute("INSERT INTO work_logs (task_id,worker_id,equipment_id,duration_sec) VALUES (1,1,1,600)")
    c.commit()
    c.close()


def logs():
    c = db.get_db()
    row = c.execute("SELECT COUNT(*) n FROM work_logs").fetchone()["n"]
    c.close()
    return row


def one_log():
    c = db.get_db()
    r = c.execute("SELECT * FROM work_logs LIMIT 1").fetchone()
    c.close()
    return dict(r) if r else None


# ---------------------------------------------------------------- 機材

seed()
r = client.post("/equipment/1/delete")
check("機材を削除できる", r.status_code, 302)
check("  実績は残る", logs(), 1)
check("  機材の参照だけ外れる", one_log()["equipment_id"], None)
check("  担当者の参照は残る", one_log()["worker_id"], 1)
check("  所要時間は残る", one_log()["duration_sec"], 600)

# ---------------------------------------------------------------- 作業者

seed()
r = client.post("/workers/1/delete")
check("作業者を削除できる", r.status_code, 302)
check("  実績は残る", logs(), 1)
check("  作業者の参照だけ外れる", one_log()["worker_id"], None)
check("  機材の参照は残る", one_log()["equipment_id"], 1)

# ---------------------------------------------------------------- タスク

seed()
r = client.post("/tasks/1/delete")
check("タスクを削除できる", r.status_code, 302)
check("  実績は残る", logs(), 1)
check("  タスクの参照だけ外れる", one_log()["task_id"], None)

# ---------------------------------------------------------------- 3つとも消す

seed()
client.post("/tasks/1/delete")
client.post("/workers/1/delete")
r = client.post("/equipment/1/delete")
check("全部消しても実績は残る", (r.status_code, logs()), (302, 1))
check("  参照は全部外れる",
      (one_log()["task_id"], one_log()["worker_id"], one_log()["equipment_id"]),
      (None, None, None))

# ---------------------------------------------------------------- AIが読めること

c = db.get_db()
rows = app._ai_logs(c)
c.close()
check("参照の外れた実績もAIが読める", len(rows), 1)
check("  報酬計算に使う所要時間が残っている", rows[0]["duration_sec"], 600)

# ---------------------------------------------------------------- 実績が無い場合

seed()
c = db.get_db()
c.execute("DELETE FROM work_logs")
c.commit()
c.close()
r = client.post("/equipment/1/delete")
check("実績が無くても削除できる", r.status_code, 302)

# ---------------------------------------------------------------- 既存DBの移行

old = Path(tempfile.mkdtemp()) / "old.db"
db.DB_PATH = old
c = db.get_db()
# NOT NULL が付いていた頃のスキーマを再現する
c.executescript("""
    CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT);
    CREATE TABLE workers (id INTEGER PRIMARY KEY, name TEXT);
    CREATE TABLE equipment (id INTEGER PRIMARY KEY, name TEXT);
    CREATE TABLE work_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL REFERENCES tasks(id),
        worker_id INTEGER NOT NULL REFERENCES workers(id),
        equipment_id INTEGER REFERENCES equipment(id),
        started_at TEXT, completed_at TEXT, duration_sec INTEGER);
    INSERT INTO tasks VALUES (1,'旧タスク');
    INSERT INTO workers VALUES (1,'旧作業者');
    INSERT INTO equipment VALUES (1,'旧機材');
    INSERT INTO work_logs (task_id,worker_id,equipment_id,duration_sec) VALUES (1,1,1,900);
""")
c.commit()
db._relax_work_logs(c)
info = {r[1]: r[3] for r in c.execute("PRAGMA table_info(work_logs)")}
n = c.execute("SELECT COUNT(*) FROM work_logs").fetchone()[0]
dur = c.execute("SELECT duration_sec FROM work_logs").fetchone()[0]
check("移行後は NOT NULL が外れる", (info["task_id"], info["worker_id"]), (0, 0))
check("  実績は失われない", (n, dur), (1, 900))
db._relax_work_logs(c)      # 2回目は何もしない
check("  もう一度呼んでも壊れない", c.execute("SELECT COUNT(*) FROM work_logs").fetchone()[0], 1)
c.close()

print()
print("すべてOK" if ok else "NG あり")
sys.exit(0 if ok else 1)
