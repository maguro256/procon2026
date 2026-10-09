# -*- coding: utf-8 -*-
"""
担当者の指定（D-3）の確認。導入先の評価「タスクをやる人を指定できないのが残念」への対応。

  - 登録時に担当者を指定できる。指定したタスクは本人にだけ提示される
  - 指定したタスクは AI割当で上書きされない（指定を外せば AI で割り当てられる）
  - 本人以外は着手できない（API を直接叩いても 403）
  - AI割当で決まった担当者は「指定」ではない（他の項目を編集しても指定にならない）

    python Test\\test_designated.py
"""
import os, sys, tempfile
from pathlib import Path

sys.path.insert(0, os.getcwd())
import db

tmp = Path(tempfile.mkdtemp()) / "t.db"
db.DB_PATH = tmp
db.init_db(seed=True)   # 田中(1, TAG-0001) / 佐藤(2, TAG-0002) / 鈴木(3, タグ無し)

import app

client = app.app.test_client()
ok = True


def check(label, got, want):
    global ok
    mark = "OK " if got == want else "NG "
    if got != want:
        ok = False
    print(f"{mark}{label}: {got!r}" + ("" if got == want else f"  期待 {want!r}"))


def task_by_title(title):
    c = db.get_db()
    row = c.execute("SELECT * FROM tasks WHERE title = ?", (title,)).fetchone()
    c.close()
    return dict(row) if row else None


def next_ids(tag):
    body = client.get(f"/api/workers/{tag}/next_task").get_json()
    return [t["id"] for t in body["tasks"] + body["elsewhere"]], body


# --- 登録時に指定
client.post("/tasks/add", data={"title": "金型の段取り", "assigned_worker_id": "2"})
t = task_by_title("金型の段取り")
check("登録: 担当者が入る", t["assigned_worker_id"], 2)
check("登録: 指定の印が付く", t["designated"], 1)
check("登録: 割当済になる", t["status"], "assigned")

ids, body = next_ids("TAG-0002")
check("本人には提示される", t["id"] in ids, True)
row = next(x for x in body["tasks"] if x["id"] == t["id"])
check("提示に指定の印が付く", row["designated"], 1)
check("モジュールの画面に「あなたが担当」", app._task_lines(row)[1].startswith("あなたが担当"), True)
ids, _ = next_ids("TAG-0001")
check("本人以外には提示されない", t["id"] in ids, False)

# 指定しないで登録したものは従来どおり
client.post("/tasks/add", data={"title": "誰でもよい清掃"})
free = task_by_title("誰でもよい清掃")
check("指定なし: 未着手・指定なし", (free["status"], free["designated"], free["assigned_worker_id"]),
      ("todo", 0, None))

# 権限の足りない人は指定できない
client.post("/tasks/add", data={"title": "溶接の仕上げ", "assigned_worker_id": "3",
                                "required_permissions": "welding"})
check("権限不足の人は指定できない", task_by_title("溶接の仕上げ"), None)

# --- AI割当で上書きされない
res = client.post(f"/tasks/{t['id']}/auto_assign", follow_redirects=True)
check("AI割当: 断った理由を出す", "担当者が指定されている" in res.get_data(as_text=True), True)
check("AI割当: 担当者はそのまま", task_by_title("金型の段取り")["assigned_worker_id"], 2)

# --- 本人以外は着手できない
res = client.post(f"/api/tasks/{t['id']}/start", json={"nfc_tag_id": "TAG-0001", "module_id": "MOD-A-02"})
check("本人以外の着手は 403", res.status_code, 403)
check("着手されていない", task_by_title("金型の段取り")["status"], "assigned")
res = client.post(f"/api/tasks/{t['id']}/start", json={"nfc_tag_id": "TAG-0002", "module_id": "MOD-A-02"})
check("本人は着手できる", res.status_code, 200)
client.post(f"/api/tasks/{t['id']}/complete")

# --- 編集で指定を外す → AI で割り当てられる → それは指定ではない
client.post("/tasks/add", data={"title": "外注品の受入", "assigned_worker_id": "1"})
t2 = task_by_title("外注品の受入")
client.post(f"/tasks/{t2['id']}/update", data={"assigned_worker_id": "", "priority": "normal",
                                                "quantity": "1", "req_perm_form": "1"})
t2 = task_by_title("外注品の受入")
check("指定を外す: 指定の印も外れる", (t2["assigned_worker_id"], t2["designated"], t2["status"]),
      (None, 0, "todo"))
client.post(f"/tasks/{t2['id']}/auto_assign")
t2 = task_by_title("外注品の受入")
check("AI割当: 担当者が入る", t2["assigned_worker_id"] is not None, True)
check("AI割当: 指定ではない", t2["designated"], 0)
# 担当者を変えずに優先度だけ変えても、指定にはならない
client.post(f"/tasks/{t2['id']}/update", data={"assigned_worker_id": str(t2["assigned_worker_id"]),
                                                "priority": "high", "quantity": "1", "req_perm_form": "1"})
check("優先度だけ変更: 指定にならない", task_by_title("外注品の受入")["designated"], 0)
# 手で担当者を選び直すと指定になる
other = 1 if t2["assigned_worker_id"] != 1 else 2
client.post(f"/tasks/{t2['id']}/update", data={"assigned_worker_id": str(other), "priority": "high",
                                                "quantity": "1", "req_perm_form": "1"})
check("手で選び直す: 指定になる", task_by_title("外注品の受入")["designated"], 1)

# --- 作業者を消すと指定も外れる
client.post("/tasks/add", data={"title": "鈴木さんの点検", "assigned_worker_id": "3"})
client.post("/workers/3/delete")
t3 = task_by_title("鈴木さんの点検")
check("作業者削除: 指定が外れて未着手へ", (t3["assigned_worker_id"], t3["designated"], t3["status"]),
      (None, 0, "todo"))

# --- 画面に「指定」が出る（旧UI・新UI）
for ui in ("classic", "new"):
    client.set_cookie(app.UI_COOKIE, ui)
    html = client.get("/tasks").get_data(as_text=True)
    check(f"{ui}: 一覧に「指定」", ">指定</span>" in html, True)
    check(f"{ui}: 登録欄に担当者の指定", "担当者を指定（任意）" in html, True)

print("\n全件OK" if ok else "\n失敗あり")
sys.exit(0 if ok else 1)
