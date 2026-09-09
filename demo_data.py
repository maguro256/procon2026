"""
demo_data.py - デモ用のタスクを投入する

    python demo_data.py           # 追加する
    python demo_data.py --reset   # 既存のタスクを消してから入れ直す

created_at をわざとずらしてあるので、実績が溜まっていない状態でも
「製品A ロット12 組立」から順に出る。

一部のタスクには必要権限（TODO.md の D-2）を付けてある。権限を持たない
作業者がタッチしても候補に出ないので、作業者管理で権限を足し引きすると
提示されるタスクが変わることを確認できる。
"""
import sys

import db

# (タイトル, 説明, 難易度, 優先度, 数量, 期限までの日数, 何日前に登録されたか, 機材ID, 必要権限)
# 必要権限（D-2）を付けたものは、その資格を持つ人にしか提示されない。
# 権限あり／なしの両方を混ぜてあるので、作業者を切り替えると候補の差が見える。
DEMO_TASKS = [
    ("製品A ロット12 組立", "治具Aを使用。1個あたり約3分",      2, "normal", 30, 2, 5, None, ""),
    ("治具B の芯出し",      "前回ロットで振れが出たため再調整",   3, "high",    1, 1, 4, 1,    ""),
    ("製品C 外形加工",      "図面 C-204 rev.3。公差 ±0.05",      4, "urgent", 20, 1, 3, None, "welding"),
    ("定期メンテナンス",    "潤滑・切粉清掃・各部点検",          1, "low",     1, 7, 2, 1,    "electric"),
    ("製品D 面取り仕上げ",  "バリ取り後、外観検査まで",          3, "normal", 45, 3, 1, None, ""),
    ("試作E 初品確認",      "寸法測定して記録票に記入",          5, "high",    2, 1, 0, None, "inspection"),
    ("資材置場からの搬入",  "パレット3枚。所定位置へ",           1, "high",    3, 1, 0, None, "forklift"),
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

    for title, desc, diff, prio, qty, due_days, age_days, eq_id, req in DEMO_TASKS:
        conn.execute(
            """INSERT INTO tasks
               (title, description, difficulty, priority, required_permissions,
                quantity, deadline, equipment_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, date('now', ?), ?, datetime('now','localtime', ?))""",
            (title, desc, diff, prio, req, qty, f"+{due_days} day", eq_id, f"-{age_days} day"),
        )
    conn.commit()

    print(f"デモタスクを {len(DEMO_TASKS)} 件登録しました（古い順）:")
    for r in conn.execute("""SELECT title, priority, quantity, deadline, created_at,
                                    required_permissions
                             FROM tasks WHERE status = 'todo'
                             ORDER BY created_at, id"""):
        req = r["required_permissions"]
        print(f"  {r['created_at'][:16]}  {r['title']}"
              f"（{r['priority']} / {r['quantity']}個 / 期限 {r['deadline']}"
              + (f" / 要 {req}）" if req else "）"))
    conn.close()


if __name__ == "__main__":
    main()
