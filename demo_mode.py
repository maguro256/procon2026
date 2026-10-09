# -*- coding: utf-8 -*-
"""
demo_mode.py - デモモード（管理画面サイドバーの「開始する」「解除する」）

**今いる作業者・機材・モジュールの紐付けには一切触らない。** その上に、
  - それぞれの機材に合ったタスク（名前から旋盤・プレス・溶接などを判断する）
  - 作業者ごとの過去2週間ぶんの実績（work_logs）と、その実績の元になった完了済みタスク
を足すだけ。足したタスクには tasks.demo = 1 の印を付け、解除するとその印の付いた
タスクと、それに紐付く実績だけを消す。デモ中に登録した本物の作業者・機材・タスクは残る。

実績は勤続年数と、人ごとの得意・不得意で所要時間を変えてあるので、AI割当
（WariAthena）がその差を学習して人を選び分けるところを見せられる。
"""
import random
from datetime import datetime, timedelta

import permissions as perms

FMT = "%Y-%m-%d %H:%M:%S"

# 1個あたりの標準的な所要時間（秒）。demo_reset.py / seed_training.py と同じ値
BASE_SECONDS = {1: 60, 2: 120, 3: 240, 4: 420, 5: 600}
# 勤続年数ごとの「標準に対する時間の倍率」。難易度1〜5の順（demo_reset.py と同じ考え方）
PROFILES = [
    (8.0, [1.05, 1.00, 0.85, 0.75, 0.70]),   # ベテラン: 難しいほど速い
    (3.0, [1.00, 1.00, 1.00, 1.00, 1.00]),   # 中堅
    (0.0, [0.90, 1.00, 1.25, 1.60, 2.00]),   # 若手: 難しくなるほど急に遅い
]

HISTORY_DAYS = 14          # 実績を作る期間（日）
LOGS_PER_WORKER = (6, 10)  # 1人あたりの実績の件数（この範囲で決める）

# 機材の名前に含まれる言葉 → その機材でやる仕事。上から順に見て最初に当たったもの。
# (タイトル, 補足, 難易度, 優先度, 数量, 期限までの日数, 必要権限)
# タイトルの {eq} は機材名に置き換わる
CATALOG = [
    (("旋盤", "lathe"), [
        ("{eq} 段取り替え", "次ロット用に治具を交換", 2, "high", 1, 1, []),
        ("製品A 旋削", "図面 A-110。1個あたり約2分", 2, "normal", 30, 3, []),
        ("シャフト 外径仕上げ", "公差 ±0.02", 4, "high", 12, 2, []),
        ("ブッシュ 内径加工", "図面 B-207", 3, "normal", 20, 4, []),
    ]),
    (("プレス", "press"), [
        ("製品B 追加注文プレス", "急ぎの追加注文分", 3, "urgent", 8, 0, ["press"]),
        ("ブラケット 曲げ加工", "金型 BK-2 を使用", 3, "normal", 40, 3, ["press"]),
        ("{eq} 金型交換", "次ロットの金型に交換", 3, "high", 1, 1, ["press"]),
    ]),
    (("溶接", "weld"), [
        ("フレーム溶接", "治具Fに固定して4辺を溶接", 4, "high", 12, 2, ["welding"]),
        ("架台 補修溶接", "割れ箇所の補修", 3, "normal", 3, 4, ["welding"]),
        ("ステー 仮付け", "本溶接の前の仮付け", 2, "normal", 20, 3, ["welding"]),
    ]),
    (("検査", "inspect"), [
        ("試作E 初品確認", "寸法測定して記録票に記入", 5, "high", 2, 1, ["inspection"]),
        ("製品A 抜き取り検査", "ロットから5個抜き取り", 2, "normal", 5, 2, ["inspection"]),
        ("出荷前 外観検査", "キズ・バリの確認", 1, "normal", 30, 3, ["inspection"]),
    ]),
    (("レーザー", "laser"), [
        ("製品C 外形切断", "図面 C-204 rev.3", 4, "urgent", 20, 1, []),
        ("板金 穴あけ", "t2.3 SPCC", 2, "normal", 50, 4, []),
        ("銘板 刻印", "ロット番号を刻印", 1, "low", 30, 5, []),
    ]),
    (("フライス", "マシニング", "mill", "machining"), [
        ("ケース ポケット加工", "図面 M-301", 4, "high", 10, 2, []),
        ("プレート 平面出し", "面粗さ Ra1.6", 2, "normal", 20, 3, []),
    ]),
    (("研削", "研磨", "grind"), [
        ("ピン 円筒研削", "公差 ±0.005", 5, "high", 15, 2, []),
        ("プレート 平面研削", "", 3, "normal", 20, 4, []),
    ]),
    (("塗装", "paint"), [
        ("カバー 塗装", "下塗り＋上塗り", 3, "normal", 25, 3, []),
        ("補修 タッチアップ", "", 1, "low", 10, 5, []),
    ]),
    (("組立", "assembly"), [
        ("製品A 組立", "治具Aを使用", 2, "normal", 30, 3, []),
        ("ユニット 最終組立", "トルク管理あり", 4, "high", 6, 2, []),
    ]),
]
# どれにも当たらない機材の仕事
GENERIC = [
    ("{eq} で部品加工", "", 3, "normal", 15, 3, []),
    ("{eq} 定期点検", "注油・清掃・各部点検", 1, "low", 1, 5, ["electric"]),
]
# 機材を使わない仕事（運搬・事務など）
NO_EQUIPMENT = [
    ("資材置場からの搬入", "パレット3枚。所定位置へ", 1, "high", 3, 0, ["forklift"]),
    ("月次 棚卸し補助", "在庫を数えて記録", 1, "low", 1, 6, []),
]


def is_active(conn) -> bool:
    return conn.execute("SELECT 1 FROM tasks WHERE demo = 1 LIMIT 1").fetchone() is not None


def demo_counts(conn) -> dict:
    """解除したときに消えるものの件数（確認ダイアログ用）"""
    row = conn.execute("""
        SELECT (SELECT COUNT(*) FROM tasks WHERE demo = 1) AS tasks,
               (SELECT COUNT(*) FROM work_logs WHERE task_id IN (SELECT id FROM tasks WHERE demo = 1)) AS logs
    """).fetchone()
    return {"tasks": row[0], "logs": row[1]}


def jobs_for(equipment_name):
    """機材名からその機材でやる仕事の一覧を選ぶ"""
    lowered = (equipment_name or "").lower()
    for words, jobs in CATALOG:
        if any(w.lower() in lowered for w in words):
            return jobs
    return GENERIC


def _profile(years):
    for floor, mult in PROFILES:
        if (years or 0) >= floor:
            return mult
    return PROFILES[-1][1]


def _insert_task(conn, job, eq, now, **extra):
    title, desc, diff, prio, qty, days, req = job
    values = {
        "title": title.format(eq=eq["name"] if eq else ""),
        "description": desc, "difficulty": diff, "priority": prio,
        "required_permissions": perms.dump(req), "quantity": qty,
        "deadline": (now + timedelta(days=days)).strftime("%Y-%m-%d"),
        "status": "todo", "equipment_id": eq["id"] if eq else None, "demo": 1,
    }
    values.update(extra)
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    return conn.execute(f"INSERT INTO tasks ({cols}) VALUES ({marks})", list(values.values())).lastrowid


def start(conn, now=None, rng=None):
    """
    デモ用のタスクと実績を足す。commit は呼び出し側。
    戻り値: {"tasks": 足した未完了タスク数, "logs": 足した実績数}
    """
    now = now or datetime.now()
    rng = rng or random.Random()
    equipment = [dict(r) for r in conn.execute("SELECT id, name FROM equipment ORDER BY id")]
    workers = [dict(r) for r in conn.execute("SELECT * FROM workers ORDER BY id")]

    # ---- 未完了のタスク（これから現場でやるもの）。機材ごとに2〜3件。
    # 同じ名前の未完了タスクが既にあれば足さない（一覧に同じ仕事が2行並ばないように）
    taken = {r[0] for r in conn.execute("SELECT title FROM tasks WHERE status != 'done'")}
    open_n = 0
    pool = []          # 実績づくりに使う (仕事, 機材)

    def add_open(job, eq):
        nonlocal open_n
        title = job[0].format(eq=eq["name"] if eq else "")
        if title in taken:
            return False
        taken.add(title)
        _insert_task(conn, job, eq, now)
        open_n += 1
        return True

    for eq in equipment:
        jobs = jobs_for(eq["name"])
        pool += [(job, eq) for job in jobs]
        want = rng.choice((2, 3))
        for job in rng.sample(jobs, len(jobs)):
            if want and add_open(job, eq):
                want -= 1
    for job in NO_EQUIPMENT:
        add_open(job, None)
        pool.append((job, None))

    # ---- 過去の実績。人ごとに得意・不得意（難易度ごとの倍率）を決めておき、
    # 毎回の所要時間はそれにばらつきを乗せる。完了済みタスクを1件ずつ作って紐付ける
    log_n = 0
    for w in workers:
        candidates = [(job, eq) for job, eq in pool
                      if not perms.missing(w, {"required_permissions": perms.dump(job[6])})]
        if not candidates:
            continue
        base = _profile(w["years_of_service"])
        knack = [m * rng.uniform(0.8, 1.2) for m in base]      # この人の癖
        for _ in range(rng.randint(*LOGS_PER_WORKER)):
            job, eq = rng.choice(candidates)
            title, desc, diff, prio, qty, days, req = job
            qty = max(1, round(qty * rng.uniform(0.3, 1.0)))
            standard = BASE_SECONDS[diff] * qty
            duration = int(max(60, standard * knack[diff - 1] * rng.lognormvariate(0, 0.15)))
            day = now - timedelta(days=rng.randint(1, HISTORY_DAYS))
            started = day.replace(hour=rng.randint(8, 14), minute=rng.choice((0, 10, 20, 30, 40, 50)),
                                  second=0, microsecond=0)
            completed = started + timedelta(seconds=duration)
            ratio = duration / standard
            felt = None if rng.random() < 0.2 else ("easy" if ratio < 0.9 else "hard" if ratio > 1.2 else "normal")
            task_id = _insert_task(
                conn, (title, desc, diff, prio, qty, 0, req), eq, started,
                status="done", assigned_worker_id=w["id"],
                started_at=started.strftime(FMT), completed_at=completed.strftime(FMT),
                created_at=(started - timedelta(days=1)).strftime(FMT))
            conn.execute(
                """INSERT INTO work_logs (task_id, worker_id, equipment_id, started_at, completed_at,
                                          duration_sec, felt_difficulty) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (task_id, w["id"], eq["id"] if eq else None, started.strftime(FMT),
                 completed.strftime(FMT), duration, felt))
            log_n += 1
    return {"tasks": open_n, "logs": log_n}


def stop(conn):
    """
    デモで足したタスクと、その実績を消す。commit は呼び出し側。
    デモのタスクで使用中になっている機材は空きに戻し、その機材の行（戻す前）を返す
    （呼び出し側でモジュールの画面を送り直すため）。
    戻り値: ({"tasks": 消したタスク数, "logs": 消した実績数}, 空きに戻した機材の行)
    """
    counts = demo_counts(conn)
    released = conn.execute("""
        SELECT * FROM equipment
        WHERE current_task_id IN (SELECT id FROM tasks WHERE demo = 1)""").fetchall()
    conn.execute("""
        UPDATE equipment SET status = 'idle', current_worker_id = NULL, current_task_id = NULL,
               updated_at = datetime('now','localtime')
        WHERE current_task_id IN (SELECT id FROM tasks WHERE demo = 1)""")
    conn.execute("DELETE FROM work_logs WHERE task_id IN (SELECT id FROM tasks WHERE demo = 1)")
    conn.execute("DELETE FROM tasks WHERE demo = 1")
    return counts, released
