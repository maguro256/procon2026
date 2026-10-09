# -*- coding: utf-8 -*-
"""
AI割当（/tasks/<id>/auto_assign）が、作業中・完了済みのタスクを書き換えないことの確認。

割り当てると status が 'assigned' に上書きされるため、以前は
  - 作業中のタスク → 「割当済」に戻り、着手の記録と機材のロックが食い違う
  - 完了済みのタスク → 未完了に戻る
ことが起きていた。旧UIの「AI割当」ボタンは全行に出るので、サーバー側で断る。

    python Test\\test_assign_guard.py
"""
import os, sys, tempfile
from pathlib import Path

sys.path.insert(0, os.getcwd())
import db

tmp = Path(tempfile.mkdtemp()) / "t.db"
db.DB_PATH = tmp
db.init_db(seed=True)   # タスク1は田中さんが作業中、タスク2・3は未着手

import app

client = app.app.test_client()
ok = True


def check(label, got, want):
    global ok
    mark = "OK " if got == want else "NG "
    if got != want:
        ok = False
    print(f"{mark}{label}: {got!r}" + ("" if got == want else f"  期待 {want!r}"))


def task(tid):
    c = db.get_db()
    row = c.execute("SELECT status, assigned_worker_id, started_at, ai_note FROM tasks WHERE id = ?",
                    (tid,)).fetchone()
    c.close()
    return dict(row)


# 作業中
c = db.get_db()
c.execute("UPDATE tasks SET started_at = datetime('now','localtime') WHERE id = 1")
c.commit()
c.close()
before = task(1)
res = client.post("/tasks/1/auto_assign", follow_redirects=True)
check("作業中: 状態はそのまま", task(1)["status"], "in_progress")
check("作業中: 担当も着手時刻もそのまま", task(1), before)
check("作業中: 断った理由を出す", "作業中のため" in res.get_data(as_text=True), True)

# 完了済み
client.post("/api/tasks/1/complete")
before = task(1)
client.post("/tasks/1/auto_assign")
check("完了済み: 未完了に戻らない", task(1)["status"], "done")
check("完了済み: 中身もそのまま", task(1), before)

# 未着手はこれまでどおり割り当てられる
client.post("/tasks/2/auto_assign")
t2 = task(2)
check("未着手: 割当済になる", t2["status"], "assigned")
check("未着手: 担当が決まる", t2["assigned_worker_id"] is not None, True)

# 割当済の割り当て直しも、これまでどおりできる
client.post("/tasks/2/auto_assign")
check("割当済: 割り当て直せる", task(2)["status"], "assigned")

print("\nすべてOK" if ok else "\nNG があります")
sys.exit(0 if ok else 1)
