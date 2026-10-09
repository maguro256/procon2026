# -*- coding: utf-8 -*-
"""
機材名の変更（/equipment/<id>/update、新UIの機材の編集欄）の確認。

  1. 名前が変わり、他の項目（状態・モジュールID・機材コード）はそのまま
  2. 空の名前は断る
  3. モジュールIDの付け替えも同じフォームでできる（/equipment/bind と同じ処理）
  4. 待機中のモジュールには新しい名前の画面を送り直す。使用中・操作中のモジュールには送らない

    python Test\\test_rename_equipment.py
"""
import os, sys, tempfile
from pathlib import Path

sys.path.insert(0, os.getcwd())
import db

tmp = Path(tempfile.mkdtemp()) / "t.db"
db.DB_PATH = tmp
db.init_db(seed=True)   # 機材1は使用中、機材2は空き（hostname=pi01）、機材3は停止

import app

client = app.app.test_client()
ok = True


def check(label, got, want):
    global ok
    mark = "OK " if got == want else "NG "
    if got != want:
        ok = False
    print(f"{mark}{label}: {got!r}" + ("" if got == want else f"  期待 {want!r}"))


def eq(eid):
    c = db.get_db()
    row = dict(c.execute("SELECT name, status, hostname, module_id FROM equipment WHERE id = ?", (eid,)).fetchone())
    c.close()
    return row


sent = []
app.send_cmd = lambda device_id, cmd, **f: sent.append((device_id, cmd, f.get("lines"))) or True

# 1. 名前だけ変える（機材2は空き。オンラインにしておく）
c = db.get_db()
c.execute("UPDATE equipment SET online = 1 WHERE id = 2")
c.commit()
c.close()
before = eq(2)
res = client.post("/equipment/2/update", data={"name": "旋盤 #3", "hostname": "pi01"}, follow_redirects=True)
after = eq(2)
check("名前が変わる", after["name"], "旋盤 #3")
check("他の項目はそのまま", {k: after[k] for k in ("status", "hostname", "module_id")},
      {k: before[k] for k in ("status", "hostname", "module_id")})
check("変更を知らせる", "「旋盤 #2」から「旋盤 #3」に変更しました" in res.get_data(as_text=True), True)
check("待機中のモジュールに新しい名前の画面を送る",
      any(d == "pi01" and cmd == "display" and lines and lines[0] == "旋盤 #3" for d, cmd, lines in sent), True)

# 2. 空の名前は断る
client.post("/equipment/2/update", data={"name": "  ", "hostname": "pi01"})
check("空の名前は断る", eq(2)["name"], "旋盤 #3")

# 3. モジュールIDの付け替えも同じフォームで
client.post("/equipment/3/update", data={"name": "プレス機 #1", "hostname": "pi09"})
check("モジュールIDを付け替えられる", eq(3)["hostname"], "pi09")
client.post("/equipment/3/update", data={"name": "プレス機 #1", "hostname": ""})
check("空にすると紐付けを外す", eq(3)["hostname"], None)

# 4. 使用中・操作中のモジュールには送らない
c = db.get_db()
c.execute("UPDATE equipment SET hostname = 'pi-a', online = 1 WHERE id = 1")
c.commit()
c.close()
sent.clear()
client.post("/equipment/1/update", data={"name": "レーザー加工機 #9", "hostname": "pi-a"})
check("使用中: 名前は変わる", eq(1)["name"], "レーザー加工機 #9")
check("使用中: 画面は送り直さない", sent, [])

sent.clear()
with app._touch_busy_lock:
    app._touch_busy["pi01"] = 0
client.post("/equipment/2/update", data={"name": "旋盤 #4", "hostname": "pi01"})
with app._touch_busy_lock:
    app._touch_busy.pop("pi01", None)
check("操作中: 名前は変わる", eq(2)["name"], "旋盤 #4")
check("操作中: 画面は送り直さない", sent, [])

check("存在しない機材は 302 で一覧へ", client.post("/equipment/999/update", data={"name": "x"}).status_code, 302)

# 新UIの編集欄に機材名の欄がある
client.get("/ui/new")
html = client.get("/equipment").get_data(as_text=True)
check("新UIの編集欄に機材名の欄", 'action="/equipment/2/update"' in html and 'name="name" value="旋盤 #4"' in html, True)

print("\nすべてOK" if ok else "\nNG があります")
sys.exit(0 if ok else 1)
