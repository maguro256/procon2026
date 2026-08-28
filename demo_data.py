"""
demo_data.py - デモ用のタスクを投入する

    python demo_data.py           # 追加する
    python demo_data.py --reset   # 既存のタスクを消してから入れ直す

割当は当面「最も古いものから順番」なので、created_at をわざとずらしてある。
投入直後は「製品A ロット12 組立」が最初の候補になる。

本番の割当（WariAthena / TODO.md の D-1）に差し替えたら、優先度や難易度が
効くようになるので、そのときの動作確認用データとしても使える。
"""
import sys

import db

# (タイトル, 説明, 難易度, 優先度, 数量, 期限までの日数, 何日前に登録されたか, 機材ID)
DEMO_TASKS = [
    ("製品A ロット12 組立", "治具Aを使用。1個あたり約3分",      2, "normal", 30, 2, 5, None),
    ("治具B の芯出し",      "前回ロットで振れが出たため再調整",   3, "high",    1, 1, 4, 1),
    ("製品C 外形加工",      "図面 C-204 rev.3。公差 ±0.05",      4, "urgent", 20, 1, 3, None),
    ("定期メンテナンス",    "潤滑・切粉清掃・各部点検",          1, "low",     1, 7, 2, 1),
    ("製品D 面取り仕上げ",  "バリ取り後、外観検査まで",          3, "normal", 45, 3, 1, None),
    ("試作E 初品確認",      "寸法測定して記録票に記入",          5, "high",    2, 1, 0, None),
]


def main():
    reset = "--reset" in sys.argv
    conn = db.get_db()

    if reset:
        # equipment が参照したままだと外部キーで消せない
        conn.execute("UPDATE equipment SET current_task_id = NULL")
        conn.execute("DELETE FROM work_logs")
        conn.execute("DELETE FROM tasks")
        print("既存のタスクと作業実績を削除しました")

    for title, desc, diff, prio, qty, due_days, age_days, eq_id in DEMO_TASKS:
        conn.execute(
            """INSERT INTO tasks
               (title, description, difficulty, priority, quantity, deadline,
                equipment_id, created_at)
               VALUES (?, ?, ?, ?, ?, date('now', ?), ?, datetime('now','localtime', ?))""",
            (title, desc, diff, prio, qty, f"+{due_days} day", eq_id, f"-{age_days} day"),
        )
    conn.commit()

    print(f"デモタスクを {len(DEMO_TASKS)} 件登録しました（古い順）:")
    for r in conn.execute("""SELECT title, priority, quantity, deadline, created_at
                             FROM tasks WHERE status = 'todo'
                             ORDER BY created_at, id"""):
        print(f"  {r['created_at'][:16]}  {r['title']}"
              f"（{r['priority']} / {r['quantity']}個 / 期限 {r['deadline']}）")
    conn.close()


if __name__ == "__main__":
    main()
