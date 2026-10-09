# -*- coding: utf-8 -*-
"""
複数人タスク（required_workers >= 2）の確認。

  1. 最初の人が着手を選ぶと集合待ちになり、同じ機材でちょうど必要人数がそろった時点で作業開始
  2. 必要権限はメンバーの誰か1人が持っていればよい（最後の1人で足りなければ断る）
  3. 作業終了は誰がタッチしてもよく、全員に実績が残る（学習データには使わない）
  4. 参加・取り消し・時間切れ・作業者の削除で、機材とタスクが食い違わない
  5. モジュールの画面・管理画面の表示

DBは一時ファイル、MQTTは差し替え、サーバー自身へのHTTPはテストクライアントへ流す。

    python Test\\test_team_task.py
"""
import os, sys, tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.getcwd())
import db

tmp = Path(tempfile.mkdtemp()) / "t.db"
db.DB_PATH = tmp
db.init_db(seed=False)

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


def q(sql, *params):
    c = db.get_db()
    rows = [dict(r) for r in c.execute(sql, params).fetchall()]
    c.close()
    return rows


def one(sql, *params):
    rows = q(sql, *params)
    return rows[0] if rows else None


def run(sql, *params):
    c = db.get_db()
    c.execute(sql, params)
    c.commit()
    c.close()


# ---------------------------------------------------------------- 下準備
# 作業者: 1 山田(権限なし) / 2 佐藤(クレーン) / 3 鈴木(権限なし) / 4 高橋(権限なし)
for name, held, tag in (("山田", "", "T1"), ("佐藤", "crane", "T2"), ("鈴木", "", "T3"), ("高橋", "", "T4")):
    run("INSERT INTO workers (name, years_of_service, permissions, nfc_tag_id) VALUES (?, 1, ?, ?)",
        name, held, tag)
run("INSERT INTO equipment (name, module_id, hostname, online) VALUES ('旋盤A', 'MOD-A', 'pi-a', 1)")
run("INSERT INTO equipment (name, module_id, hostname, online) VALUES ('プレスB', 'MOD-B', 'pi-b', 1)")

# モジュールへの送信は記録だけする（ブローカーは無い）
sent = []
app._notify = lambda device_id, lines, led=None: sent.append((device_id, list(lines), led)) or 0
app._notify_pause = lambda device_id, lines, led, sec=2.5: sent.append((device_id, list(lines), led))
app._notify_briefly = lambda device_id, module_id, lines, led, sec=6: sent.append((device_id, list(lines), led))
# 時間切れのタイマーは立てない（テストの中で直接呼ぶ）
app._schedule_gathering_timeout = lambda eq_id, since: None


class _Resp:
    def __init__(self, r):
        self.status_code = r.status_code
        self.ok = r.status_code < 400
        self.headers = {"content-type": r.headers.get("Content-Type", "")}
        self._json = r.get_json(silent=True)

    def json(self):
        return self._json

    def raise_for_status(self):
        if not self.ok:
            raise app.requests.RequestException(self.status_code)


def _path(url):
    return url.replace(app.SELF_URL, "")


app.requests.get = lambda url, params=None, timeout=None: _Resp(client.get(_path(url), query_string=params))
app.requests.post = lambda url, json=None, timeout=None: _Resp(client.post(_path(url), json=json))

# ---------------------------------------------------------------- 1. 登録
r = client.post("/tasks/add", data={"title": "大型部品の吊り上げ", "required_workers": "2",
                                    "required_permissions": "crane", "equipment_id": "1"})
team = one("SELECT * FROM tasks WHERE title = '大型部品の吊り上げ'")
check("必要人数が入る", team["required_workers"], 2)
client.post("/tasks/add", data={"title": "人数の打ち間違い", "required_workers": "999"})
check("必要人数は上限に収める", one("SELECT required_workers FROM tasks WHERE title = '人数の打ち間違い'")
      ["required_workers"], app.MAX_TEAM)
run("DELETE FROM tasks WHERE title = '人数の打ち間違い'")
client.post("/tasks/add", data={"title": "ひとりの作業"})
check("既定は1人", one("SELECT required_workers FROM tasks WHERE title = 'ひとりの作業'")["required_workers"], 1)
run("DELETE FROM tasks WHERE title = 'ひとりの作業'")

# 権限を持たない山田さんにも候補として出る（誰か1人が持っていればよいので）
body = client.get("/api/workers/T1/next_task", query_string={"module_id": "MOD-A"}).get_json()
check("権限の無い人にも複数人タスクを出す", [t["id"] for t in body["tasks"]], [team["id"]])
check("必要人数も返す", body["tasks"][0]["required_workers"], 2)
check("候補の画面に人数", app._task_lines(body["tasks"][0])[1].startswith("2人作業 / "), True)

# 担当者に権限の無い人を指定できる（1人のタスクでは断られる）
client.post(f"/tasks/{team['id']}/update", data={"assigned_worker_id": "1", "required_workers": "2",
                                                 "req_perm_form": "1", "required_permissions": "crane",
                                                 "equipment_id": "1"})
check("複数人タスクは権限の無い人も指定できる", one("SELECT assigned_worker_id FROM tasks WHERE id = ?",
                                                     team["id"])["assigned_worker_id"], 1)
client.post(f"/tasks/{team['id']}/update", data={"assigned_worker_id": "", "required_workers": "2",
                                                 "req_perm_form": "1", "required_permissions": "crane",
                                                 "equipment_id": "1"})

# ---------------------------------------------------------------- 2. 集合待ち → 開始
res = client.post(f"/api/tasks/{team['id']}/start", json={"nfc_tag_id": "T1", "module_id": "MOD-A"})
check("1人目: 集合待ちになる", (res.status_code, res.get_json().get("gathering")), (200, True))
check("タスクは集合待ち", one("SELECT status FROM tasks WHERE id = ?", team["id"])["status"], "gathering")
eq = one("SELECT * FROM equipment WHERE id = 1")
check("機材は集合待ち・リーダーは山田さん", (eq["status"], eq["current_worker_id"]), ("gathering", 1))
check("メンバーは1人", [m["worker_id"] for m in q("SELECT worker_id FROM task_members WHERE task_id = ?",
                                                  team["id"])], [1])
others = client.get("/api/workers/T3/next_task", query_string={"module_id": "MOD-A"}).get_json()["tasks"]
check("集合待ちのタスクは他の人の候補に出ない", [t["id"] for t in others], [])
check("集合待ちのタスクはAIで割り当て直せない",
      client.post(f"/tasks/{team['id']}/auto_assign").status_code == 302
      and one("SELECT status FROM tasks WHERE id = ?", team["id"])["status"], "gathering")

# モジュールの画面
sent.clear()
app._sync_module_state("pi-a", 1)
check("集合待ちの画面", sent[-1], ("pi-a", ["大型部品の吊り上げ", "1/2人 集合待ち", "あと1人 社員証をタッチ",
                                   "決定ボタン長押しで取り消し"],
                                   "gathering"))

# 権限の無い鈴木さんが最後の1人として加わろうとする → 断る
result, info = app._join_gathering(1, 3)
check("最後の1人で権限が足りなければ断る", (result, info.get("missing")), ("permission", ["クレーン・玉掛け"]))
check("断った人は加わらない", len(q("SELECT * FROM task_members WHERE task_id = ?", team["id"])), 1)
# クレーンを持つ佐藤さんが加わる → そろって開始
result, info = app._join_gathering(1, 2)
check("そろったら作業開始", (result, info["names"]), ("started", ["山田", "佐藤"]))
t = one("SELECT * FROM tasks WHERE id = ?", team["id"])
check("タスクは作業中・担当はリーダー", (t["status"], t["assigned_worker_id"], t["equipment_id"]),
      ("in_progress", 1, 1))
check("着手時刻が入る", bool(t["started_at"]), True)
check("機材は使用中", one("SELECT status FROM equipment WHERE id = 1")["status"], "working")
check("そろった後は加われない", app._join_gathering(1, 3)[0], "gone")

sent.clear()
app._sync_module_state("pi-a", 1)
check("作業中の画面に人数", sent[-1][1][0], "山田 さん 他1名 使用中")

eqs = client.get("/api/equipment/MOD-A/status").get_json()
check("機材の状態にメンバー", eqs["member_ids"], [1, 2])
check("メンバーは使用者として扱う", app._uses_equipment({"id": 2}, eqs), True)
check("メンバー以外は使用者ではない", app._uses_equipment({"id": 3}, eqs), False)

# 管理画面にメンバー全員の名前
for ui in ("classic", "new"):
    client.set_cookie(app.UI_COOKIE, ui)
    html = client.get("/").get_data(as_text=True)
    check(f"{ui}: ダッシュボードにメンバー全員", "山田・佐藤" in html, True)
    html = client.get("/tasks").get_data(as_text=True)
    check(f"{ui}: タスク一覧に人数", "2人作業" in html and "山田・佐藤" in html, True)
    check(f"{ui}: 機材画面にメンバー全員", "山田・佐藤" in client.get("/equipment").get_data(as_text=True), True)

# 作業中は必要人数を変えられない
client.post(f"/tasks/{team['id']}/update", data={"required_workers": "3", "priority": "normal"})
check("作業中は人数を変えない", one("SELECT required_workers FROM tasks WHERE id = ?", team["id"])
      ["required_workers"], 2)

# ---------------------------------------------------------------- 3. 終了は誰でも・全員に実績
res = client.post(f"/api/tasks/{team['id']}/complete", json={"worker_id": 2}).get_json()
logs = q("SELECT id, worker_id, duration_sec FROM work_logs WHERE task_id = ? ORDER BY worker_id", team["id"])
check("全員に実績", [l["worker_id"] for l in logs], [1, 2])
check("返す実績はタッチした人の分", res["work_log_id"], logs[1]["id"])
c = db.get_db()
learned = [l["task_id"] for l in app._ai_logs(c)]
c.close()
check("複数人タスクの実績は学習に使わない", team["id"] in learned, False)

# ---------------------------------------------------------------- 4. 参加・取り消し（タッチの流れ）
run("UPDATE equipment SET status = 'idle', current_worker_id = NULL, current_task_id = NULL WHERE id = 1")
run("INSERT INTO tasks (title, required_workers, equipment_id) VALUES ('3人で搬入', 3, 2)")
three = one("SELECT id FROM tasks WHERE title = '3人で搬入'")["id"]
client.post(f"/api/tasks/{three}/start", json={"nfc_tag_id": "T3", "module_id": "MOD-B"})

answers = []
app.request_confirm = lambda device_id, text, **kw: (sent.append((device_id, [text] + kw.get("lines", []), "ask"))
                                                     or (answers.pop(0) if answers else None))


def touch(tag, device="pi-b", module="MOD-B"):
    sent.clear()
    app._handle_touch(device, module, tag)


answers[:] = [True]
touch("T4")
check("高橋さんが参加", [m["worker_id"] for m in q("SELECT worker_id FROM task_members WHERE task_id = ?", three)],
      [3, 4])
check("参加の確認を出す", sent[0][1][0], "この作業に参加しますか？")
check("参加後の表示", sent[1][1], ["参加しました", "あと1人です"])

answers[:] = [True]
touch("T4")
check("メンバーがタッチすると取り消しを尋ねる", sent[0][1][0], "参加を取り消しますか？")
check("取り消すと抜ける", [m["worker_id"] for m in q("SELECT worker_id FROM task_members WHERE task_id = ?", three)],
      [3])

# 他の集合待ちに加わっている人は、別の集合待ちに加われない
run("INSERT INTO tasks (title, required_workers, equipment_id) VALUES ('2人で点検', 2, 1)")
pair = one("SELECT id FROM tasks WHERE title = '2人で点検'")["id"]
client.post(f"/api/tasks/{pair}/start", json={"nfc_tag_id": "T1", "module_id": "MOD-A"})
check("2か所で同時に待たない", app._join_gathering(2, 1)[0], "busy")
res = client.post(f"/api/tasks/{three}/start", json={"nfc_tag_id": "T1", "module_id": "MOD-B"})
check("集合待ち中の人は別の集合待ちを始められない", (res.status_code, res.get_json()["error"]),
      (409, "already gathering"))

# 他の機材で人手を待っていることを知らせる
sent.clear()
answers[:] = [True]
c = db.get_db()
g, have = app._pick_gathering(c, {"id": 4, "role": "member", "permissions": ""}, {"id": 1})
c.close()
check("他の機材の集合待ちを見つける", (g["eq_name"] if g else None, have), ("プレスB", 1))
check("集合待ちへ誘導", app._guide_to_gathering("pi-a", "MOD-A", {"id": 4, "name": "高橋", "role": "member",
                                                                 "permissions": ""},
                                                {"id": 1, "name": "旋盤A"}), True)

# リーダーが取り消すと集合待ちごと終わる
answers[:] = [True]
touch("T1", "pi-a", "MOD-A")
check("リーダーには集合待ちの取り消しを尋ねる", sent[0][1][0], "集合待ちを取り消しますか？")
check("取り消すと機材は空き", one("SELECT status, current_task_id FROM equipment WHERE id = 1"),
      {"status": "idle", "current_task_id": None})
check("タスクは未着手に戻る", one("SELECT status FROM tasks WHERE id = ?", pair)["status"], "todo")
check("メンバーも外れる", q("SELECT * FROM task_members WHERE task_id = ?", pair), [])

# ---------------------------------------------------------------- 時間切れ
since = one("SELECT updated_at FROM equipment WHERE id = 2")["updated_at"]
check("別の回の時刻では取り消さない", app._cancel_gathering(2, "2000-01-01 00:00:00"), None)
check("まだ時間内なら見回りは取り消さない", app.cancel_stale_gatherings(), [])
later = datetime.strptime(since, "%Y-%m-%d %H:%M:%S") + timedelta(seconds=app.GATHER_TIMEOUT_SEC + 1)
sent.clear()
check("時間切れを見回りで取り消す", app.cancel_stale_gatherings(now=later), [2])
check("モジュールに知らせる", sent[-1][1][0], "人数がそろいませんでした")
check("タスクは未着手に戻る", one("SELECT status FROM tasks WHERE id = ?", three)["status"], "todo")

# ---------------------------------------------------------------- 決定ボタンの長押しで取り消し
client.post(f"/api/tasks/{three}/start", json={"nfc_tag_id": "T3", "module_id": "MOD-B"})
app._join_gathering(2, 4)
sent.clear()
app._sync_module_state("pi-b", 2)
check("集合待ちの画面に取り消し方", sent[-1][1][-1], "決定ボタン長押しで取り消し")

# 誰かがタッチして操作している間は、そちらを優先する
app._touch_busy["pi-b"] = 0
app._handle_data("pi-b", {"device_id": "pi-b", "event": "long_press", "button": "ok"})
check("操作中の長押しは無視", one("SELECT status FROM equipment WHERE id = 2")["status"], "gathering")
app._touch_busy.pop("pi-b", None)

app._handle_data("pi-b", {"device_id": "pi-b", "event": "long_press", "button": "left"})
check("決定以外の長押しは無視", one("SELECT status FROM equipment WHERE id = 2")["status"], "gathering")

sent.clear()
app._handle_data("pi-b", {"device_id": "pi-b", "event": "long_press", "button": "ok"})
check("長押しで機材は空き", one("SELECT status, current_task_id FROM equipment WHERE id = 2"),
      {"status": "idle", "current_task_id": None})
check("長押しでタスクは集合前に戻る", one("SELECT status FROM tasks WHERE id = ?", three)["status"], "todo")
check("長押しでメンバーも外れる", q("SELECT * FROM task_members WHERE task_id = ?", three), [])
check("長押しの取り消しを知らせる", sent[-1][1], ["集合待ちを取り消しました", "3人で搬入"])

sent.clear()
app._handle_data("pi-b", {"device_id": "pi-b", "event": "long_press", "button": "ok"})
check("集合待ちでなければ長押しは何もしない", (one("SELECT status FROM equipment WHERE id = 2")["status"], sent),
      ("idle", []))

# ---------------------------------------------------------------- 作業者の削除
client.post(f"/api/tasks/{three}/start", json={"nfc_tag_id": "T3", "module_id": "MOD-B"})
client.post("/workers/3/delete")
check("リーダーを消すと機材は空き", one("SELECT status FROM equipment WHERE id = 2")["status"], "idle")
check("タスクも集合前に戻る", one("SELECT status FROM tasks WHERE id = ?", three)["status"], "todo")

# 作業中の複数人タスクを、メンバーが別の機材でタッチして終わらせる（終了タッチ忘れ）
client.post(f"/api/tasks/{pair}/start", json={"nfc_tag_id": "T1", "module_id": "MOD-A"})
app._join_gathering(1, 4)
check("2人で作業中", one("SELECT status FROM tasks WHERE id = ?", pair)["status"], "in_progress")
answers[:] = [True]
seen = []
app._show_menu = lambda *a, **k: seen.append("menu")
touch("T4", "pi-b", "MOD-B")
check("メンバーにも前の作業の終了を尋ねる", sent[0][1][:3], ["前の作業を終了しますか？", "旋盤A が", "使用中のままです"])
check("終了すると全員に実績", sorted(l["worker_id"] for l in q("SELECT worker_id FROM work_logs WHERE task_id = ?", pair)),
      [1, 4])

print()
print("すべて成功" if ok else "失敗あり")
sys.exit(0 if ok else 1)
