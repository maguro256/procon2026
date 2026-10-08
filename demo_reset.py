# -*- coding: utf-8 -*-
"""
demo_reset.py - DBをデモ用の状態（demo/showcase.json）へ切り替える

一通りの動作（未登録モジュール/カードの登録、タッチ→タスク実行→作業終了、
使用中の排他、誘導、権限、メンテ中の機材、音声登録、AI割当）をデモできる
状態を作る。何度でも同じ状態に戻せる。

    python demo_reset.py                 # デモ状態へ切り替える
    python demo_reset.py --empty         # 全データを消した空の状態へ
    python demo_reset.py --list          # 退避したDBの一覧
    python demo_reset.py --restore       # 直前の退避へ戻す（名前を付ければそれへ）
    python demo_reset.py --file x.json   # 別のデータセットを使う

**app.py を止めてから実行すること。** 未登録カード・モジュールの一覧や
モジュールの接続状態を app.py がメモリにも持っているため、動かしたまま
DBだけ差し替えると画面とずれる。動いていれば中止する（--force で強行）。

切り替えの前に、今のDBを gemmba.db.before-demo-<日時> へ必ず退避する。
期限・作業中の開始時刻・過去の実績は実行した日時を基準に作るので、
いつ戻しても「今日の工場」に見える。
"""
import argparse
import json
import random
import shutil
import socket
import sys
from datetime import datetime, timedelta
from pathlib import Path

import db
import permissions as perms

ROOT = Path(__file__).parent
DEFAULT_FILE = ROOT / "demo" / "showcase.json"
BACKUP_PREFIX = db.DB_PATH.name + ".before-"
TABLES = ["work_logs", "tasks", "equipment", "workers", "pending_tags", "pending_modules"]

# 1個あたりの標準的な所要時間（秒）。seed_training.py と同じ
BASE_SECONDS = {1: 60, 2: 120, 3: 240, 4: 420, 5: 600}
# 勤続年数ごとの「標準に対する時間の倍率」。難易度1〜5の順。seed_training.py と同じ考え方
PROFILES = [
    (8.0, [1.05, 1.00, 0.85, 0.75, 0.70]),   # ベテラン: 難しいほど速い
    (3.0, [1.00, 1.00, 1.00, 1.00, 1.00]),   # 中堅
    (0.0, [0.90, 1.00, 1.25, 1.60, 2.00]),   # 若手: 難しくなるほど急に遅い
]
FMT = "%Y-%m-%d %H:%M:%S"


def app_running(port=5000):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def backup(tag):
    if not db.DB_PATH.exists():
        return None
    path = db.DB_PATH.with_name(f"{BACKUP_PREFIX}{tag}-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(db.DB_PATH, path)
    print(f"今のDBを {path.name} に退避しました")
    return path


def wipe(conn):
    conn.execute("PRAGMA foreign_keys = OFF")
    for t in TABLES:
        conn.execute(f"DELETE FROM {t}")
    conn.execute("DELETE FROM sqlite_sequence")
    conn.execute("PRAGMA foreign_keys = ON")


def multiplier(years, difficulty):
    for floor, mults in PROFILES:
        if years >= floor:
            return mults[difficulty - 1]
    return 1.0


def felt_for(mult, rng):
    """速くこなせた人ほど「簡単」と答えやすい。3割は答えずに立ち去る"""
    if rng.random() < 0.3:
        return None
    if mult <= 0.85:
        return "easy"
    if mult >= 1.4:
        return "hard"
    return "normal"


def seed(conn, data, now):
    rng = random.Random(data.get("history", {}).get("seed", 2026))

    workers = {}
    for w in data["workers"]:
        cur = conn.execute(
            "INSERT INTO workers (name, years_of_service, role, permissions, nfc_tag_id) VALUES (?, ?, ?, ?, ?)",
            (w["name"], float(w.get("years_of_service", 0)), w.get("role") or perms.DEFAULT_ROLE,
             perms.dump(w.get("permissions")), w.get("nfc_tag_id")))
        workers[w["name"]] = dict(w, id=cur.lastrowid, role=w.get("role") or perms.DEFAULT_ROLE,
                                  permissions=perms.dump(w.get("permissions")))

    equipment = {}
    for i, e in enumerate(data["equipment"], 1):
        cur = conn.execute(
            "INSERT INTO equipment (name, module_id, hostname, status, online) VALUES (?, ?, ?, ?, 0)",
            (e["name"], e.get("module_id") or f"MOD-{i:03d}", e.get("hostname"), e.get("status", "idle")))
        equipment[e["name"]] = cur.lastrowid

    def eq_id(name):
        if name and name not in equipment:
            sys.exit(f"機材「{name}」が equipment にありません")
        return equipment.get(name) if name else None

    # ---- 過去の実績（完了タスク + work_logs）
    hist = data.get("history")
    n_logs = 0
    if hist:
        templates = hist["templates"]
        lot = 10
        past = hist["count"] - hist.get("today", 0)
        for k in range(hist["count"]):
            # 古い順に days 日前から昨日まで並べ、最後の today 件は今日の分にする
            days_ago = hist["days"] - k * hist["days"] // past if k < past else 0
            day = (now - timedelta(days=days_ago)).replace(hour=8, minute=30, second=0, microsecond=0)
            t = rng.choice(templates)
            cands = [w for w in workers.values() if w.get("nfc_tag_id")
                     and perms.allows(w, {"required_permissions": perms.dump(t["required_permissions"])})]
            w = rng.choice(cands)
            qty = rng.randint(*t["quantity"])
            mult = multiplier(w["years_of_service"], t["difficulty"])
            sec = int(BASE_SECONDS[t["difficulty"]] * mult * rng.uniform(0.85, 1.15) * min(qty, 6))
            start = day + timedelta(minutes=rng.randint(0, 420))
            if start + timedelta(seconds=sec) > now:
                start = now - timedelta(seconds=sec + rng.randint(600, 3600))
            end = start + timedelta(seconds=sec)
            lot += 1
            title = t["title"].replace("{n}", str(lot))
            cur = conn.execute(
                """INSERT INTO tasks (title, description, difficulty, priority, required_permissions,
                                      quantity, deadline, status, assigned_worker_id, equipment_id,
                                      started_at, completed_at, created_at)
                   VALUES (?, '', ?, 'normal', ?, ?, ?, 'done', ?, ?, ?, ?, ?)""",
                (title, t["difficulty"], perms.dump(t["required_permissions"]), qty,
                 end.strftime("%Y-%m-%d"), w["id"], eq_id(t.get("equipment_name")),
                 start.strftime(FMT), end.strftime(FMT), (start - timedelta(days=1)).strftime(FMT)))
            conn.execute(
                """INSERT INTO work_logs (task_id, worker_id, equipment_id, started_at, completed_at,
                                          duration_sec, felt_difficulty) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (cur.lastrowid, w["id"], eq_id(t.get("equipment_name")), start.strftime(FMT),
                 end.strftime(FMT), sec, felt_for(mult, rng)))
            n_logs += 1

    # ---- 今のタスク。created_at をずらして JSON の並び順どおりに出す
    for i, t in enumerate(data["tasks"]):
        created = now - timedelta(hours=len(data["tasks"]) - i)
        cur = conn.execute(
            """INSERT INTO tasks (title, description, difficulty, priority, required_permissions,
                                  quantity, deadline, equipment_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (t["title"], t.get("description", ""), int(t.get("difficulty", 3)), t.get("priority", "normal"),
             perms.dump(t.get("required_permissions")), int(t.get("quantity", 1)),
             (now + timedelta(days=int(t.get("deadline_in_days", 3)))).strftime("%Y-%m-%d"),
             eq_id(t.get("equipment_name")), created.strftime(FMT)))
        prog = t.get("in_progress")
        if prog:
            w = workers[prog["worker"]]
            e = eq_id(t["equipment_name"])
            started = (now - timedelta(minutes=prog.get("started_minutes_ago", 30))).strftime(FMT)
            conn.execute("""UPDATE tasks SET status = 'in_progress', assigned_worker_id = ?,
                            started_at = ? WHERE id = ?""", (w["id"], started, cur.lastrowid))
            conn.execute("""UPDATE equipment SET status = 'working', current_worker_id = ?,
                            current_task_id = ? WHERE id = ?""", (w["id"], cur.lastrowid, e))

    print(f"作業者 {len(workers)} 人 / 機材 {len(equipment)} 台 / タスク {len(data['tasks'])} 件"
          f" / 過去の実績 {n_logs} 件 を投入しました")
    return workers


def print_cheatsheet(data):
    print("\n社員証（sim.py では list → @番号 でもタッチできます）")
    for w in data["workers"]:
        print(f"  {w.get('nfc_tag_id') or '(未紐付け)':<18} {w['name']:<8} {w.get('_demo', '')}")
    print("\nモジュール（python raspi/sim.py <ID> で起動）")
    for e in data["equipment"]:
        print(f"  {e.get('hostname') or '(なし)':<6} {e['name']:<12} {e.get('_demo', '')}")
    print("  pi05   (未登録)      起動すると「未登録モジュールを検出」のデモになる")


def list_backups():
    """退避した順。ファイルの更新時刻は copy2 が元DBのものを引き継ぐので、名前末尾の日時で並べる"""
    return sorted(db.DB_PATH.parent.glob(BACKUP_PREFIX + "*"), key=lambda p: p.name[-15:])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default=str(DEFAULT_FILE), help="データセットのJSON")
    ap.add_argument("--empty", action="store_true", help="全データを消した空の状態にする")
    ap.add_argument("--list", action="store_true", help="退避したDBの一覧を出す")
    ap.add_argument("--restore", nargs="?", const="", metavar="NAME", help="退避したDBへ戻す（省略時は最新）")
    ap.add_argument("--force", action="store_true", help="app.py が動いていても実行する")
    args = ap.parse_args()

    if args.list:
        for p in list_backups():
            print(f"  {p.name}")
        return

    if app_running() and not args.force:
        sys.exit("app.py が動いています。止めてから実行してください（Ctrl+C）。"
                 "止めずに行う場合は --force（画面がずれるので後で app.py を再起動すること）")

    if args.restore is not None:
        files = list_backups()
        src = (db.DB_PATH.parent / args.restore) if args.restore else (files[-1] if files else None)
        if not src or not src.exists():
            sys.exit("戻せる退避がありません。--list で確認してください")
        backup("restore")
        shutil.copy2(src, db.DB_PATH)
        print(f"{src.name} の状態に戻しました")
        return

    db.init_db(seed=False)   # スキーマとマイグレーションだけ当てる
    backup("empty" if args.empty else "demo")
    conn = db.get_db()
    try:
        wipe(conn)
        if args.empty:
            print("全データを削除しました")
        else:
            with open(args.file, encoding="utf-8") as f:
                data = json.load(f)
            seed(conn, data, datetime.now())
        conn.commit()
    finally:
        conn.close()
    if not args.empty:
        print_cheatsheet(data)
    print("\napp.py を起動するとこの状態から始まります。")


if __name__ == "__main__":
    main()
