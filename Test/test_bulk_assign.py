# -*- coding: utf-8 -*-
"""
AI一括割当（/tasks/auto_assign_all）の確認。

  - 担当者の決まっていない未着手のタスクだけを、まとめて割り当てる
  - 担当者を指定したタスク・作業中・完了済みは変えない
  - 権限を満たす人にだけ割り当て、誰もいなければ未割当のまま理由を出す
  - 先に決めた分を手持ちに数えるので、1人に集中しない
  - 1件ずつの AI割当と同じく、選んだ理由（ai_note）を残す

    python Test\\test_bulk_assign.py
"""
import os, sys, tempfile, json
from pathlib import Path

sys.path.insert(0, os.getcwd())
import db

tmp = Path(tempfile.mkdtemp()) / "t.db"
db.DB_PATH = tmp
db.init_db(seed=True)   # 田中(1, forklift/crane) / 佐藤(2, welding) / 鈴木(3)。タスク1は田中さんが作業中

import app

client = app.app.test_client()
ok = True


def check(label, got, want):
    global ok
    mark = "OK " if got == want else "NG "
    if got != want:
        ok = False
    print(f"{mark}{label}: {got!r}" + ("" if got == want else f"  期待 {want!r}"))


def q(sql, *params):
    c = db.get_db()
    rows = [dict(r) for r in c.execute(sql, params).fetchall()]
    c.close()
    return rows


def task(title):
    return q("SELECT * FROM tasks WHERE title = ?", title)[0]


# 担当者のいないタスクを 12 件（難易度・数量は同じ）＋ 指定済み・完了済み・誰も持たない権限
c = db.get_db()
for i in range(12):
    c.execute("INSERT INTO tasks (title, difficulty, quantity, priority) VALUES (?, 3, 3, 'normal')", (f"検品{i}",))
c.execute("""INSERT INTO tasks (title, assigned_worker_id, status, designated)
             VALUES ('指定済み', 3, 'assigned', 1)""")
c.execute("INSERT INTO tasks (title, status, completed_at) VALUES ('完了済み', 'done', '2026-10-01 10:00:00')")
c.execute("INSERT INTO tasks (title, required_permissions) VALUES ('誰も持たない権限', 'high_voltage')")
c.commit()
c.close()
before_running = task("製品A 組立")

res = client.post("/tasks/auto_assign_all", follow_redirects=True)
html = res.get_data(as_text=True)

unassigned = q("SELECT title FROM tasks WHERE assigned_worker_id IS NULL AND status != 'done'")
check("権限を満たす人がいないもの以外は全部決まる", [r["title"] for r in unassigned], ["誰も持たない権限"])
check("未割当の理由を出す", "必要な権限を持つ作業者がいないため、1 件は未割当のまま" in html, True)
check("まとめて割り当てた件数を出す", "AIが 14 件をまとめて割り当てました" in html, True)

# 権限
check("溶接のタスクは溶接の権限を持つ佐藤さん", task("製品B 加工")["assigned_worker_id"], 2)
# 変えないもの
check("指定済みは変えない", (task("指定済み")["assigned_worker_id"], task("指定済み")["designated"]), (3, 1))
check("完了済みは変えない", (task("完了済み")["status"], task("完了済み")["assigned_worker_id"]), ("done", None))
run = task("製品A 組立")
check("作業中は変えない", (run["status"], run["assigned_worker_id"], run["ai_note"]),
      (before_running["status"], before_running["assigned_worker_id"], before_running["ai_note"]))

# 割り当てたものは割当済・指定ではない・理由が残る
t = task("検品0")
check("割当済になる", t["status"], "assigned")
check("指定にはならない（AIの割当結果）", t["designated"], 0)
note = json.loads(t["ai_note"])
check("選んだ理由が残る", note["chosen"], t["assigned_worker_id"])

# 1人に集中しない（同じ重さのタスク12件が3人に散る）
counts = {}
for r in q("SELECT assigned_worker_id FROM tasks WHERE title LIKE '検品%'"):
    counts[r["assigned_worker_id"]] = counts.get(r["assigned_worker_id"], 0) + 1
check("3人全員に配られる", len(counts), 3)
check("1人に偏らない（最多でも8件以下）", max(counts.values()) <= 8, True)

# もう一度押しても、決まっているものは変えない
snapshot = q("SELECT id, assigned_worker_id FROM tasks ORDER BY id")
res = client.post("/tasks/auto_assign_all", follow_redirects=True)
check("2回目: 決まっているものは変えない", q("SELECT id, assigned_worker_id FROM tasks ORDER BY id"), snapshot)

# 対象が無いとき
c = db.get_db()
c.execute("DELETE FROM tasks WHERE title = '誰も持たない権限'")
c.commit()
c.close()
res = client.post("/tasks/auto_assign_all", follow_redirects=True)
check("対象が無ければそう伝える", "担当者の決まっていない未着手のタスクはありません" in res.get_data(as_text=True), True)

# 1件ずつの AI割当も今までどおり動く
c = db.get_db()
c.execute("INSERT INTO tasks (title) VALUES ('単発')")
c.commit()
c.close()
client.post(f"/tasks/{task('単発')['id']}/auto_assign")
check("1件ずつの AI割当", task("単発")["status"], "assigned")

# ボタン（旧UI・新UI）。未割当があるときだけ出す
c = db.get_db()
c.execute("INSERT INTO tasks (title) VALUES ('未割当の残り')")
c.commit()
c.close()
for ui in ("classic", "new"):
    client.set_cookie(app.UI_COOKIE, ui)
    html = client.get("/tasks").get_data(as_text=True)
    check(f"{ui}: 一括割当のボタン", "AIで一括割当" in html and "auto_assign_all" in html, True)

print("\n全件OK" if ok else "\n失敗あり")
sys.exit(0 if ok else 1)
