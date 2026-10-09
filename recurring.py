"""
recurring.py - 定期タスクの自動生成（要件「定期メンテナンス等の習慣的タスクの自動生成」）

毎週の点検のように同じ内容を繰り返すタスクを、管理画面で1回登録しておけば
予定日にタスク一覧へ1件ずつ立てる。app.py が起動時と一定間隔で generate() を呼ぶ。

決まりごと:
  - 1つの設定から作るのは、予定日ごとに1件まで。サーバーを止めていて予定日を
    いくつか飛ばしていても、まとめて何件も作らず、直近の予定日の分を1件だけ作る。
  - 前回作った分がまだ終わっていなければ、新しくは作らない。同じ点検が
    未完了のまま積み上がると、どれをやればよいか分からなくなるため。
  - 担当者を指定してある設定なら、作ったタスクも担当者指定（designated=1）にする。
"""
import calendar
from datetime import date, datetime, timedelta

FREQUENCIES = {"daily": "毎日", "weekly": "毎週", "monthly": "毎月"}
WEEKDAY_LABELS = "月火水木金土日"   # date.weekday() の 0〜6 と同じ並び


def parse_weekdays(text):
    """'0,3' → [0, 3]。範囲外や数字でないものは捨てる"""
    days = set()
    for part in str(text or "").split(","):
        part = part.strip()
        if part.isdigit() and 0 <= int(part) <= 6:
            days.add(int(part))
    return sorted(days)


def dump_weekdays(values):
    return ",".join(str(d) for d in parse_weekdays(",".join(str(v) for v in values)))


def _month_day(year, month, day):
    """31日指定でも、30日までの月は月末に寄せる"""
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def is_scheduled(rule, day):
    """day がこの設定の予定日か"""
    freq = rule["frequency"]
    if freq == "daily":
        return True
    if freq == "weekly":
        return day.weekday() in parse_weekdays(rule["weekdays"])
    if freq == "monthly":
        try:
            md = int(rule["month_day"] or 0)
        except (TypeError, ValueError):
            return False
        return md >= 1 and day == _month_day(day.year, day.month, md)
    return False


def latest_due(rule, today, after=None):
    """
    today 以前で一番新しい予定日。after（前回処理した予定日）より後のものに限る。
    無ければ None。遡るのは最大 31 日（毎月でも1回分は必ず見つかる）。
    """
    for back in range(0, 32):
        day = today - timedelta(days=back)
        if after is not None and day <= after:
            return None
        if is_scheduled(rule, day):
            return day
    return None


def next_due(rule, today):
    """today 以降で一番近い予定日（一覧に「次回」として出す）。無ければ None"""
    for ahead in range(0, 62):
        day = today + timedelta(days=ahead)
        if is_scheduled(rule, day):
            return day
    return None


def describe(rule):
    """一覧に出す頻度の説明。例: 毎週 月・木 / 毎月 15日 / 毎日"""
    freq = rule["frequency"]
    if freq == "weekly":
        days = parse_weekdays(rule["weekdays"])
        return "毎週 " + ("・".join(WEEKDAY_LABELS[d] for d in days) if days else "（曜日未設定）")
    if freq == "monthly":
        return f"毎月 {rule['month_day'] or '?'}日"
    return FREQUENCIES.get(freq, freq)


def _to_date(text):
    try:
        return datetime.strptime(text, "%Y-%m-%d").date() if text else None
    except ValueError:
        return None


def open_instance(conn, rule_id):
    """この設定から作った、まだ終わっていないタスク（無ければ None）"""
    return conn.execute(
        "SELECT * FROM tasks WHERE recurring_id = ? AND status != 'done' ORDER BY id DESC LIMIT 1",
        (rule_id,)).fetchone()


def create_task(conn, rule, due):
    """設定 rule から、予定日 due のタスクを1件作る。作ったタスクの id を返す"""
    deadline = due + timedelta(days=max(int(rule["deadline_days"] or 0), 0))
    worker_id = rule["worker_id"]
    cur = conn.execute(
        """INSERT INTO tasks (title, description, difficulty, priority, required_permissions,
                              quantity, deadline, equipment_id, assigned_worker_id, status,
                              designated, recurring_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (rule["title"], rule["description"] or "", rule["difficulty"], rule["priority"],
         rule["required_permissions"] or "", rule["quantity"] or 1, deadline.isoformat(),
         rule["equipment_id"], worker_id, "assigned" if worker_id else "todo",
         1 if worker_id else 0, rule["id"]))
    return cur.lastrowid


def generate(conn, today=None):
    """
    予定日を迎えた設定からタスクを作る（冪等。同じ日に何度呼んでも1件まで）。
    戻り値: 作ったタスクの [(task_id, title)]。commit は呼び出し側。
    """
    today = today or date.today()
    made = []
    rules = conn.execute("SELECT * FROM recurring_tasks WHERE active = 1 ORDER BY id").fetchall()
    for rule in rules:
        due = latest_due(rule, today, after=_to_date(rule["last_run"]))
        if due is None:
            continue
        # 前回分が残っていれば作らない。予定日は処理済みにして、終わった後の
        # 呼び出しで今回の分を後から作り直さないようにする（次の予定日を待つ）
        if open_instance(conn, rule["id"]) is None:
            task_id = create_task(conn, rule, due)
            made.append((task_id, rule["title"]))
        conn.execute("UPDATE recurring_tasks SET last_run = ? WHERE id = ?",
                     (due.isoformat(), rule["id"]))
    return made
