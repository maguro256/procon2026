"""
voice/intent.py - 文字起こしのテキスト → タスクのJSON（TODO.md の E-2 後半）

処理の分担のうち、最後の1段:

    ラズパイ: ボタンを押している間だけ録音 → PCへ送信
    PC      : Whisper で文字起こし（voice/stt.py）
              → ★ここ（Gemma 3 で意図分析）→ tasks に INSERT（app.py の /api/voice）

**このファイルは stt.py と違って app.py と同じ Python 3.8 で動く。** Ollama に
HTTP を投げるだけで重い依存が無いので、環境を分ける理由がない（stt.py を
`.venv-voice` に分けたのは faster-whisper が 3.9 以降しか入らないため）。
`requests` は app.py が既に使っている。

    ollama pull gemma3:4b
    python voice/intent.py "旋盤の切粉清掃を至急お願いします"
    python voice/intent.py --json "製品Cの外形加工を20個、明日までにお願いします"

**Gemma の出力をそのまま信用しない。** `_sanitize()` が全フィールドを検証して
既定値に丸める。Ollama が落ちていても `analyze()` は必ず正しい形の dict を返し、
その場合は意図分析の入る前と同じ挙動（1文目がタスク名）に落ちる。現場で登録が
丸ごと失敗するより、既定値のタスクが1件立つ方がましなので。
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta

import requests

# `python voice/intent.py` と単体で動かしたときも root の permissions.py を読めるよう
# にする。app.py から import されたときは既に通っているので触らない
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import permissions as perms  # noqa: E402

HOST = os.environ.get("GEMMBA_INTENT_HOST", "http://127.0.0.1:11434")
MODEL = os.environ.get("GEMMBA_INTENT_MODEL", "gemma3:4b")
TIMEOUT = float(os.environ.get("GEMMBA_INTENT_TIMEOUT", "60"))

# **Whisper と VRAM を食い合うので短くしてある。** large-v3 が約5GB、gemma3:4b が
# 約3.3GB で、3060Ti(8GB) には同時に載らない。/api/voice は文字起こし → 意図分析の
# 順に動くので同時には要らない。長く抱えると「次の録音の文字起こし」が CUDA を
# 取れず CPU に落ちて遅くなる（stt.py は落ちずにフォールバックする）。
KEEP_ALIVE = os.environ.get("GEMMBA_INTENT_KEEPALIVE", "60s")

TITLE_MAX = 40           # 一覧が崩れない長さ。app.py の表示幅に合わせてある
DEADLINE_MAX_DAYS = 365  # これより先の期限は聞き間違いとみなして捨てる

PRIORITIES = ("urgent", "high", "normal", "low")
PERMISSION_CODES = tuple(perms.PERMISSIONS)

# Ollama の構造化出力に渡す JSON スキーマ。enum を書いておくと、そもそも範囲外の
# 値が出てこない（それでも _sanitize は通す。モデルは約束を破ることがある）。
SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "priority": {"type": "string", "enum": list(PRIORITIES)},
        "difficulty": {"type": "integer", "minimum": 1, "maximum": 5},
        "quantity": {"type": "integer", "minimum": 1},
        "deadline": {"type": "string"},
        "required_permissions": {
            "type": "array",
            "items": {"type": "string", "enum": list(PERMISSION_CODES)},
        },
    },
    "required": ["title", "priority", "difficulty", "quantity",
                 "deadline", "required_permissions"],
}

# 想定している工場は stt.py の DEFAULT_PROMPT と同じ。
# **権限の判定基準はここが唯一の定義。** 迷ったら付ける側に倒す方針にしてある
# （付け過ぎ＝誰も着手できず画面で気づく。付け忘れ＝無資格者が着手できてしまう）。
SYSTEM_PROMPT = """\
あなたは工場の作業指示を聞き取って、タスク管理システムに登録する係です。
作業者が口頭で話した内容の文字起こしが与えられるので、JSONで構造化してください。

各フィールドの決め方:

title: 何をするかが分かる短い名詞句。機材名や対象物を含める。{title_max}字以内。
  敬語・依頼表現（「〜をお願いします」「〜してください」）は落とす。
  例: 「旋盤の切粉清掃をお願いします」→「旋盤の切粉清掃」

priority: 次の4つから選ぶ。
  urgent … 「至急」「大至急」「今すぐ」「今日中」など、今すぐ着手を求めている
  high   … 「なるべく早く」「優先して」「先にやって」など、急ぎだが即時ではない
  normal … 特に急ぎの言及が無い（既定。迷ったらこれ）
  low    … 「手が空いたら」「いつでもいい」「余裕があれば」

difficulty: 作業の難しさを1〜5の整数で見積もる。分からなければ3。
  1=誰でもできる清掃・運搬 / 3=標準的な機械加工 / 5=高精度な調整や検査

quantity: 明示された個数。「20個」なら20。個数の言及が無ければ1。
  「いくつか」「何個か」のような曖昧な表現は1にする。

deadline: 期限を YYYY-MM-DD で書く。今日は {today}（{weekday}曜日）。
  「明日まで」「今週中」「来週の月曜」などは、この日付を基準に実際の日付へ直す。
  **期限に当たる言葉が無ければ必ず空文字列 "" にする。**
  下の例に出てくる日付を、期限を言っていない指示に写さないこと。過去の日付は出さない。

required_permissions: その作業に必要な法定資格のコードを配列で。該当が無ければ []。
  forklift   … フォークリフトの運転を伴う（運搬、荷降ろし）
  crane      … クレーン操作・玉掛けを伴う（吊り上げ、移動）
  press      … プレス機を操作する（プレス機の清掃・点検だけなら不要）
  welding    … アーク溶接・溶接作業
  electric   … 低圧電気の取扱い（配線、制御盤の作業）
  inspection … 完成検査・出荷検査（工程内の外観確認だけなら不要）
  機材名が出ただけでは付けない。その機材を「操作する」内容のときに付ける。

**文字起こしに書かれていないことを補わないこと。** 聞き取れていない部分は既定値にする。

例:
{examples}
"""

# few-shot。実機で録った台本（TODO.md の E-2）をそのまま使っている。
#
# **会話の履歴（user/assistant の往復）としてではなく、システムプロンプトの中に
# 文章として置いている。** 往復で渡していたときは、期限を言っていない3文目に
# 2例目の日付がそのまま写ってきた（「治具Bの芯出し」に明日の日付が付いた）。
# 4bクラスだと直前のターンを履歴として引きずるので、例だと明示する方が安定する。
EXAMPLES = [
    ("旋盤の切粉清掃、至急をお願いします。",
     {"title": "旋盤の切粉清掃", "priority": "urgent", "difficulty": 1,
      "quantity": 1, "deadline": "", "required_permissions": []}),
    ("製品Cの外形加工20個、明日までにお願いします。",
     {"title": "製品Cの外形加工", "priority": "normal", "difficulty": 3,
      "quantity": 20, "deadline": "{tomorrow}", "required_permissions": []}),
    ("治具Bの芯出しをやり直してください。振れが出ています。",
     {"title": "治具Bの芯出しやり直し", "priority": "normal", "difficulty": 4,
      "quantity": 1, "deadline": "", "required_permissions": []}),
    ("フォークリフトで資材置き場から鋼材を運んでおいてください。",
     {"title": "資材置き場から鋼材の運搬", "priority": "normal", "difficulty": 2,
      "quantity": 1, "deadline": "", "required_permissions": ["forklift"]}),
]

WEEKDAYS = "月火水木金土日"

# 権限の取りこぼし対策。**付け忘れだけは機械的に拾う。**
# 付け過ぎ（誰も着手できない）は管理画面ですぐ気づけるが、付け忘れ（無資格者が
# 着手できてしまう）は誰も気づかない。非対称なので、危険な側だけ底上げする。
# 実際に 4b は「フレームの溶接をお願いします」から welding を落とした。
#
# **作業そのものを指す語だけを入れること。** 「プレス機」「配線」のように
# 機材名どまりの語を入れると、清掃や点検にまで資格が要ることになる。
PERMISSION_HINTS = (
    ("welding", re.compile("溶接")),
    ("forklift", re.compile("フォークリフト")),
    ("crane", re.compile("クレーン|玉掛")),
    ("inspection", re.compile("完成検査|出荷検査")),
)


def _permission_floor(text):
    """文字起こしに作業名が出ている権限。Gemma の結果とこれの和をとる"""
    return [code for code, pattern in PERMISSION_HINTS if pattern.search(text)]


def _build_messages(text, today):
    tomorrow = (today + timedelta(days=1)).strftime("%Y-%m-%d")
    examples = []
    for said, want in EXAMPLES:
        filled = dict(want, deadline=want["deadline"].format(tomorrow=tomorrow))
        examples.append("入力: {}\n出力: {}".format(
            said, json.dumps(filled, ensure_ascii=False)))
    system = SYSTEM_PROMPT.format(title_max=TITLE_MAX,
                                  today=today.strftime("%Y-%m-%d"),
                                  weekday=WEEKDAYS[today.weekday()],
                                  examples="\n".join(examples))
    return [{"role": "system", "content": system},
            {"role": "user", "content": text}]


def _ask_gemma(text, today):
    """Ollama に投げて生の dict を返す。失敗したら None（例外は投げない）"""
    body = {
        "model": MODEL,
        "messages": _build_messages(text, today),
        "format": SCHEMA,
        "stream": False,
        "keep_alive": KEEP_ALIVE,
        # 指示の構造化に創造性は要らない。同じ入力から毎回同じ結果が出る方が
        # デモでも検証でも扱いやすい
        "options": {"temperature": 0, "num_predict": 256},
    }
    try:
        res = requests.post(HOST.rstrip("/") + "/api/chat", json=body, timeout=TIMEOUT)
    except requests.RequestException as e:
        print("[intent] Ollama に繋がりません({}): {}".format(HOST, e))
        return None
    if not res.ok:
        print("[intent] Ollama がエラーを返しました: {} {}".format(
            res.status_code, res.text[:200]))
        return None
    try:
        content = res.json()["message"]["content"]
    except (ValueError, KeyError) as e:
        print("[intent] 応答の形が違います: {}".format(e))
        return None
    try:
        raw = json.loads(content)
    except ValueError:
        print("[intent] JSONとして読めません: {}".format(content[:200]))
        return None
    return raw if isinstance(raw, dict) else None


def _clean_title(value, fallback_text):
    """タスク名を整える。空なら文字起こしの1文目に落とす"""
    title = re.sub(r"\s+", " ", str(value or "")).strip()
    if not title:
        title = (fallback_text.split("。")[0] or fallback_text).strip()
    if len(title) > TITLE_MAX:
        title = title[:TITLE_MAX] + "…"
    return title


def _clean_int(value, default, low, high):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


def _clean_deadline(value, today):
    """YYYY-MM-DD だけ通す。過去・遠すぎる未来は聞き間違いとみなして捨てる"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        day = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        print("[intent] 期限の形式が違うので捨てます: {!r}".format(text))
        return None
    if day < today.date():
        print("[intent] 期限が過去なので捨てます: {}".format(text))
        return None
    if (day - today.date()).days > DEADLINE_MAX_DAYS:
        print("[intent] 期限が遠すぎるので捨てます: {}".format(text))
        return None
    return text


def _clean_permissions(value):
    """既知の権限コードだけ残す。知らないコードは黙って落とす"""
    if isinstance(value, str):
        value = perms.parse(value)
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for code in value:
        code = str(code).strip()
        if code in PERMISSION_CODES and code not in out:
            out.append(code)
    return out


def _sanitize(raw, text, today):
    """Gemma の出力を POST /api/tasks が受け取れる形に丸める"""
    granted = _clean_permissions(raw.get("required_permissions"))
    for code in _permission_floor(text):
        if code not in granted:
            print("[intent] 権限 {} を文字起こしから補いました".format(code))
            granted.append(code)
    return {
        "title": _clean_title(raw.get("title"), text),
        "description": text,   # 全文は必ず残す。何と言ったかの証跡になる
        "difficulty": _clean_int(raw.get("difficulty"), 3, 1, 5),
        "priority": (raw.get("priority") if raw.get("priority") in PRIORITIES
                     else "normal"),
        "quantity": _clean_int(raw.get("quantity"), 1, 1, 9999),
        "deadline": _clean_deadline(raw.get("deadline"), today),
        "required_permissions": granted,
    }


def fallback(text):
    """
    Ollama が使えないときの結果。意図分析を入れる前の /api/voice と同じ挙動で、
    1文目がタスク名・全文が補足・あとは既定値。

    **権限の底上げだけはここでも効かせる。** Gemma が居ないからといって
    「溶接」のタスクを無資格者に開放してよい理由にはならない。
    """
    return {
        "title": _clean_title(None, text),
        "description": text,
        "difficulty": 3,
        "priority": "normal",
        "quantity": 1,
        "deadline": None,
        "required_permissions": _permission_floor(text),
        "source": "fallback",
    }


def analyze(text, today=None):
    """
    文字起こしのテキストを POST /api/tasks の body の形にする。

    **必ず dict を返す。** Ollama が落ちていても例外にはせず fallback() に落ちる。
    どちらを通ったかは戻り値の "source" で分かる（"gemma" / "fallback"）。
    """
    text = (text or "").strip()
    if not text:
        return dict(fallback(""), title="")
    today = today or datetime.now()

    raw = _ask_gemma(text, today)
    if raw is None:
        return fallback(text)
    result = _sanitize(raw, text, today)
    result["source"] = "gemma"
    return result


def available():
    """Ollama が起きていて、使うモデルが入っているか。起動時の警告用"""
    try:
        res = requests.get(HOST.rstrip("/") + "/api/tags", timeout=3)
        names = [m.get("name", "") for m in res.json().get("models", [])]
    except (requests.RequestException, ValueError, AttributeError):
        return False, "Ollama に繋がりません（{}）".format(HOST)
    # "gemma3:4b" と "gemma3:4b-it-q4_K_M" のような表記ゆれを吸収する
    if not any(n == MODEL or n.startswith(MODEL + "-") or n.split(":")[0] == MODEL
               for n in names):
        return False, "モデル {} がありません（ollama pull {}）".format(MODEL, MODEL)
    return True, "{} / {}".format(MODEL, HOST)


def _main(argv):
    as_json = "--json" in argv
    words = [a for a in argv if not a.startswith("--")]
    if not words:
        print(__doc__)
        return 2
    text = " ".join(words)

    ok, why = available()
    if not ok:
        print("[intent] {}".format(why), file=sys.stderr)

    started = datetime.now()
    result = analyze(text)
    elapsed = (datetime.now() - started).total_seconds()

    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print("入力: {}".format(text))
    print("  経路      : {} ({:.1f}秒)".format(result["source"], elapsed))
    print("  タスク名  : {}".format(result["title"]))
    print("  優先度    : {}".format(result["priority"]))
    print("  難易度    : {}".format(result["difficulty"]))
    print("  数量      : {}".format(result["quantity"]))
    print("  期限      : {}".format(result["deadline"] or "（指定なし）"))
    print("  必要権限  : {}".format(
        "/".join(perms.labels(result["required_permissions"])) or "（なし）"))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
