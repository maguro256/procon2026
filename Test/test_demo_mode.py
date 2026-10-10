# -*- coding: utf-8 -*-
"""
デモモード（サイドバーの「開始する」「解除する」）の確認。

  1. 開始するとデモ用DBに切り替わり、作業者は宮田・南川、機材は3Dプリンタ・旋盤、
     タスクは機材ごとに至急と通常になる
  2. 社員証とモジュールの紐付けは本番DBの同じ人・機材から引き継ぐ
  3. タッチすると至急のタスクが先に出る
  4. 本番DBには触らない（以前のデモモードの残りだけは片付ける）
  5. 解除すると本番DBに戻り、デモ中の作業はデモ用DBと一緒に消える。次の開始は作り直し
  6. 二重に開始・解除しない。app.py を再起動してもデモモードのまま（resume）
  7. 「初期化」でデモモードのまま開始直後の状態に戻る。本番DBには触らない

    python Test\\test_demo_mode.py
"""
import os, sys, tempfile
from pathlib import Path

sys.path.insert(0, os.getcwd())
import db

tmp = Path(tempfile.mkdtemp()) / "t.db"
db.DB_PATH = tmp
db.init_db(seed=False)
c = db.get_db()
c.executescript("""
    INSERT INTO workers (name, years_of_service, role, permissions, nfc_tag_id) VALUES
        ('宮田さん', 5, 'supervisor', '', 'TAG-MIYATA'),
        ('南川健志郎', 30, 'manager', 'press', 'TAG-MINAMI'),
        ('山口さん', 0.5, 'member', '', 'TAG-YAMA');
    INSERT INTO equipment (name, module_id, status, hostname, online, ip) VALUES
        ('3Dプリンタ', 'MOD-002', 'idle', 'esp-0e5310', 1, '192.168.137.59'),
        ('試験台', 'MOD-003', 'idle', 'esp-af4828', 0, NULL),
        ('旋盤', 'MOD-006', 'idle', 'pi01', 1, '192.168.137.51');
    INSERT INTO tasks (title, priority, equipment_id) VALUES ('本物のタスク', 'normal', 3);
    INSERT INTO tasks (title, priority, equipment_id, demo) VALUES ('以前のデモのタスク', 'high', 3, 1);
""")
c.commit()
c.close()

import app
import demo_mode as demo

client = app.app.test_client()
ok = True


def check(label, got, want):
    global ok
    mark = "OK " if got == want else "NG "
    if got != want:
        ok = False
    print(f"{mark}{label}: {got!r}" + ("" if got == want else f"  期待 {want!r}"))


def snapshot(path):
    c = db.get_db(path)
    snap = {t: [tuple(r) for r in c.execute(f"SELECT * FROM {t} ORDER BY id")]
            for t in ("workers", "equipment", "tasks", "work_logs")}
    c.close()
    return snap


# ---- 開始
html = client.get("/").get_data(as_text=True)
check("開始前のサイドバーは OFF", "デモモード OFF" in html, True)
res = client.post("/demo-mode", data={"on": "1"}, follow_redirects=True)
html = res.get_data(as_text=True)
check("開始を知らせる", "デモモードを開始しました" in html, True)
check("サイドバーが ON と「解除する」に変わる", ("デモモード ON" in html, "解除する" in html), (True, True))
check("デモ用DBに切り替わる", db.DB_PATH, tmp.with_name("gemmba-demo.db"))

c = db.get_db()
check("作業者は宮田・南川",
      [tuple(r) for r in c.execute("SELECT name, nfc_tag_id FROM workers ORDER BY id")],
      [("宮田", "TAG-MIYATA"), ("南川", "TAG-MINAMI")])
check("機材は3Dプリンタ・旋盤（モジュールを引き継ぐ）",
      [tuple(r) for r in c.execute("SELECT name, module_id, hostname, online FROM equipment ORDER BY id")],
      [("3Dプリンタ", "MOD-002", "esp-0e5310", 1), ("旋盤", "MOD-006", "pi01", 1)])
check("機材ごとに至急と通常のタスク",
      sorted({(r["name"], r["priority"]) for r in c.execute(
          "SELECT e.name, t.priority FROM tasks t JOIN equipment e ON e.id = t.equipment_id")}),
      [("3Dプリンタ", "normal"), ("3Dプリンタ", "urgent"), ("旋盤", "normal"), ("旋盤", "urgent")])
check("実績は無い", c.execute("SELECT COUNT(*) FROM work_logs").fetchone()[0], 0)
c.close()

for tag in ("TAG-MIYATA", "TAG-MINAMI"):
    body = client.get(f"/api/workers/{tag}/next_task?module_id=MOD-006").get_json()
    check(f"{tag} が旋盤でタッチすると至急が先", [t["priority"] for t in body["tasks"]][:1], ["urgent"])

real = snapshot(tmp)
check("本番の作業者はそのまま", [r[1] for r in real["workers"]], ["宮田さん", "南川健志郎", "山口さん"])
check("本番のタスクは残り、以前のデモの残りだけ消える", [r[1] for r in real["tasks"]], ["本物のタスク"])

res = client.post("/demo-mode", data={"on": "1"}, follow_redirects=True)
check("二重に開始しない", "すでにデモモードです" in res.get_data(as_text=True), True)

# ---- デモ中の作業：至急のタスクに着手（機材が使用中になる）
c = db.get_db()
urgent = c.execute("SELECT id FROM tasks WHERE priority = 'urgent' AND equipment_id = 2").fetchone()["id"]
c.close()
client.post(f"/api/tasks/{urgent}/start", json={"nfc_tag_id": "TAG-MIYATA", "module_id": "MOD-006"})
c = db.get_db()
check("デモ中に着手すると旋盤が使用中", c.execute("SELECT status FROM equipment WHERE id = 2").fetchone()[0], "working")
c.execute("UPDATE equipment SET online = 0 WHERE hostname = 'esp-0e5310'")   # デモ中に切断した
c.commit()
c.close()

# ---- 解除
res = client.post("/demo-mode", data={"on": "0"}, follow_redirects=True)
html = res.get_data(as_text=True)
check("解除を知らせる", "デモモードを解除しました" in html, True)
check("サイドバーが OFF に戻る", "デモモード OFF" in html, True)
check("本番DBに戻る", db.DB_PATH, tmp)
check("デモ用DBは消える", tmp.with_name("gemmba-demo.db").exists(), False)
after = snapshot(tmp)
check("本番のタスクと実績は開始前と同じ", (after["tasks"], after["work_logs"]), (real["tasks"], real["work_logs"]))
check("本番の作業者は開始前と同じ", after["workers"], real["workers"])
c = db.get_db()
check("デモ中の切断は本番DBにも反映", c.execute("SELECT online FROM equipment WHERE hostname = 'esp-0e5310'").fetchone()[0], 0)
check("本番の旋盤は空きのまま", c.execute("SELECT status FROM equipment WHERE hostname = 'pi01'").fetchone()[0], "idle")
c.close()

res = client.post("/demo-mode", data={"on": "0"}, follow_redirects=True)
check("二重に解除しない", "デモモードではありません" in res.get_data(as_text=True), True)

# ---- もう一度開始すると、前回の作業は残らず最初の状態から
client.post("/demo-mode", data={"on": "1"})
c = db.get_db()
check("作り直されて全タスク未着手", {r[0] for r in c.execute("SELECT status FROM tasks")}, {"todo"})
check("旋盤も空き", c.execute("SELECT status FROM equipment WHERE name = '旋盤'").fetchone()[0], "idle")
c.close()

# ---- 初期化：デモモードのまま、デモ中の作業を消して最初の状態へ
html = client.get("/").get_data(as_text=True)
check("デモ中のサイドバーに初期化ボタン", "初期化" in html and "demo-mode/reset" in html, True)
c = db.get_db()
urgent = c.execute("SELECT id FROM tasks WHERE priority = 'urgent' AND equipment_id = 2").fetchone()["id"]
c.close()
client.post(f"/api/tasks/{urgent}/start", json={"nfc_tag_id": "TAG-MINAMI", "module_id": "MOD-006"})
client.post(f"/api/tasks/{urgent}/complete")
client.post("/tasks/add", data={"title": "デモ中に足したタスク"})
c = db.get_db()
check("初期化前は作業の跡がある", (c.execute("SELECT COUNT(*) FROM work_logs").fetchone()[0] > 0,
                               c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]), (True, 5))
c.execute("UPDATE equipment SET online = 0 WHERE hostname = 'pi01'")   # デモ中に旋盤が切断した
c.commit()
c.close()
res = client.post("/demo-mode/reset", follow_redirects=True)
check("初期化を知らせる", "デモモードを初期化しました" in res.get_data(as_text=True), True)
check("初期化してもデモモードのまま", (demo.is_active(), db.DB_PATH.name), (True, "gemmba-demo.db"))
c = db.get_db()
check("初期化後は開始直後と同じ",
      ([tuple(r) for r in c.execute("SELECT id, name FROM workers")],
       [tuple(r) for r in c.execute("SELECT id, name, status FROM equipment")],
       [tuple(r) for r in c.execute("SELECT id, status FROM tasks")],
       c.execute("SELECT COUNT(*) FROM work_logs").fetchone()[0]),
      ([(1, "宮田"), (2, "南川")], [(1, "3Dプリンタ", "idle"), (2, "旋盤", "idle")],
       [(1, "todo"), (2, "todo"), (3, "todo"), (4, "todo")], 0))
check("初期化しても切断中の旋盤は切断中のまま", c.execute("SELECT online FROM equipment WHERE name = '旋盤'").fetchone()[0], 0)
c.close()
check("初期化しても本番DBのタスクはそのまま", [r[1] for r in snapshot(tmp)["tasks"]], ["本物のタスク"])

# ---- 再起動しても（デモ用DBが残っていれば）デモモードのまま
demo._real_path, db.DB_PATH = None, tmp      # app.py を起動し直した直後と同じ状態
check("起動時にデモモードを再開する", demo.resume(), True)
check("再開後はデモ用DB", (db.DB_PATH.name, demo.is_active()), ("gemmba-demo.db", True))
client.post("/demo-mode", data={"on": "0"})
check("再開後も解除で本番DBに戻る", db.DB_PATH, tmp)
check("デモ用DBが無ければ再開しない", demo.resume(), False)
res = client.post("/demo-mode/reset", follow_redirects=True)
check("デモモードでなければ初期化しない", "デモモードではありません" in res.get_data(as_text=True), True)
check("解除中のサイドバーに初期化ボタンは出ない", "demo-mode/reset" in client.get("/").get_data(as_text=True), False)

# 旧UIのサイドバーでも同じボタン
cl = app.app.test_client()
cl.get("/ui/classic")
check("旧UIにも開始ボタン", "デモモード OFF" in cl.get("/").get_data(as_text=True), True)

print("\nすべてOK" if ok else "\nNG があります")
sys.exit(0 if ok else 1)
