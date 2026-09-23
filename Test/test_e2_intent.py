# -*- coding: utf-8 -*-
"""
E-2 後半（Gemma 3 での意図分析）の検証。DBは一時ファイル、Ollama も実機も要らない。

確認するのは3層:
  1. `_sanitize()` が Gemma の出まかせを既定値に丸めること（ここが本体）
  2. Ollama が落ちているときに `analyze()` が fallback に落ち、例外を出さないこと
  3. `POST /api/voice` が意図分析の結果を tasks に入れること

**3 は HTTP 呼び出しだけを差し替えて、プロンプト組み立て → 検証 → INSERT の経路は
本物を通している。** D-6 で「テストが組み立て側を素通りして TypeError を見逃した」
のを踏んでいるため（TODO.md の E-2 / D-6）。

    python Test/test_e2_intent.py
"""
import io
import os
import sys
import tempfile
from datetime import datetime, timedelta
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
import permissions as perms
from voice import intent

app.app.config["TESTING"] = True
client = app.app.test_client()

conn = db.get_db()
conn.execute("INSERT INTO equipment (id,name,module_id,hostname,status,online)"
             " VALUES (1,'旋盤A','MOD-A-01','pi01','idle',1)")
conn.execute("INSERT INTO workers (id,name,years_of_service,role,permissions,nfc_tag_id)"
             " VALUES (1,'田中',12,'supervisor','forklift','TAG-1')")
conn.commit()
conn.close()

TODAY = datetime(2026, 9, 16)          # 水曜。相対日付の基準を固定する
TOMORROW = (TODAY + timedelta(days=1)).strftime("%Y-%m-%d")

# /api/voice を通す統合検査だけは、基準日を固定できない（app.py が本物の analyze() を
# 実時間の今日で呼ぶため）。固定日を使うと、その日を過ぎた瞬間に _clean_deadline が
# 「過去の期限」として正しく捨て、テストだけが落ちる時限爆弾になる。実際そうなっていた。
API_TOMORROW = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

ok = True


def check(label, got, want):
    global ok
    if got != want:
        ok = False
        print("NG {}: {!r}  期待 {!r}".format(label, got, want))
    else:
        print("OK {}: {!r}".format(label, got))


def quiet(fn, *a, **kw):
    """_clean_* は捨てた理由を print するので、テスト出力に混ぜない"""
    held, sys.stdout = sys.stdout, io.StringIO()
    try:
        return fn(*a, **kw)
    finally:
        sys.stdout = held


# ---------------------------------------------------------------- 1. 丸め込み
print("\n-- _sanitize: Gemma の出まかせを丸める --")

full = quiet(intent._sanitize, {
    "title": "製品Cの外形加工", "priority": "high", "difficulty": 4,
    "quantity": 20, "deadline": TOMORROW, "required_permissions": ["forklift"],
}, "製品Cの外形加工を20個、明日までにお願いします", TODAY)
check("素直な出力はそのまま通る",
      (full["title"], full["priority"], full["difficulty"], full["quantity"],
       full["deadline"], full["required_permissions"]),
      ("製品Cの外形加工", "high", 4, 20, TOMORROW, ["forklift"]))
check("description は文字起こし全文", full["description"],
      "製品Cの外形加工を20個、明日までにお願いします")

junk = quiet(intent._sanitize, {
    "title": "", "priority": "大至急", "difficulty": 99, "quantity": -3,
    "deadline": "あした", "required_permissions": ["forklift", "nuclear", "forklift"],
}, "旋盤の切粉清掃をお願いします。急ぎです", TODAY)
check("空のタスク名は1文目に落ちる", junk["title"], "旋盤の切粉清掃をお願いします")
check("範囲外の優先度は normal", junk["priority"], "normal")
check("難易度は1〜5に丸める", junk["difficulty"], 5)
check("数量は1以上に丸める", junk["quantity"], 1)
check("日付でない期限は捨てる", junk["deadline"], None)
check("未知の権限コードは落とし、重複も潰す",
      junk["required_permissions"], ["forklift"])

missing = quiet(intent._sanitize, {}, "治具Bの芯出し", TODAY)
check("キーが丸ごと無くても既定値で埋まる",
      (missing["title"], missing["priority"], missing["difficulty"],
       missing["quantity"], missing["deadline"], missing["required_permissions"]),
      ("治具Bの芯出し", "normal", 3, 1, None, []))

long_title = quiet(intent._sanitize, {"title": "あ" * 60}, "あ", TODAY)
check("長すぎるタスク名は切り詰める",
      (len(long_title["title"]), long_title["title"].endswith("…")),
      (intent.TITLE_MAX + 1, True))

# 権限の取りこぼし対策。付け忘れ（無資格者が着手できてしまう）だけは機械的に拾う
print("\n-- 権限の底上げ: 付け忘れだけを拾う --")

floored = quiet(intent._sanitize, {"title": "フレームの溶接", "required_permissions": []},
                "フレームの溶接をお願いします。10本です", TODAY)
check("Gemma が落とした welding を文字起こしから補う",
      floored["required_permissions"], ["welding"])

dup = quiet(intent._sanitize,
            {"title": "鋼材の運搬", "required_permissions": ["forklift"]},
            "フォークリフトで鋼材を運んでください", TODAY)
check("既に付いていれば二重にしない", dup["required_permissions"], ["forklift"])

check("機材名どまりの語では底上げしない",
      quiet(intent._permission_floor, "プレス機の切粉清掃をお願いします"), [])
check("制御盤の配線でも底上げしない（electric は曖昧なので入れていない）",
      quiet(intent._permission_floor, "制御盤の配線を直してください"), [])
check("玉掛けは crane として拾う",
      quiet(intent._permission_floor, "玉掛けをお願いします"), ["crane"])

check("Ollama が居なくても溶接は無資格者に開放しない",
      quiet(intent.fallback, "フレームの溶接をお願いします")["required_permissions"],
      ["welding"])

past = quiet(intent._clean_deadline, "2020-01-01", TODAY)
check("過去の期限は捨てる", past, None)
far = quiet(intent._clean_deadline,
            (TODAY + timedelta(days=intent.DEADLINE_MAX_DAYS + 1)).strftime("%Y-%m-%d"),
            TODAY)
check("遠すぎる期限は捨てる", far, None)
check("今日は期限として通る", quiet(intent._clean_deadline,
                                   TODAY.strftime("%Y-%m-%d"), TODAY),
      TODAY.strftime("%Y-%m-%d"))

# ---------------------------------------------------- 2. Ollama が落ちている
print("\n-- Ollama が落ちていても登録は成立する --")

real_host = intent.HOST
intent.HOST = "http://127.0.0.1:9"      # 何も待ち受けていないポート
dead = quiet(intent.analyze, "旋盤の切粉清掃を至急お願いします。振れが出ています")
intent.HOST = real_host
check("繋がらなければ fallback に落ちる", dead["source"], "fallback")
check("fallback でもタスク名は取れる", dead["title"], "旋盤の切粉清掃を至急お願いします")
check("fallback は既定値で埋める",
      (dead["priority"], dead["difficulty"], dead["quantity"], dead["deadline"]),
      ("normal", 3, 1, None))

empty = quiet(intent.analyze, "")
check("空文字でも例外にならない", (empty["title"], empty["source"]), ("", "fallback"))

# ----------------------------------------- 3. /api/voice の経路を本物で通す
print("\n-- POST /api/voice: 文字起こし → 意図分析 → INSERT --")

SAID = "フォークリフトで資材置き場から鋼材を20本、明日までに運んでください"

# 差し替えるのは「Whisper を呼ぶところ」と「Ollama に HTTP を投げるところ」だけ。
# プロンプト組み立て・_sanitize・INSERT は本物が走る
app._transcribe = lambda path: SAID
real_post = intent.requests.post


class _FakeRes(object):
    ok = True

    def json(self):
        return {"message": {"content": (
            '{"title": "資材置き場から鋼材の運搬", "priority": "high", "difficulty": 2,'
            ' "quantity": 20, "deadline": "%s",'
            ' "required_permissions": ["forklift"]}' % API_TOMORROW)}}


sent = {}


def _fake_post(url, json=None, timeout=None):
    sent["url"] = url
    sent["body"] = json
    return _FakeRes()


intent.requests.post = _fake_post
res = client.post("/api/voice?tag_id=TAG-1&module_id=MOD-A-01", data=b"x" * 2000,
                  content_type="audio/wav")
intent.requests.post = real_post

body = res.get_json()
check("登録が成立する", (body["ok"], body["source"]), (True, "gemma"))
check("Ollama に投げた先", sent["url"].endswith("/api/chat"), True)
check("構造化出力のスキーマを渡している",
      sent["body"]["format"]["required"], intent.SCHEMA["required"])
check("Whisper と VRAM を食い合わない keep_alive",
      sent["body"]["keep_alive"], intent.KEEP_ALIVE)
check("プロンプトに今日の日付が入っている",
      datetime.now().strftime("%Y-%m-%d") in sent["body"]["messages"][0]["content"], True)
check("文字起こしの全文を最後に渡している",
      sent["body"]["messages"][-1]["content"], SAID)
# few-shot を会話の往復で渡していたときは、期限を言っていない指示に例文の日付が
# 写ってきた。system + user の2通だけになっていることを固定しておく
check("few-shot は会話の往復ではなくシステムプロンプトの中",
      [m["role"] for m in sent["body"]["messages"]], ["system", "user"])
check("例がシステムプロンプトに入っている",
      "旋盤の切粉清掃" in sent["body"]["messages"][0]["content"], True)

row = db.get_db().execute(
    "SELECT title, description, difficulty, priority, required_permissions,"
    " quantity, deadline, status FROM tasks WHERE id = ?", (body["task_id"],)).fetchone()
check("tasks に意図分析の結果が入る",
      (row["title"], row["difficulty"], row["priority"], row["required_permissions"],
       row["quantity"], row["deadline"], row["status"]),
      ("資材置き場から鋼材の運搬", 2, "high", "forklift", 20, API_TOMORROW, "todo"))
check("description は文字起こし全文", row["description"], SAID)

# 必要権限が入った以上、D-2 の絞り込みが効かないと意味がない
check("無資格者はこのタスクを着手できない",
      perms.missing({"role": "member", "permissions": ""},
                    {"required_permissions": row["required_permissions"]}),
      ["forklift"])
check("有資格者は着手できる",
      perms.missing({"role": "supervisor", "permissions": "forklift"},
                    {"required_permissions": row["required_permissions"]}),
      [])

# 聞き取れなかったときは登録しない（従来どおり）
app._transcribe = lambda path: None
res = client.post("/api/voice?tag_id=TAG-1&module_id=MOD-A-01", data=b"x" * 2000,
                  content_type="audio/wav")
check("聞き取れなければ登録しない", res.get_json()["ok"], False)

print("\n" + ("すべて期待通り" if ok else "失敗あり"))
sys.exit(0 if ok else 1)
