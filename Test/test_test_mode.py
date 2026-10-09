# -*- coding: utf-8 -*-
"""
本番設定と /test（仮想モジュール）の確認。DBは一時ファイル、MQTTも要らない。

    python Test/test_test_mode.py

  1. 環境変数なしでは Flask のデバッグモードは OFF
  2. /test と /api/test/* は切り替え無しでいつでも使える（URLを直接開く）
  3. サイドバーには「テスト」のリンクもテストモードのボタンも出さない（新旧どちらのUIでも）
  4. 以前の切り替え口（/test-mode）は無い
"""
import os, sys, tempfile
from pathlib import Path

os.environ.pop("GEMMBA_DEBUG", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
db.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
db.init_db(seed=False)
conn = db.get_db()
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
check("/test が開ける", client.get("/test").status_code == 200)
check("/api/test/state が使える", client.get("/api/test/state").status_code == 200)

client.post("/api/test/modules", json={"device_id": "v1", "equipment_id": 1})
conn = db.get_db()
online = conn.execute("SELECT online FROM equipment WHERE id = 1").fetchone()[0]
conn.close()
check("仮想モジュールが繋がる", "v1" in app._virtual_modules and online == 1)
client.delete("/api/test/modules/v1")
check("仮想モジュールを止められる", "v1" not in app._virtual_modules)

for ui in ("classic", "new"):
    cl = app.app.test_client()
    cl.get(f"/ui/{ui}")
    page = cl.get("/").get_data(as_text=True)
    check(f"[{ui}] サイドバーに「テスト」のリンクが無い", 'href="/test"' not in page)
    check(f"[{ui}] テストモードのボタンが無い", "テストモード" not in page)

check("/test-mode は無い", client.post("/test-mode", data={"on": "1"}).status_code == 404)

print("\nすべて期待通り" if ok else "\n失敗あり")
sys.exit(0 if ok else 1)
