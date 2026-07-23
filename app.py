"""
app.py - Gemmba 管理者画面（雛形）

起動:
    pip install flask
    python app.py
    → http://127.0.0.1:5000

構成:
    画面 (HTML)   : ダッシュボード / 作業者管理 / タスク管理 / 機材管理
    API  (JSON)   : モジュール(ESP32)や割り当てAIが叩くエンドポイント
"""
from datetime import datetime

from flask import Flask, render_template, request, redirect, url_for, jsonify, flash

import db
import ai_stub

app = Flask(__name__)
app.secret_key = "dev-secret-change-me"  # flash用。本番では変更する

PRIORITY_LABELS = {"urgent": "至急", "high": "高", "normal": "通常", "low": "低"}
STATUS_LABELS = {"todo": "未着手", "assigned": "割当済", "in_progress": "作業中", "done": "完了"}
EQ_STATUS_LABELS = {"idle": "空き", "working": "稼働中", "stopped": "停止", "maintenance": "メンテ中"}


@app.context_processor
def inject_labels():
    return dict(P=PRIORITY_LABELS, S=STATUS_LABELS, E=EQ_STATUS_LABELS)


# ---------------------------------------------------------------- 画面

@app.route("/")
def dashboard():
    """機材の使用状況ダッシュボード"""
    conn = db.get_db()
    equipment = conn.execute("""
        SELECT e.*, w.name AS worker_name, t.title AS task_title,
               t.quantity AS task_quantity, t.deadline AS task_deadline
        FROM equipment e
        LEFT JOIN workers w ON w.id = e.current_worker_id
        LEFT JOIN tasks t   ON t.id = e.current_task_id
        ORDER BY e.name
    """).fetchall()
    counts = conn.execute("""
        SELECT
          (SELECT COUNT(*) FROM tasks WHERE status != 'done')       AS open_tasks,
          (SELECT COUNT(*) FROM tasks WHERE status = 'in_progress') AS active_tasks,
          (SELECT COUNT(*) FROM workers)                            AS workers,
          (SELECT COUNT(*) FROM equipment WHERE status = 'working') AS working_eq
    """).fetchone()
    recent = conn.execute("""
        SELECT t.*, w.name AS worker_name FROM tasks t
        LEFT JOIN workers w ON w.id = t.assigned_worker_id
        ORDER BY t.created_at DESC LIMIT 8
    """).fetchall()
    conn.close()
    return render_template("dashboard.html", equipment=equipment, counts=counts, recent=recent)


@app.route("/workers")
def workers():
    conn = db.get_db()
    rows = conn.execute("""
        SELECT w.*,
               (SELECT COUNT(*) FROM tasks t WHERE t.assigned_worker_id = w.id AND t.status != 'done') AS open_tasks
        FROM workers w ORDER BY w.id
    """).fetchall()
    conn.close()
    return render_template("workers.html", workers=rows)


@app.route("/workers/add", methods=["POST"])
def add_worker():
    name = request.form.get("name", "").strip()
    years = request.form.get("years_of_service", "0").strip()
    nfc = request.form.get("nfc_tag_id", "").strip() or None
    if not name:
        flash("名前を入力してください", "error")
        return redirect(url_for("workers"))
    conn = db.get_db()
    try:
        conn.execute(
            "INSERT INTO workers (name, years_of_service, nfc_tag_id) VALUES (?, ?, ?)",
            (name, float(years or 0), nfc),
        )
        conn.commit()
        flash(f"{name} さんを登録しました", "ok")
    except db.sqlite3.IntegrityError:
        flash("そのICタグIDは既に使われています", "error")
    finally:
        conn.close()
    return redirect(url_for("workers"))


@app.route("/workers/<int:worker_id>/delete", methods=["POST"])
def delete_worker(worker_id):
    conn = db.get_db()
    conn.execute("UPDATE tasks SET assigned_worker_id = NULL WHERE assigned_worker_id = ?", (worker_id,))
    conn.execute("UPDATE equipment SET current_worker_id = NULL WHERE current_worker_id = ?", (worker_id,))
    conn.execute("DELETE FROM workers WHERE id = ?", (worker_id,))
    conn.commit()
    conn.close()
    flash("作業者を削除しました", "ok")
    return redirect(url_for("workers"))


@app.route("/tasks")
def tasks():
    conn = db.get_db()
    rows = conn.execute("""
        SELECT t.*, w.name AS worker_name, e.name AS equipment_name
        FROM tasks t
        LEFT JOIN workers w   ON w.id = t.assigned_worker_id
        LEFT JOIN equipment e ON e.id = t.equipment_id
        ORDER BY CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,
                 t.deadline IS NULL, t.deadline
    """).fetchall()
    worker_list = conn.execute("SELECT id, name FROM workers ORDER BY name").fetchall()
    equipment_list = conn.execute("SELECT id, name FROM equipment ORDER BY name").fetchall()
    conn.close()
    return render_template("tasks.html", tasks=rows, worker_list=worker_list, equipment_list=equipment_list)


@app.route("/tasks/add", methods=["POST"])
def add_task():
    f = request.form
    title = f.get("title", "").strip()
    if not title:
        flash("タスク名を入力してください", "error")
        return redirect(url_for("tasks"))
    conn = db.get_db()
    conn.execute(
        """INSERT INTO tasks (title, description, difficulty, priority, quantity, deadline)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            title,
            f.get("description", "").strip(),
            int(f.get("difficulty", 3)),
            f.get("priority", "normal"),
            int(f.get("quantity", 1) or 1),
            f.get("deadline") or None,
        ),
    )
    conn.commit()
    conn.close()
    flash(f"タスク「{title}」を登録しました", "ok")
    return redirect(url_for("tasks"))


@app.route("/tasks/<int:task_id>/update", methods=["POST"])
def update_task(task_id):
    """状態変更・手動割り当て（担当者/機材）"""
    f = request.form
    conn = db.get_db()
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        conn.close()
        flash("タスクが見つかりません", "error")
        return redirect(url_for("tasks"))

    status = f.get("status", task["status"])
    worker_id = f.get("assigned_worker_id") or None
    equipment_id = f.get("equipment_id") or None
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    started_at = task["started_at"]
    completed_at = task["completed_at"]
    if status == "in_progress" and not started_at:
        started_at = now
    if status == "done" and not completed_at:
        completed_at = now
        # 実績ログを残す（WariAthena の学習データになる）
        if task["assigned_worker_id"] and started_at:
            dur = int(
                (datetime.strptime(now, "%Y-%m-%d %H:%M:%S")
                 - datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S")).total_seconds()
            )
            conn.execute(
                """INSERT INTO work_logs (task_id, worker_id, equipment_id, started_at, completed_at, duration_sec)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (task_id, task["assigned_worker_id"], task["equipment_id"], started_at, now, dur),
            )

    conn.execute(
        """UPDATE tasks SET status = ?, assigned_worker_id = ?, equipment_id = ?,
           started_at = ?, completed_at = ? WHERE id = ?""",
        (status, worker_id, equipment_id, started_at, completed_at, task_id),
    )
    conn.commit()
    conn.close()
    flash("タスクを更新しました", "ok")
    return redirect(url_for("tasks"))


@app.route("/tasks/<int:task_id>/auto_assign", methods=["POST"])
def auto_assign(task_id):
    """AI割り当て（現状は ai_stub のダミーを呼ぶ）"""
    conn = db.get_db()
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    workers_ = conn.execute("SELECT * FROM workers").fetchall()
    logs = conn.execute("SELECT * FROM work_logs").fetchall()
    wid = ai_stub.assign_task(dict(task), [dict(w) for w in workers_], [dict(l) for l in logs])
    if wid:
        conn.execute("UPDATE tasks SET assigned_worker_id = ?, status = 'assigned' WHERE id = ?", (wid, task_id))
        conn.commit()
        flash("AIがタスクを割り当てました（現状はダミーロジック）", "ok")
    else:
        flash("割り当て候補がいません", "error")
    conn.close()
    return redirect(url_for("tasks"))


@app.route("/tasks/<int:task_id>/delete", methods=["POST"])
def delete_task(task_id):
    conn = db.get_db()
    conn.execute("UPDATE equipment SET current_task_id = NULL WHERE current_task_id = ?", (task_id,))
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    flash("タスクを削除しました", "ok")
    return redirect(url_for("tasks"))


@app.route("/equipment")
def equipment():
    conn = db.get_db()
    rows = conn.execute("""
        SELECT e.*, w.name AS worker_name, t.title AS task_title
        FROM equipment e
        LEFT JOIN workers w ON w.id = e.current_worker_id
        LEFT JOIN tasks t   ON t.id = e.current_task_id
        ORDER BY e.id
    """).fetchall()
    conn.close()
    return render_template("equipment.html", equipment=rows)


@app.route("/equipment/add", methods=["POST"])
def add_equipment():
    name = request.form.get("name", "").strip()
    module_id = request.form.get("module_id", "").strip() or None
    if not name:
        flash("機材名を入力してください", "error")
        return redirect(url_for("equipment"))
    conn = db.get_db()
    try:
        conn.execute("INSERT INTO equipment (name, module_id) VALUES (?, ?)", (name, module_id))
        conn.commit()
        flash(f"機材「{name}」を登録しました", "ok")
    except db.sqlite3.IntegrityError:
        flash("そのモジュールIDは既に使われています", "error")
    finally:
        conn.close()
    return redirect(url_for("equipment"))


@app.route("/equipment/<int:eq_id>/delete", methods=["POST"])
def delete_equipment(eq_id):
    conn = db.get_db()
    conn.execute("UPDATE tasks SET equipment_id = NULL WHERE equipment_id = ?", (eq_id,))
    conn.execute("DELETE FROM equipment WHERE id = ?", (eq_id,))
    conn.commit()
    conn.close()
    flash("機材を削除しました", "ok")
    return redirect(url_for("equipment"))


# ------------------------------------------------ API（モジュール/AI連携用）
# ESP32 側からはここを HTTP で叩く想定。WebSocket 化する場合もこの層を置き換えるだけでよい。

@app.route("/api/equipment/<module_id>/status", methods=["GET"])
def api_get_equipment_status(module_id):
    """モジュールからの状態確認。タッチ時にハード側が「開始/終了/ロック中」を判定するために使う"""
    conn = db.get_db()
    eq = conn.execute("SELECT * FROM equipment WHERE module_id = ?", (module_id,)).fetchone()
    conn.close()
    if not eq:
        return jsonify({"error": "unknown module_id"}), 404
    return jsonify(dict(eq))


@app.route("/api/equipment/<module_id>/status", methods=["POST"])
def api_update_equipment_status(module_id):
    """
    モジュールからの状態報告。
    body 例: {"status": "working", "nfc_tag_id": "TAG-0001", "task_id": 1}
    """
    data = request.get_json(silent=True) or {}
    conn = db.get_db()
    eq = conn.execute("SELECT * FROM equipment WHERE module_id = ?", (module_id,)).fetchone()
    if not eq:
        conn.close()
        return jsonify({"error": "unknown module_id"}), 404

    worker_id = None
    if data.get("nfc_tag_id"):
        w = conn.execute("SELECT id FROM workers WHERE nfc_tag_id = ?", (data["nfc_tag_id"],)).fetchone()
        worker_id = w["id"] if w else None

    conn.execute(
        """UPDATE equipment SET status = ?, current_worker_id = ?, current_task_id = ?,
           updated_at = datetime('now','localtime') WHERE id = ?""",
        (data.get("status", eq["status"]), worker_id, data.get("task_id"), eq["id"]),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/workers/<nfc_tag_id>/next_task", methods=["GET"])
def api_next_task(nfc_tag_id):
    """NFCタッチ時: その作業者に表示すべき次のタスクを優先度順に返す"""
    conn = db.get_db()
    w = conn.execute("SELECT * FROM workers WHERE nfc_tag_id = ?", (nfc_tag_id,)).fetchone()
    if not w:
        conn.close()
        return jsonify({"error": "unknown tag"}), 404
    rows = conn.execute("""
        SELECT id, title, priority, difficulty, quantity, deadline, status, equipment_id FROM tasks
        WHERE status IN ('todo', 'assigned', 'in_progress')
          AND (assigned_worker_id = ? OR assigned_worker_id IS NULL)
        ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,
                 deadline IS NULL, deadline
        LIMIT 5
    """, (w["id"],)).fetchall()
    conn.close()
    return jsonify({"worker": {"id": w["id"], "name": w["name"]}, "tasks": [dict(r) for r in rows]})


@app.route("/api/tasks/<int:task_id>/start", methods=["POST"])
def api_start_task(task_id):
    """
    モジュールでのタッチ開始（1回目）で呼ぶ。タスクを着手状態にし、対応する機材もロックする。
    body 例: {"nfc_tag_id": "TAG-0001", "module_id": "MOD-A-01"}
    """
    data = request.get_json(silent=True) or {}
    conn = db.get_db()
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        conn.close()
        return jsonify({"error": "task not found"}), 404

    worker = conn.execute("SELECT * FROM workers WHERE nfc_tag_id = ?", (data.get("nfc_tag_id"),)).fetchone()
    equipment = conn.execute("SELECT * FROM equipment WHERE module_id = ?", (data.get("module_id"),)).fetchone()
    if not worker or not equipment:
        conn.close()
        return jsonify({"error": "unknown worker or module_id"}), 404

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    started_at = task["started_at"] or now
    conn.execute(
        """UPDATE tasks SET status = 'in_progress', assigned_worker_id = ?, equipment_id = ?,
           started_at = ? WHERE id = ?""",
        (worker["id"], equipment["id"], started_at, task_id),
    )
    conn.execute(
        """UPDATE equipment SET status = 'working', current_worker_id = ?, current_task_id = ?,
           updated_at = datetime('now','localtime') WHERE id = ?""",
        (worker["id"], task_id, equipment["id"]),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "started_at": started_at})


@app.route("/api/tasks", methods=["POST"])
def api_create_task():
    """音声入力（Whisper→Gemma でタスク化した結果）からの登録を想定"""
    data = request.get_json(silent=True) or {}
    if not data.get("title"):
        return jsonify({"error": "title required"}), 400
    conn = db.get_db()
    cur = conn.execute(
        """INSERT INTO tasks (title, description, difficulty, priority, quantity, deadline)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            data["title"],
            data.get("description", ""),
            int(data.get("difficulty", 3)),
            data.get("priority", "normal"),
            int(data.get("quantity", 1)),
            data.get("deadline"),
        ),
    )
    conn.commit()
    task_id = cur.lastrowid
    conn.close()
    return jsonify({"ok": True, "task_id": task_id}), 201


@app.route("/api/tasks/<int:task_id>/complete", methods=["POST"])
def api_complete_task(task_id):
    """モジュールの完了タッチ。所要時間を記録し、AIの学習フックを呼ぶ"""
    conn = db.get_db()
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        conn.close()
        return jsonify({"error": "not found"}), 404
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    duration = None
    if task["started_at"]:
        duration = int(
            (datetime.strptime(now, "%Y-%m-%d %H:%M:%S")
             - datetime.strptime(task["started_at"], "%Y-%m-%d %H:%M:%S")).total_seconds()
        )
    conn.execute("UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?", (now, task_id))
    if task["assigned_worker_id"]:
        conn.execute(
            """INSERT INTO work_logs (task_id, worker_id, equipment_id, started_at, completed_at, duration_sec)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (task_id, task["assigned_worker_id"], task["equipment_id"], task["started_at"], now, duration),
        )
        logs = [dict(l) for l in conn.execute("SELECT * FROM work_logs").fetchall()]
        ai_stub.update_model(dict(task), task["assigned_worker_id"], duration or 0, logs)
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "duration_sec": duration})


if __name__ == "__main__":
    db.init_db()
    app.run(debug=True, host="0.0.0.0", port=5000)
