"""
app.py - Gemmba 管理者画面（雛形）

起動:
    pip install flask
    python app.py
    → http://127.0.0.1:5000

構成:
    画面 (HTML)   : ダッシュボード / 作業者管理 / タスク管理 / 機材管理
    API  (JSON)   : モジュール(ラズパイ)や割り当てAIが叩くエンドポイント
"""
import os
import json
import queue
import socket
import tempfile
import threading
import time
from datetime import datetime
from uuid import uuid4

from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
import requests

try:
    # 実機（ESP32/ラズパイ）やブローカーが無い環境（デモ環境など）でも
    # 管理画面とAPIだけは動かしたいので、無ければ import で落とさず諦める。
    import paho.mqtt.client as mqtt
    _MQTT_LIB_AVAILABLE = True
except ImportError:
    mqtt = None
    _MQTT_LIB_AVAILABLE = False

import db
import ai_stub
import permissions as perms
from voice import intent

app = Flask(__name__)
app.secret_key = "dev-secret-change-me"  # flash用。本番では変更する

# Flask のデバッグモード（自動リロード＋ブラウザ上のデバッガ）。**既定は OFF。**
# host=0.0.0.0 で配信しているので、ON のままだと LAN 内の誰でもデバッガから
# このPCでコードを実行できてしまう。開発時だけ GEMMBA_DEBUG=1 を付けて起動する。
# 起動後には切り替えられない（管理画面のボタンで切り替わるのは下のテストモード）。
DEBUG = os.environ.get("GEMMBA_DEBUG") == "1"

# テストモード: /test の仮想モジュールを使えるようにする。管理画面のサイドバーの
# ボタンで切り替える。本番では OFF にしておく（実機と同じモジュールIDで仮想
# モジュールを起動すると、実機への指示を横取りできてしまうため）。
# 状態はメモリにだけ持つので、app.py を再起動すると OFF に戻る。
_test_mode = {"on": DEBUG or os.environ.get("GEMMBA_TEST_PAGE") == "1"}

# 未登録NFCタグの一時保持: {tag_id: module_id}
_pending_tags: dict = {}
# 未登録タグが最後にタッチされた時刻: {tag_id: time.time()}。管理画面のポップアップを
# 「タッチ1回につき1回」だけ出すための目印（画面を移るたびに出し直さない）
_pending_tag_touched: dict = {}
# どの機材にも紐付いていないモジュールの一時保持: {device_id: {"ip":..., "seen_at":...}}
_pending_modules: dict = {}
# モジュールの起動セッション: {device_id: session}。再起動の検出に使う
_module_sessions: dict = {}
# 上の3つは Flask のリクエストスレッドと MQTT 受信スレッドの両方から読み書きされる
_pending_lock = threading.Lock()

PRIORITY_LABELS = {"urgent": "至急", "high": "高", "normal": "通常", "low": "低"}
# 完了直後に現場で答えてもらう体感難易度。順番がそのままモジュールの選択肢の並びになる
FELT_CODES = ["easy", "normal", "hard"]
FELT_LABELS = {"easy": "簡単", "normal": "普通", "hard": "難しい"}
STATUS_LABELS = {"todo": "未着手", "assigned": "割当済", "in_progress": "作業中", "done": "完了"}
EQ_STATUS_LABELS = {"idle": "空き", "working": "稼働中", "stopped": "停止", "maintenance": "メンテ中"}

# WariAthena の文脈ベクトルには work_logs だけでは足りない（難易度・数量・必要権限と
# 勤続年数が要る）ので tasks / workers を結合して渡す。作業者やタスクが消されたログも
# 報酬計算には使えるよう LEFT JOIN。機材は work_logs.equipment_id（実際に使った機材）を使う。
AI_LOG_SQL = """
    SELECT l.*, t.difficulty AS difficulty, t.quantity AS quantity,
           t.required_permissions AS required_permissions,
           w.years_of_service AS years_of_service
    FROM work_logs l
    LEFT JOIN tasks   t ON t.id = l.task_id
    LEFT JOIN workers w ON w.id = l.worker_id
    ORDER BY l.completed_at, l.id
"""


def _ai_logs(conn):
    """ai_stub に渡す学習データ。呼び出し側は開いた conn をそのまま渡す"""
    return [dict(r) for r in conn.execute(AI_LOG_SQL).fetchall()]


def _like_escape(text):
    """
    LIKE の % / _ をワイルドカードとして解釈させない。検索欄に "50%" のような
    文字列を打たれても、そのまま部分一致の対象として扱うため。
    """
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _safe_int(value, default):
    """
    フォームやJSONの数値項目を int にする。空文字や null が来ると
    int() がそのまま例外を投げて500になるので、その場合は default を返す。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@app.context_processor
def inject_labels():
    # perms は権限コード → 表示名の変換と、テンプレート側でのチェック状態の判定に使う
    return dict(P=PRIORITY_LABELS, S=STATUS_LABELS, E=EQ_STATUS_LABELS, F=FELT_LABELS,
                PERMISSIONS=perms.PERMISSIONS, ROLES=perms.ROLES, perms=perms,
                test_mode=_test_mode["on"])


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
    with _pending_lock:
        pending_tags = dict(_pending_tags)
    unlinked = sorted((w for w in rows if not w["nfc_tag_id"]), key=lambda w: w["name"])
    return render_template("workers.html", workers=rows, pending_tags=pending_tags,
                           unlinked_workers=unlinked)


def _back_to(default_endpoint):
    """
    フォームの next へ戻る。ポップアップはどの画面からでも出るので、登録後に
    作業者管理へ飛ばされると元の画面を見失う。外部URLへは飛ばさない。
    """
    nxt = request.form.get("next", "")
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect(url_for(default_endpoint))


@app.route("/workers/add", methods=["POST"])
def add_worker():
    name = request.form.get("name", "").strip()
    years = request.form.get("years_of_service", "0").strip()
    nfc = request.form.get("nfc_tag_id", "").strip() or None
    role = request.form.get("role") or perms.DEFAULT_ROLE
    held = perms.dump(request.form.getlist("permissions"))
    if not name:
        flash("名前を入力してください", "error")
        return _back_to("workers")
    conn = db.get_db()
    try:
        conn.execute(
            "INSERT INTO workers (name, years_of_service, role, permissions, nfc_tag_id) VALUES (?, ?, ?, ?, ?)",
            (name, float(years or 0), role, held, nfc),
        )
        conn.commit()
        with _pending_lock:
            _pending_tags.pop(nfc, None)
            _pending_tag_touched.pop(nfc, None)
        flash(f"{name} さんを登録しました", "ok")
    except db.sqlite3.IntegrityError:
        flash("そのICタグIDは既に使われています", "error")
    finally:
        conn.close()
    return _back_to("workers")


@app.route("/workers/link_tag", methods=["POST"])
def link_worker_tag():
    """
    タッチされた未登録タグを、ICタグIDを空欄のまま登録してあった作業者に紐付ける。
    カードを後から配った人のための口。既にタグを持っている人の付け替えは
    誤操作で他人のカードを奪わないよう、ここでは受けず一覧の編集からに限る。
    """
    nfc = request.form.get("nfc_tag_id", "").strip()
    worker_id = request.form.get("worker_id", "").strip()
    if not nfc or not worker_id:
        flash("紐付ける作業者を選んでください", "error")
        return _back_to("workers")
    conn = db.get_db()
    try:
        row = conn.execute("SELECT name, nfc_tag_id FROM workers WHERE id = ?", (worker_id,)).fetchone()
        if not row:
            flash("作業者が見つかりません", "error")
        elif row["nfc_tag_id"]:
            flash(f"{row['name']} さんには既に別のICタグが紐付いています", "error")
        else:
            conn.execute("UPDATE workers SET nfc_tag_id = ? WHERE id = ?", (nfc, worker_id))
            conn.commit()
            with _pending_lock:
                _pending_tags.pop(nfc, None)
            _pending_tag_touched.pop(nfc, None)
            flash(f"このICタグを {row['name']} さんに紐付けました", "ok")
    except db.sqlite3.IntegrityError:
        flash("そのICタグIDは既に別の作業者が使っています", "error")
    finally:
        conn.close()
    return _back_to("workers")


@app.route("/workers/<int:worker_id>/update", methods=["POST"])
def update_worker(worker_id):
    """
    役職・保有権限・勤続年数・腕輪ICタグIDの変更（D-2）。資格は後から取るものなので
    編集口が要る。ICタグは紛失・再発行があるので、登録後でも付け替えられるようにする。
    """
    f = request.form
    nfc = f.get("nfc_tag_id", "").strip() or None
    conn = db.get_db()
    row = conn.execute("SELECT name FROM workers WHERE id = ?", (worker_id,)).fetchone()
    if not row:
        conn.close()
        flash("作業者が見つかりません", "error")
        return redirect(url_for("workers"))
    try:
        conn.execute(
            "UPDATE workers SET years_of_service = ?, role = ?, permissions = ?, nfc_tag_id = ? WHERE id = ?",
            (float(f.get("years_of_service") or 0), f.get("role") or perms.DEFAULT_ROLE,
             perms.dump(f.getlist("permissions")), nfc, worker_id),
        )
        conn.commit()
        with _pending_lock:
            _pending_tags.pop(nfc, None)
            _pending_tag_touched.pop(nfc, None)
        flash(f"{row['name']} さんの役職・権限・ICタグを更新しました", "ok")
    except db.sqlite3.IntegrityError:
        flash("そのICタグIDは既に別の作業者が使っています", "error")
    finally:
        conn.close()
    return redirect(url_for("workers"))


@app.route("/workers/<int:worker_id>/delete", methods=["POST"])
def delete_worker(worker_id):
    conn = db.get_db()
    conn.execute("UPDATE tasks SET assigned_worker_id = NULL WHERE assigned_worker_id = ?", (worker_id,))
    conn.execute("UPDATE equipment SET current_worker_id = NULL WHERE current_worker_id = ?", (worker_id,))
    logs = _detach_work_logs(conn, "worker_id", worker_id)
    conn.execute("DELETE FROM workers WHERE id = ?", (worker_id,))
    conn.commit()
    conn.close()
    flash("作業者を削除しました" + (f"（実績 {logs} 件は残しています）" if logs else ""), "ok")
    return redirect(url_for("workers"))


@app.route("/tasks")
def tasks():
    # タスク一覧・完了済みタスクの両方に効く検索（タスク名・担当者名の部分一致）。
    # GETのクエリ文字列なので、検索結果のURLをそのまま共有・ブックマークできる。
    q_title = request.args.get("q_title", "").strip()
    q_worker = request.args.get("q_worker", "").strip()

    conditions = []
    params = []
    if q_title:
        conditions.append("t.title LIKE ? ESCAPE '\\'")
        params.append(_like_escape(q_title))
    if q_worker:
        conditions.append("w.name LIKE ? ESCAPE '\\'")
        params.append(_like_escape(q_worker))
    where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    conn = db.get_db()
    rows = conn.execute(f"""
        SELECT t.*, w.name AS worker_name, e.name AS equipment_name,
               l.felt_difficulty AS felt_difficulty
        FROM tasks t
        LEFT JOIN workers w   ON w.id = t.assigned_worker_id
        LEFT JOIN equipment e ON e.id = t.equipment_id
        -- 完了後に現場で答えてもらった体感難易度。同じタスクを繰り返し実績に
        -- 残すことがあるので、答えのある一番新しい1件だけを引く
        LEFT JOIN work_logs l ON l.id = (
            SELECT id FROM work_logs
            WHERE task_id = t.id AND felt_difficulty IS NOT NULL
            ORDER BY id DESC LIMIT 1)
        {where_sql}
        ORDER BY CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,
                 t.deadline IS NULL, t.deadline
    """, params).fetchall()

    # 完了済みは別欄に出す。優先度順のままだと未着手の列に紛れて探しにくいので、
    # 完了が新しい順に並べ替える。
    active_tasks = [t for t in rows if t["status"] != "done"]
    done_tasks = sorted((t for t in rows if t["status"] == "done"),
                        key=lambda t: t["completed_at"] or "", reverse=True)

    worker_list = conn.execute("SELECT id, name FROM workers ORDER BY name").fetchall()
    equipment_list = conn.execute("SELECT id, name FROM equipment ORDER BY name").fetchall()
    conn.close()
    return render_template("tasks.html", active_tasks=active_tasks, done_tasks=done_tasks,
                           worker_list=worker_list, equipment_list=equipment_list,
                           q_title=q_title, q_worker=q_worker)


@app.route("/tasks/add", methods=["POST"])
def add_task():
    f = request.form
    title = f.get("title", "").strip()
    if not title:
        flash("タスク名を入力してください", "error")
        return redirect(url_for("tasks"))
    conn = db.get_db()
    conn.execute(
        """INSERT INTO tasks (title, description, difficulty, priority, required_permissions,
                            quantity, deadline, equipment_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            title,
            f.get("description", "").strip(),
            _safe_int(f.get("difficulty"), 3),
            f.get("priority", "normal"),
            perms.dump(f.getlist("required_permissions")),
            _safe_int(f.get("quantity"), 1) or 1,
            f.get("deadline") or None,
            f.get("equipment_id") or None,
        ),
    )
    conn.commit()
    conn.close()
    flash(f"タスク「{title}」を登録しました", "ok")
    return redirect(url_for("tasks"))


@app.route("/tasks/<int:task_id>/update", methods=["POST"])
def update_task(task_id):
    """手動編集（担当者・優先度・数量・期限・機材・必要権限）。状態はNFCタッチ側でのみ変わる"""
    f = request.form
    conn = db.get_db()
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        conn.close()
        flash("タスクが見つかりません", "error")
        return _back_to("tasks")

    worker_id = f.get("assigned_worker_id") or None
    equipment_id = f.get("equipment_id") or None
    priority = f.get("priority", task["priority"])
    quantity = _safe_int(f.get("quantity") or task["quantity"], 1) or 1
    deadline = f.get("deadline") or None

    # 必要権限（D-2）。チェックボックスは未チェックだと POST に現れないので、
    # フォームに含まれていたことを隠しフィールドで見分ける。含まれない経路から
    # 呼ばれたときに既存の設定を消さないため。
    if "req_perm_form" in f:
        required_perms = perms.dump(f.getlist("required_permissions"))
    else:
        required_perms = task["required_permissions"]

    # 手動割り当てでも権限は無視できない。判定は「このフォームで指定された必要権限」
    # に対して行うので、必要権限を外すのと同時に割り当てる操作は通る。
    if worker_id:
        cand = conn.execute("SELECT * FROM workers WHERE id = ?", (worker_id,)).fetchone()
        lacking = perms.missing(cand, {"required_permissions": required_perms}) if cand else []
        if lacking:
            conn.close()
            flash(f"{cand['name']} さんは権限が足りないため割り当てできません"
                  f"（不足: {'・'.join(perms.labels(lacking))}）", "error")
            return _back_to("tasks")

    # status はここでは触らない。着手・完了はNFCタッチ側でしか起きない設計にしてある
    # （画面から done にできると、所要時間の入っていない実績が混ざる）。
    conn.execute(
        """UPDATE tasks SET assigned_worker_id = ?, equipment_id = ?, priority = ?,
           quantity = ?, deadline = ?, required_permissions = ? WHERE id = ?""",
        (worker_id, equipment_id, priority, quantity, deadline, required_perms, task_id),
    )
    conn.commit()
    conn.close()
    flash("タスクを更新しました", "ok")
    return _back_to("tasks")


@app.route("/tasks/<int:task_id>/auto_assign", methods=["POST"])
def auto_assign(task_id):
    """AI割り当て（WariAthena = ai_stub。work_logs から学習した事後分布で選ぶ）"""
    conn = db.get_db()
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        conn.close()
        flash("タスクが見つかりません", "error")
        return _back_to("tasks")
    workers_ = conn.execute("SELECT * FROM workers").fetchall()
    logs = _ai_logs(conn)
    # 権限（D-2）はハード制約なので、学習器に渡す前に候補から落とす。
    # 無資格者を選ばせてから弾くのでは、AI が選べなかった理由を説明できない。
    eligible = perms.eligible_workers([dict(w) for w in workers_], task)
    dropped = len(workers_) - len(eligible)
    # 各人の手持ち（割り当て済み・作業中）。多い人ほど選ばれにくくして負荷を分散する。
    # 割り当て直しのときに自分自身を手持ちに数えないよう、このタスクは除く
    open_tasks = [dict(r) for r in conn.execute(
        """SELECT id, difficulty, quantity, status, started_at, assigned_worker_id FROM tasks
           WHERE status IN ('assigned', 'in_progress') AND assigned_worker_id IS NOT NULL
             AND id != ?""", (task_id,))]
    wid = ai_stub.assign_task(dict(task), eligible, logs, open_tasks=open_tasks)
    if wid:
        conn.execute("UPDATE tasks SET assigned_worker_id = ?, status = 'assigned' WHERE id = ?", (wid, task_id))
        conn.commit()
        loads = ai_stub.workloads(open_tasks, logs)
        chosen = next(w["name"] for w in eligible if w["id"] == wid)
        note = f"（{chosen} さん / 手持ち 約{loads.get(wid, 0) / 3600:.1f}時間 / 実績 {len(logs)} 件から学習"
        note += f" / 権限不足の {dropped} 名を除外）" if dropped else "）"
        if ai_stub.is_ready():
            flash(f"AIがタスクを割り当てました{note}", "ok")
        else:
            flash("AI本体を読み込めなかったため、勤続年数で暫定割り当てしました", "error")
    elif dropped:
        flash(f"必要権限（{'・'.join(perms.labels(task['required_permissions']))}）を"
              f"持つ作業者がいません", "error")
    else:
        flash("割り当て候補がいません", "error")
    conn.close()
    return _back_to("tasks")


@app.route("/tasks/<int:task_id>/delete", methods=["POST"])
def delete_task(task_id):
    conn = db.get_db()
    conn.execute("UPDATE equipment SET current_task_id = NULL WHERE current_task_id = ?", (task_id,))
    logs = _detach_work_logs(conn, "task_id", task_id)
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    flash("タスクを削除しました" + (f"（実績 {logs} 件は残しています）" if logs else ""), "ok")
    return _back_to("tasks")


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
    with _pending_lock:
        pending_modules = dict(_pending_modules)
    return render_template("equipment.html", equipment=rows, pending_modules=pending_modules)


@app.route("/equipment/add", methods=["POST"])
def add_equipment():
    name = request.form.get("name", "").strip()
    # 機材コード(module_id)は画面では入力させず、登録後に採番する。
    # 古い画面やスクリプトから送られてきたときだけそのまま使う
    module_id = request.form.get("module_id", "").strip() or None
    hostname = request.form.get("hostname", "").strip() or None
    if not name:
        flash("機材名を入力してください", "error")
        return redirect(url_for("equipment"))
    conn = db.get_db()
    try:
        cur = conn.execute("INSERT INTO equipment (name, module_id, hostname) VALUES (?, ?, ?)",
                           (name, module_id, hostname))
        conn.commit()
        if not module_id:
            # 空のままだとタッチが宛先不明で捨てられるので、ここで採番しておく
            row = conn.execute("SELECT id, name, module_id FROM equipment WHERE id = ?",
                               (cur.lastrowid,)).fetchone()
            module_id = _ensure_module_id(conn, row)
        flash(f"機材「{name}」を登録しました", "ok")
    except db.sqlite3.IntegrityError:
        flash("その機材コードは既に使われています", "error")
    finally:
        conn.close()
    return redirect(url_for("equipment"))


@app.route("/equipment/bind", methods=["POST"])
def bind_equipment():
    """
    機材にモジュールID(hostname)を紐付ける。MQTTの宛先解決に使う。
    一覧のインライン編集と、未登録モジュールパネルの両方から呼ばれる。
    """
    eq_id = request.form.get("equipment_id", "").strip()
    hostname = request.form.get("hostname", "").strip() or None
    if not eq_id:
        flash("紐付け先の機材を選んでください", "error")
        return redirect(url_for("equipment"))

    conn = db.get_db()
    _bind_module(conn, eq_id, hostname)
    conn.close()

    flash(f"モジュールID「{hostname}」を紐付けました" if hostname else "紐付けを解除しました", "ok")
    return redirect(url_for("equipment"))


def _bind_module(conn, eq_id, hostname):
    """機材にモジュールIDを付け替える。/test の仮想モジュールからも使う"""
    # 同じモジュールIDが複数機材に付くと宛先が一意に決まらないため、先に他を外す
    if hostname:
        conn.execute("UPDATE equipment SET hostname = NULL, online = 0 WHERE hostname = ? AND id != ?",
                     (hostname, eq_id))
    conn.execute("UPDATE equipment SET hostname = ? WHERE id = ?", (hostname, eq_id))

    # 紐付け前に受信していた死活情報を引き継ぐ。次のハートビートを待たずに
    # 「オンライン」と表示できる。
    with _pending_lock:
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


@app.route("/equipment/<int:eq_id>/delete", methods=["POST"])
def delete_equipment(eq_id):
    conn = db.get_db()
    conn.execute("UPDATE tasks SET equipment_id = NULL WHERE equipment_id = ?", (eq_id,))
    # 実績は消さない。どの機材だったかは分からなくなるが、所要時間と担当者は
    # WariAthena の学習データなので残す（AI_LOG_SQL が LEFT JOIN しているのはこのため）
    logs = _detach_work_logs(conn, "equipment_id", eq_id)
    conn.execute("DELETE FROM equipment WHERE id = ?", (eq_id,))
    conn.commit()
    conn.close()
    flash("機材を削除しました" + (f"（実績 {logs} 件は残しています）" if logs else ""), "ok")
    return redirect(url_for("equipment"))


@app.route("/equipment/<int:eq_id>/restart", methods=["POST"])
def restart_module(eq_id):
    """
    モジュールのプログラムを再起動させる。

    現場のラズパイに ssh で入らずに復帰させるための口。向こうは終了ではなく
    os.execv で自分を作り直すので、起動引数（GEMMBA_INPUT=buttons など）は
    そのまま引き継がれる。**機材の電源は切らない。** 落ちるのはプログラムだけ。
    """
    conn = db.get_db()
    eq = conn.execute("SELECT * FROM equipment WHERE id = ?", (eq_id,)).fetchone()
    conn.close()
    if not eq:
        flash("機材が見つかりません", "error")
        return redirect(url_for("equipment"))

    device_id = _device_id_of(eq)
    if not device_id:
        flash(f"「{eq['name']}」にモジュールが紐付いていません", "error")
        return redirect(url_for("equipment"))
    if not eq["online"]:
        # retain されない cmd なので、繋がっていない相手に送っても消えるだけ
        flash(f"「{eq['name']}」はオフラインです。指示が届きません", "error")
        return redirect(url_for("equipment"))

    # 操作中だと、作業者は理由が分からないまま画面が消える。止めはしないが伝える
    with _touch_busy_lock:
        busy = device_id in _touch_busy

    if not send_cmd(device_id, "restart"):
        flash("ブローカーに繋がっていないため指示を送れませんでした", "error")
        return redirect(url_for("equipment"))

    print(f"[{device_id}] 管理画面からモジュールの再起動を指示しました")
    flash(f"「{eq['name']}」のモジュールを再起動しています"
          + ("（操作中だったので、その操作は中断されます）" if busy else ""), "ok")
    return redirect(url_for("equipment"))


def _detach_work_logs(conn, column, value):
    """
    削除される機材・作業者・タスクを参照している実績の参照だけを外す。

    実績そのものは消さない。**外部キーが NOT NULL のままだと、実績のある行は
    削除しようとした時点で 500 になる**（2026-09-15 に実際に起きた）。
    スキーマ側は db._relax_work_logs() で NULL 可にしてある。
    """
    n = conn.execute(f"SELECT COUNT(*) FROM work_logs WHERE {column} = ?", (value,)).fetchone()[0]
    if n:
        conn.execute(f"UPDATE work_logs SET {column} = NULL WHERE {column} = ?", (value,))
    return n


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


@app.route("/api/pending", methods=["GET"])
def api_pending():
    """
    未登録のICタグ・モジュールの一覧。管理画面がポーリングして、タッチされた
    瞬間に登録フォームをポップアップで出すために使う。
    """
    conn = db.get_db()
    names = {r["module_id"]: r["name"] for r in
             conn.execute("SELECT module_id, name FROM equipment WHERE module_id IS NOT NULL")}
    # タグ未紐付けの作業者。ポップアップで「登録済みの人に紐付け」の候補にする
    unlinked = [{"id": r["id"], "name": r["name"]} for r in
                conn.execute("SELECT id, name FROM workers WHERE nfc_tag_id IS NULL ORDER BY name")]
    conn.close()
    with _replies_lock:
        confirms = [
            {"request_id": rid, "device_id": s["device_id"], "text": s["text"],
             "lines": s["lines"], "options": s.get("options"),
             "equipment_name": s["equipment_name"],
             "worker_name": s["worker_name"],
             # /test の仮想モジュール宛て。/test の画面ではポップアップを出さない
             "virtual": s["device_id"] in _virtual_modules}
            for rid, s in _pending_replies.items()
            if s.get("text") is not None and not s["event"].is_set()
        ]
    with _pending_lock:
        tags = [(tag, mod, _pending_tag_touched.get(tag)) for tag, mod in _pending_tags.items()]
        modules = list(_pending_modules.items())
    return jsonify({
        # touch: このタッチの識別子。ポップアップはこれ単位で一度だけ出す
        "tags": [{"tag_id": tag, "module_id": mod, "equipment_name": names.get(mod),
                  "touch": f"{tag}@{touched}"}
                 for tag, mod, touched in tags],
        "modules": [dict(info, device_id=dev) for dev, info in modules],
        "confirms": confirms,
        "unlinked_workers": unlinked,
    })


@app.route("/api/confirm/<request_id>", methods=["POST"])
def api_answer_confirm(request_id):
    """
    モジュールの代わりに管理画面から答える。Yes/No と選択肢の両方を受ける。

        {"answer": true}   … Yes/No の問い合わせ
        {"index": 2}       … 選択肢の問い合わせ（難易度フィードバックなど）

    モジュールに物理ボタン（TODO.md の B-2）が付くまでの操作手段。実運用では
    現場の作業者が機材の前で答えるのが本来の流れで、これはデモ用の抜け道。
    """
    data = request.get_json(silent=True) or {}
    with _replies_lock:
        slot = _pending_replies.get(request_id)
        if slot is None or slot["event"].is_set():
            return jsonify({"error": "その問い合わせは既に終わっています"}), 404
        options = slot.get("options")
        if options:
            try:
                index = int(data.get("index"))
            except (TypeError, ValueError):
                return jsonify({"error": "index required"}), 400
            if not 0 <= index < len(options):
                return jsonify({"error": "index out of range"}), 400
            slot["answer"] = index
        else:
            slot["answer"] = bool(data.get("answer"))
        slot["event"].set()
    return jsonify({"ok": True})


@app.route("/api/equipment/<module_id>/cmd", methods=["POST"])
def api_send_equipment_cmd(module_id):
    """
    モジュールへ指示を送る（下り通信の入口）。動作確認と、将来の管理画面からの
    呼び出しを想定している。
    body 例:
        {"cmd": "display", "lines": ["点検してください"]}
        {"cmd": "led", "state": "maintenance"}
        {"cmd": "confirm", "text": "この機材を使いますか？", "timeout": 20}
    confirm のときだけモジュールの応答を待ち、answer(true/false/null) を返す。
    """
    data = request.get_json(silent=True) or {}
    cmd = data.get("cmd")
    if not cmd:
        return jsonify({"error": "cmd required"}), 400

    conn = db.get_db()
    eq = conn.execute("SELECT * FROM equipment WHERE module_id = ?", (module_id,)).fetchone()
    conn.close()
    if not eq:
        return jsonify({"error": "unknown module_id"}), 404
    device_id = _device_id_of(eq)
    if not device_id:
        return jsonify({"error": "no device bound to this equipment"}), 409

    fields = {k: v for k, v in data.items() if k != "cmd"}
    if cmd == "confirm":
        answer = request_confirm(
            device_id,
            fields.pop("text", ""),
            timeout=float(fields.pop("timeout", CMD_TIMEOUT_SEC)),
            **fields,
        )
        return jsonify({"ok": answer is not None, "answer": answer})

    if not send_cmd(device_id, cmd, **fields):
        return jsonify({"error": "module unreachable"}), 503
    return jsonify({"ok": True})


@app.route("/api/workers/<nfc_tag_id>/next_task", methods=["GET"])
def api_next_task(nfc_tag_id):
    """
    NFCタッチ時: その作業者に表示すべきタスクを、提示する順に返す。

    並び順:
        1. 作業中のタスク（投げ出させない）
        2. 自分に割り当て済みのタスク（管理画面の「AI割当」などで決まったもの）
        3. 誰にも割り当てられていないタスク
    2と3の中はそれぞれ「優先度 → WariAthena の評価」の順。

    ?module_id= でタッチされた機材を渡すと、tasks はその機材で出来るもの
    （機材指定なし＋その機材）だけにし、他機材のものは elsewhere に分けて返す。
    件数は絞った後で切るので、この機材のタスクが他機材のタスクに押し出されない。
    elsewhere は他機材への誘導（C-3）の材料。
    """
    conn = db.get_db()
    w = conn.execute("SELECT * FROM workers WHERE nfc_tag_id = ?", (nfc_tag_id,)).fetchone()
    if not w:
        conn.close()
        return jsonify({"error": "unknown tag"}), 404
    rows = [dict(r) for r in conn.execute("""
        SELECT id, title, priority, difficulty, quantity, deadline, status, equipment_id,
               required_permissions, assigned_worker_id
        FROM tasks
        WHERE status IN ('todo', 'assigned', 'in_progress')
          AND (assigned_worker_id = ? OR assigned_worker_id IS NULL)
        ORDER BY created_at, id
    """, (w["id"],)).fetchall()]
    logs = _ai_logs(conn)
    conn.close()
    # 着手済みは投げ出させないよう先頭に固定する。ここは権限で落とさない。
    # 作業中に資格が取り消されても、完了して機材を解放する経路は残す必要がある。
    in_progress = [t for t in rows if t["status"] == "in_progress"]
    # 未着手の候補は権限（D-2）を満たすものだけ。この1行でモジュール側の
    # 候補提示（C-1/C-2）と他機材への誘導（C-3）の両方に効く。
    rest = perms.eligible_tasks(w, [t for t in rows if t["status"] != "in_progress"])
    # タッチされた機材（?module_id=）。機材指定の無いタスクを「この機材でやったら」で評価する
    eq_row = None
    if request.args.get("module_id"):
        conn = db.get_db()
        eq_row = conn.execute("SELECT id FROM equipment WHERE module_id = ?",
                              (request.args["module_id"],)).fetchone()
        conn.close()
    here_id = eq_row["id"] if eq_row else None
    ranked = ai_stub.rank_tasks(dict(w), rest, logs, equipment_id=here_id)
    # sort は安定なので、同じ組・同じ優先度の中では AI の並びがそのまま残る
    ranked.sort(key=lambda t: (0 if t["assigned_worker_id"] == w["id"] else 1,
                               PRIORITY_RANK.get(t.get("priority"), 9)))
    ordered = in_progress + ranked

    if eq_row is None:
        here, elsewhere = ordered, []
    else:
        here = [t for t in ordered if t["equipment_id"] in (None, here_id)]
        elsewhere = [t for t in ordered if t["equipment_id"] not in (None, here_id)]
    return jsonify({"worker": {"id": w["id"], "name": w["name"], "role": w["role"],
                               "permissions": perms.held(w)},
                    "tasks": here[:5], "elsewhere": elsewhere[:5]})


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

    # 候補は api_next_task で絞ってあるが、このAPIは単体でも叩けるので二重に見る（D-2）
    lacking = perms.missing(worker, task)
    if lacking:
        conn.close()
        print(f"[{data.get('module_id')}] {worker['name']}: 権限不足で着手を拒否 "
              f"({'/'.join(lacking)}) task={task['title']}")
        return jsonify({"error": "permission denied", "missing": lacking,
                        "missing_labels": perms.labels(lacking)}), 403

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
        """INSERT INTO tasks (title, description, difficulty, priority, required_permissions,
                            quantity, deadline)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            data["title"],
            data.get("description", ""),
            _safe_int(data.get("difficulty"), 3),
            data.get("priority", "normal"),
            perms.dump(data.get("required_permissions")),
            _safe_int(data.get("quantity"), 1) or 1,
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
    work_log_id = None
    if task["assigned_worker_id"]:
        cur = conn.execute(
            """INSERT INTO work_logs (task_id, worker_id, equipment_id, started_at, completed_at, duration_sec)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (task_id, task["assigned_worker_id"], task["equipment_id"], task["started_at"], now, duration),
        )
        work_log_id = cur.lastrowid
        ai_stub.update_model(dict(task), task["assigned_worker_id"], duration or 0, _ai_logs(conn))
    conn.commit()
    conn.close()
    # work_log_id は、この直後に現場で答えてもらう難易度フィードバックの宛先。
    # 先に完了させるのは、所要時間に「答えるのを待った時間」を混ぜないため。
    return jsonify({"ok": True, "duration_sec": duration, "work_log_id": work_log_id,
                    "title": task["title"]})


# 文字起こしは別のPythonで動かす。app.py は 3.8 固定だが faster-whisper は 3.9 以降
# しか入らないため（voice/requirements.txt 参照）。呼ぶたびにモデルを読み直すので
# 1回あたり4〜6秒かかる。常駐させたくなったら、ここをHTTP呼び出しに替えればよい。
VOICE_PYTHON = os.environ.get(
    "GEMMBA_STT_PYTHON",
    os.path.join(".venv-voice", "Scripts" if os.name == "nt" else "bin",
                 "python.exe" if os.name == "nt" else "python"))
# 文字起こしに使ってよい上限。GPUなら数秒で終わるが、CPUに落ちると large-v3 は
# 0.7倍速なので 30秒の録音に 50秒前後かかる。ここを伸ばすと下の録音の予算
# （RECORD_* ）がそのまま伸びて、現場の待ち時間になることに注意する。
VOICE_TIMEOUT = 90
# 常駐の起動を待つ上限。モデルが手元に無いと数GBのダウンロードから始まるので、
# 1回の文字起こし（VOICE_TIMEOUT）とは別物として長く取る
WARMUP_TIMEOUT = 1800
# タスク名の長さは voice/intent.py が持っている（文字起こしの整形もあちらの仕事）


# 文字起こしは **常駐プロセス** に任せる（E-2）。
#
# 以前は呼び出しのたびに stt.py を起こしていたが、モデルの読み込みだけで
# large-v3 / CPU なら30秒台かかり、それが毎回そのまま現場の待ち時間になっていた。
# 常駐にすれば読み込みは起動時の1回だけで済む。
#
# やりとりは「パスを1行送る → 結果のJSONが1行返る」だけ。__main__ 以外
# （テストなど）から import されたときは起動しないので、使う側は
# 今までどおり _transcribe() を呼べばよい。
_stt = {"proc": None, "out": None}   # out は stdout の行を積むキュー
_stt_lock = threading.Lock()         # 文字起こしは1件ずつ。並行させても速くならない


def _stt_spawn():
    """常駐プロセスを起こす。戻り値: Popen / None（起こせなかった）"""
    import subprocess

    if not os.path.exists(VOICE_PYTHON):
        print(f"[voice] 文字起こし用のPythonがありません: {VOICE_PYTHON}")
        return None
    try:
        proc = subprocess.Popen(
            [VOICE_PYTHON, os.path.join("voice", "stt.py"), "--serve"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=1, universal_newlines=True, encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"[voice] 文字起こしを起動できません: {e}")
        return None

    q = queue.Queue()

    def pump_out():
        for line in proc.stdout:
            q.put(line)
        q.put(None)          # 相手が死んだ。待っている側を起こす

    def pump_err():
        # 読み捨てずに出す。溜めたままにするとパイプが詰まって相手が止まる
        for line in proc.stderr:
            line = line.rstrip()
            if line:
                print(f"[stt] {line}")

    threading.Thread(target=pump_out, daemon=True).start()
    threading.Thread(target=pump_err, daemon=True).start()
    _stt["proc"], _stt["out"] = proc, q
    return proc


def _stt_kill():
    """常駐を畳む。次の依頼で作り直される"""
    proc = _stt["proc"]
    _stt["proc"], _stt["out"] = None, None
    if proc is None:
        return
    try:
        proc.kill()
    except OSError:
        pass


def _stt_reply(timeout):
    """常駐からの1行を読む。戻り値: dict / None（時間切れ・異常終了・壊れた行）"""
    q = _stt["out"]
    if q is None:
        return None
    try:
        line = q.get(timeout=timeout)
    except queue.Empty:
        return None
    if line is None:
        print("[voice] 文字起こしが終了しました")
        return None
    try:
        return json.loads(line)
    except ValueError:
        print(f"[voice] 応答を読めません: {line[:200]}")
        return None


def _stt_ensure():
    """常駐が生きていることを保証する。居なければ起こしてモデルの読み込みを待つ"""
    proc = _stt["proc"]
    if proc is not None and proc.poll() is None:
        return proc
    proc = _stt_spawn()
    if proc is None:
        return None
    # 最初の1行は準備完了の合図。**ここで読み捨てないと、次の依頼の答えとして
    # 受け取ってしまう。** モデルが手元に無ければダウンロードから始まるので長め
    ready = _stt_reply(WARMUP_TIMEOUT)
    if not (ready or {}).get("ready"):
        print(f"[voice] 文字起こしの準備に失敗しました: {ready}")
        _stt_kill()
        return None
    print(f"[voice] 文字起こしを常駐させました "
          f"{ready.get('model')} / {ready.get('device')} {ready.get('compute_type')}")
    return proc


def _transcribe(path):
    """録音を文字にする。戻り値: テキスト / None（失敗）"""
    with _stt_lock:
        if _stt_ensure() is None:
            return None
        try:
            _stt["proc"].stdin.write(path + "\n")
            _stt["proc"].stdin.flush()
        except (OSError, ValueError) as e:
            print(f"[voice] 文字起こしへ送れません: {e}")
            _stt_kill()
            return None
        res = _stt_reply(VOICE_TIMEOUT)
        if res is None:
            # どこまで進んだか分からない。次の依頼に前回の答えが混ざらないよう畳む
            print("[voice] 文字起こしが時間内に終わりませんでした")
            _stt_kill()
            return None
        if res.get("error"):
            print(f"[voice] 文字起こしに失敗: {res['error']}")
            return None
        return (res.get("text") or "").strip()


def start_stt_warmup():
    """
    起動時に文字起こしの常駐を起こしておく（E-2）。

    _transcribe() も必要になれば自分で起こすので、これが無くても動く。ただし
    その場合、**最初の録音だけ**がモデルの読み込み（手元に無ければ数GBの
    ダウンロード）を丸ごと被り、VOICE_TIMEOUT を使い切って「聞き取れません
    でした」になりかねない。先に済ませておけばそれが起きない。

    起動を止めないよう別スレッドで走らせる。失敗しても録音の時点で作り直せる
    ので、ここでは警告を出すだけにする。
    """
    def run():
        started = time.time()
        model = os.environ.get("GEMMBA_STT_MODEL", "既定")
        print(f"[voice] 文字起こし({model})を準備しています…")
        with _stt_lock:
            ok = _stt_ensure() is not None
        if ok:
            print(f"[voice] 文字起こしの準備ができました ({time.time() - started:.1f}秒)")

    threading.Thread(target=run, daemon=True).start()


# 音声を受け取ってから応答を返すまでの間、モジュールの画面は
# 「決定ボタンを押している間 話してください」のまま止まっている。文字起こしと
# 意図分析で数秒〜1分かかるので、そのままだと録り直しを誘う。節目で画面を送る。
PROGRESS_TEXT_MAX = 36   # 画面に出す聞き取り結果の長さ。溢れると本文が切れる


def _clip(text, limit=PROGRESS_TEXT_MAX):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _device_of_module(module_id):
    """module_id から、その機材に繋がっているモジュールの device_id を引く"""
    if not module_id:
        return None
    conn = db.get_db()
    row = conn.execute("SELECT hostname FROM equipment WHERE module_id = ?",
                       (module_id,)).fetchone()
    conn.close()
    if not row:
        return None
    # hostname が空でも、module_id をそのままデバイス名にしている構成がある
    # （_find_equipment_by_device と同じ扱い）
    return row["hostname"] or module_id


def _notify_progress(device_id, lines):
    """
    処理中であることを画面に出す。**LEDは触らない。**
    タスク登録中(recording)のままにしておかないと、周りから見た状態が変わる。
    """
    if device_id:
        _notify(device_id, lines)


@app.route("/api/voice", methods=["POST"])
def api_voice():
    """
    モジュールが録音した音声を受け取り、文字起こししてタスクにする（E-2）。

    body は WAV そのもの。誰がどの機材で話したかはクエリで受ける。
    文字起こし(voice/stt.py) → 意図分析(voice/intent.py) の順に同期で通してから
    INSERT する。**どちらが失敗しても登録は成立させる**（意図分析が落ちたら
    既定値のタスクが1件立つ）。現場で「何も残らない」のが一番困るため。
    """
    tag_id = request.args.get("tag_id")
    # 処理中の表示を出す先。紐付けが分からなければ黙って出さないだけで、登録は続ける
    device_id = _device_of_module(request.args.get("module_id"))
    audio = request.get_data()
    if len(audio) < 1000:
        return jsonify({"ok": False, "error": "音声が短すぎます"}), 400

    tmp = os.path.join(tempfile.gettempdir(), f"gemmba_voice_{uuid4().hex[:8]}.wav")
    try:
        with open(tmp, "wb") as fp:
            fp.write(audio)
        _notify_progress(device_id, ["音声を文字にしています", "そのままお待ちください"])
        text = _transcribe(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    if not text:
        return jsonify({"ok": False, "error": "聞き取れませんでした"})
    return jsonify(_register_task_from_text(tag_id, text, device_id))


def _register_task_from_text(tag_id, text, device_id=None):
    """
    文字起こし済みのテキストからタスクを立てる。/api/voice の後段で、
    仮想モジュール（/test）は録音の代わりにテキストを直接ここへ渡す。
    device_id は処理中の表示を出す先（無ければ出さない）。
    """
    # 意図分析（E-2）。Ollama が落ちていても analyze() は既定値の入った dict を
    # 返すので、ここに失敗の分岐は要らない。どちらを通ったかは source で分かる
    #
    # ここが一番長い（gemma3:4b で数秒、モデルの読み込みが入ると1分近い）。
    # 聞き取った内容を一緒に出すので、待つ間に言い直しの要否も判断できる
    _notify_progress(device_id, ["内容を解析しています", _clip(text)])
    parsed = intent.analyze(text)

    conn = db.get_db()
    worker = conn.execute("SELECT id FROM workers WHERE nfc_tag_id = ?", (tag_id,)).fetchone()
    cur = conn.execute(
        """INSERT INTO tasks (title, description, difficulty, priority,
                              required_permissions, quantity, deadline)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (parsed["title"], parsed["description"], parsed["difficulty"], parsed["priority"],
         perms.dump(parsed["required_permissions"]), parsed["quantity"], parsed["deadline"]),
    )
    conn.commit()
    task_id = cur.lastrowid
    conn.close()
    print(f"[voice] タスク#{task_id} を登録しました（{tag_id} / "
          f"worker={worker['id'] if worker else '?'} / {parsed['source']}）"
          f"「{parsed['title']}」 優先度={parsed['priority']} 難易度={parsed['difficulty']} "
          f"数量={parsed['quantity']} 期限={parsed['deadline'] or '-'} "
          f"権限={','.join(parsed['required_permissions']) or '-'} 全文「{text}」")
    return {"ok": True, "task_id": task_id, "text": parsed["title"],
            "full_text": text, "priority": parsed["priority"],
            "difficulty": parsed["difficulty"], "quantity": parsed["quantity"],
            "deadline": parsed["deadline"],
            "required_permissions": parsed["required_permissions"],
            "source": parsed["source"]}


@app.route("/api/work_logs/<int:log_id>/feedback", methods=["POST"])
def api_work_log_feedback(log_id):
    """
    完了直後に本人が答えた体感難易度を実績に書く（easy / normal / hard）。

    tasks.difficulty は触らない。ai_stub は work_logs を再生するとき現在の
    tasks.difficulty を JOIN して文脈ベクトルを組むので、ここで書き換えると
    過去のログの文脈まで遡って変わってしまう。
    """
    data = request.get_json(silent=True) or {}
    felt = data.get("felt_difficulty")
    if felt not in FELT_CODES:
        return jsonify({"error": f"felt_difficulty must be one of {FELT_CODES}"}), 400
    conn = db.get_db()
    cur = conn.execute("UPDATE work_logs SET felt_difficulty = ? WHERE id = ?", (felt, log_id))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return jsonify({"error": "not found"}), 404
    # felt_difficulty は既存行の UPDATE なので、ai_stub のキャッシュキー
    # （件数・最大id）だけ見ていると変化が検知されない。ここで明示的に捨てて
    # 次回の割り当てからこのフィードバックが学習に反映されるようにする。
    ai_stub.invalidate_cache()
    return jsonify({"ok": True, "felt_difficulty": felt})


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
    with _pending_lock:
        _pending_tags[tag_id] = module_id
        _pending_tag_touched[tag_id] = time.time()
        pending = len(_pending_tags)
    return jsonify({"ok": True, "pending": pending})


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
    モジュールID(ラズパイの DEVICE_ID) から機材を引く。
    モジュールの同一性はこの device_id で決まり、IPには依存しない。
    hostname への紐付けが本筋だが、module_id をそのままデバイス名に
    している構成でも動くよう両方を見る（hostname 一致を優先）。
    """
    return conn.execute(
        """SELECT id, name, module_id, online FROM equipment
           WHERE hostname = ? OR module_id = ?
           ORDER BY (hostname = ?) DESC LIMIT 1""",
        (device_id, device_id, device_id),
    ).fetchone()


def _device_id_of(eq_row):
    """機材レコードから MQTT の宛先となるモジュールIDを取り出す（_find_equipment_by_device の逆引き）"""
    return eq_row["hostname"] or eq_row["module_id"]


def _ensure_module_id(conn, row):
    """
    module_id が空の機材に採番する。

    タッチ処理の宛先解決は module_id を返す作りなので、ここが NULL のままだと
    「死活は映るのにタッチだけ黙って捨てられる」状態になる。機材登録フォームは
    機材コード欄を空にできるので、この状態は普通に作られてしまう。
    """
    if row["module_id"]:
        return row["module_id"]
    base = f"MOD-{row['id']:03d}"
    candidate, n = base, 1
    while conn.execute("SELECT 1 FROM equipment WHERE module_id = ?", (candidate,)).fetchone():
        n += 1
        candidate = f"{base}-{n}"
    conn.execute("UPDATE equipment SET module_id = ? WHERE id = ?", (candidate, row["id"]))
    conn.commit()
    print(f"[equipment] 「{row['name']}」に機材コードを採番しました: {candidate}")
    return candidate


def _resolve_module_id(device_id):
    """NFCタッチの宛先となる module_id を返す。通信があった証拠として last_seen も更新する"""
    conn = db.get_db()
    row = _find_equipment_by_device(conn, device_id)
    module_id = None
    if row:
        module_id = _ensure_module_id(conn, row)
        conn.execute(
            "UPDATE equipment SET last_seen = datetime('now','localtime'), online = 1 WHERE id = ?",
            (row["id"],),
        )
        conn.commit()
    conn.close()
    return module_id


# ------------------------------------------------ 下り通信（サーバー → モジュール）
# 上り: pi/<device_id>/data (NFCタッチ), pi/<device_id>/status (死活)
# 下り: pi/<device_id>/cmd   (指示)      → 応答は pi/<device_id>/reply
#
# 応答待ちは MQTT の受信スレッドを塞ぐとデッドロックする（返事のメッセージ自体を
# 受け取れなくなる）。待つ側は必ず別スレッド、つまり下の _touch_worker か
# Flask のリクエストスレッドで動かすこと。

CMD_TIMEOUT_SEC = 30  # モジュールの応答を待つ既定の秒数

_mqtt_client = None
_pending_replies: dict = {}  # request_id -> {"event": Event, "answer": ...}
_replies_lock = threading.Lock()


def send_cmd(device_id, cmd, **fields):
    """モジュールへ指示を1件送る。返事が要る場合は request_confirm を使う"""
    if not device_id:
        return False
    # /test の仮想モジュール宛てはブローカーを通さず、プロセス内で受け渡す
    if device_id in _virtual_modules:
        _virtual_receive(device_id, dict(fields, cmd=cmd))
        return True
    client = _mqtt_client
    if client is None or not client.is_connected():
        print(f"[cmd] ブローカー未接続のため {device_id} へ '{cmd}' を送れません")
        return False
    payload = json.dumps(dict(fields, cmd=cmd), ensure_ascii=False)
    # retain しない。再起動したモジュールに古い指示が復活すると誤動作になる
    client.publish(f"pi/{device_id}/cmd", payload, qos=1)
    return True


def request_confirm(device_id, text, timeout=CMD_TIMEOUT_SEC, **fields):
    """
    モジュールに Yes/No を尋ねて答えを待つ。C-1（承認フロー）の土台。
    戻り値: True=Yes / False=No / None=送れなかった or 時間切れ
    """
    answer = _request_answer(device_id, "confirm", text, timeout, **fields)
    return None if answer is None else bool(answer)


def request_choice(device_id, text, options, timeout=CMD_TIMEOUT_SEC, **fields):
    """
    モジュールに選択肢を出して1つ選んでもらう。左右ボタンで選び、決定で確定する。
    戻り値: 選ばれた添字 / None=送れなかった or 時間切れ
    """
    return _request_answer(device_id, "choice", text, timeout,
                           options=list(options), **fields)


# 音声でのタスク登録（E-2）の待ち時間の予算。
#
# **諦める順番が全てで、これが逆転すると最悪の壊れ方をする。** 先に諦めた側が
# 「登録できませんでした」と出す一方、サーバーは処理を続けてタスクを作るため、
# 作業者は失敗したと思って録り直し、**同じタスクが2件**できる。必ずこの順:
#
#   サーバーの処理 < モジュールの送信待ち < サーバーの応答待ち
#
# 個別に数字を置かず積み上げで決めているのは、片方だけ直して順序を崩すのを
# 防ぐため。不変条件は Test/test_voice_budget.py が見張っている。
RECORD_WAIT_SEC = 30      # 決定ボタンが押されるのを待つ秒数
RECORD_MAX_SEC = 30       # 1回の録音の上限
# サーバーが /api/voice で使いうる最大。文字起こしも意図分析も自前の timeout を
# 持っているので、その合計が上限になる（両方が上限まで粘るのが最悪ケース）
RECORD_PROCESS_SEC = VOICE_TIMEOUT + intent.TIMEOUT
# モジュールがHTTPの応答を待つ秒数。**サーバーが諦めた後に諦める**ようにする。
# 上乗せはWAVの送信と応答の往復ぶん
RECORD_UPLOAD_SEC = RECORD_PROCESS_SEC + 15
# サーバーがモジュールの応答を待つ秒数。モジュールが取りうる最長
# （押下待ち + 録音 + 送信待ち）より後に諦める
RECORD_TOTAL_SEC = RECORD_WAIT_SEC + RECORD_MAX_SEC + RECORD_UPLOAD_SEC + 5


def request_record(device_id, url, **fields):
    """
    モジュールに「決定ボタンを押している間だけ録音して送れ」と指示する（E-2）。
    文字起こしとタスク登録は、送り先の /api/voice が同期でやる。
    戻り値: モジュールからの応答そのもの（text / task_id を含む）/ None
    """
    return _request_answer(device_id, "record", None, RECORD_TOTAL_SEC,
                           want_payload=True, url=url,
                           max_sec=RECORD_MAX_SEC, wait_sec=RECORD_WAIT_SEC,
                           upload_timeout=RECORD_UPLOAD_SEC, **fields)


def _request_answer(device_id, cmd, text, timeout, want_payload=False, **fields):
    """confirm / choice / record の共通部分。答えが返るまで待つ"""
    request_id = uuid4().hex[:8]
    # 待っている内容も持たせておく。モジュールに物理ボタンが付くまでは、
    # 管理画面がこれを読んで代わりに答えられるようにするため（/api/pending）。
    slot = {
        "event": threading.Event(), "answer": None, "payload": None,
        "device_id": device_id, "text": text,
        "lines": fields.get("lines") or [],
        # None なら Yes/No。管理画面が代替ポップアップを出し分けるのに使う
        "options": fields.get("options"),
        "equipment_name": fields.get("equipment_name"),
        "worker_name": fields.get("worker_name"),
    }
    with _replies_lock:
        _pending_replies[request_id] = slot
    try:
        if not send_cmd(device_id, cmd, request_id=request_id, text=text,
                        timeout=timeout, **fields):
            return None
        # 問い合わせも画面を書き換える。世代を進めておかないと、直前の
        # _notify_briefly が残した「戻し」が、いま出ている問い合わせを消してしまう
        _bump_screen_gen(device_id)
        # モジュール側のタイムアウトより少しだけ長く待つ。先に諦めると、
        # 後から届いた答えの行き場が無くなる。
        if not slot["event"].wait(timeout + 2):
            print(f"[cmd] {device_id} から応答がありません (request_id={request_id})")
            return None
        return slot["payload"] if want_payload else slot["answer"]
    finally:
        with _replies_lock:
            _pending_replies.pop(request_id, None)


def _handle_reply(device_id, payload):
    """モジュールからの応答を、待っている request_confirm に渡す"""
    request_id = payload.get("request_id")
    with _replies_lock:
        slot = _pending_replies.get(request_id)
    if slot is None:
        # 時間切れ後に届いた答えや、既に処理済みの request_id
        print(f"[{device_id}] 待ち受けの無い応答を無視しました: {payload}")
        return
    slot["answer"] = payload.get("answer")
    slot["payload"] = payload    # record のように答え以外の情報も返ってくる場合に使う
    slot["event"].set()


_screen_gen: dict = {}  # device_id -> 表示の世代番号。戻し処理の割り込み判定に使う
_screen_lock = threading.Lock()


def _bump_screen_gen(device_id):
    """画面を書き換えたことを記録して、新しい世代番号を返す"""
    with _screen_lock:
        _screen_gen[device_id] = gen = _screen_gen.get(device_id, 0) + 1
    return gen


def _notify(device_id, lines, led=None):
    """モジュールの画面とLEDをまとめて更新する。部品が付くまでは向こうで print される"""
    send_cmd(device_id, "display", lines=lines)
    if led:
        send_cmd(device_id, "led", state=led)
    return _bump_screen_gen(device_id)


def _notify_briefly(device_id, module_id, lines, led, sec=6):
    """
    エラー表示を出して、しばらくしたら本来の画面へ戻す。戻さないと
    「未登録のICカードです」が次に誰かが操作するまで出しっぱなしになる。
    戻す前に別の表示が出ていたら何もしない（世代番号で判定）。
    """
    gen = _notify(device_id, lines, led)

    def revert():
        time.sleep(sec)
        with _screen_lock:
            if _screen_gen.get(device_id) != gen:
                return
        conn = db.get_db()
        row = conn.execute("SELECT id FROM equipment WHERE module_id = ?", (module_id,)).fetchone()
        conn.close()
        if row:
            _sync_module_state(device_id, row["id"])

    threading.Thread(target=revert, daemon=True).start()


def _sync_module_state(device_id, eq_id):
    """
    モジュールが（再）接続したときに、DB上の現状を画面とLEDへ反映する。
    ラズパイが再起動しても表示が実態とずれない。
    """
    conn = db.get_db()
    row = conn.execute("""
        SELECT e.name, e.status, w.name AS worker_name, t.title AS task_title
        FROM equipment e
        LEFT JOIN workers w ON w.id = e.current_worker_id
        LEFT JOIN tasks t   ON t.id = e.current_task_id
        WHERE e.id = ?
    """, (eq_id,)).fetchone()
    conn.close()
    if not row:
        return
    if row["status"] == "working":
        lines = [f"{row['worker_name'] or '?'} さん 使用中", row["task_title"] or "フリー利用"]
        led = "working" if row["task_title"] else "free"
    elif row["status"] in ("stopped", "maintenance"):
        lines = [row["name"], EQ_STATUS_LABELS.get(row["status"], row["status"])]
        led = "error"
    else:
        lines = [row["name"], "社員証をタッチしてください"]
        led = "idle"
    _notify(device_id, lines, led)


# タッチ処理は承認の応答待ちでブロックしうるので、MQTTの受信スレッドから外す。
# 同じモジュールのタッチは順番に処理したいので、デバイスごとに1本のワーカーを持つ。
_touch_queues: dict = {}
_touch_lock = threading.Lock()

# 操作中のモジュール: device_id -> 操作を始めた時刻(monotonic)。
#
# 1回のタッチで始まった操作が終わる（何かを選ぶ／時間切れ）までは、同じモジュール
# への後続のタッチを捨てる。順番待ちに積むと、終わった直後にメニューがもう一度
# 出るのが延々と続き、何を操作しているのか分からなくなるため。
#
# 操作中かどうかだけで決めていて、カードが載っているかは見ていない。モジュールは
# カードが離れても何も送らない（raspi.py の touch_loop は離脱時に次のタッチへ
# 備えるだけ）ので、読ませたカードをすぐ外しても操作は時間切れまで続く。
#
# 札を下ろすのは操作を終えた _touch_worker だけ。ワーカーは機材に1本なので、
# 「操作中は積まずに捨てる」と「積んだものは必ず処理する」はここで一致する。
_touch_busy: dict = {}
_touch_busy_lock = threading.Lock()


def _dispatch_touch(device_id, module_id, tag_id):
    now = time.monotonic()
    with _touch_busy_lock:
        if device_id in _touch_busy:
            print(f"[{module_id}] 操作中なのでタッチを無視しました (tag={tag_id})")
            return
        _touch_busy[device_id] = now
    with _touch_lock:
        q = _touch_queues.get(device_id)
        if q is None:
            q = queue.Queue()
            _touch_queues[device_id] = q
            threading.Thread(target=_touch_worker, args=(device_id, q), daemon=True).start()
    q.put((module_id, tag_id, now))


def _touch_worker(device_id, q):
    while True:
        module_id, tag_id, token = q.get()
        try:
            _handle_touch(device_id, module_id, tag_id)
        except Exception as e:  # ワーカーを絶対に落とさない
            print(f"[{device_id}] タッチ処理で例外: {e}")
        finally:
            # 例外で抜けても必ず下ろす。ここを落とすとその機材が
            # 二度とタッチを受け付けなくなる
            with _touch_busy_lock:
                if _touch_busy.get(device_id) == token:
                    _touch_busy.pop(device_id, None)


def _mqtt_on_connect(client, userdata, flags, reason_code, properties):
    print(f"[MQTT] connected to broker (reason={reason_code})")
    # status は retained で publish されるので、購読した瞬間に現在オンラインの
    # モジュールが一括で流れてくる。app.py を再起動しても状態が復元される。
    client.subscribe([("pi/+/data", 0), ("pi/+/status", 1), ("pi/+/reply", 1)])


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
    if kind == "reply":
        _handle_reply(device_id, payload)
        return
    _handle_data(device_id, payload)


def _handle_data(device_id, payload):
    """NFCタッチ（pi/<device_id>/data）。仮想モジュールのタッチもここへ入る"""
    tag_id = payload.get("tag_id")
    if not tag_id:
        print(f"[{device_id}] payload missing tag_id")
        return
    module_id = _resolve_module_id(device_id)  # pi01 → MOD-A-02
    if not module_id:
        print(f"[{device_id}] このモジュールIDに対応する機材がありません。"
              f"機材管理画面で「モジュールID」に '{device_id}' を設定してください。")
        _notify(device_id, ["未登録のモジュールです", "機材管理画面で紐付けてください"], "error")
        return
    _dispatch_touch(device_id, module_id, tag_id)


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
        with _pending_lock:
            if online:
                _pending_modules[device_id] = {"ip": ip, "seen_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            else:
                _pending_modules.pop(device_id, None)
        if online:
            print(f"[{device_id}] 未登録モジュールを検出 (ip={ip}) → 機材管理画面に表示")
        conn.close()
        return

    with _pending_lock:
        _pending_modules.pop(device_id, None)
        # モジュールが再起動すると session が変わる。DB上は online のままなので
        # 「変化なし」に見えるが、向こうの画面は起動時の汎用表示に戻っているため
        # 送り直す必要がある。
        session = payload.get("session")
        restarted = bool(session) and _module_sessions.get(device_id) != session
        if session:
            _module_sessions[device_id] = session
    was_online = bool(row["online"]) and not restarted
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
    if online == was_online:
        # 30秒ごとのハートビート。状態が変わっていないので何もしない。ここで
        # 画面を送り直すと、表示中の承認プロンプト(C-1)を消してしまう。
        return
    print(f"[{device_id}] {row['name']} が{'オンライン' if online else 'オフライン'}になりました"
          + (f" (ip={ip})" if online and ip else ""))
    if online:
        _sync_module_state(device_id, row["id"])


def _handle_touch(device_id, module_id, tag_id):
    """1回のNFCタッチを機材の状態に応じて 開始/終了/拒否 に振り分ける"""
    try:
        w_resp = requests.get(f"{SELF_URL}/api/workers/{tag_id}/next_task",
                              params={"module_id": module_id}, timeout=HTTP_TIMEOUT)
        eq_resp = requests.get(f"{SELF_URL}/api/equipment/{module_id}/status", timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        print(f"[{module_id}] self API unreachable: {e}")
        return

    if w_resp.status_code == 404:
        print(f"[{module_id}] unknown NFC tag: {tag_id} → notifying (管理画面にポップアップ)")
        _notify_briefly(device_id, module_id,
                        ["未登録のICカードです", "管理画面から登録してください"], "error")
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
    # tasks はこの機材の候補（提示順）、elsewhere は他機材の分。_start_session が
    # この機材の分だけを提示し、他機材の分は誘導（C-3）の判断に使う
    tasks = w_resp.json()["tasks"] + w_resp.json().get("elsewhere", [])
    equipment = eq_resp.json()

    if equipment["status"] in ("stopped", "maintenance"):
        print(f"[{module_id}] {worker['name']}: equipment unavailable ({equipment['status']})")
        _notify_briefly(device_id, module_id,
                        ["この機材は使用できません",
                         EQ_STATUS_LABELS.get(equipment["status"], equipment["status"])], "error")
        return

    # 排他制御。他人が使っている機材には、メニューを出す前に断る
    if equipment["status"] == "working" and equipment["current_worker_id"] != worker["id"]:
        print(f"[{module_id}] locked by another worker; rejecting {worker['name']}")
        _notify_briefly(device_id, module_id,
                        ["他の人が使用中です", f"{worker['name']} さんは使用できません"], "working")
        return

    _show_menu(device_id, module_id, tag_id, worker, equipment, tasks)


# タッチ後に必ず出るメニュー。**タッチだけでは何も起きない。**
# 以前は「作業中の本人がタッチしたら即完了」だったが、意図しない完了が起きるうえ、
# タスク登録の入口が無かった。操作は必ずここを通す。
MENU_OPTIONS = ["タスク実行", "タスク登録", "作業終了"]
MENU_TIMEOUT = 25


def _notify_pause(device_id, lines, led, sec=2.5):
    """短いメッセージを出してから次へ進む。タッチ処理スレッドなので待ってよい"""
    _notify(device_id, lines, led)
    time.sleep(sec)


def _show_menu(device_id, module_id, tag_id, worker, equipment, tasks):
    """
    タッチ後のメニュー。タスク実行 / タスク登録 / 作業終了 に分岐する。

    項目の並びは常に同じにして、指が位置を覚えられるようにする。そのうえで
    作業中なら「作業終了」を選んだ状態で出すので、終わるときは決定を1回押すだけ。
    選べない項目を選んだときは理由を出す。「タスク実行」ならメニューへ戻り、
    「作業終了」なら終われる物が無いのでカード受付（待機画面）まで戻す。
    """
    while True:
        # 状態はメニューを出すたびに取り直す。着手した直後に戻ってくる場合がある
        working_here = (equipment.get("status") == "working"
                        and equipment.get("current_worker_id") == worker["id"])
        index = request_choice(
            device_id, "どうしますか？", MENU_OPTIONS,
            timeout=MENU_TIMEOUT,
            lines=[f"{worker['name']} さん", equipment.get("name") or module_id],
            default=2 if working_here else 0,
            equipment_name=equipment.get("name"), worker_name=worker["name"],
        )
        if index is None:
            print(f"[{module_id}] {worker['name']}: メニューで無応答のため中止")
            _notify_briefly(device_id, module_id,
                            ["応答がありませんでした", "もう一度タッチしてください"],
                            "working" if working_here else "idle")
            return

        choice = MENU_OPTIONS[index]
        print(f"[{module_id}] {worker['name']}: メニュー → {choice}")

        if choice == "タスク実行":
            if working_here:
                _notify_pause(device_id, ["すでに作業中です",
                                          "終わるときは「作業終了」"], "working")
                continue
            _start_session(device_id, module_id, tag_id, worker, equipment, tasks)
            return

        if choice == "タスク登録":
            _register_by_voice(device_id, module_id, tag_id, worker, equipment, working_here)
            return

        if choice == "作業終了":
            if not working_here:
                # メニューへは戻さず、カード受付（待機画面）まで戻す。終わる物が
                # 無いのにここへ来たのは大抵ただの勘違いなので、一度仕切り直した
                # 方が早い。_notify_briefly が数秒後に待機画面へ戻す
                print(f"[{module_id}] {worker['name']}: 作業中のタスクが無いので作業終了はできません")
                _notify_briefly(device_id, module_id,
                                ["作業中のタスクがありません",
                                 "「タスク実行」から始めてください"], "idle", sec=4)
                return
            _end_session(device_id, module_id, worker, equipment)
            return


MAX_CHOICES = 3        # 1回のタッチで提示する候補の上限。多すぎると現場で待たされる
CHOICE_TIMEOUT = 25    # 1件あたりの応答待ち秒数

# --- C-3（誘導通知）の設定
GUIDE_TIMEOUT = 25         # 「移動しますか？」の応答待ち秒数
GUIDE_NOTICE_SEC = 15      # 移動元に行き先を出しておく秒数
GUIDE_ARRIVAL_SEC = 180    # 移動先に「向かっています」を出しておく秒数。歩く時間ぶん長め
PRIORITY_RANK = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
# ここに該当する優先度なら、この機材にやることがあっても呼び戻して誘導する
GUIDE_PRIORITIES = ("urgent", "high")


def _task_lines(task):
    """
    選択画面の本文。1行目が大きく出るので、タスク名だけを置く。

    候補の何件目かは本文に混ぜず、画面右上のバッジ（badge）で出す。頭に
    「(1/3) 」を付けるとその分だけタスク名が押し出されて末尾が切れる。
    """
    detail = f"{task.get('quantity') or 1}個"
    if task.get("deadline"):
        detail += f" / 期限 {task['deadline']}"
    return [task["title"], detail]


def _start_session(device_id, module_id, tag_id, worker, equipment, tasks):
    """
    空き機材でのタッチ = 開始。候補タスクを api_next_task の並び順
    （割り当て済み → 優先度 → AIの評価）に1件ずつ提示し、
    承認されたら着手・ロックする（TODO.md の C-1 / C-2）。

    - はい     → そのタスクに着手して機材をロック
    - いいえ   → 次の候補へ。候補が尽きたらフリー利用を尋ねる
    - 無応答   → 何もせず空きのまま戻す。ロックしっぱなしを防ぐ（C-4）
    """
    candidates = [t for t in tasks if t.get("equipment_id") in (None, equipment["id"])][:MAX_CHOICES]
    total = len(candidates)

    # 他機材に先にやるべきタスクがあれば、ロックする前に尋ねる（C-3）。着手して
    # からでは、移動しても この機材が塞がったままになる。
    if _guide_to_other_equipment(device_id, module_id, worker, equipment, tasks, bool(candidates)):
        return

    for i, task in enumerate(candidates, 1):
        answer = request_confirm(
            device_id, "このタスクに着手しますか？",
            lines=_task_lines(task),
            badge=f"{i}/{total}",
            timeout=CHOICE_TIMEOUT,
            equipment_name=equipment.get("name"), worker_name=worker["name"],
        )
        if answer is None:
            print(f"[{module_id}] {worker['name']}: 応答なしのため中止（{task['title']}）")
            _notify_briefly(device_id, module_id, ["応答がありませんでした", "もう一度タッチしてください"], "idle")
            return
        if answer:
            res = requests.post(
                f"{SELF_URL}/api/tasks/{task['id']}/start",
                json={"nfc_tag_id": tag_id, "module_id": module_id}, timeout=HTTP_TIMEOUT,
            )
            # 権限不足(403)など。候補は絞ってあるので通常は起きないが、承認の間に
            # 権限や必要権限が変わることはある。作業中画面を出すとロックした様に見える
            if res.status_code != 200:
                body = res.json() if res.headers.get("content-type", "").startswith("application/json") else {}
                reason = "・".join(body.get("missing_labels") or []) or "着手できませんでした"
                print(f"[{module_id}] {worker['name']}: 着手に失敗 ({res.status_code}) {reason}")
                _notify_briefly(device_id, module_id,
                                ["このタスクには権限が必要です", reason], "error")
                return
            print(f"[{module_id}] {worker['name']} started task: {task['title']}")
            _notify(device_id,
                    [f"{worker['name']} さん", task["title"],
                     _task_lines(task)[1], "終了時にもう一度タッチ"], "working")
            return
        print(f"[{module_id}] {worker['name']}: スキップ（{task['title']}）")

    # 候補が無い、または全部スキップされた
    answer = request_confirm(
        device_id, "フリー利用で使いますか？",
        lines=["着手するタスクがありません"] if total == 0 else ["すべてスキップしました"],
        timeout=CHOICE_TIMEOUT, equipment_name=equipment.get("name"),
        worker_name=worker["name"],
    )
    if answer:
        requests.post(
            f"{SELF_URL}/api/equipment/{module_id}/status",
            json={"status": "working", "nfc_tag_id": tag_id}, timeout=HTTP_TIMEOUT,
        )
        print(f"[{module_id}] {worker['name']} started free-use")
        _notify(device_id, [f"{worker['name']} さん", "フリー利用中", "終了時にもう一度タッチ"], "free")
    else:
        print(f"[{module_id}] {worker['name']}: 使用しないで終了")
        _notify_briefly(device_id, module_id, ["キャンセルしました"], "idle", sec=3)


WEB_PORT = 5000   # app.run() のポート。モジュールが音声を送ってくる先にも使う


def _voice_upload_url(module_ip, tag_id, module_id):
    """
    モジュールから見たこのPCのURLを組み立てる。SELF_URL は 127.0.0.1 なので
    そのままでは向こうから届かない。相手のIPへ到達する側の自IPを使う。
    """
    if not module_ip:
        return None
    host = _outbound_ip_toward(module_ip)
    if not host:
        return None
    return f"http://{host}:{WEB_PORT}/api/voice?tag_id={tag_id}&module_id={module_id}"


def _register_by_voice(device_id, module_id, tag_id, worker, equipment, working_here):
    """
    音声でタスクを登録する（E-2）。決定ボタンを押している間だけ録音させ、
    送られてきた音声を /api/voice が文字起こししてタスクにする。

    **機材はロックしない。** 登録は機材を使う操作ではないので、作業中でも
    空きでも同じように使えるようにしてある。
    """
    back_led = "working" if working_here else "idle"
    url = _voice_upload_url(equipment.get("ip"), tag_id, module_id)
    if not url:
        print(f"[{module_id}] モジュールのIPが分からないので音声を受け取れません")
        _notify_briefly(device_id, module_id,
                        ["音声を受け取れません", "モジュールのIPが不明です"], "error")
        return

    # 「フリー利用中」ではなく専用の状態にする。機材を使っているわけではないので、
    # 周りから見て使用中と取り違えられないようにしておく
    _notify(device_id, [f"{worker['name']} さん", "決定ボタンを押している間",
                        "話してください"], "recording")
    reply = request_record(device_id, url,
                           equipment_name=equipment.get("name"), worker_name=worker["name"])

    if reply is None:
        print(f"[{module_id}] {worker['name']}: 録音の応答がありません")
        _notify_briefly(device_id, module_id,
                        ["登録できませんでした", "もう一度お試しください"], "error")
        return
    if not reply.get("answer"):
        reason = {"no audio": "録音できませんでした",
                  "upload failed": "PCへ送れませんでした"}.get(reply.get("error"), "登録できませんでした")
        print(f"[{module_id}] {worker['name']}: 音声登録に失敗 ({reply.get('error')})")
        _notify_briefly(device_id, module_id, [reason, "もう一度お試しください"], "error")
        return

    text = reply.get("text") or ""
    print(f"[{module_id}] {worker['name']}: 音声でタスク登録 #{reply.get('task_id')} 「{text}」")
    _notify_briefly(device_id, module_id,
                    ["タスクを登録しました", text], back_led, sec=8)


def _pick_guidance(conn, equipment, tasks, has_candidates):
    """
    「この機材ではなく、あちらでやってほしい」タスクを1件選ぶ（C-3）。
    戻り値: (task, 移動先の機材row) / 該当なしなら (None, None)
    """
    others = [t for t in tasks if t.get("equipment_id") not in (None, equipment["id"])]
    # 優先度の高い順。同順位は API が返した順（＝登録の古い順）のまま
    others.sort(key=lambda t: PRIORITY_RANK.get(t.get("priority"), 9))

    for task in others:
        # 優先度が高くないタスクは、この機材でやることが無いときだけ誘導する。
        # そうでないと、ここで作業できるのに毎回よそへ歩かされることになる。
        if task.get("priority") not in GUIDE_PRIORITIES and has_candidates:
            continue
        row = conn.execute(
            """SELECT id, name, module_id, hostname, status, online
               FROM equipment WHERE id = ?""",
            (task["equipment_id"],),
        ).fetchone()
        # 使用中・停止中の機材へ送っても無駄足になる。空きだけを誘導先にする
        if row and row["status"] == "idle":
            return task, row
    return None, None


def _guide_to_other_equipment(device_id, module_id, worker, equipment, tasks, has_candidates):
    """
    他機材に先にやるべきタスクがあれば、そちらへ移動するか尋ねる（C-3）。

    戻り値 True = 誘導した。呼び出し側はこの機材をロックせずに終了する
    """
    conn = db.get_db()
    try:
        task, target = _pick_guidance(conn, equipment, tasks, has_candidates)
    finally:
        conn.close()
    if not task:
        return False

    label = PRIORITY_LABELS.get(task.get("priority"), task.get("priority"))
    answer = request_confirm(
        device_id, f"{target['name']} へ移動しますか？",
        lines=[f"{target['name']} に{label}のタスク", task["title"]],
        timeout=GUIDE_TIMEOUT,
        equipment_name=equipment.get("name"), worker_name=worker["name"],
    )
    if not answer:
        # いいえ / 無応答。断ったのだから、この機材での通常フローへ戻す
        print(f"[{module_id}] {worker['name']}: 誘導を辞退（{target['name']} / {task['title']}）")
        return False

    print(f"[{module_id}] {worker['name']} を {target['name']} へ誘導: {task['title']}")
    _notify_briefly(device_id, module_id,
                    [f"{target['name']} へ移動してください", task["title"],
                     "移動先で社員証をタッチ"], "guide", sec=GUIDE_NOTICE_SEC)

    # 移動先にも予告を出す。着いた本人が「ここで合っている」と確認できる。
    # 相手がオフラインでも send_cmd が黙って捨てるので、分岐は要らない。
    if target["hostname"]:
        _notify_briefly(target["hostname"], target["module_id"],
                        [f"{worker['name']} さんが向かっています", task["title"],
                         "社員証をタッチしてください"], "guide", sec=GUIDE_ARRIVAL_SEC)
    return True


def _end_session(device_id, module_id, worker, equipment):
    """使用中の本人が再タッチ = 終了。タスク中なら完了記録、フリー利用ならロック解除のみ"""
    task_id = equipment.get("current_task_id")
    work_log_id = None
    task_title = None
    if task_id:
        res = requests.post(f"{SELF_URL}/api/tasks/{task_id}/complete", timeout=HTTP_TIMEOUT)
        if res.ok:
            work_log_id = res.json().get("work_log_id")
            task_title = res.json().get("title")
        print(f"[{module_id}] {worker['name']} completed task #{task_id}")
    else:
        print(f"[{module_id}] {worker['name']} ended free-use")
    # 先に機材を解放する。難易度を答えている間ずっと塞がっていると、
    # 次の人が待たされるうえ、答えなかった場合に解放が漏れる
    requests.post(
        f"{SELF_URL}/api/equipment/{module_id}/status",
        json={"status": "idle"}, timeout=HTTP_TIMEOUT,
    )
    if work_log_id:
        _ask_felt_difficulty(device_id, module_id, worker, equipment, work_log_id, task_title)
    # 「お疲れさまでした」だけを見せてから待機画面へ戻す。タッチの案内を
    # 同時に出すと、ねぎらいの画面なのか受付なのか分からなくなる。
    # このワーカーの中で待つので、表示している間のタッチは _dispatch_touch が捨てる
    _notify(device_id, ["お疲れさまでした"], "idle")
    time.sleep(DONE_NOTICE_SEC)
    conn = db.get_db()
    row = conn.execute("SELECT id FROM equipment WHERE module_id = ?", (module_id,)).fetchone()
    conn.close()
    if row:
        _sync_module_state(device_id, row["id"])   # 機材名 + 「社員証をタッチしてください」


DONE_NOTICE_SEC = 3     # 作業終了後に「お疲れさまでした」を出しておく秒数


FEEDBACK_TIMEOUT = 20   # 難易度フィードバックの応答待ち。答えないまま立ち去られてもよい
FEEDBACK_DEFAULT = 1    # 最初に選ばれている選択肢＝「普通」。決定を1回押すだけで終わる


def _ask_felt_difficulty(device_id, module_id, worker, equipment, work_log_id, task_title):
    """
    完了直後に体感難易度を尋ねて実績に書く。答えなくても完了は済んでいるので、
    時間切れなら何も書かずに次へ進む（現場を待たせない）。
    """
    index = request_choice(
        device_id, "この作業の難易度は？",
        [FELT_LABELS[c] for c in FELT_CODES],
        timeout=FEEDBACK_TIMEOUT,
        lines=[task_title or "作業を完了しました", "お疲れさまでした"],
        default=FEEDBACK_DEFAULT,
        equipment_name=equipment.get("name"), worker_name=worker["name"],
    )
    if index is None:
        print(f"[{module_id}] {worker['name']}: 難易度フィードバックは無回答")
        return
    felt = FELT_CODES[index]
    try:
        requests.post(f"{SELF_URL}/api/work_logs/{work_log_id}/feedback",
                      json={"felt_difficulty": felt}, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        print(f"[{module_id}] 難易度フィードバックの記録に失敗: {e}")
        return
    print(f"[{module_id}] {worker['name']}: 難易度フィードバック = {FELT_LABELS[felt]}")


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
    global _mqtt_client
    if not _MQTT_LIB_AVAILABLE:
        print("[MQTT] paho-mqtt が入っていないため bridge は起動しません（デモモード）。")
        print("       → Web/APIは動きます。NFCタッチの代わりに /api を直接叩くか、"
              "simulate_shift.py で仮想タッチを流してください。")
        return
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
        _mqtt_client = client  # 下り(cmd)の publish に使う
        threading.Thread(target=client.loop_forever, daemon=True).start()
        print(f"[MQTT] bridge started (broker {MQTT_HOST}:{MQTT_PORT})")
    except Exception as e:
        print(f"[MQTT] bridge NOT started (broker unreachable): {e}")
        print("       → Web/APIは動きますが、NFCタッチは受信されません。")


# ------------------------------------------------ 仮想モジュール（/test）
# 実機（ラズパイ）もブローカーも無い環境で、現場の操作をブラウザから試すためのもの。
# raspi.py と同じ振る舞いを app.py の中に持ち、MQTT の代わりに関数呼び出しで
# 受け渡す。サーバー側のフロー（_handle_touch 以降）は実機のときと同じコードが走る。
#
#   下り: send_cmd() が宛先を見て _virtual_receive() へ回す
#   上り: タッチは _handle_data()、応答は _handle_reply()、死活は _handle_presence()
#
# 状態はメモリにだけ持つ。app.py を再起動すると消えるが、/test の画面が
# 起動していたモジュールを覚えていて、自動で繋ぎ直す。

VIRTUAL_IP = "127.0.0.1"   # 音声の送り先URLを組み立てるのに使われる（_voice_upload_url）
VIRTUAL_LOG_MAX = 60

_virtual_modules: dict = {}  # device_id -> 状態
# _replies_lock と両方取るときは必ずこちらが先。_handle_reply は _replies_lock を
# 取るので、応答は必ずこのロックを放してから呼ぶ（_virtual_reply）
_virtual_lock = threading.RLock()


def _virtual_timer(sec, *args):
    # daemon にしないと、待ちが残っている間 app.py を終了できない
    t = threading.Timer(sec, _virtual_timeout, args=args)
    t.daemon = True
    t.start()


def _virtual_log(vm, direction, text):
    vm["log"].append({"at": datetime.now().strftime("%H:%M:%S"), "dir": direction, "text": text})
    del vm["log"][:-VIRTUAL_LOG_MAX]


def _virtual_reply(device_id, body):
    """モジュール → サーバーの応答。raspi.publish_reply に当たる"""
    with _virtual_lock:
        vm = _virtual_modules.get(device_id)
        if vm is None:
            return
        _virtual_log(vm, "up", "応答 " + json.dumps(
            {k: v for k, v in body.items() if k != "request_id"}, ensure_ascii=False))
    _handle_reply(device_id, dict(body, device_id=device_id))


def _virtual_timeout(device_id, request_id, body):
    """問い合わせの時間切れ。まだ答えていなければ、実機と同じく無回答で返す"""
    with _virtual_lock:
        vm = _virtual_modules.get(device_id)
        if vm is None or vm["waiting"] != request_id:
            return
        vm["waiting"] = None
        vm["choice"] = None
        vm["record"] = None
    _virtual_reply(device_id, dict(body, request_id=request_id))


def _virtual_receive(device_id, payload):
    """サーバーからの指示。raspi.on_message と同じ分岐"""
    cmd = payload.get("cmd")
    request_id = payload.get("request_id")
    timeout = float(payload.get("timeout") or CMD_TIMEOUT_SEC)
    with _virtual_lock:
        vm = _virtual_modules.get(device_id)
        if vm is None:
            return
        summary = {k: v for k, v in payload.items() if k not in ("cmd", "request_id")}
        _virtual_log(vm, "down", f"{cmd} " + json.dumps(summary, ensure_ascii=False))

        if cmd == "display":
            vm["lines"] = [str(x) for x in payload.get("lines") or []]
            vm["badge"] = payload.get("badge")
            vm["choice"] = None   # 新しい表示が来たら選択画面は畳む（実機と同じ）
            return
        if cmd == "led":
            vm["led"] = payload.get("state", "idle")
            return
        if cmd in ("confirm", "choice"):
            options = ["はい", "いいえ"] if cmd == "confirm" else [str(o) for o in payload.get("options") or []]
            if not options:
                pending = {"answer": None}
            else:
                default = _safe_int(payload.get("default"), 0)
                vm["choice"] = {
                    "request_id": request_id, "kind": cmd, "text": payload.get("text") or "",
                    "lines": [str(x) for x in payload.get("lines") or []],
                    "badge": payload.get("badge"), "options": options,
                    "selected": min(max(default, 0), len(options) - 1),
                    "deadline": time.time() + timeout,
                }
                vm["waiting"] = request_id
                _virtual_timer(timeout, device_id, request_id, {"answer": None})
                return
        elif cmd == "record":
            wait = float(payload.get("wait_sec") or 30)
            vm["record"] = {"request_id": request_id, "url": payload.get("url"),
                            "deadline": time.time() + wait}
            vm["waiting"] = request_id
            _virtual_timer(wait, device_id, request_id, {"answer": False, "error": "no audio"})
            return
        elif cmd == "ping":
            pending = {"answer": "pong"}
        else:
            _virtual_log(vm, "info", f"未知のコマンド: {cmd}")
            return
    _virtual_reply(device_id, dict(pending, request_id=request_id))


def _virtual_state(vm, active_requests, equipment_by_device):
    """画面に渡す形にする。管理画面のポップアップ側で答えられた問い合わせはここで畳む"""
    choice = vm["choice"]
    if choice and choice["request_id"] not in active_requests:
        vm["choice"] = choice = None
        vm["waiting"] = None
    record = vm["record"]
    if record and record["request_id"] not in active_requests:
        vm["record"] = record = None
        vm["waiting"] = None
    now = time.time()
    eq = equipment_by_device.get(vm["device_id"])
    return {
        "device_id": vm["device_id"],
        "led": vm["led"], "lines": vm["lines"], "badge": vm["badge"],
        # app.py は 3.8 固定なので dict の | は使えない
        "choice": choice and dict({k: v for k, v in choice.items() if k != "deadline"},
                                  remaining=max(0, int(choice["deadline"] - now + 0.99))),
        "record": record and {"remaining": max(0, int(record["deadline"] - now + 0.99)),
                              "busy": record.get("busy", False)},
        "equipment": eq,
        "log": list(vm["log"]),
    }


@app.before_request
def _guard_test_mode():
    """テストモードが OFF の間は、/test も仮想モジュールの API も存在しない扱いにする"""
    path = request.path
    if (path == "/test" or path.startswith("/api/test/")) and not _test_mode["on"]:
        return (jsonify({"error": "テストモードが OFF です"}), 404) if path.startswith("/api/") \
            else ("Not Found", 404)
    return None


@app.route("/test-mode", methods=["POST"])
def toggle_test_mode():
    """サイドバーのボタン。テストモードを切り替える。OFF にしたら仮想モジュールは全部止める"""
    turn_on = request.form.get("on") == "1"
    _test_mode["on"] = turn_on
    stopped = []
    if not turn_on:
        with _virtual_lock:
            stopped = list(_virtual_modules)
            _virtual_modules.clear()
        for device_id in stopped:
            # 実機なら LWT で届く offline。機材の表示をオフラインに戻す
            _handle_presence(device_id, {"device_id": device_id, "online": False})
    print(f"[app] テストモードを {'ON' if turn_on else 'OFF'} にしました"
          + (f"（仮想モジュール {len(stopped)} 台を停止）" if stopped else ""))
    flash("テストモードを ON にしました。サイドバーの「テスト」から仮想モジュールを使えます" if turn_on
          else "テストモードを OFF にしました" + (f"（仮想モジュール {len(stopped)} 台を停止）" if stopped else ""),
          "ok")
    if turn_on:
        return redirect(url_for("test_page"))
    # /test にいたなら、消えたページには戻さない
    if request.form.get("next", "").startswith("/test"):
        return redirect(url_for("dashboard"))
    return _back_to("dashboard")


# /test の「社員証をタッチ」に並べる未登録の仮カード
TEST_UNREGISTERED_TAGS = ["test-card-01", "test-card-02", "test-card-03"]


@app.route("/test")
def test_page():
    conn = db.get_db()
    equipment_rows = conn.execute("SELECT id, name, module_id, hostname FROM equipment ORDER BY id").fetchall()
    worker_rows = conn.execute(
        "SELECT id, name, nfc_tag_id FROM workers WHERE nfc_tag_id IS NOT NULL AND nfc_tag_id != '' ORDER BY id"
    ).fetchall()
    used = {r["nfc_tag_id"] for r in conn.execute("SELECT nfc_tag_id FROM workers WHERE nfc_tag_id IS NOT NULL")}
    conn.close()
    # 未登録カードのタッチ（登録ポップアップ・既存作業者への紐付け）を試すための仮の社員証。
    # 誰かに紐付けたものは登録済みの欄に移るので、ここからは外す
    unregistered = [t for t in TEST_UNREGISTERED_TAGS if t not in used]
    return render_template("test.html", equipment=equipment_rows, workers=worker_rows,
                           unregistered_tags=unregistered)


@app.route("/api/test/state")
def api_test_state():
    conn = db.get_db()
    rows = [dict(r) for r in conn.execute("""
        SELECT e.name, e.module_id, e.hostname, e.status, w.name AS worker_name, t.title AS task_title
        FROM equipment e
        LEFT JOIN workers w ON w.id = e.current_worker_id
        LEFT JOIN tasks t   ON t.id = e.current_task_id
    """)]
    conn.close()
    # _find_equipment_by_device と同じく hostname の一致を優先する（後から上書き）
    equipment_by_device = {r["module_id"]: r for r in rows if r["module_id"]}
    equipment_by_device.update({r["hostname"]: r for r in rows if r["hostname"]})
    with _virtual_lock:
        # 問い合わせの一覧は仮想モジュールのロックの中で取る。外で取ると、その直後に
        # 届いた問い合わせを「もう答えられた」と見なして畳んでしまう。
        # （_virtual_lock → _replies_lock の順。逆順で取る箇所は無い）
        with _replies_lock:
            active = {rid for rid, s in _pending_replies.items() if not s["event"].is_set()}
        modules = [_virtual_state(vm, active, equipment_by_device) for vm in _virtual_modules.values()]
    return jsonify({"modules": modules})


def _valid_device_id(device_id):
    return bool(device_id) and len(device_id) <= 40 and all(
        c.isalnum() or c in "-_." for c in device_id)


@app.route("/api/test/modules", methods=["POST"])
def api_test_connect():
    """
    仮想モジュールを起動する。equipment_id を渡すとその機材に紐付けてから繋ぐ。
    渡さなければ未登録モジュールとして検出され、機材管理画面に紐付けパネルが出る。
    """
    data = request.get_json(silent=True) or {}
    device_id = (data.get("device_id") or "").strip()
    if not _valid_device_id(device_id):
        return jsonify({"error": "モジュールIDは英数字と - _ . の40文字以内にしてください"}), 400
    eq_id = _safe_int(data.get("equipment_id"), None)
    if eq_id is not None:
        conn = db.get_db()
        _bind_module(conn, eq_id, device_id)
        conn.close()

    with _virtual_lock:
        if device_id in _virtual_modules:
            return jsonify({"ok": True, "note": "already running"})
        vm = {"device_id": device_id, "led": "offline", "lines": [], "badge": None,
              "choice": None, "record": None, "waiting": None, "log": []}
        _virtual_modules[device_id] = vm
        _virtual_log(vm, "info", "起動しました")
        # raspi.on_connect と同じ暫定表示。紐付いていれば直後にサーバーが本来の表示を送る
        vm["led"] = "idle"
        vm["lines"] = ["社員証をタッチしてください"]
    _handle_presence(device_id, {"device_id": device_id, "online": True,
                                 "session": uuid4().hex[:8], "ip": VIRTUAL_IP})
    return jsonify({"ok": True})


@app.route("/api/test/modules/<device_id>", methods=["DELETE"])
def api_test_disconnect(device_id):
    with _virtual_lock:
        if _virtual_modules.pop(device_id, None) is None:
            return jsonify({"error": "not running"}), 404
    # 実機なら LWT でブローカーが代理送信する offline
    _handle_presence(device_id, {"device_id": device_id, "online": False})
    return jsonify({"ok": True})


@app.route("/api/test/modules/<device_id>/touch", methods=["POST"])
def api_test_touch(device_id):
    """社員証のタッチ。raspi.send_to_host_tag_id に当たる"""
    tag_id = ((request.get_json(silent=True) or {}).get("tag_id") or "").strip()
    if not tag_id:
        return jsonify({"error": "tag_id required"}), 400
    with _virtual_lock:
        vm = _virtual_modules.get(device_id)
        if vm is None:
            return jsonify({"error": "not running"}), 404
        _virtual_log(vm, "up", f"タッチ {tag_id}")
    _handle_data(device_id, {"device_id": device_id, "tag_id": tag_id, "timestamp": time.time()})
    return jsonify({"ok": True})


@app.route("/api/test/modules/<device_id>/button", methods=["POST"])
def api_test_button(device_id):
    """
    物理ボタン（左/決定/右）。{"button": "left"|"ok"|"right"}
    {"select": n} は画面の選択肢を直接指したとき（カーソルだけ動かす）。
    """
    data = request.get_json(silent=True) or {}
    button = data.get("button")
    with _virtual_lock:
        vm = _virtual_modules.get(device_id)
        if vm is None:
            return jsonify({"error": "not running"}), 404
        choice = vm["choice"]
        if not choice:
            # 実機も、問い合わせが無いときのボタンは何も起こさない
            return jsonify({"ok": True, "note": "no question"})
        n = len(choice["options"])
        if "select" in data:
            choice["selected"] = min(max(_safe_int(data.get("select"), 0), 0), n - 1)
            return jsonify({"ok": True})
        if button in ("left", "right"):
            # 端では止める（raspi._move_selection と同じ）
            choice["selected"] = min(max(choice["selected"] + (-1 if button == "left" else 1), 0), n - 1)
            return jsonify({"ok": True})
        if button != "ok":
            return jsonify({"error": "button must be left / ok / right"}), 400
        index = choice["selected"]
        answer = index == 0 if choice["kind"] == "confirm" else index
        request_id = choice["request_id"]
        vm["choice"] = None
        vm["waiting"] = None
        _virtual_log(vm, "info", f"決定: {choice['options'][index]}")
    _virtual_reply(device_id, {"request_id": request_id, "answer": answer})
    return jsonify({"ok": True})


@app.route("/api/test/modules/<device_id>/record", methods=["POST"])
def api_test_record(device_id):
    """
    録音の代わり（E-2）。3通りある:
        JSON {"text": "..."}   … 話した内容をそのまま渡す（文字起こしを飛ばす）
        multipart の audio     … WAV を実機と同じく /api/voice へ送る
        JSON {"cancel": true}  … 決定を押さなかった扱い
    """
    data = request.get_json(silent=True) or {}
    audio = request.files.get("audio")
    text = (data.get("text") or "").strip()
    with _virtual_lock:
        vm = _virtual_modules.get(device_id)
        if vm is None:
            return jsonify({"error": "not running"}), 404
        record = vm["record"]
        if not record or record.get("busy"):
            return jsonify({"error": "録音の指示が来ていません"}), 409
        request_id, url = record["request_id"], record["url"]
        if data.get("cancel"):
            vm["record"] = None
            vm["waiting"] = None
            cancel = True
        elif not text and not audio:
            return jsonify({"error": "text か audio が必要です"}), 400
        else:
            cancel = False
            # 実機も送信中は時間切れにしない（録音が済めば待ちは終わっている）
            record["busy"] = True
            vm["waiting"] = None
            _virtual_log(vm, "info", f"録音の代わりに送信: {text or audio.filename}")
    if cancel:
        _virtual_reply(device_id, {"request_id": request_id, "answer": False, "error": "no audio"})
        return jsonify({"ok": True})

    wav = audio.read() if audio else None

    def upload():
        body = None
        try:
            if wav is not None:
                res = requests.post(url, data=wav, headers={"Content-Type": "audio/wav"},
                                    timeout=RECORD_UPLOAD_SEC)
                body = res.json() if res.ok else None
            else:
                from urllib.parse import parse_qs, urlparse
                tag_id = (parse_qs(urlparse(url or "").query).get("tag_id") or [None])[0]
                body = _register_task_from_text(tag_id, text, device_id)
        except Exception as e:
            print(f"[{device_id}] 仮想モジュールの音声登録に失敗: {e}")
        with _virtual_lock:
            vm = _virtual_modules.get(device_id)
            if vm is not None and vm["record"] and vm["record"]["request_id"] == request_id:
                vm["record"] = None
        if body is None:
            reply = {"answer": False, "error": "upload failed"}
        else:
            reply = {"answer": bool(body.get("ok")), "text": body.get("text"),
                     "task_id": body.get("task_id"), "error": body.get("error")}
        _virtual_reply(device_id, dict(reply, request_id=request_id))

    threading.Thread(target=upload, daemon=True).start()
    return jsonify({"ok": True})


if __name__ == "__main__":
    db.init_db()
    print(f"[app] デバッグモード: {'ON（開発用。LANに公開しないこと）' if DEBUG else 'OFF'}"
          f" / テストモード: {'ON' if _test_mode['on'] else 'OFF'}")
    # 音声タスク登録の後段(E-2)。無くても既定値で登録はできるので落とさず警告だけ
    _intent_ok, _intent_why = intent.available()
    print(f"[voice] 意図分析: {'有効' if _intent_ok else '無効'} - {_intent_why}")
    # debug=True のリローダーは子プロセスで再実行されるため、実際に配信する
    # プロセス(WERKZEUG_RUN_MAIN)でのみ MQTT を起動して二重接続を防ぐ。
    if not DEBUG or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_discovery_responder()
        start_mqtt_bridge()
        start_stt_warmup()
    app.run(debug=DEBUG, host="0.0.0.0", port=5000, threaded=True)
