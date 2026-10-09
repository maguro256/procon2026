# -*- coding: utf-8 -*-
"""
定期タスクの自動生成の確認。導入先の評価「毎週やるタスクも毎回入れるのがめんどくさかった」への対応。

  - 予定日の判定（毎日・毎週の曜日・毎月の日。31日指定は月末に寄せる）
  - 予定日に1件だけ作る（同じ日に何度呼んでも増えない。飛ばした予定日をまとめて作らない）
  - 前回の分が終わっていなければ作らない
  - 担当者を指定した設定からは、担当者指定のタスクができる
  - 管理画面からの登録・一時停止・今すぐ追加・削除

    python Test\\test_recurring.py
"""
import os, sys, tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, os.getcwd())
import db

tmp = Path(tempfile.mkdtemp()) / "t.db"
db.DB_PATH = tmp
db.init_db(seed=True)

import app
import recurring

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


# --- 予定日の判定
weekly = {"frequency": "weekly", "weekdays": "0,3", "month_day": None}   # 月・木
check("毎週: 月曜は予定日", recurring.is_scheduled(weekly, date(2026, 10, 5)), True)
check("毎週: 火曜は予定日でない", recurring.is_scheduled(weekly, date(2026, 10, 6)), False)
check("毎週: 説明", recurring.describe(weekly), "毎週 月・木")
monthly = {"frequency": "monthly", "weekdays": "", "month_day": 31}
check("毎月31日: 2月は28日に寄せる", recurring.is_scheduled(monthly, date(2027, 2, 28)), True)
check("毎月31日: 10月31日", recurring.is_scheduled(monthly, date(2026, 10, 31)), True)
check("毎月31日: 10月30日は違う", recurring.is_scheduled(monthly, date(2026, 10, 30)), False)
check("毎日", recurring.is_scheduled({"frequency": "daily"}, date(2026, 10, 7)), True)
check("直近の予定日（木曜から見た月曜）",
      recurring.latest_due(weekly, date(2026, 10, 7), after=date(2026, 10, 4)), date(2026, 10, 5))
check("処理済みより前はさかのぼらない",
      recurring.latest_due(weekly, date(2026, 10, 7), after=date(2026, 10, 5)), None)
check("次回（火曜から見た木曜）", recurring.next_due(weekly, date(2026, 10, 6)), date(2026, 10, 8))

# --- generate: 1日1件・前回分が残っていれば作らない
c = db.get_db()
c.execute("""INSERT INTO recurring_tasks (title, frequency, weekdays, worker_id, equipment_id,
                                          deadline_days, last_run, priority)
             VALUES ('旋盤 週次点検', 'weekly', '0', 2, 2, 2, '2026-10-04', 'high')""")
c.commit()
made = recurring.generate(c, date(2026, 10, 5))   # 月曜
c.commit()
check("月曜: 1件作る", len(made), 1)
t = q("SELECT * FROM tasks WHERE recurring_id IS NOT NULL")[0]
check("担当者指定のタスクになる", (t["assigned_worker_id"], t["designated"], t["status"]), (2, 1, "assigned"))
check("期限は予定日 + 2日", t["deadline"], "2026-10-07")
check("機材・優先度を引き継ぐ", (t["equipment_id"], t["priority"]), (2, "high"))
check("同じ日にもう一度呼んでも増えない", recurring.generate(c, date(2026, 10, 5)), [])
# 前回分が未完了のまま翌週の月曜
check("前回分が未完了なら作らない", recurring.generate(c, date(2026, 10, 12)), [])
c.commit()
c.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (t["id"],))
c.commit()
# 終わった後に同じ週の中でもう一度呼んでも、飛ばした月曜の分は作らない（次の予定日を待つ）
check("終わった後も、処理済みの予定日の分は作らない", recurring.generate(c, date(2026, 10, 13)), [])
check("次の月曜には作る", len(recurring.generate(c, date(2026, 10, 19))), 1)
# 何週も止めていた後の起動では、直近の1件だけ
c.execute("UPDATE tasks SET status = 'done' WHERE recurring_id IS NOT NULL")
c.commit()
check("3週飛ばしても作るのは1件", len(recurring.generate(c, date(2026, 11, 9))), 1)
c.commit()
c.close()

# --- 管理画面: 今日が予定日なら登録したその場で作る
today = date.today()
res = client.post("/recurring/add", data={"title": "毎日の清掃", "frequency": "daily", "priority": "low"},
                  follow_redirects=True)
check("毎日: 登録した日の分がすぐできる",
      len(q("SELECT * FROM tasks WHERE title = '毎日の清掃'")), 1)
check("登録の知らせ", "今日の分をタスク一覧に追加しました" in res.get_data(as_text=True), True)

tomorrow = (today.weekday() + 1) % 7
client.post("/recurring/add", data={"title": "明日の曜日の点検", "frequency": "weekly",
                                    "weekdays": str(tomorrow)})
check("今日が予定日でなければ作らない", q("SELECT * FROM tasks WHERE title = '明日の曜日の点検'"), [])
yesterday = (today.weekday() - 1) % 7
client.post("/recurring/add", data={"title": "昨日の曜日の点検", "frequency": "weekly",
                                    "weekdays": str(yesterday)})
check("登録前の予定日の分をさかのぼって作らない",
      q("SELECT * FROM tasks WHERE title = '昨日の曜日の点検'"), [])

# 入力の誤り
res = client.post("/recurring/add", data={"title": "曜日なし", "frequency": "weekly"}, follow_redirects=True)
check("毎週で曜日なしは断る", "曜日を1つ以上" in res.get_data(as_text=True), True)
res = client.post("/recurring/add", data={"title": "日なし", "frequency": "monthly"}, follow_redirects=True)
check("毎月で日なしは断る", "日にち（1〜31）" in res.get_data(as_text=True), True)
check("断ったものは登録されない", q("SELECT * FROM recurring_tasks WHERE title IN ('曜日なし', '日なし')"), [])
res = client.post("/recurring/add", data={"title": "無資格の指定", "frequency": "daily", "worker_id": "3",
                                          "required_permissions": "welding"}, follow_redirects=True)
check("権限不足の人は担当者に指定できない", q("SELECT * FROM recurring_tasks WHERE title = '無資格の指定'"), [])

# 今すぐ追加（前回分が残っていれば作らない）
rule = q("SELECT * FROM recurring_tasks WHERE title = '明日の曜日の点検'")[0]
client.post(f"/recurring/{rule['id']}/run")
check("今すぐ追加: 1件できる", len(q("SELECT * FROM tasks WHERE title = '明日の曜日の点検'")), 1)
res = client.post(f"/recurring/{rule['id']}/run", follow_redirects=True)
check("今すぐ追加: 前回分が残っていれば作らない",
      len(q("SELECT * FROM tasks WHERE title = '明日の曜日の点検'")), 1)

# 一時停止中は作らない
daily = q("SELECT * FROM recurring_tasks WHERE title = '毎日の清掃'")[0]
client.post(f"/recurring/{daily['id']}/toggle")
check("一時停止", q("SELECT active FROM recurring_tasks WHERE id = ?", daily["id"])[0]["active"], 0)
c = db.get_db()
c.execute("UPDATE tasks SET status = 'done' WHERE title = '毎日の清掃'")
c.commit()
check("停止中は翌日になっても作らない",
      [m for m in recurring.generate(c, today + timedelta(days=1)) if m[1] == "毎日の清掃"], [])
c.close()

# 画面（旧UI・新UI）に一覧が出る
for ui in ("classic", "new"):
    client.set_cookie(app.UI_COOKIE, ui)
    html = client.get("/tasks").get_data(as_text=True)
    check(f"{ui}: 定期タスクの一覧", "毎日の清掃" in html and "定期タスクを登録" in html, True)
    check(f"{ui}: 自動で追加したタスクに「定期」", ">定期</span>" in html, True)
# 新UIの .lrow-edit は summary を CSS で隠す（行の「編集」ボタンで開く）。登録欄をそれで
# 包むと「定期タスクを登録」のボタンが出なくなる（実際にそうなっていた）
check("new: 登録欄は summary の見えるボタンで開く",
      'class="recurring-add"' in html and 'class="lrow-edit" id="add-recurring"' not in html, True)

# 削除しても作ったタスクは残る
client.post(f"/recurring/{rule['id']}/delete")
check("削除: 設定は消える", q("SELECT * FROM recurring_tasks WHERE id = ?", rule["id"]), [])
check("削除: 作ったタスクは残る（元の参照だけ外す）",
      [t["recurring_id"] for t in q("SELECT recurring_id FROM tasks WHERE title = '明日の曜日の点検'")], [None])

# 機材・作業者を消しても設定は残り、参照だけ外れる
c = db.get_db()
c.execute("""INSERT INTO recurring_tasks (title, frequency, worker_id, equipment_id)
             VALUES ('参照テスト', 'daily', 1, 3)""")
c.commit()
c.close()
client.post("/equipment/3/delete")
client.post("/workers/1/delete")
r = q("SELECT worker_id, equipment_id FROM recurring_tasks WHERE title = '参照テスト'")[0]
check("機材・作業者の削除で参照が外れる", (r["worker_id"], r["equipment_id"]), (None, None))

print("\n全件OK" if ok else "\n失敗あり")
sys.exit(0 if ok else 1)
