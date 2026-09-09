# -*- coding: utf-8 -*-
"""
D-2（役職・権限モデル）の検証。DBは一時ファイル、実機もMQTTも要らない。

確認するのは3層:
  1. permissions.py 単体の判定（役職由来 ∪ 個別付与、必要権限は全部必要）
  2. api_next_task が権限で候補を落とすこと（= モジュールの提示と C-3 の誘導に効く）
  3. api_start_task / 手動割り当て / AI割当 が無資格を通さないこと
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
import permissions as perms

app.app.config["TESTING"] = True
client = app.app.test_client()

conn = db.get_db()
conn.execute("INSERT INTO equipment (id,name,module_id,hostname,status,online) VALUES (1,'旋盤A','MOD-A-01','pi01','idle',1)")
# 田中: 作業主任者（inspection/press が役職で付く）＋ 個別に forklift
conn.execute("INSERT INTO workers (id,name,years_of_service,role,permissions,nfc_tag_id)"
             " VALUES (1,'田中',12,'supervisor','forklift','TAG-1')")
# 鈴木: 一般作業者、権限なし
conn.execute("INSERT INTO workers (id,name,years_of_service,role,permissions,nfc_tag_id)"
             " VALUES (2,'鈴木',1,'member','','TAG-2')")
conn.executemany(
    "INSERT INTO tasks (id,title,difficulty,priority,required_permissions,status) VALUES (?,?,?,?,?,'todo')",
    [(1, "誰でもできる作業", 2, "normal", ""),
     (2, "フォーク搬入", 1, "high", "forklift"),
     (3, "プレス加工", 4, "high", "press"),
     (4, "溶接して検査", 3, "normal", "welding,inspection")])
conn.commit()
conn.close()

ok = True
def check(label, got, want):
    global ok
    if got != want:
        ok = False
        print(f"NG {label}: {got!r}  期待 {want!r}")
    else:
        print(f"OK {label}: {got!r}")

def titles(tag):
    res = client.get(f"/api/workers/{tag}/next_task")
    return [t["title"] for t in res.get_json()["tasks"]]

# --- 1. permissions.py 単体 -------------------------------------------------
tanaka = {"role": "supervisor", "permissions": "forklift"}
suzuki = {"role": "member", "permissions": ""}
check("役職由来∪個別付与", sorted(perms.held(tanaka)), ["forklift", "inspection", "press"])
check("権限なしの人", perms.held(suzuki), [])
check("必要権限なしは誰でも可", perms.allows(suzuki, {"required_permissions": ""}), True)
check("複数必要は全部揃って初めて可",
      perms.missing(tanaka, {"required_permissions": "welding,inspection"}), ["welding"])
check("列が無い行でも落ちない", perms.allows({"name": "旧データ"}, {"title": "旧タスク"}), True)

# --- 2. 候補の絞り込み（モジュールへの提示と C-3 の誘導に効く） --------------
check("有資格者に見えるタスク", sorted(titles("TAG-1")),
      sorted(["誰でもできる作業", "フォーク搬入", "プレス加工"]))
check("無資格者に見えるタスク", titles("TAG-2"), ["誰でもできる作業"])

# --- 3. 着手・割り当ての拒否 ------------------------------------------------
res = client.post("/api/tasks/2/start", json={"nfc_tag_id": "TAG-2", "module_id": "MOD-A-01"})
check("無資格の着手は403", res.status_code, 403)
check("不足権限を返す", res.get_json()["missing"], ["forklift"])
check("機材はロックされない",
      db.get_db().execute("SELECT status FROM equipment WHERE id=1").fetchone()["status"], "idle")

res = client.post("/api/tasks/2/start", json={"nfc_tag_id": "TAG-1", "module_id": "MOD-A-01"})
check("有資格の着手は通る", res.status_code, 200)

# 手動割り当て: 無資格の人は弾く
client.post("/tasks/3/update", data={"status": "todo", "assigned_worker_id": "2",
                                     "req_perm_form": "1", "required_permissions": "press"})
check("無資格への手動割り当ては拒否",
      db.get_db().execute("SELECT assigned_worker_id FROM tasks WHERE id=3").fetchone()[0], None)

# 必要権限を外すのと同時なら通す（フォームの値で判定しているか）
client.post("/tasks/3/update", data={"status": "todo", "assigned_worker_id": "2",
                                     "req_perm_form": "1"})
row = db.get_db().execute("SELECT assigned_worker_id, required_permissions FROM tasks WHERE id=3").fetchone()
check("必要権限を外せば割り当てできる", (row[0], row[1]), (2, ""))

# AI割当: 必要権限を満たす人しか候補にしない
client.post("/tasks/4/update", data={"status": "todo", "req_perm_form": "1",
                                     "required_permissions": "welding,inspection"})
client.post("/tasks/4/auto_assign")
check("誰も権限を満たさなければ割り当てない",
      db.get_db().execute("SELECT assigned_worker_id FROM tasks WHERE id=4").fetchone()[0], None)

conn = db.get_db()
conn.execute("UPDATE workers SET permissions='forklift,welding' WHERE id=1")
conn.commit(); conn.close()
client.post("/tasks/4/auto_assign")
check("満たす人がいればその人が選ばれる",
      db.get_db().execute("SELECT assigned_worker_id FROM tasks WHERE id=4").fetchone()[0], 1)

print("\n" + ("すべて期待通り" if ok else "失敗あり"))
sys.exit(0 if ok else 1)
