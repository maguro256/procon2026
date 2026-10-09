# -*- coding: utf-8 -*-
"""
seed_demo.py - 架空の工場データ(demo/factory.json)をDBへ流し込む

実機のESP32/ラズパイやMQTTブローカーが無くても、この架空データと
simulate_shift.py だけで管理画面(http://127.0.0.1:5000)を一通りデモできる。

    python seed_demo.py                     # demo/factory.json を追加登録
    python seed_demo.py --reset             # 既存の作業者・機材・タスク・実績を消してから入れ直す
    python seed_demo.py --file other.json   # 別のJSONを使う

JSONの形は demo/factory.json を参照。permissions / required_permissions は
コード名の配列（例: ["welding", "electric"]）で書く。
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import db
import permissions as perms

DEFAULT_FACTORY = Path(__file__).parent / "demo" / "factory.json"


def _reset(conn):
    # 外部キー制約があるので、参照している側から先に外す/消す
    conn.execute("UPDATE equipment SET current_worker_id = NULL, current_task_id = NULL")
    conn.execute("DELETE FROM work_logs")
    conn.execute("DELETE FROM tasks")
    conn.execute("DELETE FROM equipment")
    conn.execute("DELETE FROM workers")
    print("既存の作業者・機材・タスク・作業実績を削除しました")


def load_factory(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def seed_workers(conn, workers: list) -> None:
    for w in workers:
        conn.execute(
            "INSERT INTO workers (name, years_of_service, role, permissions, nfc_tag_id) VALUES (?, ?, ?, ?, ?)",
            (
                w["name"],
                float(w.get("years_of_service", 0)),
                w.get("role") or perms.DEFAULT_ROLE,
                perms.dump(w.get("permissions")),
                w.get("nfc_tag_id"),
            ),
        )
    print(f"作業者を {len(workers)} 人登録しました")


def seed_equipment(conn, equipment: list) -> dict:
    """機材名 → id の対応表を返す（タスク側の equipment_name 解決に使う）"""
    name_to_id = {}
    for e in equipment:
        cur = conn.execute(
            "INSERT INTO equipment (name, module_id, hostname, status) VALUES (?, ?, ?, ?)",
            (e["name"], e.get("module_id"), e.get("hostname"), e.get("status", "idle")),
        )
        name_to_id[e["name"]] = cur.lastrowid
    print(f"機材(モジュール込み)を {len(equipment)} 件登録しました")
    return name_to_id


def seed_tasks(conn, tasks: list, name_to_id: dict) -> None:
    for t in tasks:
        eq_name = t.get("equipment_name")
        equipment_id = name_to_id.get(eq_name) if eq_name else None
        if eq_name and equipment_id is None:
            print(f"  ! 「{t['title']}」: 機材「{eq_name}」が見つからないので機材指定なしで登録します")
        conn.execute(
            """INSERT INTO tasks (title, description, difficulty, priority, required_permissions,
                                quantity, deadline, equipment_id)
               VALUES (?, ?, ?, ?, ?, ?, date('now', ?), ?)""",
            (
                t["title"],
                t.get("description", ""),
                int(t.get("difficulty", 3)),
                t.get("priority", "normal"),
                perms.dump(t.get("required_permissions")),
                int(t.get("quantity", 1)),
                f"+{int(t.get('deadline_in_days', 3))} day",
                equipment_id,
            ),
        )
    print(f"タスクを {len(tasks)} 件登録しました")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default=str(DEFAULT_FACTORY), help="読み込むJSONファイル")
    ap.add_argument("--reset", action="store_true", help="既存データを消してから入れ直す")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"[seed_demo] JSONが見つかりません: {path}", file=sys.stderr)
        sys.exit(1)

    # スキーマ/マイグレーションだけ当てる。組み込みのサンプルデータ(db.py の
    # init_db)は入れない -- こちらのJSONだけで工場を構成したいので。
    db.init_db(seed=False)

    if args.reset and db.DB_PATH.exists():
        backup = db.DB_PATH.with_name(
            f"{db.DB_PATH.name}.before-reset-{datetime.now():%Y%m%d-%H%M%S}"
        )
        db.backup_to(backup)
        print(f"リセット前のDBを {backup.name} に退避しました")

    factory = load_factory(path)
    conn = db.get_db()
    try:
        if args.reset:
            _reset(conn)
        seed_workers(conn, factory.get("workers", []))
        name_to_id = seed_equipment(conn, factory.get("equipment", []))
        seed_tasks(conn, factory.get("tasks", []), name_to_id)
        conn.commit()
    finally:
        conn.close()

    print()
    print("架空の工場データを投入しました。")
    print("  python app.py             でサーバーを起動 (paho-mqtt が無ければ自動でデモモード)")
    print("  python simulate_shift.py  で仮想NFCタッチを流して稼働実績を作る")


if __name__ == "__main__":
    main()
