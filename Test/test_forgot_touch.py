# -*- coding: utf-8 -*-
"""
終了のタッチ忘れへの対策の確認。導入先の評価「タッチを忘れる人がいた」への対応。

  1. 見込み時間を大きく超えた使用を「タッチ忘れの可能性」として拾う
     （管理画面・掲示用ダッシュボードに出し、モジュールの画面で終了を促す）
  2. 別の機材でタッチしたら、前の機材を終わらせるか尋ねる
  3. 管理者が代わりに終了できる（終わった時刻までを所要時間にする / 中断して戻す）

    python Test\\test_forgot_touch.py
"""
import os, sys, tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.getcwd())
import db

tmp = Path(tempfile.mkdtemp()) / "t.db"
db.DB_PATH = tmp
db.init_db(seed=True)   # 機材1(MOD-A-01)は田中さんが作業中、2(MOD-A-02/pi01)は空き

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


def run(sql, *params):
    c = db.get_db()
    c.execute(sql, params)
    c.commit()
    c.close()


def ago(**kw):
    return (datetime.now() - timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S")


# モジュールへの送信は記録だけする（ブローカーは無い）
sent = []
app._notify = lambda device_id, lines, led=None: sent.append((device_id, list(lines), led)) or 0
app._notify_pause = lambda device_id, lines, led, sec=2.5: sent.append((device_id, list(lines), led))
app._notify_briefly = lambda device_id, module_id, lines, led, sec=6: sent.append((device_id, list(lines), led))

# --- 1. 見込みを大きく超えた使用を拾う
# タスク1: 数量30・難易度3。実績が無いので 1個10分 × 30 = 5時間が見込み。その2倍 = 10時間
run("UPDATE tasks SET started_at = ? WHERE id = 1", ago(hours=1))
check("1時間: まだ知らせない", 1 in app._overdue_now(), False)
run("UPDATE tasks SET started_at = ? WHERE id = 1", ago(hours=11))
over = app._overdue_now()
check("11時間: タッチ忘れの可能性", 1 in over, True)
check("経過と上限", (over[1]["elapsed_sec"] // 3600, over[1]["limit_sec"] // 3600), (11, 10))

# フリー利用は一律 FREE_USE_FORGOT_SEC（2時間）
run("""UPDATE equipment SET status = 'working', current_worker_id = 2, current_task_id = NULL,
       updated_at = ? WHERE id = 2""", ago(minutes=30))
check("フリー利用30分: 知らせない", 2 in app._overdue_now(), False)
run("UPDATE equipment SET updated_at = ? WHERE id = 2", ago(hours=3))
check("フリー利用3時間: 知らせる", 2 in app._overdue_now(), True)

# 画面に出る
for ui in ("classic", "new"):
    client.set_cookie(app.UI_COOKIE, ui)
    html = client.get("/").get_data(as_text=True)
    check(f"{ui}: ダッシュボードに「終了タッチ忘れ？」", "終了タッチ忘れ？" in html, True)
    html = client.get("/equipment").get_data(as_text=True)
    check(f"{ui}: 機材画面に代理終了の欄", "代わりに終了" in html and "終了タッチ忘れ？" in html, True)
# 掲示用ダッシュボード（/factorydashboard）のある版だけ確かめる
if "factory_dashboard" in app.app.view_functions:
    check("掲示用ダッシュボード", "終了タッチ忘れ？" in client.get("/factorydashboard").get_data(as_text=True), True)

# モジュールの画面で促す（見回り）。接続中のモジュールにだけ、1回の使用につき1回
run("UPDATE equipment SET hostname = 'pi00', online = 1 WHERE id = 1")
run("UPDATE equipment SET online = 1 WHERE id = 2")   # pi01
sent.clear()
notified = app.check_forgotten_sessions()
check("見回り: 2台に知らせる", sorted(notified), [1, 2])
check("見回り: 画面に終了の案内", all("「作業終了」を選んでください" in lines for _, lines, _ in sent), True)
check("見回り: 2回目は送らない", app.check_forgotten_sessions(), [])
with app._touch_busy_lock:
    app._touch_busy["pi01"] = 0
app._forgot_notified.clear()
check("見回り: 操作中のモジュールには送らない", app.check_forgotten_sessions(), [1])
with app._touch_busy_lock:
    app._touch_busy.pop("pi01")

# --- 3. 管理者が代わりに終了
# 時刻の誤り
future = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
res = client.post("/equipment/1/finish", data={"outcome": "done", "ended_at": future}, follow_redirects=True)
check("先の時刻は断る", "これから先の時刻" in res.get_data(as_text=True), True)
before_start = (datetime.now() - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M")
res = client.post("/equipment/1/finish", data={"outcome": "done", "ended_at": before_start},
                  follow_redirects=True)
check("始めた時刻より前は断る", "始めた時刻" in res.get_data(as_text=True), True)
check("断ったら使用中のまま", q("SELECT status FROM equipment WHERE id = 1")[0]["status"], "working")

# 完了: 終わった時刻までを所要時間に（11時間前に開始、10時間前に終わっていた → 1時間）
ended = (datetime.now() - timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M")
started = q("SELECT started_at FROM tasks WHERE id = 1")[0]["started_at"]
client.post("/equipment/1/finish", data={"outcome": "done", "ended_at": ended})
t = q("SELECT status, completed_at FROM tasks WHERE id = 1")[0]
check("完了: タスクは完了", t["status"], "done")
check("完了: 完了時刻は入力した時刻", t["completed_at"][:16], ended.replace("T", " "))
log = q("SELECT * FROM work_logs WHERE task_id = 1")[0]
want = int((datetime.strptime(t["completed_at"], "%Y-%m-%d %H:%M:%S")
            - datetime.strptime(started, "%Y-%m-%d %H:%M:%S")).total_seconds())
check("完了: 所要時間は忘れていた時間を含まない", log["duration_sec"], want)
check("完了: 所要時間は約1時間", round(log["duration_sec"] / 3600), 1)
eq = q("SELECT status, current_worker_id, current_task_id FROM equipment WHERE id = 1")[0]
check("完了: 機材は空き", (eq["status"], eq["current_worker_id"], eq["current_task_id"]), ("idle", None, None))

# フリー利用の代理終了
client.post("/equipment/2/finish", data={})
check("フリー利用: 空きに戻る", q("SELECT status FROM equipment WHERE id = 2")[0]["status"], "idle")

# 中断: 指定なしのタスクは誰でも拾える未着手に、指定ありは本人の割当済に戻す
for title, designated, want_state in (("中断（指定なし）", 0, ("todo", None)),
                                      ("中断（指定あり）", 1, ("assigned", 2))):
    c = db.get_db()
    tid = c.execute("""INSERT INTO tasks (title, status, assigned_worker_id, equipment_id, started_at, designated)
                       VALUES (?, 'in_progress', 2, 2, ?, ?)""", (title, ago(hours=1), designated)).lastrowid
    c.execute("""UPDATE equipment SET status = 'working', current_worker_id = 2, current_task_id = ?
                 WHERE id = 2""", (tid,))
    c.commit()
    c.close()
    client.post("/equipment/2/finish", data={"outcome": "abort"})
    t = q("SELECT status, assigned_worker_id, started_at FROM tasks WHERE id = ?", tid)[0]
    check(f"{title}: 戻り先", (t["status"], t["assigned_worker_id"]), want_state)
    check(f"{title}: 着手時刻は消す", t["started_at"], None)
    check(f"{title}: 実績は残さない", q("SELECT * FROM work_logs WHERE task_id = ?", tid), [])

res = client.post("/equipment/2/finish", data={}, follow_redirects=True)
check("使用中でない機材は断る", "使用中ではありません" in res.get_data(as_text=True), True)

# --- 2. 別の機材でタッチしたら、前の機材を終わらせるか尋ねる
c = db.get_db()
tid = c.execute("""INSERT INTO tasks (title, status, assigned_worker_id, equipment_id, started_at, difficulty)
                   VALUES ('部品の研磨', 'in_progress', 2, 1, ?, 2)""", (ago(minutes=40),)).lastrowid
c.execute("UPDATE equipment SET status = 'working', current_worker_id = 2, current_task_id = ? WHERE id = 1",
          (tid,))
c.commit()
c.close()
worker = {"id": 2, "name": "佐藤 花子"}
here = {"id": 2, "name": "旋盤 #2", "status": "idle"}

asked = []
app.request_confirm = lambda device_id, text, **kw: asked.append((text, kw.get("lines"))) or False
check("いいえ: 何も終わらせない", app._ask_forgotten_session("pi01", "MOD-A-02", worker, here), 0)
check("いいえ: 前の機材は使用中のまま", q("SELECT status FROM equipment WHERE id = 1")[0]["status"], "working")
check("尋ねた内容に前の機材とタスク", asked[0][1], ["レーザー加工機 #1 が", "使用中のままです", "部品の研磨"])

app.request_confirm = lambda device_id, text, **kw: None
check("無応答: 中止", app._ask_forgotten_session("pi01", "MOD-A-02", worker, here), None)

app.request_confirm = lambda device_id, text, **kw: True
check("はい: 1台終わらせる", app._ask_forgotten_session("pi01", "MOD-A-02", worker, here), 1)
check("はい: 前の機材は空き", q("SELECT status FROM equipment WHERE id = 1")[0]["status"], "idle")
check("はい: タスクは完了", q("SELECT status FROM tasks WHERE id = ?", tid)[0]["status"], "done")
check("はい: 実績が残る", len(q("SELECT * FROM work_logs WHERE task_id = ?", tid)), 1)
check("前の機材の画面を待機に戻す", any(d == "pi00" and "社員証をタッチしてください" in lines
                                    for d, lines, _ in sent), True)
check("他に使用中が無ければ尋ねない", app._ask_forgotten_session("pi01", "MOD-A-02", worker, here), 0)

print("\n全件OK" if ok else "\n失敗あり")
sys.exit(0 if ok else 1)
