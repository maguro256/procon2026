# -*- coding: utf-8 -*-
"""
タッチ後に提示するタスクの並び順（api_next_task）の検証。DBは一時ファイル。

    python Test/test_next_task_order.py

確認すること:
  1. 作業中 → 自分に割り当て済み → 割り当てなし の順
  2. 各組の中は優先度順（至急が先）
  3. 他人に割り当て済みのタスクは出ない
  4. ?module_id= を渡すと、他機材のタスクに押し出されずこの機材の分が出る。
     他機材の分は elsewhere に分かれる（誘導用）
"""
import os, sys, tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
db.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
conn = db.get_db()
conn.executescript(db.SCHEMA)
conn.commit()
conn.close()

import app

client = app.app.test_client()

conn = db.get_db()
conn.executemany("INSERT INTO equipment (id,name,module_id,status) VALUES (?,?,?,'idle')",
                 [(1, "旋盤A", "MOD-A"), (2, "プレスB", "MOD-B")])
conn.executemany("INSERT INTO workers (id,name,years_of_service,nfc_tag_id) VALUES (?,?,?,?)",
                 [(1, "田中", 10, "TAG-1"), (2, "鈴木", 1, "TAG-2")])
# (id, title, priority, status, assigned_worker_id, equipment_id)
conn.executemany(
    """INSERT INTO tasks (id,title,priority,status,assigned_worker_id,equipment_id)
       VALUES (?,?,?,?,?,?)""",
    [(1, "未割当・通常", "normal", "todo", None, None),
     (2, "未割当・至急", "urgent", "todo", None, None),
     (3, "田中に割当・低", "low", "assigned", 1, None),
     (4, "田中に割当・高", "high", "assigned", 1, None),
     (5, "鈴木に割当", "urgent", "assigned", 2, None),
     (6, "田中が作業中", "normal", "in_progress", 1, 1)]
    # プレスBのタスクを大量に。以前は上位5件がこれで埋まり、旋盤Aの分が消えた
    + [(10 + i, f"プレスB-{i}", "urgent", "todo", None, 2) for i in range(6)])
conn.commit()
conn.close()

ok = True


def check(label, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"{'OK' if cond else 'NG'} {label} {detail}")


# 機材を指定しない（API単体で叩いた場合）: 全機材の分が1本の並びになる
titles = [t["title"] for t in client.get("/api/workers/TAG-1/next_task").get_json()["tasks"]]
check("作業中が先頭", titles[0] == "田中が作業中", titles)
check("他人に割り当て済みは出ない", "鈴木に割当" not in titles)

# 旋盤A でタッチ
body = client.get("/api/workers/TAG-1/next_task?module_id=MOD-A").get_json()
titles = [t["title"] for t in body["tasks"]]
check("旋盤Aの並び: 作業中 → 割当済み(高→低) → 未割当(至急→通常)",
      titles == ["田中が作業中", "田中に割当・高", "田中に割当・低", "未割当・至急", "未割当・通常"],
      titles)
check("他機材のタスクに押し出されない", all(not t.startswith("プレスB") for t in titles))
check("他機材の分は elsewhere に入る（誘導用）",
      [t["title"] for t in body["elsewhere"]] and
      all(t["title"].startswith("プレスB") for t in body["elsewhere"]),
      [t["title"] for t in body["elsewhere"]])

# 割当済みは、優先度が低くても未割当の至急より先
check("割当済み(低)が未割当(至急)より先",
      titles.index("田中に割当・低") < titles.index("未割当・至急"))

print("\nすべて期待通り" if ok else "\n失敗あり")
sys.exit(0 if ok else 1)
