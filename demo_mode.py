# -*- coding: utf-8 -*-
"""
demo_mode.py - デモモード（管理画面サイドバーの「開始する」「解除する」）

ボタン一つで、発表用に決めた状態のデモ用DB（gemmba-demo.db）へ切り替える。

  - 作業者: 宮田・南川の2人
  - 機材:   3Dプリンタ・旋盤の2台（モジュール2台）
  - タスク: 機材ごとに「至急」と「通常」

**本番のDB（gemmba.db）には触らない。** db.DB_PATH をデモ用DBへ向け替えるだけなので、
解除すれば開始前の状態にそのまま戻る。デモ中に登録・作業したものはデモ用DBにだけ残り、
次に開始したときは作り直す（いつ開始しても同じ状態から始まる）。
デモ中に「初期化」を押すと、デモモードのまま最初の状態に戻す（reset）。

社員証のICタグとモジュールの紐付けは、本番DBの同じ名前の作業者・機材から引き継ぐ
（宮田さん・南川さんのカードと、3Dプリンタ・旋盤のモジュールがそのまま使える）。
本番DBに見つからなければ未紐付けで作るので、管理画面で紐付ければよい。

app.py を再起動しても、デモ用DBが残っていればデモモードのまま始まる（resume）。
"""
import os
from datetime import datetime, timedelta

import db
import permissions as perms

FMT = "%Y-%m-%d %H:%M:%S"
DEMO_NAME = "gemmba-demo.db"
# 作り直すときに中身を消すテーブル。参照される側が後
TABLES = ["work_logs", "task_members", "tasks", "recurring_tasks", "equipment", "workers",
          "pending_tags", "pending_modules"]

# (表示名, 本番DBで探すときの名前の手がかり, 勤続年数, 役職) 。勤続年数と役職は本番DBに
# 同じ人がいなかったときだけ使う
WORKERS = [
    ("宮田", ("宮田",), 5.0, "supervisor"),
    ("南川", ("南川",), 30.0, "manager"),
]
# (表示名, 本番DBで探すときの名前の手がかり, 機材コード（本番に無いとき）)
EQUIPMENT = [
    ("3Dプリンタ", ("3d", "３ｄ", "プリンタ"), "MOD-3DP"),
    ("旋盤", ("旋盤", "lathe"), "MOD-LATHE"),
]
# (タイトル, 補足, 難易度, 優先度, 数量, 期限までの日数, 機材名)
# 至急のタスクをあえて後から登録した扱いにして、登録順ではなく優先度で先に出ることを見せる
TASKS = [
    ("治具ホルダー 出力", "PLA・充填率20%", 2, "normal", 4, 3, "3Dプリンタ"),
    ("製品A ブッシュ旋削", "図面 A-110。1個あたり約2分", 2, "normal", 20, 3, "旋盤"),
    ("ケース試作 出力", "設計変更後の確認用。本日中", 3, "urgent", 1, 0, "3Dプリンタ"),
    ("シャフト 追加工", "急ぎの追加注文分。公差 ±0.02", 3, "urgent", 5, 0, "旋盤"),
]

# デモに切り替える前の DB_PATH。None ならデモモードではない
_real_path = None


def demo_path():
    """デモ用DBの場所。本番DBと同じフォルダ"""
    return (_real_path or db.DB_PATH).with_name(DEMO_NAME)


def is_active() -> bool:
    return _real_path is not None


def _find(rows, hints):
    for r in rows:
        name = (r["name"] or "").lower()
        if any(h.lower() in name for h in hints):
            return r
    return None


def _remove(path):
    for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if p.exists():
            os.remove(p)


def build(real_path, path, now=None):
    """
    real_path（本番DB）の作業者・機材を手がかりに、path にデモ用DBを作り直す。
    ファイルは消さずに中身だけ入れ替える（デモ中の初期化では app.py がこのDBを使っている）。
    戻り値: 作った件数 {"workers", "equipment", "tasks"}
    """
    now = now or datetime.now()
    src = db.get_db(real_path)
    try:
        real_workers = src.execute("SELECT * FROM workers ORDER BY id").fetchall()
        real_equipment = src.execute("SELECT * FROM equipment ORDER BY id").fetchall()
    finally:
        src.close()

    db.init_db(seed=False, path=path)
    conn = db.get_db(path)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        for t in TABLES:
            conn.execute(f"DELETE FROM {t}")
        conn.execute("DELETE FROM sqlite_sequence")   # id を 1 から振り直す
        conn.execute("PRAGMA foreign_keys = ON")
        for name, hints, years, role in WORKERS:
            r = _find(real_workers, hints)
            conn.execute(
                "INSERT INTO workers (name, years_of_service, role, permissions, nfc_tag_id) VALUES (?, ?, ?, ?, ?)",
                (name, r["years_of_service"] if r else years, r["role"] if r else role,
                 r["permissions"] if r else perms.dump([]), r["nfc_tag_id"] if r else None))

        eq_ids = {}
        for name, hints, code in EQUIPMENT:
            r = _find(real_equipment, hints)
            # 死活（online・ip・last_seen）も引き継ぐ。0 から始めると、ハートビートが
            # 「変化なし」のまま届き続けて、繋がっているのに切断中と表示される
            eq_ids[name] = conn.execute(
                """INSERT INTO equipment (name, module_id, status, ip, hostname, last_seen, online)
                   VALUES (?, ?, 'idle', ?, ?, ?, ?)""",
                (name, r["module_id"] if r and r["module_id"] else code,
                 r["ip"] if r else None, r["hostname"] if r else None,
                 r["last_seen"] if r else None, r["online"] if r else 0)).lastrowid

        for i, (title, desc, diff, prio, qty, days, eq) in enumerate(TASKS):
            conn.execute(
                """INSERT INTO tasks (title, description, difficulty, priority, quantity, deadline,
                                      equipment_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (title, desc, diff, prio, qty, (now + timedelta(days=days)).strftime("%Y-%m-%d"),
                 eq_ids[eq], (now - timedelta(minutes=10 * (len(TASKS) - i))).strftime(FMT)))
        conn.commit()
    finally:
        conn.close()
    return {"workers": len(WORKERS), "equipment": len(EQUIPMENT), "tasks": len(TASKS)}


def _carry_liveness(src_path, dst_path):
    """
    モジュールの死活（online・ip・last_seen）を src から dst の同じモジュールへ写す。
    切り替えている間に届いた接続・切断は、今向いている側のDBにしか書かれないため
    """
    src = db.get_db(src_path)
    try:
        rows = src.execute("SELECT hostname, online, ip, last_seen FROM equipment"
                           " WHERE hostname IS NOT NULL").fetchall()
    finally:
        src.close()
    dst = db.get_db(dst_path)
    try:
        for r in rows:
            dst.execute("UPDATE equipment SET online = ?, ip = COALESCE(?, ip), last_seen = COALESCE(?, last_seen)"
                        " WHERE hostname = ?", (r["online"], r["ip"], r["last_seen"], r["hostname"]))
        dst.commit()
    finally:
        dst.close()


def start(now=None):
    """デモ用DBを作り直して切り替える。戻り値は build() と同じ"""
    global _real_path
    real = db.DB_PATH
    counts = build(real, real.with_name(DEMO_NAME), now)
    _real_path = real
    db.DB_PATH = real.with_name(DEMO_NAME)
    return counts


def reset(now=None):
    """デモモードのまま、デモ用DBを最初の状態に戻す。戻り値は build() と同じ"""
    # 今のモジュールの死活はデモ用DBにしか書かれていないので、作り直しの元にする本番DBへ先に写す
    _carry_liveness(db.DB_PATH, _real_path)
    return build(_real_path, db.DB_PATH, now)


def stop():
    """本番DBへ戻し、デモ用DBは消す（次の開始で作り直す）"""
    global _real_path
    demo, real = db.DB_PATH, _real_path
    _carry_liveness(demo, real)
    db.DB_PATH, _real_path = real, None
    try:
        _remove(demo)
    except OSError as e:   # まだ誰かが開いている（Windows）。次の開始で作り直すので困らない
        print(f"[demo] デモ用DBを消せませんでした: {e}")


def resume():
    """起動時に、デモ用DBが残っていればデモモードのまま始める。戻り値: 再開したか"""
    global _real_path
    path = db.DB_PATH.with_name(DEMO_NAME)
    if _real_path is not None or not path.exists():
        return False
    _real_path = db.DB_PATH
    db.DB_PATH = path
    return True


def remove_legacy(conn):
    """
    以前のデモモード（本番DBに tasks.demo = 1 の印付きでタスクと実績を足していた）の
    残りを消す。commit は呼び出し側。戻り値: 消したタスク数
    """
    conn.execute("""
        UPDATE equipment SET status = 'idle', current_worker_id = NULL, current_task_id = NULL
        WHERE current_task_id IN (SELECT id FROM tasks WHERE demo = 1)""")
    conn.execute("DELETE FROM work_logs WHERE task_id IN (SELECT id FROM tasks WHERE demo = 1)")
    return conn.execute("DELETE FROM tasks WHERE demo = 1").rowcount
