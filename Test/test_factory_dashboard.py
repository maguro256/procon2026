# -*- coding: utf-8 -*-
"""
工場掲示用ダッシュボード（/factorydashboard）の確認。

  1. どちらのUI（旧/新）で開いても同じ全画面のページ（サイドバー無し）
  2. 機材ごとに状態（使用中・空き・メンテ中・停止・接続切れ）と、使用中なら作業者・タスクが出る
  3. 見出しの件数：接続の切れた機材は「接続切れ」だけに数え、空きには数えない
  4. 要対応のタスクが下の帯に出る

    python Test\\test_factory_dashboard.py
"""
import os, sys, tempfile
from pathlib import Path

sys.path.insert(0, os.getcwd())
import db

db.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
db.init_db(seed=True)
# 機材1: 使用中（田中さん・製品A 組立）、機材2: 空き（pi01・接続中にする）、機材3: 停止
c = db.get_db()
c.execute("UPDATE equipment SET online = 1 WHERE id = 2")
c.execute("INSERT INTO equipment (name, module_id, hostname, status, online) VALUES ('検査台 #9', 'MOD-9', 'pi09', 'idle', 0)")
c.execute("INSERT INTO equipment (name, module_id, status) VALUES ('研削盤 #1', 'MOD-10', 'maintenance')")
c.commit()
c.close()

import app

ok = True


def check(label, got, want):
    global ok
    mark = "OK " if got == want else "NG "
    if got != want:
        ok = False
    print(f"{mark}{label}: {got!r}" + ("" if got == want else f"  期待 {want!r}"))


pages = {}
for ui in ("classic", "new"):
    cl = app.app.test_client()
    cl.get(f"/ui/{ui}")
    res = cl.get("/factorydashboard")
    check(f"[{ui}] 開ける", res.status_code, 200)
    pages[ui] = res.get_data(as_text=True)

html = pages["new"]
check("どちらのUIでも同じ画面", pages["classic"] == pages["new"] or
      pages["classic"].split('id="clockTime"')[0] == html.split('id="clockTime"')[0], True)
check("サイドバーが無い", 'class="sidebar"' not in html, True)
check("使用中の機材に作業者とタスク", "田中 太郎" in html and "製品A 組立" in html, True)
check("機材の状態がそれぞれ出る",
      all(x in html for x in ("t-working", "t-idle", "t-stopped", "t-maintenance", "t-offline")), True)
check("接続切れの機材の案内", "タッチできません" in html, True)
check("件数: 使用中1・空き1（接続切れの空きは数えない）・停止メンテ2・接続切れ1",
      all(x in html for x in ("使用中 1<", "空き 1<", "停止・メンテ 2<", "接続切れ 1<")), True)
check("要対応の帯に至急のタスク", "ticker-entry" in html and "製品A 組立" in html.split('id="ticker"')[1], True)
check("機材5台は3列×2段", "--cols: 3; --rows: 2" in html, True)

print("\nすべてOK" if ok else "\nNG があります")
sys.exit(0 if ok else 1)
