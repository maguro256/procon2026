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
import os
import json
import socket
import threading
from datetime import datetime

from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
import paho.mqtt.client as mqtt
import requests

import db
import ai_stub

app = Flask(__name__)
app.secret_key = "dev-secret-change-me"  # flash用。本番では変更する

# 未登録NFCタグの一時保持: {tag_id: module_id}
_pending_tags: dict = {}
# どの機材にも紐付いていないモジュールの一時保持: {device_id: {"ip":..., "seen_at":...}}
_pending_modules: dict = {}

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
    return render_template("workers.html", workers=rows, pending_tags=_pending_tags)


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
        _pending_tags.pop(nfc, None)
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
    return render_template("equipment.html", equipment=rows, pending_modules=_pending_modules)


@app.route("/equipment/add", methods=["POST"])
def add_equipment():
    name = request.form.get("name", "").strip()
    module_id = request.form.get("module_id", "").strip() or None
    hostname = request.form.get("hostname", "").strip() or None
    if not name:
        flash("機材名を入力してください", "error")
        return redirect(url_for("equipment"))
    conn = db.get_db()
    try:
        conn.execute("INSERT INTO equipment (name, module_id, hostname) VALUES (?, ?, ?)",
                     (name, module_id, hostname))
        conn.commit()
        flash(f"機材「{name}」を登録しました", "ok")
    except db.sqlite3.IntegrityError:
        flash("そのモジュールIDは既に使われています", "error")
    finally:
        conn.close()
    return redirect(url_for("equipment"))


@app.route("/equipment/bind", methods=["POST"])
def bind_equipment():
    """
    機材にモジュールのデバイスID(hostname)を紐付ける。MQTTの宛先解決に使う。
    一覧のインライン編集と、未登録モジュールパネルの両方から呼ばれる。
    """
    eq_id = request.form.get("equipment_id", "").strip()
    hostname = request.form.get("hostname", "").strip() or None
    if not eq_id:
        flash("紐付け先の機材を選んでください", "error")
        return redirect(url_for("equipment"))

    conn = db.get_db()
    # 同じデバイスIDが複数機材に付くと宛先が一意に決まらないため、先に他を外す
    if hostname:
        conn.execute("UPDATE equipment SET hostname = NULL, online = 0 WHERE hostname = ? AND id != ?",
                     (hostname, eq_id))
    conn.execute("UPDATE equipment SET hostname = ? WHERE id = ?", (hostname, eq_id))

    # 紐付け前に受信していた死活情報を引き継ぐ。次のハートビートを待たずに
    # 「オンライン」と表示できる。
    pending = _pending_modules.pop(hostname, None) if hostname else None
    if pending:
        conn.execute(
            """UPDATE equipment SET online = 1, ip = COALESCE(?, ip),
               last_seen = datetime('now','localtime') WHERE id = ?""",
            (pending.get("ip"), eq_id),
        )
    elif not hostname:
        conn.execute("UPDATE equipment SET online = 0 WHERE id = ?", (eq_id,))
    conn.commit()
    conn.close()

    flash(f"デバイスID「{hostname}」を紐付けました" if hostname else "紐付けを解除しました", "ok")
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


@app.route("/api/unknown_tag", methods=["POST"])
def api_unknown_tag():
    """hard/main.py から未登録タグの通知を受け取り、管理画面に表示するために保持する"""
    data = request.get_json(silent=True) or {}
    tag_id = data.get("nfc_tag_id", "").strip()
    module_id = data.get("module_id", "")
    if not tag_id:
        return jsonify({"error": "nfc_tag_id required"}), 400
    conn = db.get_db()
    already = conn.execute("SELECT id FROM workers WHERE nfc_tag_id = ?", (tag_id,)).fetchone()
    conn.close()
    if already:
        return jsonify({"ok": True, "note": "already registered"})
    _pending_tags[tag_id] = module_id
    return jsonify({"ok": True, "pending": len(_pending_tags)})


# ------------------------------------------------ MQTT ブリッジ（旧 hard/main.py を統合）
# ラズパイ(raspi/raspi.py)が publish する NFC タッチを購読し、
# 上の HTTP API を localhost 経由で叩いて割当・ロックを行う。app.py 内の
# バックグラウンドスレッドで動くので、別プロセス(hard/main.py)は不要。

MQTT_HOST = os.environ.get("GEMMBA_MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("GEMMBA_MQTT_PORT", "1883"))
SELF_URL = os.environ.get("GEMMBA_SELF_URL", "http://127.0.0.1:5000")
HTTP_TIMEOUT = 5


def _find_equipment_by_device(conn, device_id):
    """
    デバイスID(ラズパイの DEVICE_ID) から機材を引く。
    モジュールの同一性はこの device_id で決まり、IPには依存しない。
    hostname への紐付けが本筋だが、module_id をそのままデバイス名に
    している構成でも動くよう両方を見る（hostname 一致を優先）。
    """
    return conn.execute(
        """SELECT id, name, module_id FROM equipment
           WHERE hostname = ? OR module_id = ?
           ORDER BY (hostname = ?) DESC LIMIT 1""",
        (device_id, device_id, device_id),
    ).fetchone()


def _resolve_module_id(device_id):
    """NFCタッチの宛先となる module_id を返す。通信があった証拠として last_seen も更新する"""
    conn = db.get_db()
    row = _find_equipment_by_device(conn, device_id)
    if row:
        conn.execute(
            "UPDATE equipment SET last_seen = datetime('now','localtime'), online = 1 WHERE id = ?",
            (row["id"],),
        )
        conn.commit()
    conn.close()
    return row["module_id"] if row else None


def _mqtt_on_connect(client, userdata, flags, reason_code, properties):
    print(f"[MQTT] connected to broker (reason={reason_code})")
    # status は retained で publish されるので、購読した瞬間に現在オンラインの
    # モジュールが一括で流れてくる。app.py を再起動しても状態が復元される。
    client.subscribe([("pi/+/data", 0), ("pi/+/status", 1)])


def _mqtt_on_message(client, userdata, msg):
    # topic: pi/<device_id>/data （NFCタッチ） または pi/<device_id>/status （死活）
    parts = msg.topic.split("/")
    if len(parts) < 3:
        return
    device_id, kind = parts[1], parts[2]
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except ValueError:
        print(f"[{device_id}] invalid JSON payload: {msg.payload!r}")
        return

    if kind == "status":
        _handle_presence(device_id, payload)
        return

    tag_id = payload.get("tag_id")
    if not tag_id:
        print(f"[{device_id}] payload missing tag_id")
        return
    module_id = _resolve_module_id(device_id)  # pi01 → MOD-A-02
    if not module_id:
        print(f"[{device_id}] このデバイスIDに対応する機材がありません。"
              f"機材管理画面で「デバイスID」に '{device_id}' を設定してください。")
        return
    _handle_touch(module_id, tag_id)


def _handle_presence(device_id, payload):
    """
    モジュールの死活通知。ラズパイが接続時に online、切断時は LWT により
    ブローカーが offline を代理送信する。IPは変わっても device_id は不変なので、
    ここで受け取った ip は「今どこにいるか」の記録用として上書きするだけでよい。
    """
    online = bool(payload.get("online"))
    ip = payload.get("ip")
    conn = db.get_db()
    row = _find_equipment_by_device(conn, device_id)

    if not row:
        # 未登録モジュール。機材管理画面に出して紐付けを促す（未登録タグと同じ流れ）
        if online:
            _pending_modules[device_id] = {"ip": ip, "seen_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            print(f"[{device_id}] 未登録モジュールを検出 (ip={ip}) → 機材管理画面に表示")
        else:
            _pending_modules.pop(device_id, None)
        conn.close()
        return

    _pending_modules.pop(device_id, None)
    if online:
        conn.execute(
            """UPDATE equipment SET online = 1, ip = COALESCE(?, ip),
               last_seen = datetime('now','localtime') WHERE id = ?""",
            (ip, row["id"]),
        )
    else:
        # 切断時は last_seen を更新しない（最終「通信」時刻を残すため）
        conn.execute("UPDATE equipment SET online = 0 WHERE id = ?", (row["id"],))
    conn.commit()
    conn.close()
    print(f"[{device_id}] {row['name']} が{'オンライン' if online else 'オフライン'}になりました"
          + (f" (ip={ip})" if online and ip else ""))


def _handle_touch(module_id, tag_id):
    """1回のNFCタッチを機材の状態に応じて 開始/終了/拒否 に振り分ける"""
    try:
        w_resp = requests.get(f"{SELF_URL}/api/workers/{tag_id}/next_task", timeout=HTTP_TIMEOUT)
        eq_resp = requests.get(f"{SELF_URL}/api/equipment/{module_id}/status", timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        print(f"[{module_id}] self API unreachable: {e}")
        return

    if w_resp.status_code == 404:
        print(f"[{module_id}] unknown NFC tag: {tag_id} → notifying (作業者管理画面に表示)")
        try:
            requests.post(
                f"{SELF_URL}/api/unknown_tag",
                json={"nfc_tag_id": tag_id, "module_id": module_id},
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException as e:
            print(f"[{module_id}] failed to notify unknown_tag: {e}")
        return
    if eq_resp.status_code == 404:
        print(f"[{module_id}] module_id not registered as equipment: {module_id}")
        return

    worker = w_resp.json()["worker"]
    tasks = w_resp.json()["tasks"]
    equipment = eq_resp.json()

    if equipment["status"] in ("stopped", "maintenance"):
        print(f"[{module_id}] {worker['name']}: equipment unavailable ({equipment['status']})")
        return

    if equipment["status"] == "working":
        if equipment["current_worker_id"] == worker["id"]:
            _end_session(module_id, worker, equipment)
        else:
            print(f"[{module_id}] locked by another worker; rejecting {worker['name']}")
        return

    _start_session(module_id, tag_id, worker, equipment, tasks)


def _start_session(module_id, tag_id, worker, equipment, tasks):
    """空き機材でのタッチ = 開始。担当タスクがあれば着手、無ければフリー利用でロック"""
    candidate = next((t for t in tasks if t.get("equipment_id") in (None, equipment["id"])), None)
    if candidate:
        requests.post(
            f"{SELF_URL}/api/tasks/{candidate['id']}/start",
            json={"nfc_tag_id": tag_id, "module_id": module_id}, timeout=HTTP_TIMEOUT,
        )
        print(f"[{module_id}] {worker['name']} started task: {candidate['title']}")
    else:
        requests.post(
            f"{SELF_URL}/api/equipment/{module_id}/status",
            json={"status": "working", "nfc_tag_id": tag_id}, timeout=HTTP_TIMEOUT,
        )
        print(f"[{module_id}] {worker['name']} started free-use (no task)")

    elsewhere = next(
        (t for t in tasks
         if t.get("equipment_id") not in (None, equipment["id"]) and t["priority"] in ("urgent", "high")),
        None,
    )
    if elsewhere:
        print(f"[{module_id}] NOTE: {worker['name']} has higher-priority task elsewhere: {elsewhere['title']}")


def _end_session(module_id, worker, equipment):
    """使用中の本人が再タッチ = 終了。タスク中なら完了記録、フリー利用ならロック解除のみ"""
    task_id = equipment.get("current_task_id")
    if task_id:
        requests.post(f"{SELF_URL}/api/tasks/{task_id}/complete", timeout=HTTP_TIMEOUT)
        print(f"[{module_id}] {worker['name']} completed task #{task_id}")
    else:
        print(f"[{module_id}] {worker['name']} ended free-use")
    requests.post(
        f"{SELF_URL}/api/equipment/{module_id}/status",
        json={"status": "idle"}, timeout=HTTP_TIMEOUT,
    )


# --------------------------------------------------- ブローカー自動探索（UDP）
# ラズパイ側にPCのIPを固定で持たせると、DHCPでIPが変わるたびに手で書き換える
# ことになる。ラズパイがLANへブロードキャストで問い合わせ、ここが応答する。

DISCOVERY_PORT = int(os.environ.get("GEMMBA_DISCOVERY_PORT", "50505"))
DISCOVERY_REQUEST = b"GEMMBA_DISCOVER_V1"


def _outbound_ip_toward(peer_ip):
    """
    peer に到達するインターフェースの自IPを返す。
    このPCのようにWi-Fiが複数枚ある場合でも、問い合わせ元に届く側を自動で選べる。
    UDPなので connect() しても実際のパケットは飛ばない。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((peer_ip, 9))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def _discovery_responder(sock):
    while True:
        try:
            data, addr = sock.recvfrom(1024)
        except OSError as e:
            # Windowsでは、応答先が既にソケットを閉じていると次の recvfrom が
            # WSAECONNRESET(10054) を投げる。UDPなので無視して受信を続ける。
            if sock.fileno() == -1:
                print(f"[discovery] responder stopped: {e}")
                return
            continue
        if data.strip() != DISCOVERY_REQUEST:
            continue
        host = _outbound_ip_toward(addr[0])
        if not host:
            continue
        reply = json.dumps({"host": host, "port": MQTT_PORT}).encode("utf-8")
        try:
            sock.sendto(reply, addr)
            print(f"[discovery] {addr[0]} へ broker {host}:{MQTT_PORT} を通知しました")
        except OSError as e:
            print(f"[discovery] {addr[0]} への応答に失敗: {e}")


def start_discovery_responder():
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Windows限定。応答先が閉じていた場合にICMPを例外へ昇格させる挙動を止める
        if hasattr(socket, "SIO_UDP_CONNRESET"):
            try:
                sock.ioctl(socket.SIO_UDP_CONNRESET, False)
            except OSError:
                pass
        sock.bind(("", DISCOVERY_PORT))
    except OSError as e:
        print(f"[discovery] responder NOT started (udp/{DISCOVERY_PORT}): {e}")
        print("       → ラズパイ側は GEMMBA_BROKER_HOST でIPを直接指定してください。")
        return
    threading.Thread(target=_discovery_responder, args=(sock,), daemon=True).start()
    print(f"[discovery] responder started (udp/{DISCOVERY_PORT})")


def start_mqtt_bridge():
    """MQTT クライアントをバックグラウンドスレッドで起動する"""
    # 前回終了時の online が残っていると誤表示になる。購読時に retained な
    # status が流れてくるので、いったん全部落としてから真の状態を受け直す。
    conn = db.get_db()
    conn.execute("UPDATE equipment SET online = 0")
    conn.commit()
    conn.close()
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.on_connect = _mqtt_on_connect
        client.on_message = _mqtt_on_message
        client.connect(MQTT_HOST, MQTT_PORT, 60)
        threading.Thread(target=client.loop_forever, daemon=True).start()
        print(f"[MQTT] bridge started (broker {MQTT_HOST}:{MQTT_PORT})")
    except Exception as e:
        print(f"[MQTT] bridge NOT started (broker unreachable): {e}")
        print("       → Web/APIは動きますが、NFCタッチは受信されません。")


DEBUG = True

if __name__ == "__main__":
    db.init_db()
    # debug=True のリローダーは子プロセスで再実行されるため、実際に配信する
    # プロセス(WERKZEUG_RUN_MAIN)でのみ MQTT を起動して二重接続を防ぐ。
    if not DEBUG or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_discovery_responder()
        start_mqtt_bridge()
    app.run(debug=DEBUG, host="0.0.0.0", port=5000, threaded=True)
