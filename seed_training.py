# -*- coding: utf-8 -*-
"""
seed_training.py - WariAthena の動作確認用に、登録済みの作業者の作業実績を作る

作業者ごとに N 件（既定100件）の「完了した作業」を work_logs に直接書き込む。
HTTP も実機も使わない。所要時間は勤続年数から決めた得意・不得意に沿って作るので、
学習後に「難しい仕事はベテラン、簡単な仕事は若手」のような割り当てになるかを確かめられる。

    python seed_training.py                # 全作業者に100件ずつ
    python seed_training.py --per-worker 50
    python seed_training.py --clear        # このスクリプトが作ったものだけ消す

実行前に gemmba.db を gemmba.db.bak-<日時> へ退避する。

作るもの:
    tasks     … 「[学習用] 難易度n / 機材名」の完了済みタスク（難易度×機材の数だけ）
    work_logs … 上のタスクを参照する実績。所要時間・体感難易度つき
どちらも description に SEED_MARK を入れてあり、--clear はそれだけを消す。
"""
import argparse
import math
import random
import sys
from datetime import datetime, timedelta

import db

SEED_MARK = "seed_training"
TITLE_PREFIX = "[学習用]"

# 1個あたりの標準的な所要時間（秒）。simulate_shift.py の BASE_SECONDS と同じ
BASE_SECONDS = {1: 60, 2: 120, 3: 240, 4: 420, 5: 600}

# 勤続年数ごとの「標準に対する時間の倍率」。難易度1〜5の順。小さいほど速い。
# 若手は簡単な作業なら少し速い（身軽）が、難しくなるほど急に遅くなる。
# ベテランは難しい作業ほど差をつける。
PROFILES = [
    # (勤続年数の下限, 名前, 倍率)
    (8.0, "ベテラン", [1.05, 1.00, 0.85, 0.75, 0.70]),
    (3.0, "中堅",     [1.00, 1.00, 1.05, 1.20, 1.45]),
    (0.0, "若手",     [0.95, 1.15, 1.45, 1.90, 2.50]),
]

# 機材の得意・不得意（機材名に含まれる語 → 対象の型 → 倍率）。
# 機材の特徴量が効くかを見るため、中堅だけ旋盤が得意ということにしておく
EQUIPMENT_BONUS = {"旋盤": {"中堅": 0.80}}

# 数量。機材ごとに変えて、数量で割った報酬が効いているかも見られるようにする
QUANTITIES = [1, 5, 10]
NOISE = 0.12           # 所要時間のばらつき（対数正規の σ）
ANSWER_RATE = 0.7      # 体感難易度に答える割合（残りは無回答＝時間から推定）


def profile_for(years):
    for floor, name, mult in PROFILES:
        if (years or 0) >= floor:
            return name, mult
    return PROFILES[-1][1], PROFILES[-1][2]


def backup():
    dst = db.DB_PATH.with_name(f"{db.DB_PATH.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    db.backup_to(dst)
    print(f"退避しました: {dst}")


def clear(conn):
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM tasks WHERE description = ?", (SEED_MARK,))]
    if not ids:
        print("学習用のデータはありません")
        return
    marks = ",".join("?" * len(ids))
    n_logs = conn.execute(f"DELETE FROM work_logs WHERE task_id IN ({marks})", ids).rowcount
    conn.execute(f"DELETE FROM tasks WHERE id IN ({marks})", ids)
    conn.commit()
    print(f"学習用のタスク {len(ids)} 件と実績 {n_logs} 件を消しました")


def ensure_tasks(conn, equipment):
    """難易度×機材ぶんの完了済みタスクを用意する。既にあれば使い回す"""
    tasks = {}
    for e_i, eq in enumerate(equipment):
        quantity = QUANTITIES[e_i % len(QUANTITIES)]
        for d in range(1, 6):
            title = f"{TITLE_PREFIX} 難易度{d} / {eq['name']}"
            row = conn.execute("SELECT id, quantity FROM tasks WHERE title = ? AND description = ?",
                               (title, SEED_MARK)).fetchone()
            if row is None:
                cur = conn.execute(
                    """INSERT INTO tasks (title, description, difficulty, priority, quantity,
                                          status, equipment_id, completed_at)
                       VALUES (?, ?, ?, 'normal', ?, 'done', ?, datetime('now','localtime'))""",
                    (title, SEED_MARK, d, quantity, eq["id"]))
                row = {"id": cur.lastrowid, "quantity": quantity}
            tasks[(eq["id"], d)] = {"id": row["id"], "quantity": row["quantity"], "difficulty": d,
                                    "equipment": eq}
    return tasks


def felt_for(mult):
    """本人の体感。標準より明らかに速ければ簡単、遅ければ難しい"""
    if random.random() > ANSWER_RATE:
        return None
    if mult < 0.92:
        return "easy"
    if mult > 1.3:
        return "hard"
    return "normal"


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--per-worker", type=int, default=100)
    parser.add_argument("--days", type=int, default=30, help="何日前から積み上げるか")
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--clear", action="store_true")
    args = parser.parse_args()
    random.seed(args.seed)

    db.init_db()
    backup()
    conn = db.get_db()
    if args.clear:
        clear(conn)
        conn.close()
        return 0

    workers = [dict(r) for r in conn.execute("SELECT id, name, years_of_service FROM workers ORDER BY id")]
    equipment = [dict(r) for r in conn.execute("SELECT id, name FROM equipment ORDER BY id")]
    if not workers or not equipment:
        print("作業者と機材を1件以上登録してから実行してください")
        return 1
    tasks = ensure_tasks(conn, equipment)

    # 全員ぶんを時刻順に混ぜて積む。RewardConverter は「その時点までの平均」と
    # 比べるので、1人ずつ固めて入れると先に入れた人の基準で後の人が測られてしまう
    jobs = [w for w in workers for _ in range(args.per_worker)]
    random.shuffle(jobs)
    start = datetime.now() - timedelta(days=args.days)
    step = timedelta(days=args.days) / max(len(jobs), 1)

    summary = {}
    for n, w in enumerate(jobs):
        label, mults = profile_for(w["years_of_service"])
        task = tasks[(random.choice(equipment)["id"], random.randint(1, 5))]
        d = task["difficulty"]
        mult = mults[d - 1]
        for word, bonus in EQUIPMENT_BONUS.items():
            if word in task["equipment"]["name"]:
                mult *= bonus.get(label, 1.0)
        unit = BASE_SECONDS[d] * mult * math.exp(random.gauss(0, NOISE))
        duration = max(1, int(unit * task["quantity"]))
        completed = start + step * n
        started = completed - timedelta(seconds=duration)
        conn.execute(
            """INSERT INTO work_logs (task_id, worker_id, equipment_id, started_at, completed_at,
                                      duration_sec, felt_difficulty)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (task["id"], w["id"], task["equipment"]["id"], f"{started:%Y-%m-%d %H:%M:%S}",
             f"{completed:%Y-%m-%d %H:%M:%S}", duration, felt_for(mult)))
        summary.setdefault((w["name"], label), 0)
        summary[(w["name"], label)] += 1
    conn.commit()
    conn.close()

    for (name, label), count in summary.items():
        print(f"  {name}（{label}として生成）: {count} 件")
    print(f"実績を {len(jobs)} 件追加しました。app.py は次の割り当てから自動で学習し直します。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
