# -*- coding: utf-8 -*-
"""prompts.csv の正解と、意図分析の結果（voice/intent.py の analyze() の戻り値）を比べる。

各欄の判定:
  title       : title_keywords の語がすべて含まれる（| = かつ、/ = どれか）。全角半角・空白は無視
  priority    : 一致
  quantity    : 一致
  deadline    : 録音日（基準日）から解決した日付と一致。正解が空なら結果も空
  permissions : 集合として一致
  all         : 上の5つすべて正しい（＝そのまま登録してよいタスク）
"""
import csv, os, unicodedata
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
FIELDS = ("title", "priority", "quantity", "deadline", "permissions")


def load_prompts(path=os.path.join(HERE, "prompts.csv")):
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def norm(s):
    return unicodedata.normalize("NFKC", s or "").replace(" ", "").lower()


def resolve_deadline(code, ref):
    """prompts.csv の deadline 欄を、基準日 ref（date）から実際の日付にする"""
    if not code:
        return None
    if code == "today":
        return ref
    if code.startswith("+"):
        return ref + timedelta(days=int(code[1:]))
    if code == "fri":  # 次に来る金曜（当日が金曜なら当日）
        return ref + timedelta(days=(4 - ref.weekday()) % 7)
    if code == "next_mon":  # 来週の月曜
        return ref + timedelta(days=7 - ref.weekday())
    if code.startswith("day:"):
        d = int(code[4:])
        cand = ref.replace(day=d)
        if cand < ref:  # 過ぎていれば翌月
            y, m = (ref.year + 1, 1) if ref.month == 12 else (ref.year, ref.month + 1)
            cand = date(y, m, d)
        return cand
    raise ValueError(code)


def judge(row, result, ref):
    t = norm(result.get("title"))
    ok = {}
    ok["title"] = all(any(norm(alt) in t for alt in group.split("/"))
                      for group in row["title_keywords"].split("|"))
    ok["priority"] = result.get("priority") == row["priority"]
    ok["quantity"] = int(result.get("quantity") or 0) == int(row["quantity"])
    want = resolve_deadline(row["deadline"], ref)
    got = result.get("deadline") or None
    ok["deadline"] = (want is None and got is None) or (want is not None and got == want.isoformat())
    want_p = set(filter(None, row["permissions"].split(" ")))
    ok["permissions"] = set(result.get("required_permissions") or []) == want_p
    ok["all"] = all(ok[f] for f in FIELDS)
    return ok


def summarize(judged):
    """judged: judge() の戻り値のリスト → 欄ごとの正解率"""
    n = len(judged)
    return {f: sum(j[f] for j in judged) / n for f in FIELDS + ("all",)}
