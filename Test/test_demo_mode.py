# -*- coding: utf-8 -*-
"""
デモモード（サイドバーの「開始する」「解除する」）の確認。

  1. 開始しても、作業者・機材・モジュールの紐付け・デモ以外のタスクと実績はそのまま
  2. 機材ごとに合ったタスク（旋盤なら旋盤の仕事、プレスはプレス資格が必要…）が入る
  3. 作業者ごとの実績が入り、本人の権限で出来る仕事の実績だけになっている
  4. 解除すると、足したタスクと実績だけが消え、開始前と同じに戻る
  5. デモ中に登録した本物のタスクは解除しても残る。デモのタスクを使用中の機材は空きに戻る
  6. 二重に開始・解除しない。サイドバーの表示が切り替わる

    python Test\\test_demo_mode.py
"""
import os, sys, tempfile
from pathlib import Path

sys.path.insert(0, os.getcwd())
import db

tmp = Path(tempfile.mkdtemp()) / "t.db"
db.DB_PATH = tmp
db.init_db(seed=True)

import app
import permissions as perms

client = app.app.test_client()
ok = True


def check(label, got, want):
    global ok
    mark = "OK " if got == want else "NG "
    if got != want:
        ok = False
    print(f"{mark}{label}: {got!r}" + ("" if got == want else f"  期待 {want!r}"))


def snapshot():
    c = db.get_db()
    snap = {t: [tuple(r) for r in c.execute(f"SELECT * FROM {t} ORDER BY id")]
            for t in ("workers", "equipment", "tasks", "work_logs")}
    c.close()
    return snap


# 本物の実績を1件作っておく（タスク1を完了）。デモの解除で消えないことを見る
client.post("/api/tasks/1/complete")
c = db.get_db()
c.execute("UPDATE equipment SET hostname = 'pi-press', online = 1 WHERE id = 3")
c.commit()
c.close()
before = snapshot()

# ---- 開始
html = client.get("/").get_data(as_text=True)
check("開始前のサイドバーは OFF", "デモモード OFF" in html, True)
res = client.post("/demo-mode", data={"on": "1"}, follow_redirects=True)
html = res.get_data(as_text=True)
check("開始を知らせる", "デモモードを開始しました" in html, True)
check("サイドバーが ON と「解除する」に変わる", ("デモモード ON" in html, "解除する" in html), (True, True))

after = snapshot()
check("作業者はそのまま", after["workers"], before["workers"])
check("機材・モジュールの紐付けはそのまま", after["equipment"], before["equipment"])
check("元のタスクはそのまま", after["tasks"][:len(before["tasks"])], before["tasks"])
check("元の実績はそのまま", after["work_logs"][:len(before["work_logs"])], before["work_logs"])

c = db.get_db()
eq_tasks = {r["name"]: [(t["title"], t["required_permissions"]) for t in c.execute(
    "SELECT title, required_permissions FROM tasks WHERE demo = 1 AND status != 'done' AND equipment_id = ?",
    (r["id"],))] for r in c.execute("SELECT id, name FROM equipment")}
check("どの機材にもデモのタスクが入る", all(len(v) >= 2 for v in eq_tasks.values()), True)
check("プレス機のタスクはプレス資格が要る", all(p == "press" for _, p in eq_tasks["プレス機 #1"]), True)
check("旋盤のタスクは旋盤の仕事", any("旋" in t or "段取り" in t or "シャフト" in t or "ブッシュ" in t
                                   for t, _ in eq_tasks["旋盤 #2"]), True)

logs = c.execute("""
    SELECT l.*, t.required_permissions, t.demo FROM work_logs l JOIN tasks t ON t.id = l.task_id
    WHERE t.demo = 1""").fetchall()
workers = {r["id"]: r for r in c.execute("SELECT * FROM workers")}
check("作業者ごとに実績が入る", {l["worker_id"] for l in logs} == set(workers), True)
check("実績は本人の権限で出来る仕事だけ",
      all(not perms.missing(workers[l["worker_id"]], {"required_permissions": l["required_permissions"]})
          for l in logs), True)
check("実績の所要時間は正の値", all((l["duration_sec"] or 0) > 0 for l in logs), True)
c.close()

check("AIが実績を学習に使える", len(app._ai_logs(db.get_db())) > len(before["work_logs"]), True)

res = client.post("/demo-mode", data={"on": "1"}, follow_redirects=True)
check("二重に開始しない", "すでにデモモードです" in res.get_data(as_text=True), True)

# ---- デモ中の操作：本物のタスクを登録、デモのタスクに着手（機材が使用中になる）
client.post("/tasks/add", data={"title": "本物のタスク"})
c = db.get_db()
demo_task = c.execute("SELECT id FROM tasks WHERE demo = 1 AND status != 'done' AND equipment_id = 2 LIMIT 1").fetchone()["id"]
c.close()
client.post(f"/api/tasks/{demo_task}/start", json={"nfc_tag_id": "TAG-0002", "module_id": "MOD-A-02"})
c = db.get_db()
check("デモのタスクに着手すると機材が使用中", c.execute("SELECT status FROM equipment WHERE id = 2").fetchone()[0], "working")
c.close()

# ---- 解除
res = client.post("/demo-mode", data={"on": "0"}, follow_redirects=True)
html = res.get_data(as_text=True)
check("解除を知らせる", "デモモードを解除しました" in html, True)
check("サイドバーが OFF に戻る", "デモモード OFF" in html, True)

end = snapshot()
c = db.get_db()
check("デモのタスクは残らない", c.execute("SELECT COUNT(*) FROM tasks WHERE demo = 1").fetchone()[0], 0)
check("本物のタスクは残る", c.execute("SELECT COUNT(*) FROM tasks WHERE title = '本物のタスク'").fetchone()[0], 1)
check("デモのタスクで使用中だった機材は空きに戻る", c.execute("SELECT status, current_task_id FROM equipment WHERE id = 2").fetchone()[:], ("idle", None))
c.close()
check("実績は開始前と同じ", end["work_logs"], before["work_logs"])
check("作業者は開始前と同じ", end["workers"], before["workers"])
check("元のタスクは開始前と同じ", [t for t in end["tasks"] if t[0] <= before["tasks"][-1][0]], before["tasks"])

res = client.post("/demo-mode", data={"on": "0"}, follow_redirects=True)
check("二重に解除しない", "デモモードではありません" in res.get_data(as_text=True), True)

# デモのタスクを途中で削除しても、その実績は一緒に消える（解除後に架空の実績が残らない）
client.post("/demo-mode", data={"on": "1"})
c = db.get_db()
done = c.execute("SELECT id FROM tasks WHERE demo = 1 AND status = 'done' LIMIT 1").fetchone()["id"]
c.close()
client.post(f"/tasks/{done}/delete")
client.post("/demo-mode", data={"on": "0"})
check("途中で消したデモのタスクの実績も残らない", snapshot()["work_logs"], before["work_logs"])

# 旧UIのサイドバーでも同じボタン
cl = app.app.test_client()
check("旧UIにも開始ボタン", "デモモード OFF" in cl.get("/").get_data(as_text=True), True)

print("\nすべてOK" if ok else "\nNG があります")
sys.exit(0 if ok else 1)
