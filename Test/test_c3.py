# -*- coding: utf-8 -*-
"""C-3（誘導通知）の検証。DBは一時ファイル、MQTTは差し替えて実機なしで回す"""
import os, sys, tempfile, io
from pathlib import Path

sys.path.insert(0, os.getcwd())
import db
tmp = Path(tempfile.mkdtemp()) / "t.db"
db.DB_PATH = tmp
conn = db.get_db()
conn.executescript(db.SCHEMA)
conn.commit()
conn.close()

import app

conn = db.get_db()
conn.execute("INSERT INTO equipment (id,name,module_id,hostname,status,online) VALUES (1,'旋盤A','MOD-A-01','pi01','idle',1)")
conn.execute("INSERT INTO equipment (id,name,module_id,hostname,status,online) VALUES (2,'プレスB','MOD-B-01','pi02','idle',1)")
conn.execute("INSERT INTO equipment (id,name,module_id,hostname,status,online) VALUES (3,'検査台C','MOD-C-01','pi03','working',1)")
conn.execute("INSERT INTO equipment (id,name,module_id,hostname,status,online) VALUES (4,'溶接D','MOD-D-01',NULL,'idle',0)")
conn.commit()
conn.close()

EQ1 = {"id": 1, "name": "旋盤A", "status": "idle"}

def T(id, eq, pri, title):
    return {"id": id, "title": title, "priority": pri, "equipment_id": eq, "quantity": 1, "deadline": None}

def pick(tasks, has_candidates):
    c = db.get_db()
    try:
        t, target = app._pick_guidance(c, EQ1, tasks, has_candidates)
    finally:
        c.close()
    return (t["title"] if t else None), (target["name"] if target else None)

ok = True
def check(label, got, want):
    global ok
    mark = "OK " if got == want else "NG "
    if got != want: ok = False
    print(f"{mark}{label}: {got!r}" + ("" if got == want else f"  期待 {want!r}"))

# 1. 他機材に至急タスク → この機材に候補があっても誘導する
check("至急を他機材で検出",
      pick([T(1, None, "normal", "ここでできる作業"), T(2, 2, "urgent", "プレスB 緊急")], True),
      ("プレスB 緊急", "プレスB"))

# 2. 他機材のタスクが通常優先度 → この機材に候補があれば誘導しない
check("通常＋ここに候補あり",
      pick([T(1, None, "normal", "ここでできる作業"), T(2, 2, "normal", "プレスB 通常")], True),
      (None, None))

# 3. 同じでも、ここに候補が無ければ誘導する
check("通常＋ここに候補なし",
      pick([T(2, 2, "normal", "プレスB 通常")], False),
      ("プレスB 通常", "プレスB"))

# 4. 誘導先が使用中 → 無駄足なので誘導しない
check("誘導先が使用中",
      pick([T(3, 3, "urgent", "検査台C 緊急")], False),
      (None, None))

# 5. 優先度の高い順に選ぶ（登録順は urgent が後ろ）
check("優先度順に選ぶ",
      pick([T(1, 2, "high", "プレスB 高"), T(2, 4, "urgent", "溶接D 至急")], True),
      ("溶接D 至急", "溶接D"))

# 6. この機材のタスクは誘導対象にしない
check("自機材は対象外",
      pick([T(1, 1, "urgent", "旋盤A 緊急")], True),
      (None, None))

# --- 誘導の一連の流れ（confirm と通知を差し替えて観測する）
sent = []
app._notify_briefly = lambda d, m, lines, led, sec=6: sent.append((d, m, lines, led, sec))

app.request_confirm = lambda dev, text, **kw: (asked.append((dev, text, kw.get("lines"))), True)[1]
asked = []
worker = {"id": 1, "name": "山田"}
tasks = [T(1, None, "normal", "ここでできる作業"), T(2, 2, "urgent", "プレスB 緊急")]
r = app._guide_to_other_equipment("pi01", "MOD-A-01", worker, EQ1, tasks, True)
check("はい → 誘導した", r, True)
check("尋ねた文面", asked[0][1], "プレスB へ移動しますか？")
check("移動元へ通知", (sent[0][0], sent[0][3]), ("pi01", "guide"))
check("移動先へ予告", (sent[1][0], sent[1][1], sent[1][3]), ("pi02", "MOD-B-01", "guide"))
check("予告の本文", sent[1][2][0], "山田 さんが向かっています")

# いいえ → 誘導せず、この機材の通常フローへ戻る
sent.clear()
app.request_confirm = lambda dev, text, **kw: False
check("いいえ → 通常フロー", app._guide_to_other_equipment("pi01","MOD-A-01",worker,EQ1,tasks,True), False)
check("いいえなら通知しない", sent, [])

# 無応答（None）も同じ扱い
app.request_confirm = lambda dev, text, **kw: None
check("無応答 → 通常フロー", app._guide_to_other_equipment("pi01","MOD-A-01",worker,EQ1,tasks,True), False)

# hostname 未設定の機材へ誘導 → 予告は送らず落ちない
sent.clear()
app.request_confirm = lambda dev, text, **kw: True
check("hostname無しでも誘導できる",
      app._guide_to_other_equipment("pi01","MOD-A-01",worker,EQ1,[T(2,4,"urgent","溶接D 至急")], False), True)
check("予告は移動元だけ", len(sent), 1)

print("\n" + ("すべて成功" if ok else "失敗あり"))
sys.exit(0 if ok else 1)
