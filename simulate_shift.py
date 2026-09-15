# -*- coding: utf-8 -*-
"""
simulate_shift.py - 実機(ESP32/ラズパイ)もMQTTブローカーも無い状態で、
仮想的なNFCタッチを app.py の HTTP API に直接投げてデモ用の稼働実績を作る。

事前に別プロセスで `python app.py` を起動しておくこと（paho-mqtt が
入っていなくても、その場合は自動でデモモードになりWeb/APIだけ動く）。
このスクリプトは MQTT を一切使わず、実機が叩くのと同じ /api/... を
requests で叩くだけなので、ハードウェアが無くても丸ごと再現できる。

    python seed_demo.py --reset      # 先に架空の工場データを入れる
    python app.py                    # 別ターミナルでサーバーを起動
    python simulate_shift.py         # このスクリプトで仮想タッチを流す

    python simulate_shift.py --speed live --rounds 15
        ダッシュボードをブラウザで開いたまま見せるデモ用。機材が数秒だけ
        「稼働中」になってから空きに戻るので、実際に動いているのが分かる。

    python simulate_shift.py --speed fast --rounds 300
        所要時間をDBで巻き戻して積み上げるので、実時間はかからない。
        work_logs が一気に溜まるので、AI割り当て(auto_assign)の効き方を
        比較するのに向く（先に少人数へ偏らせてから auto_assign を叩く、等）。
"""
import argparse
import random
import sys
import time
from datetime import datetime

import requests

import db

# 難易度(1-5)ごとの「標準的な」所要時間（秒）。実際の所要時間はここに
# 作業者ごとの適性(aptitude)と多少のばらつきを掛けたもの。
BASE_SECONDS = {1: 60, 2: 120, 3: 240, 4: 420, 5: 600}

# ライブ演出用。実際の秒数だと待っていられないので数秒に圧縮する
LIVE_SLEEP_RANGE = (2, 6)


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def fetch_state(conn):
    idle_equipment = conn.execute(
        "SELECT id, name, module_id FROM equipment WHERE status = 'idle' AND module_id IS NOT NULL"
    ).fetchall()
    workers = conn.execute(
        "SELECT id, name, nfc_tag_id FROM workers WHERE nfc_tag_id IS NOT NULL"
    ).fetchall()
    return idle_equipment, workers


def backdate_start(conn, task_id, seconds_ago):
    """fast モード: started_at をさかのぼらせて、complete時にその分の所要時間を作る"""
    conn.execute(
        "UPDATE tasks SET started_at = datetime('now', 'localtime', ?) WHERE id = ?",
        (f"-{seconds_ago} seconds", task_id),
    )
    conn.commit()


def pick_feedback(duration_sec, difficulty, no_answer_rate=0.15):
    """所要時間から体感難易度を逆算する（アンケートに答えなかった扱いも混ぜる）"""
    if random.random() < no_answer_rate:
        return None
    base = BASE_SECONDS.get(difficulty, 240)
    if duration_sec < base * 0.85:
        return "easy"
    if duration_sec > base * 1.15:
        return "hard"
    return "normal"


class Shift:
    def __init__(self, base_url, speed, rng):
        self.base_url = base_url.rstrip("/")
        self.speed = speed
        self.rng = rng
        self.session = requests.Session()
        # 作業者ごとの適性。1.0が標準、小さいほど速い。デモの中だけの仮想値で、
        # AI.py の worker.skill とは別物（こちらはAPI越しに実績を作るためのシム）。
        self.aptitude = {}

    def _aptitude_of(self, worker_id):
        if worker_id not in self.aptitude:
            self.aptitude[worker_id] = max(0.5, self.rng.gauss(1.0, 0.25))
        return self.aptitude[worker_id]

    def run_round(self, conn, assign_every, round_no):
        idle_equipment, workers = fetch_state(conn)
        if not idle_equipment or not workers:
            log("空いている機材か、ICタグ登録済みの作業者がいません")
            return

        for eq in idle_equipment:
            worker = self.rng.choice(workers)
            self._do_touch(conn, worker, eq)

        if assign_every and round_no % assign_every == 0:
            self._auto_assign_random(conn)

    def _do_touch(self, conn, worker, eq):
        base = self.base_url
        next_res = self.session.get(f"{base}/api/workers/{worker['nfc_tag_id']}/next_task", timeout=10)
        if next_res.status_code != 200:
            return
        tasks = next_res.json().get("tasks", [])
        candidates = [t for t in tasks if t.get("equipment_id") in (None, eq["id"])]
        if not candidates:
            return
        task = candidates[0]

        start_res = self.session.post(
            f"{base}/api/tasks/{task['id']}/start",
            json={"nfc_tag_id": worker["nfc_tag_id"], "module_id": eq["module_id"]},
            timeout=10,
        )
        if start_res.status_code != 200:
            log(f"  ! {worker['name']} → {task['title']}: 着手できず ({start_res.status_code})")
            return

        difficulty = task.get("difficulty") or 3
        aptitude = self._aptitude_of(worker["id"])
        target = BASE_SECONDS.get(difficulty, 240) * aptitude * self.rng.uniform(0.85, 1.15)

        if self.speed == "fast":
            backdate_start(conn, task["id"], int(target))
            duration_used = int(target)
        else:
            duration_used = int(self.rng.uniform(*LIVE_SLEEP_RANGE))
            time.sleep(duration_used)

        complete_res = self.session.post(f"{base}/api/tasks/{task['id']}/complete", timeout=10)
        if complete_res.status_code != 200:
            log(f"  ! {worker['name']}: 完了APIが失敗 ({complete_res.status_code})")
            return
        body = complete_res.json()
        actual_duration = body.get("duration_sec") or duration_used
        work_log_id = body.get("work_log_id")

        felt = pick_feedback(target if self.speed == "fast" else actual_duration, difficulty)
        if felt and work_log_id:
            self.session.post(
                f"{base}/api/work_logs/{work_log_id}/feedback",
                json={"felt_difficulty": felt}, timeout=10,
            )

        # /complete はタスクを done にするだけで機材は解放しない（実機では
        # 「終了」も本人のもう一度のタッチで、_end_session がここを呼ぶ）。
        # ここで呼ばずにいると2周目以降 idle な機材が無くなって進まなくなる。
        self.session.post(
            f"{base}/api/equipment/{eq['module_id']}/status",
            json={"status": "idle"}, timeout=10,
        )

        felt_note = f"体感「{felt}」" if felt else "体感は無回答"
        log(f"  {worker['name']} @ {eq['name']}: 「{task['title']}」 "
            f"{actual_duration}秒 ({felt_note})")

    def _auto_assign_random(self, conn):
        row = conn.execute(
            "SELECT id, title FROM tasks WHERE status = 'todo' ORDER BY RANDOM() LIMIT 1"
        ).fetchone()
        if not row:
            return
        res = self.session.post(f"{self.base_url}/tasks/{row['id']}/auto_assign", timeout=10)
        assigned = conn.execute(
            "SELECT w.name FROM tasks t LEFT JOIN workers w ON w.id = t.assigned_worker_id WHERE t.id = ?",
            (row["id"],),
        ).fetchone()
        who = assigned["name"] if assigned and assigned["name"] else "(未割当)"
        log(f"  [AI割当] 「{row['title']}」 → {who} (HTTP {res.status_code})")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default="http://127.0.0.1:5000")
    ap.add_argument("--rounds", type=int, default=30)
    ap.add_argument("--speed", choices=["fast", "live"], default="fast")
    ap.add_argument("--assign-every", type=int, default=5,
                    help="Nラウンドごとに手動AI割当(auto_assign)も試す。0で無効")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    rng = random.Random(args.seed)

    try:
        ping = requests.get(f"{args.base_url}/", timeout=5)
        ping.raise_for_status()
    except requests.RequestException as e:
        print(f"[simulate_shift] {args.base_url} に接続できません: {e}", file=sys.stderr)
        print("  先に別ターミナルで `python app.py` を起動してください。", file=sys.stderr)
        sys.exit(1)

    shift = Shift(args.base_url, args.speed, rng)
    conn = db.get_db()
    try:
        for round_no in range(1, args.rounds + 1):
            log(f"--- round {round_no}/{args.rounds} ---")
            shift.run_round(conn, args.assign_every, round_no)
    finally:
        conn.close()

    log("シミュレーション終了。管理画面のタスク一覧・ダッシュボードを確認してください。")


if __name__ == "__main__":
    main()
