# -*- coding: utf-8 -*-
"""
音声でのタスク登録で、聞き取った文に出てきた登録済みの機材をタスクに付けることの確認。
DBは一時ファイル。文字起こしと意図分析は差し替える（ここで見たいのは機材の選び方）。

  1. 「旋盤を掃除して」→ 旋盤のタスクになる
  2. 全角/半角・空白・長音の揺れ（「3Dプリンター」「３Ｄプリンタ」）でも当たる
  3. 同じ種類が2台あるとき、番号まで言えばその1台、番号が無ければ付けない
  4. 別々の機材が2つ出てきたら付けない。何も出てこなければ付けない
  5. 「レーザー加工機」と「加工機」のように名前が重なるときは長いほう

    python Test/test_voice_equipment.py
"""
import os, sys, tempfile
from pathlib import Path

sys.path.insert(0, os.getcwd())
import db

db.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
db.init_db(seed=False)
c = db.get_db()
c.executescript("""
    INSERT INTO equipment (id, name, module_id) VALUES
        (1, '旋盤', 'MOD-1'), (2, '3Dプリンタ', 'MOD-2'), (3, 'レーザー加工機', 'MOD-3'),
        (4, '加工機', 'MOD-4'), (5, 'プレス機 #1', 'MOD-5'), (6, 'プレス機 #2', 'MOD-6');
    INSERT INTO workers (id, name, nfc_tag_id) VALUES (1, '宮田', 'TAG-1');
""")
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


def pick(text):
    c = db.get_db()
    try:
        row = app._equipment_in_text(c, text)
        return row["name"] if row else None
    finally:
        c.close()


check("旋盤を掃除して", pick("旋盤を掃除してください。"), "旋盤")
check("3Dプリンター（長音つき）", pick("3Dプリンターでケースを出力して"), "3Dプリンタ")
check("３Ｄプリンタ（全角）", pick("３Ｄプリンタの点検をお願いします"), "3Dプリンタ")
check("レーザー加工機は加工機より優先", pick("レーザー加工機で切断して"), "レーザー加工機")
check("加工機だけなら加工機", pick("加工機の清掃"), "加工機")
check("プレス機 2号機", pick("プレス機2号機の金型交換"), "プレス機 #2")
check("プレス機 #1（空白なし）", pick("プレス機#1を点検して"), "プレス機 #1")
check("番号の無いプレス機は決められない", pick("プレス機の清掃をお願いします"), None)
check("プレス機1はプレス機12個に含めない", pick("プレス機12個を至急"), None)
check("別々の機材が2つ出たら付けない", pick("旋盤用の治具を3Dプリンタで出力"), None)
check("機材が出てこなければ付けない", pick("資材置場から搬入してください"), None)
check("空の文", pick(""), None)

# /api/voice を通して tasks.equipment_id に入ること
app._transcribe = lambda path: "旋盤の切粉清掃を至急お願いします"
app.intent.analyze = lambda text: {
    "title": "旋盤の切粉清掃", "description": "", "difficulty": 2, "priority": "urgent",
    "required_permissions": [], "quantity": 1, "deadline": None, "source": "test"}
body = app.app.test_client().post("/api/voice?tag_id=TAG-1&module_id=MOD-1", data=b"x" * 2000).get_json()
check("応答に機材名", body.get("equipment"), "旋盤")
c = db.get_db()
check("タスクの機材が旋盤", c.execute("SELECT equipment_id FROM tasks WHERE id = ?",
                                   (body["task_id"],)).fetchone()[0], 1)
c.close()

print("\nすべてOK" if ok else "\nNG があります")
sys.exit(0 if ok else 1)
