# -*- coding: utf-8 -*-
"""
文字起こし後 → 意図分析(Gemma) → タスク化 の通し確認（E-2 の後半だけ）。

音声と Whisper は通さず、**文字起こし済みのテキストを与えたことにして**
`POST /api/voice` を叩く。DBは一時ファイルなので gemmba.db は汚さない。

    python Test/check_intent_task.py
    python Test/check_intent_task.py "フレームの溶接をお願いします。10本です"

Ollama が起きていれば Gemma を通り（source=gemma）、居なければ
fallback に落ちる（source=fallback）。どちらを通ったかは必ず表示する。
"""
import os
import sys
import json
import time
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db

db.DB_PATH = Path(tempfile.mkdtemp()) / "check.db"
conn = db.get_db()
conn.executescript(db.SCHEMA)
conn.execute("INSERT INTO workers (id,name,years_of_service,nfc_tag_id) "
             "VALUES (1,'田中 太郎',5,'TAG-1')")
conn.commit()
conn.close()

import app
from voice import intent

arg = sys.argv[1] if len(sys.argv) > 1 else "明日までに旋盤で製品Aを削り出し"
# .wav を渡したときだけ本物の文字起こしを通す。それ以外は
# 「文字起こしは済んでいる」として、意図分析から先だけを見る
WAV = arg if arg.lower().endswith(".wav") else None
TEXT = None if WAV else arg

print("=" * 70)
if WAV:
    print(f"音声ファイル: {WAV}（文字起こしから通す）")
else:
    print("文字起こし結果（と仮定するテキスト）:")
    print(f"  「{TEXT}」")

ok, why = intent.available()
print(f"\n意図分析(Gemma): {'有効' if ok else '無効'} - {why}")
print(f"  model={intent.MODEL}  host={intent.HOST}")

if WAV:
    print(f"文字起こし: {os.environ.get('GEMMBA_STT_MODEL', 'large-v3')} / "
          f"{app.VOICE_PYTHON}")
    with open(WAV, "rb") as fp:
        audio = fp.read()
else:
    # Whisper は通さない。この1行が「音声認識は済んでいる」という仮定そのもの
    app._transcribe = lambda path: TEXT
    audio = b"\0" * 2000

print("\n" + "=" * 70)
print("POST /api/voice")
started = time.time()
client = app.app.test_client()
res = client.post("/api/voice?tag_id=TAG-1&module_id=MOD-A-01",
                  data=audio, content_type="audio/wav")
body = res.get_json()
print(f"  所要 {time.time() - started:.1f}秒")
print(f"  HTTP {res.status_code}")
print("  " + json.dumps(body, ensure_ascii=False, indent=2).replace("\n", "\n  "))

if not body.get("ok"):
    sys.exit(1)

print("\n" + "=" * 70)
print("tasks に入った行")
c = db.get_db()
row = c.execute("SELECT * FROM tasks WHERE id = ?", (body["task_id"],)).fetchone()
c.close()
LABELS = [
    ("title", "タスク名"), ("description", "補足（全文）"),
    ("priority", "優先度"), ("difficulty", "難易度"), ("quantity", "数量"),
    ("deadline", "期限"), ("required_permissions", "必要権限"),
    ("status", "状態"), ("equipment_id", "機材"),
]
for key, label in LABELS:
    print(f"  {label:<14} {row[key] if row[key] not in (None, '') else '-'}")

print("\n" + "=" * 70)
src = body.get("source")
print(f"通った経路: {src}")
if src != "gemma":
    print("  → Gemma は通っていない。Ollama を起動して gemma3:4b を入れると")
    print("     優先度・数量・期限・権限が文から読み取られる（今は既定値）。")
sys.exit(0)
