# -*- coding: utf-8 -*-
"""
本番設定の確認。DBは一時ファイル、MQTTも要らない。

    python Test/test_test_mode.py

  1. 環境変数なしでは Flask のデバッグモードもテストモードも OFF
  2. テストモード OFF の間は /test と /api/test/* が 404、サイドバーにも出ない
  3. サイドバーのボタンで ON にすると使える
  4. OFF に戻すと、起動中の仮想モジュールが止まってオフラインになる
  5. 戻り先に外部URLを渡されても飛ばない
"""
import os, sys, tempfile
from pathlib import Path

os.environ.pop("GEMMBA_DEBUG", None)
os.environ.pop("GEMMBA_TEST_PAGE", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
db.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
conn = db.get_db()
conn.executescript(db.SCHEMA)
conn.execute("INSERT INTO equipment (id,name,module_id,status) VALUES (1,'旋盤A','MOD-A','idle')")
conn.commit()
conn.close()

import app

client = app.app.test_client()
ok = True


def check(label, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"{'OK' if cond else 'NG'} {label} {detail}")


check("既定でデバッグモードは OFF", app.DEBUG is False)
check("既定でテストモードは OFF", app._test_mode["on"] is False)
check("/test は 404", client.get("/test").status_code == 404)
check("/api/test/state は 404", client.get("/api/test/state").status_code == 404)
check("仮想モジュールを起動できない",
      client.post("/api/test/modules", json={"device_id": "v1"}).status_code == 404)
page = client.get("/").get_data(as_text=True)
check("サイドバーに「テスト」が出ない", 'href="/test"' not in page)
check("切り替えボタンは出る", "テストモード OFF" in page and "ON にする" in page)

res = client.post("/test-mode", data={"on": "1", "next": "/"})
check("ON にすると /test へ移る", res.status_code == 302 and res.headers["Location"].endswith("/test"))
check("/test が開ける", client.get("/test").status_code == 200)
check("サイドバーに「テスト」が出る", 'href="/test"' in client.get("/").get_data(as_text=True))

client.post("/api/test/modules", json={"device_id": "v1", "equipment_id": 1})
conn = db.get_db()
online = conn.execute("SELECT online FROM equipment WHERE id = 1").fetchone()[0]
conn.close()
check("仮想モジュールが繋がる", "v1" in app._virtual_modules and online == 1)

res = client.post("/test-mode", data={"on": "0", "next": "/test"})
conn = db.get_db()
online = conn.execute("SELECT online FROM equipment WHERE id = 1").fetchone()[0]
conn.close()
check("OFF にすると仮想モジュールは止まる", not app._virtual_modules and online == 0)
check("/test にいたら使用状況へ戻す", res.headers["Location"].endswith("/"))
check("OFF の後は /test が 404", client.get("/test").status_code == 404)

client.post("/test-mode", data={"on": "1"})
res = client.post("/test-mode", data={"on": "0", "next": "//evil.example/"})
check("外部URLへは飛ばない", "evil" not in res.headers["Location"])

print("\nすべて期待通り" if ok else "\n失敗あり")
sys.exit(0 if ok else 1)
