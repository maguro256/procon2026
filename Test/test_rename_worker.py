# -*- coding: utf-8 -*-
"""
作業者の名前の変更の確認。

  - 編集欄から名前を変えられる。担当のタスク・実績・使用中の機材は引き継がれる
  - 空にはできない（自動保存なので、打ち直すために消した瞬間にも送られてくる）
  - 名前欄の無い経路からの更新では名前を変えない
  - 使用中の機材の画面を新しい名前で出し直す

    python Test\\test_rename_worker.py
"""
import os, sys, tempfile
from pathlib import Path

sys.path.insert(0, os.getcwd())
import db

tmp = Path(tempfile.mkdtemp()) / "t.db"
db.DB_PATH = tmp
db.init_db(seed=True)   # 田中(1)はレーザー加工機 #1 でタスク1を作業中

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


sent = []
app._notify = lambda device_id, lines, led=None: sent.append((device_id, list(lines))) or 0
c = db.get_db()
c.execute("UPDATE equipment SET hostname = 'pi00', online = 1 WHERE id = 1")
c.execute("""INSERT INTO work_logs (task_id, worker_id, equipment_id, started_at, completed_at, duration_sec)
             VALUES (2, 1, 2, '2026-10-01 09:00:00', '2026-10-01 09:30:00', 1800)""")
c.commit()
c.close()

form = {"years_of_service": "12", "role": "supervisor", "permissions": ["forklift", "crane"],
        "nfc_tag_id": "TAG-0001"}

res = client.post("/workers/1/update", data=dict(form, name="田中 一郎"), follow_redirects=True)
check("名前が変わる", q("SELECT name FROM workers WHERE id = 1")[0]["name"], "田中 一郎")
check("知らせに新旧の名前", "田中 太郎 さんの名前を「田中 一郎」に変更しました" in res.get_data(as_text=True), True)
check("他の項目はそのまま", q("SELECT years_of_service, role, nfc_tag_id FROM workers WHERE id = 1")[0],
      {"years_of_service": 12.0, "role": "supervisor", "nfc_tag_id": "TAG-0001"})
check("担当のタスクは引き継ぐ", q("SELECT assigned_worker_id FROM tasks WHERE id = 1")[0]["assigned_worker_id"], 1)
check("実績は引き継ぐ", len(q("SELECT * FROM work_logs WHERE worker_id = 1")), 1)
check("使用中の機材の画面を新しい名前で出し直す",
      any(d == "pi00" and "田中 一郎 さん 使用中" in lines for d, lines in sent), True)
check("一覧に新しい名前", "田中 一郎" in client.get("/workers").get_data(as_text=True), True)
check("タッチ時の名前も新しい名前",
      client.get("/api/workers/TAG-0001/next_task").get_json()["worker"]["name"], "田中 一郎")

# 前後の空白は落とす
client.post("/workers/1/update", data=dict(form, name="  田中 一郎  "))
check("前後の空白は落とす", q("SELECT name FROM workers WHERE id = 1")[0]["name"], "田中 一郎")

# 空にはできない
res = client.post("/workers/1/update", data=dict(form, name="   "), follow_redirects=True)
check("空なら元の名前のまま", q("SELECT name FROM workers WHERE id = 1")[0]["name"], "田中 一郎")
check("空の理由を出す", "名前は空にできない" in res.get_data(as_text=True), True)

# 名前欄の無い経路（他の項目だけの更新）
client.post("/workers/1/update", data=dict(form, years_of_service="13"))
row = q("SELECT name, years_of_service FROM workers WHERE id = 1")[0]
check("名前欄が無ければ名前は変えない", (row["name"], row["years_of_service"]), ("田中 一郎", 13.0))

# 編集欄に名前の入力がある（旧UI・新UI）
for ui in ("classic", "new"):
    client.set_cookie(app.UI_COOKIE, ui)
    html = client.get("/workers").get_data(as_text=True)
    check(f"{ui}: 編集欄に名前", 'name="name" value="田中 一郎"' in html, True)

print("\n全件OK" if ok else "\n失敗あり")
sys.exit(0 if ok else 1)
