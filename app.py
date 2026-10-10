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
from datetime import datetime, timedelta
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
import demo_mode as demo
import recurring
from voice import intent

app = Flask(__name__)
app.secret_key = "dev-secret-change-me"  # flash用。本番では変更する
# 受け付けるリクエスト本体の上限。一番大きいのは /api/voice の WAV
# （16kHz・2ch・最長30秒で約2MB）。上限が無いと巨大な送信でメモリを食い潰せる
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

# Flask のデバッグモード（自動リロード＋ブラウザ上のデバッガ）。**既定は OFF。**
# host=0.0.0.0 で配信しているので、ON のままだと LAN 内の誰でもデバッガから
# このPCでコードを実行できてしまう。開発時だけ GEMMBA_DEBUG=1 を付けて起動する。
# 起動後には切り替えられない。
DEBUG = os.environ.get("GEMMBA_DEBUG") == "1"

# /test（仮想モジュール）はいつでも開ける。サイドバーには出さないので、URLを直接開く。
# 実機と同じモジュールIDで仮想モジュールを起動すると、実機への指示を横取りできる
# ことに注意（同じLANの誰でも開ける）。

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


# 未登録タグ・モジュールはDB（pending_tags / pending_modules）にも書いておき、
# 起動時に読み戻す（G-3）。メモリの dict は読み出しを軽くするための写しで、
# 書き換えは必ず下の4関数を通す。
def _pending_db(sql, params, conn=None):
    """conn を渡すとその接続で書く（commit は呼び出し側）。書き込み途中の接続を
    持ったまま別の接続で書くと、SQLite のロック待ちで失敗するため"""
    if conn is not None:
        conn.execute(sql, params)
        return
    try:
        conn = db.get_db()
        conn.execute(sql, params)
        conn.commit()
        conn.close()
    except db.sqlite3.OperationalError as e:   # テーブルがまだ無い古いDB（テストの一時DBなど）
        print(f"[pending] DBに保存できませんでした: {e}")


def _remember_tag(tag_id, module_id):
    with _pending_lock:
        _pending_tags[tag_id] = module_id
        _pending_tag_touched[tag_id] = time.time()
        n = len(_pending_tags)
    _pending_db("INSERT OR REPLACE INTO pending_tags (tag_id, module_id, seen_at)"
                " VALUES (?, ?, datetime('now','localtime'))", (tag_id, module_id))
    return n


def _forget_tag(tag_id):
    if not tag_id:
        return
    with _pending_lock:
        _pending_tags.pop(tag_id, None)
        _pending_tag_touched.pop(tag_id, None)
    _pending_db("DELETE FROM pending_tags WHERE tag_id = ?", (tag_id,))


def _remember_module(device_id, ip):
    info = {"ip": ip, "seen_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    with _pending_lock:
        _pending_modules[device_id] = info
    _pending_db("INSERT OR REPLACE INTO pending_modules (device_id, ip, seen_at) VALUES (?, ?, ?)",
                (device_id, ip, info["seen_at"]))


def _forget_module(device_id, conn=None):
    """外した情報を返す（紐付け時に死活情報を引き継ぐため）"""
    if not device_id:
        return None
    with _pending_lock:
        info = _pending_modules.pop(device_id, None)
    _pending_db("DELETE FROM pending_modules WHERE device_id = ?", (device_id,), conn)
    return info


def _load_pending():
    """起動時にDBから読み戻す。既に登録・紐付け済みになっているものは捨てる"""
    try:
        conn = db.get_db()
        tags = conn.execute("""SELECT tag_id, module_id FROM pending_tags
                               WHERE tag_id NOT IN (SELECT nfc_tag_id FROM workers
                                                    WHERE nfc_tag_id IS NOT NULL)""").fetchall()
        mods = conn.execute("""SELECT device_id, ip, seen_at FROM pending_modules
                               WHERE device_id NOT IN (SELECT hostname FROM equipment
                                                       WHERE hostname IS NOT NULL)""").fetchall()
        conn.execute("DELETE FROM pending_tags WHERE tag_id IN (SELECT nfc_tag_id FROM workers)")
        conn.execute("DELETE FROM pending_modules WHERE device_id IN (SELECT hostname FROM equipment)")
        conn.commit()
        conn.close()
    except db.sqlite3.OperationalError as e:
        print(f"[pending] 読み戻せませんでした: {e}")
        return
    with _pending_lock:
        _pending_tags.update({r["tag_id"]: r["module_id"] for r in tags})
        _pending_modules.update({r["device_id"]: {"ip": r["ip"], "seen_at": r["seen_at"]} for r in mods})
    if tags or mods:
        print(f"[pending] 未登録タグ {len(tags)} 件・未登録モジュール {len(mods)} 件を読み戻しました")

PRIORITY_LABELS = {"urgent": "至急", "high": "高", "normal": "通常", "low": "低"}
# 完了直後に現場で答えてもらう体感難易度。順番がそのままモジュールの選択肢の並びになる
FELT_CODES = ["easy", "normal", "hard"]
FELT_LABELS = {"easy": "簡単", "normal": "普通", "hard": "難しい"}
STATUS_LABELS = {"todo": "未着手", "assigned": "割当済", "gathering": "集合待ち",
                 "in_progress": "作業中", "done": "完了"}
EQ_STATUS_LABELS = {"idle": "空き", "working": "稼働中", "stopped": "停止", "maintenance": "メンテ中",
                    "gathering": "集合待ち"}

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
    -- 複数人タスクの実績は学習に使わない。所要時間は全員で同じ値になり、1人の速さを
    -- 表していないため（人数で割っても、手分けできる作業かどうかで意味が変わる）
    WHERE COALESCE(t.required_workers, 1) = 1
    ORDER BY l.completed_at, l.id
"""


def _team_names_sql(task_expr):
    """
    複数人タスクのメンバー全員の名前（「山田 太郎・佐藤 花子」）を返す相関サブクエリ。
    メンバーのいない（1人で行う）タスクでは NULL になるので、COALESCE で担当者名に
    落とせば、どの画面も担当者の欄をそのまま使える
    """
    return f"""(SELECT GROUP_CONCAT(name, '・') FROM (
                   SELECT mw.name FROM task_members m JOIN workers mw ON mw.id = m.worker_id
                   WHERE m.task_id = {task_expr} ORDER BY m.joined_at, m.rowid))"""


def _ai_logs(conn):
    """
    ai_stub に渡す学習データ。呼び出し側は開いた conn をそのまま渡す。

    実績の件数と最大idが前回と同じなら、全件の読み込みは省いて前回の結果を返す
    （タッチのたびに全件 JOIN していた）。既存の行や JOIN 先が変わる操作
    （難易度フィードバック・作業者/タスクの編集・削除）では ai_stub.invalidate_cache()
    を呼んで捨てること。返したリストは共有なので書き換えない。
    """
    key = tuple(conn.execute("SELECT COUNT(*), MAX(id) FROM work_logs").fetchone())
    return ai_stub.cached_logs(
        key, lambda: [dict(r) for r in conn.execute(AI_LOG_SQL).fetchall()])


def _like_escape(text):
    """
    LIKE の % / _ をワイルドカードとして解釈させない。検索欄に "50%" のような
    文字列を打たれても、そのまま部分一致の対象として扱うため。
    """
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _parse_ai_note(text):
    """tasks.ai_note(JSON) を読む。壊れていたら無かったことにする"""
    try:
        note = json.loads(text)
    except (TypeError, ValueError):
        return None
    return note if isinstance(note, dict) else None


def _safe_int(value, default):
    """
    フォームやJSONの数値項目を int にする。空文字や null が来ると
    int() がそのまま例外を投げて500になるので、その場合は default を返す。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default):
    """_safe_int の小数版（勤続年数）。"abc" や "nan" で500にしない"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if f == f and abs(f) != float("inf") else default


MAX_TEAM = 10   # 必要人数の上限。打ち間違い（100人など）で永遠にそろわないタスクを作らせない


def _team_size_input(value, default=1):
    """フォームやJSONの必要人数。1〜MAX_TEAM に収める"""
    return min(max(_safe_int(value, default) or default, 1), MAX_TEAM)


def _valid_priority(value, default="normal"):
    """未知の優先度は並び替え（PRIORITY_RANK）や表示で浮くので既定値に寄せる"""
    return value if value in PRIORITY_LABELS else default


def _row_exists(conn, table, row_id):
    """外部キーの参照先があるか。無い id で書くと IntegrityError で500になる"""
    return conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (row_id,)).fetchone() is not None


# 管理画面の見た目（UI）。審査用に提出した旧UI（classic）と、デモ用の新UI（new）を
# **ブラウザごとに** 切り替える。/ui/new・/ui/classic を開くと Cookie に記録される。
# 変わるのは templates/<ui>/ と static/<ui>/style.css だけで、サーバーの処理・DB・
# モジュールは共通。
#
# 既定は classic。Cookie の無いブラウザ（審査で初めて開く画面）に新UIを出さないため。
# サーバー全体の設定にしないのは、旧UIを開いたままのタブの自動更新に新UIの
# <main> が流れ込み、旧UIの枠の中で崩れて見えるため。
UI_COOKIE = "gemmba_ui"
UI_CHOICES = ("classic", "new")
DEFAULT_UI = "classic"


def _ui():
    ui = request.cookies.get(UI_COOKIE)
    return ui if ui in UI_CHOICES else DEFAULT_UI


def _render(name, **context):
    """いまのブラウザのUIのテンプレートで描く。画面の描画は必ずこれを通す"""
    return render_template(f"{_ui()}/{name}", **context)


@app.route("/ui/<choice>")
def switch_ui(choice):
    """UIを切り替えて元の画面へ戻る。URLを打つだけで済むよう GET で受ける"""
    if choice not in UI_CHOICES:
        return "Not Found", 404
    nxt = request.args.get("next", "")
    # 外部URLへは飛ばさない（_back_to と同じ判定。/\ もブラウザは // と同じに扱う）
    if not nxt.startswith("/") or nxt.startswith("//") or nxt.startswith("/\\"):
        nxt = url_for("dashboard")
    resp = redirect(nxt)
    resp.set_cookie(UI_COOKIE, choice, max_age=60 * 60 * 24 * 365, samesite="Lax")
    return resp


@app.context_processor
def inject_labels():
    # perms は権限コード → 表示名の変換と、テンプレート側でのチェック状態の判定に使う
    return dict(P=PRIORITY_LABELS, S=STATUS_LABELS, E=EQ_STATUS_LABELS, F=FELT_LABELS,
                PERMISSIONS=perms.PERMISSIONS, ROLES=perms.ROLES, perms=perms,
                # 期限の「今日まで」「期限切れ」の判定用（新UIの一覧）
                today=datetime.now().strftime("%Y-%m-%d"),
                demo=_demo_state())


def _demo_state():
    """サイドバーのデモモード表示用"""
    return {"on": demo.is_active()}


# ---------------------------------------------------------------- 画面

# 機材ごとの使用状況（使用状況ダッシュボードと工場掲示用ダッシュボードで共通）
EQUIPMENT_BOARD_SQL = f"""
    SELECT e.*, COALESCE({_team_names_sql('e.current_task_id')}, w.name) AS worker_name,
           t.title AS task_title,
           t.quantity AS task_quantity, t.deadline AS task_deadline,
           t.priority AS task_priority,
           -- 使い始めた時刻。タスクがあれば着手時刻、フリー利用なら機材の最終更新
           COALESCE(t.started_at, e.updated_at) AS since
    FROM equipment e
    LEFT JOIN workers w ON w.id = e.current_worker_id
    LEFT JOIN tasks t   ON t.id = e.current_task_id
    ORDER BY e.name
"""

# 要対応: 至急、または期限切れ・今日が期限の未完了タスク（同じく両方のダッシュボードで共通）
ATTENTION_SQL = f"""
    SELECT t.*, COALESCE({_team_names_sql('t.id')}, w.name) AS worker_name, e.name AS equipment_name,
           CASE WHEN t.deadline < date('now','localtime') THEN 'overdue'
                WHEN t.deadline = date('now','localtime') THEN 'today' END AS due
    FROM tasks t
    LEFT JOIN workers w   ON w.id = t.assigned_worker_id
    LEFT JOIN equipment e ON e.id = t.equipment_id
    WHERE t.status != 'done'
      AND (t.priority = 'urgent' OR t.deadline <= date('now','localtime'))
    ORDER BY t.deadline IS NULL, t.deadline,
             CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END
"""


# --- 終了のタッチ忘れ（導入先の評価で「タッチを忘れる人がいた」）
# 作業を終えたのに終了のタッチをしないと、機材が「使用中」のまま残り、次の人が使えない。
# 見込み時間を大きく超えた使用を「忘れているかもしれない」として、モジュールの画面・
# 管理画面・掲示用ダッシュボードで知らせる。勝手に終了させはしない（本当に長い作業もある）。
FORGOT_FACTOR = 2.0   # 見込み時間（ai_stub.estimate_seconds）の何倍を超えたら知らせるか
# 見込みが短いタスクでも、これより早くは知らせない。デモでは環境変数で縮める
FORGOT_MIN_SEC = int(os.environ.get("GEMMBA_FORGOT_MIN_SEC", 30 * 60))
# フリー利用は見込みが無いので、一律この時間で知らせる
FREE_USE_FORGOT_SEC = int(os.environ.get("GEMMBA_FREE_FORGOT_SEC", 2 * 3600))


def _overdue_sessions(conn, now=None):
    """
    終了のタッチを忘れていそうな使用中の機材。
    戻り値: {equipment_id: {"since", "elapsed_sec", "limit_sec"}}
    """
    rows = conn.execute("""
        SELECT e.id, e.current_task_id, t.difficulty, t.quantity,
               COALESCE(t.started_at, e.updated_at) AS since
        FROM equipment e
        LEFT JOIN tasks t ON t.id = e.current_task_id
        WHERE e.status = 'working'
    """).fetchall()
    if not rows:
        return {}
    now = now or datetime.now()
    logs = None
    out = {}
    for r in rows:
        try:
            since = datetime.strptime(r["since"], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        if r["current_task_id"]:
            if logs is None:
                logs = _ai_logs(conn)
            estimate = ai_stub.estimate_seconds(
                {"difficulty": r["difficulty"], "quantity": r["quantity"]}, logs)
            limit = max(FORGOT_MIN_SEC, FORGOT_FACTOR * estimate)
        else:
            limit = FREE_USE_FORGOT_SEC
        elapsed = (now - since).total_seconds()
        if elapsed > limit:
            out[r["id"]] = {"since": r["since"], "elapsed_sec": int(elapsed), "limit_sec": int(limit)}
    return out


def _overdue_now():
    conn = db.get_db()
    try:
        return _overdue_sessions(conn)
    finally:
        conn.close()


@app.route("/")
def dashboard():
    """機材の使用状況ダッシュボード"""
    conn = db.get_db()
    equipment = conn.execute(EQUIPMENT_BOARD_SQL).fetchall()
    counts = conn.execute("""
        SELECT
          (SELECT COUNT(*) FROM tasks WHERE status != 'done')       AS open_tasks,
          (SELECT COUNT(*) FROM tasks WHERE status = 'in_progress') AS active_tasks,
          (SELECT COUNT(*) FROM workers)                            AS workers,
          (SELECT COUNT(*) FROM equipment WHERE status = 'working') AS working_eq,
          -- モジュールを紐付けてあるのに繋がっていない機材。タッチしても何も起きない
          (SELECT COUNT(*) FROM equipment WHERE hostname IS NOT NULL AND online = 0) AS offline_eq
    """).fetchone()
    # 要対応: 至急、または期限切れ・今日が期限の未完了タスク
    attention = conn.execute(ATTENTION_SQL).fetchall()
    conn.close()
    return _render("dashboard.html", equipment=equipment, counts=counts, attention=attention,
                           overdue=_overdue_now(),
                           now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


# 工場の壁のモニターに映す掲示用ダッシュボード。サイドバーの無い全画面で、
# どちらのUI（旧/新）で開いても同じ画面。見るだけで操作はしない
WEEKDAYS = "月火水木金土日"


@app.route("/factorydashboard")
def factory_dashboard():
    conn = db.get_db()
    equipment = conn.execute(EQUIPMENT_BOARD_SQL).fetchall()
    attention = conn.execute(ATTENTION_SQL).fetchall()
    conn.close()
    now = datetime.now()
    # 見出しの件数。接続の切れた機材は「接続切れ」だけに数え、空きには数えない（タッチできないため）
    offline = [e for e in equipment if e["hostname"] and not e["online"]]
    summary = {
        "working": sum(e["status"] == "working" for e in equipment),
        "idle": sum(e["status"] == "idle" for e in equipment if e not in offline),
        "down": sum(e["status"] in ("stopped", "maintenance") for e in equipment),
        "offline": len(offline),
    }
    return render_template("factorydashboard.html", equipment=equipment, attention=attention,
                           summary=summary, overdue=_overdue_now(), now=now.strftime("%Y-%m-%d %H:%M:%S"),
                           date_label=f"{now.month}月{now.day}日（{WEEKDAYS[now.weekday()]}）")


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
    # 新UIの名前検索。紐付け候補（unlinked）と人数の表示は絞り込みに関係なく全員から出す
    q = request.args.get("q", "").strip()
    shown = [w for w in rows if q.casefold() in w["name"].casefold()] if q else rows
    return _render("workers.html", workers=shown, pending_tags=pending_tags,
                   unlinked_workers=unlinked, q=q, total_workers=len(rows))


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
            (name, _safe_float(years, 0.0), role, held, nfc),
        )
        conn.commit()
        _forget_tag(nfc)
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
            _forget_tag(nfc)
            flash(f"このICタグを {row['name']} さんに紐付けました", "ok")
    except db.sqlite3.IntegrityError:
        flash("そのICタグIDは既に別の作業者が使っています", "error")
    finally:
        conn.close()
    return _back_to("workers")


@app.route("/workers/<int:worker_id>/update", methods=["POST"])
def update_worker(worker_id):
    """
    名前・役職・保有権限・勤続年数・腕輪ICタグIDの変更（D-2）。資格は後から取るものなので
    編集口が要る。ICタグは紛失・再発行があるので、登録後でも付け替えられるようにする。
    名前は結婚などで変わるほか、登録時の打ち間違いも直せるようにする。タスク・実績・
    機材は id で紐付いているので、名前を変えても担当や実績はそのまま引き継がれる。
    """
    f = request.form
    nfc = f.get("nfc_tag_id", "").strip() or None
    conn = db.get_db()
    row = conn.execute("SELECT name FROM workers WHERE id = ?", (worker_id,)).fetchone()
    if not row:
        conn.close()
        flash("作業者が見つかりません", "error")
        return redirect(url_for("workers"))
    # 名前欄の無い経路（古い画面など）からは名前を変えない。空にはさせない
    # （自動保存なので、打ち直すために消した瞬間にも送られてくる）
    name = f.get("name", "").strip() if "name" in f else row["name"]
    kept_name = not name
    name = name or row["name"]
    try:
        conn.execute(
            "UPDATE workers SET name = ?, years_of_service = ?, role = ?, permissions = ?, nfc_tag_id = ?"
            " WHERE id = ?",
            (name, _safe_float(f.get("years_of_service"), 0.0), f.get("role") or perms.DEFAULT_ROLE,
             perms.dump(f.getlist("permissions")), nfc, worker_id),
        )
        conn.commit()
        _forget_tag(nfc)
        # 勤続年数は学習の文脈ベクトルに入っている。ログの件数は変わらないので
        # AI のキャッシュは自分では気づけない
        ai_stub.invalidate_cache()
        if kept_name:
            flash("名前は空にできないため、元の名前のままにしました", "error")
        elif name != row["name"]:
            # 使用中の機材の画面には「〇〇 さん 使用中」と出ているので、新しい名前で出し直す
            using = conn.execute("SELECT * FROM equipment WHERE status = 'working' AND current_worker_id = ?",
                                 (worker_id,)).fetchall()
            _resync_released(using)
            flash(f"{row['name']} さんの名前を「{name}」に変更しました", "ok")
        else:
            flash(f"{name} さんの役職・権限・ICタグを更新しました", "ok")
    except db.sqlite3.IntegrityError:
        flash("そのICタグIDは既に別の作業者が使っています", "error")
    finally:
        conn.close()
    return redirect(url_for("workers"))


@app.route("/workers/<int:worker_id>/delete", methods=["POST"])
def delete_worker(worker_id):
    conn = db.get_db()
    # 本人がリーダーの複数人タスクを未着手に戻すので、ほかのメンバーも外す（下の UPDATE の前に）
    conn.execute("""DELETE FROM task_members WHERE task_id IN (
                        SELECT id FROM tasks WHERE assigned_worker_id = ? AND status = 'in_progress')""",
                 (worker_id,))
    # 本人が使っていた機材は空きに戻す。担当者だけ外すと「誰かが使用中」のまま残る
    released = _release_equipment(conn, "current_worker_id", worker_id)
    # 割当済・作業中のタスクは未着手からやり直す。作業中のまま担当者だけ空にすると、
    # 誰も完了できないタスクが残る。着手時刻も消さないと次の人の所要時間に混ざる
    reset = conn.execute(
        """UPDATE tasks SET assigned_worker_id = NULL, status = 'todo', started_at = NULL, ai_note = NULL,
           designated = 0
           WHERE assigned_worker_id = ? AND status IN ('assigned', 'in_progress')""", (worker_id,)).rowcount
    conn.execute("UPDATE tasks SET assigned_worker_id = NULL WHERE assigned_worker_id = ?", (worker_id,))
    # 定期タスクの担当者指定も外す（以後は誰にでも提示される）
    conn.execute("UPDATE recurring_tasks SET worker_id = NULL WHERE worker_id = ?", (worker_id,))
    conn.execute("UPDATE equipment SET current_worker_id = NULL WHERE current_worker_id = ?", (worker_id,))
    logs = _detach_work_logs(conn, "worker_id", worker_id)
    conn.execute("DELETE FROM workers WHERE id = ?", (worker_id,))
    conn.commit()
    conn.close()
    _resync_released(released)
    notes = [f"担当中のタスク {reset} 件を未着手に戻しました" if reset else "",
             _released_note(released), f"実績 {logs} 件は残しています" if logs else ""]
    notes = [n for n in notes if n]
    flash("作業者を削除しました" + (f"（{' / '.join(notes)}）" if notes else ""), "ok")
    return redirect(url_for("workers"))


@app.route("/tasks")
def tasks():
    # タスク一覧・完了済みタスクの両方に効く検索（タスク名・担当者名の部分一致）。
    # GETのクエリ文字列なので、検索結果のURLをそのまま共有・ブックマークできる。
    q_title = request.args.get("q_title", "").strip()
    q_worker = request.args.get("q_worker", "").strip()
    # 絞り込み。値は既知のコードだけ受け付ける（それ以外は無視＝絞り込まない）
    q_priority = request.args.get("q_priority", "")
    q_priority = q_priority if q_priority in PRIORITY_LABELS else ""
    q_status = request.args.get("q_status", "")
    q_status = q_status if q_status in STATUS_LABELS or q_status == "unassigned" else ""
    q_equipment = request.args.get("q_equipment", "")
    q_equipment = q_equipment if q_equipment.isdigit() or q_equipment == "none" else ""
    # 新UIの検索欄（タスク名・担当者名のどちらかに部分一致）。旧UIは q_title / q_worker を使う
    q = request.args.get("q", "").strip()

    conditions = []
    params = []
    if q:
        conditions.append("(t.title LIKE ? ESCAPE '\\' OR w.name LIKE ? ESCAPE '\\')")
        params += [_like_escape(q), _like_escape(q)]
    if q_title:
        conditions.append("t.title LIKE ? ESCAPE '\\'")
        params.append(_like_escape(q_title))
    if q_worker:
        conditions.append("w.name LIKE ? ESCAPE '\\'")
        params.append(_like_escape(q_worker))
    if q_priority:
        conditions.append("t.priority = ?")
        params.append(q_priority)
    if q_status == "unassigned":
        conditions.append("t.assigned_worker_id IS NULL AND t.status != 'done'")
    elif q_status:
        conditions.append("t.status = ?")
        params.append(q_status)
    if q_equipment == "none":
        conditions.append("t.equipment_id IS NULL")
    elif q_equipment:
        conditions.append("t.equipment_id = ?")
        params.append(int(q_equipment))
    where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    conn = db.get_db()
    rows = conn.execute(f"""
        SELECT t.*, COALESCE({_team_names_sql('t.id')}, w.name) AS worker_name,
               e.name AS equipment_name,
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
    # 状態ごとの件数（新UIの状態タブ）。絞り込みに関係なく全体で数える
    status_counts = conn.execute("""
        SELECT
          COALESCE(SUM(status != 'done'), 0)                                AS open,
          COALESCE(SUM(status != 'done' AND assigned_worker_id IS NULL), 0) AS unassigned,
          COALESCE(SUM(status = 'assigned'), 0)                             AS assigned,
          COALESCE(SUM(status = 'in_progress'), 0)                          AS in_progress,
          COALESCE(SUM(status = 'done'), 0)                                 AS done
        FROM tasks
    """).fetchone()
    conn.close()
    filtered = any((q, q_title, q_worker, q_priority, q_status, q_equipment))
    recurring_rules = _recurring_rules()
    return _render("tasks.html", active_tasks=active_tasks, done_tasks=done_tasks,
                           recurring_rules=recurring_rules,
                           FREQ=recurring.FREQUENCIES, WEEKDAYS=recurring.WEEKDAY_LABELS,
                           worker_list=worker_list, equipment_list=equipment_list,
                           q=q, status_counts=status_counts,
                           q_title=q_title, q_worker=q_worker, q_priority=q_priority,
                           q_status=q_status, q_equipment=q_equipment, filtered=filtered,
                           ai_notes={t["id"]: _parse_ai_note(t["ai_note"]) for t in rows
                                     if t["ai_note"]})


@app.route("/tasks/add", methods=["POST"])
def add_task():
    f = request.form
    title = f.get("title", "").strip()
    if not title:
        flash("タスク名を入力してください", "error")
        return redirect(url_for("tasks"))
    conn = db.get_db()
    equipment_id = f.get("equipment_id") or None
    if equipment_id and not _row_exists(conn, "equipment", equipment_id):
        conn.close()
        flash("指定された機材が見つかりません", "error")
        return redirect(url_for("tasks"))
    required_perms = perms.dump(f.getlist("required_permissions"))
    team = _team_size_input(f.get("required_workers"))
    # 担当者の指定（D-3）。指定したタスクは本人にしか提示されず、AI割当でも変わらない
    worker_id = f.get("assigned_worker_id") or None
    if worker_id:
        cand, error = _check_designee(conn, worker_id, required_perms, team)
        if error:
            conn.close()
            flash(error, "error")
            return redirect(url_for("tasks"))
    conn.execute(
        """INSERT INTO tasks (title, description, difficulty, priority, required_permissions,
                            quantity, deadline, equipment_id, assigned_worker_id, status, designated,
                            required_workers)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            title,
            f.get("description", "").strip(),
            _safe_int(f.get("difficulty"), 3),
            _valid_priority(f.get("priority")),
            required_perms,
            _safe_int(f.get("quantity"), 1) or 1,
            f.get("deadline") or None,
            equipment_id,
            worker_id,
            "assigned" if worker_id else "todo",
            1 if worker_id else 0,
            team,
        ),
    )
    conn.commit()
    conn.close()
    flash(f"タスク「{title}」を登録しました" + (f"（担当 {cand['name']} さん）" if worker_id else ""), "ok")
    return redirect(url_for("tasks"))


def _check_designee(conn, worker_id, required_perms, team=1):
    """
    担当者に指定してよいか。戻り値: (作業者row, エラー文 or None)
    手で指定するときも権限は無視できない（無資格の人に提示されてしまう）。
    複数人タスク（team >= 2）は、権限は加わる人の誰かが持っていればよいので見ない
    """
    cand = conn.execute("SELECT * FROM workers WHERE id = ?", (worker_id,)).fetchone()
    if not cand:
        return None, "指定された作業者が見つかりません"
    if team > 1:
        return cand, None
    lacking = perms.missing(cand, {"required_permissions": required_perms})
    if lacking:
        return cand, (f"{cand['name']} さんは権限が足りないため割り当てできません"
                      f"（不足: {'・'.join(perms.labels(lacking))}）")
    return cand, None


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
    team = task["required_workers"] or 1
    if "required_workers" in f:
        team = _team_size_input(f.get("required_workers"), team)
    # 作業中・完了のタスクは担当者と機材を動かさない。機材は着手した人のまま使用中で、
    # 完了時の実績も task の担当者で記録するので、ここで変えると両者が食い違う。
    # 集合待ち（複数人タスク）も同じで、人数を変えると集まっている人と食い違う。
    # 画面では欄を無効にしてあり送られてこない。送られてきて違っていれば断る
    locked = task["status"] in ("in_progress", "done", "gathering")
    if locked:
        changed = [label for key, label in (("assigned_worker_id", "担当者"), ("equipment_id", "機材"),
                                            ("required_workers", "必要人数"))
                   if key in f and str(f.get(key) or "") != str(task[key] or "")]
        if changed:
            conn.close()
            flash(f"{STATUS_LABELS.get(task['status'], task['status'])}のタスクは"
                  f"{'・'.join(changed)}を変更できません", "error")
            return _back_to("tasks")
        worker_id, equipment_id = task["assigned_worker_id"], task["equipment_id"]
        team = task["required_workers"] or 1
    priority = _valid_priority(f.get("priority"), task["priority"])
    quantity = _safe_int(f.get("quantity") or task["quantity"], 1) or 1
    deadline = f.get("deadline") or None
    if equipment_id and not _row_exists(conn, "equipment", equipment_id):
        conn.close()
        flash("指定された機材が見つかりません", "error")
        return _back_to("tasks")

    # 必要権限（D-2）。チェックボックスは未チェックだと POST に現れないので、
    # フォームに含まれていたことを隠しフィールドで見分ける。含まれない経路から
    # 呼ばれたときに既存の設定を消さないため。
    if "req_perm_form" in f:
        required_perms = perms.dump(f.getlist("required_permissions"))
    else:
        required_perms = task["required_permissions"]

    # 手動割り当てでも権限は無視できない。判定は「このフォームで指定された必要権限」
    # に対して行うので、必要権限を外すのと同時に割り当てる操作は通る。
    # 作業中のタスクは見ない（api_next_task と同じく、着手済みの人から取り上げない）
    if worker_id and not locked:
        _, error = _check_designee(conn, worker_id, required_perms, team)
        if error:
            conn.close()
            flash(error, "error")
            return _back_to("tasks")

    # 着手・完了はNFCタッチ側でしか起きない設計にしてある（画面から done にできると、
    # 所要時間の入っていない実績が混ざる）。ここで動かすのは未着手⇔割当済だけで、
    # AI割当と同じく担当者がいれば割当済にする。
    status = task["status"] if locked else ("assigned" if worker_id else "todo")
    # 担当者を手で変えたら、AI割当の根拠はもう当てはまらないので消す。
    # 手で選んだ担当者は「指定」（D-3）として扱い、AI割当で上書きさせない。
    # 担当者を変えていなければ、AI割当で決まったものは割当結果のまま残す
    same_worker = str(task["assigned_worker_id"] or "") == str(worker_id or "")
    ai_note = task["ai_note"] if same_worker else None
    designated = task["designated"] if same_worker else (1 if worker_id else 0)
    conn.execute(
        """UPDATE tasks SET assigned_worker_id = ?, equipment_id = ?, priority = ?, status = ?,
           quantity = ?, deadline = ?, required_permissions = ?, ai_note = ?, designated = ?,
           required_workers = ?
           WHERE id = ?""",
        (worker_id, equipment_id, priority, status, quantity, deadline, required_perms, ai_note,
         designated, team, task_id),
    )
    conn.commit()
    conn.close()
    # 数量・必要権限は過去の実績の再生にも使われる（AI_LOG_SQL が tasks を JOIN している）
    ai_stub.invalidate_cache()
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
    # 割り当てると状態が 'assigned' に上書きされる。作業中のタスクは着手済みの人から
    # 取り上げることになり、機材は元の人のまま使用中で残り食い違う。完了済みのタスクは
    # 未完了に戻ってしまう。どちらも断る
    if task["status"] in ("in_progress", "done", "gathering"):
        conn.close()
        flash(f"「{task['title']}」は{STATUS_LABELS[task['status']]}のため、AIで割り当て直せません", "error")
        return _back_to("tasks")
    # 担当者を指定したタスクは、管理者の判断を優先する（D-3）
    if task["designated"] and task["assigned_worker_id"]:
        conn.close()
        flash(f"「{task['title']}」は担当者が指定されているため、AIでは割り当て直しません"
              f"（担当者を「担当者なし」に戻すとAIで割り当てられます）", "error")
        return _back_to("tasks")
    workers_ = conn.execute("SELECT * FROM workers").fetchall()
    logs = _ai_logs(conn)
    # 割り当て直しのときに自分自身を手持ちに数えないよう、このタスクは除く
    open_tasks = [t for t in _open_assignments(conn) if t["id"] != task_id]
    wid, eligible = _ai_pick(conn, task, workers_, logs, open_tasks)
    dropped = len(workers_) - len(eligible)
    if wid:
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


def _open_assignments(conn):
    """各人の手持ち（割り当て済み・作業中）。多い人ほど選ばれにくくして負荷を分散する"""
    return [dict(r) for r in conn.execute(
        """SELECT id, difficulty, quantity, status, started_at, assigned_worker_id FROM tasks
           WHERE status IN ('assigned', 'in_progress') AND assigned_worker_id IS NOT NULL""")]


def _ai_pick(conn, task, workers_, logs, open_tasks):
    """
    AI割当（WariAthena）で1件の担当者を決めて tasks に書く。commit は呼び出し側。
    戻り値: (選んだ作業者の id / 候補なしなら None, 権限を満たした作業者のリスト)
    """
    # 権限（D-2）はハード制約なので、学習器に渡す前に候補から落とす。
    # 無資格者を選ばせてから弾くのでは、AI が選べなかった理由を説明できない。
    eligible = perms.eligible_workers([dict(w) for w in workers_], task)
    wid, explain = ai_stub.assign_task_explained(dict(task), eligible, logs, open_tasks=open_tasks)
    if wid:
        # 根拠は一覧の行に残す。フラッシュ1行だけだと、画面を移ると確かめようがない
        eligible_ids = {w["id"] for w in eligible}
        explain.update(
            chosen=wid, at=datetime.now().strftime("%Y-%m-%d %H:%M"),
            excluded=[{"name": w["name"], "missing": perms.labels(perms.missing(w, task))}
                      for w in workers_ if w["id"] not in eligible_ids])
        conn.execute("UPDATE tasks SET assigned_worker_id = ?, status = 'assigned', ai_note = ? WHERE id = ?",
                     (wid, json.dumps(explain, ensure_ascii=False), task["id"]))
    return wid, eligible


@app.route("/tasks/auto_assign_all", methods=["POST"])
def auto_assign_all():
    """
    担当者の決まっていない未着手のタスクを、まとめて AI で割り当てる。

    急ぐものから順に決める（至急 → 高 → … 、同じなら期限の近い順）。先に決めた分は
    その人の手持ちに足してから次を決めるので、1人に集中しない。担当者を指定した
    タスク・作業中・完了済みは対象にしない（担当者が決まっているため）。
    """
    conn = db.get_db()
    targets = conn.execute("""
        SELECT * FROM tasks
        WHERE assigned_worker_id IS NULL AND status IN ('todo', 'assigned')
        ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,
                 deadline IS NULL, deadline, id
    """).fetchall()
    if not targets:
        conn.close()
        flash("担当者の決まっていない未着手のタスクはありません", "error")
        return _back_to("tasks")
    workers_ = conn.execute("SELECT * FROM workers").fetchall()
    if not workers_:
        conn.close()
        flash("作業者が登録されていないため、割り当てできません", "error")
        return _back_to("tasks")
    logs = _ai_logs(conn)
    open_tasks = _open_assignments(conn)
    per_worker, left = {}, []
    for task in targets:
        wid, _ = _ai_pick(conn, task, workers_, logs, open_tasks)
        if wid:
            open_tasks.append({"id": task["id"], "difficulty": task["difficulty"], "quantity": task["quantity"],
                               "status": "assigned", "started_at": None, "assigned_worker_id": wid})
            per_worker[wid] = per_worker.get(wid, 0) + 1
        else:
            left.append(task["title"])
    conn.commit()
    conn.close()

    names = {w["id"]: w["name"] for w in workers_}
    done = sum(per_worker.values())
    if done:
        breakdown = "・".join(f"{names[w]} さん {n}件" for w, n in
                             sorted(per_worker.items(), key=lambda kv: -kv[1]))
        how = "" if ai_stub.is_ready() else "AI本体を読み込めなかったため勤続年数で暫定的に。"
        flash(f"{how}AIが {done} 件をまとめて割り当てました（{breakdown}）。"
              f"理由は各タスクの「AIが選んだ理由」で確認できます", "ok" if ai_stub.is_ready() else "error")
    if left:
        shown = "、".join(f"「{t}」" for t in left[:3]) + (f" ほか {len(left) - 3} 件" if len(left) > 3 else "")
        flash(f"必要な権限を持つ作業者がいないため、{len(left)} 件は未割当のままです（{shown}）", "error")
    return _back_to("tasks")


@app.route("/tasks/<int:task_id>/delete", methods=["POST"])
def delete_task(task_id):
    conn = db.get_db()
    # このタスクで使用中の機材は空きに戻す。タスクだけ外すとフリー利用に見えて残る
    released = _release_equipment(conn, "current_task_id", task_id)
    conn.execute("UPDATE equipment SET current_task_id = NULL WHERE current_task_id = ?", (task_id,))
    row = conn.execute("SELECT demo FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row and row["demo"]:
        # デモで足したタスクの実績はデモと一緒に消す。参照を外して残すと、解除しても
        # どのデモの実績だったか分からなくなり、架空の実績が学習データに残り続ける
        dropped = conn.execute("DELETE FROM work_logs WHERE task_id = ?", (task_id,)).rowcount
        if dropped:
            ai_stub.invalidate_cache()
        logs = 0
    else:
        logs = _detach_work_logs(conn, "task_id", task_id)
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    _resync_released(released)
    notes = [n for n in (_released_note(released), f"実績 {logs} 件は残しています" if logs else "") if n]
    flash("タスクを削除しました" + (f"（{' / '.join(notes)}）" if notes else ""), "ok")
    return _back_to("tasks")


# ---------------------------------------------------------------- 定期タスク
# 毎週の点検のように繰り返すタスクを1回登録しておけば、予定日に recurring.py が
# タスク一覧へ1件ずつ立てる（要件「定期メンテナンス等の習慣的タスクの自動生成」）。


def _recurring_rules():
    """タスク管理画面に出す定期タスクの一覧。次回の予定日と、前回分が残っているかを添える"""
    conn = db.get_db()
    rows = conn.execute("""
        SELECT r.*, w.name AS worker_name, e.name AS equipment_name,
               (SELECT COUNT(*) FROM tasks t WHERE t.recurring_id = r.id AND t.status != 'done') AS open_n
        FROM recurring_tasks r
        LEFT JOIN workers w   ON w.id = r.worker_id
        LEFT JOIN equipment e ON e.id = r.equipment_id
        ORDER BY r.active DESC, r.id
    """).fetchall()
    conn.close()
    today = datetime.now().date()
    out = []
    for r in rows:
        nxt = recurring.next_due(r, today)
        out.append(dict(r, schedule=recurring.describe(r),
                        next_due=nxt.isoformat() if nxt else None))
    return out


def run_recurring(today=None):
    """予定日を迎えた定期タスクを作る。作ったタスクの [(id, タスク名)] を返す"""
    try:
        conn = db.get_db()
        try:
            made = recurring.generate(conn, today)
            conn.commit()
        finally:
            conn.close()
    except db.sqlite3.OperationalError as e:   # recurring_tasks の無い古いDB（テストの一時DBなど）
        print(f"[recurring] 定期タスクを確認できませんでした: {e}")
        return []
    for task_id, title in made:
        print(f"[recurring] 定期タスク「{title}」を作りました (#{task_id})")
    return made


@app.route("/recurring/add", methods=["POST"])
def add_recurring():
    f = request.form
    title = f.get("title", "").strip()
    if not title:
        flash("タスク名を入力してください", "error")
        return _back_to("tasks")
    frequency = f.get("frequency") if f.get("frequency") in recurring.FREQUENCIES else "weekly"
    weekdays = recurring.dump_weekdays(f.getlist("weekdays"))
    month_day = _safe_int(f.get("month_day"), 0)
    if frequency == "weekly" and not weekdays:
        flash("毎週の場合は曜日を1つ以上選んでください", "error")
        return _back_to("tasks")
    if frequency == "monthly" and not 1 <= month_day <= 31:
        flash("毎月の場合は日にち（1〜31）を入力してください", "error")
        return _back_to("tasks")

    conn = db.get_db()
    equipment_id = f.get("equipment_id") or None
    if equipment_id and not _row_exists(conn, "equipment", equipment_id):
        conn.close()
        flash("指定された機材が見つかりません", "error")
        return _back_to("tasks")
    required_perms = perms.dump(f.getlist("required_permissions"))
    team = _team_size_input(f.get("required_workers"))
    worker_id = f.get("worker_id") or None
    if worker_id:
        _, error = _check_designee(conn, worker_id, required_perms, team)
        if error:
            conn.close()
            flash(error, "error")
            return _back_to("tasks")
    # last_run を昨日にしておく。空のままだと、登録した時点で過去の予定日
    # （例: 木曜に登録した「毎週月曜」の今週月曜分）を期限切れで作ってしまう
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    conn.execute(
        """INSERT INTO recurring_tasks (title, description, difficulty, priority, required_permissions,
                                        quantity, equipment_id, worker_id, frequency, weekdays,
                                        month_day, deadline_days, last_run, required_workers)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (title, f.get("description", "").strip(), _safe_int(f.get("difficulty"), 3),
         _valid_priority(f.get("priority")), required_perms, _safe_int(f.get("quantity"), 1) or 1,
         equipment_id, worker_id, frequency, weekdays if frequency == "weekly" else "",
         month_day if frequency == "monthly" else None,
         max(_safe_int(f.get("deadline_days"), 0), 0), yesterday, team))
    conn.commit()
    conn.close()
    # 今日が予定日なら、その場で今日の分を作る（翌日まで待たせない）
    made = run_recurring()
    flash(f"定期タスク「{title}」を登録しました"
          + ("（今日の分をタスク一覧に追加しました）" if made else ""), "ok")
    return _back_to("tasks")


@app.route("/recurring/<int:rule_id>/toggle", methods=["POST"])
def toggle_recurring(rule_id):
    """一時停止 / 再開。再開した日より前の予定日の分はさかのぼって作らない"""
    conn = db.get_db()
    row = conn.execute("SELECT * FROM recurring_tasks WHERE id = ?", (rule_id,)).fetchone()
    if not row:
        conn.close()
        flash("定期タスクが見つかりません", "error")
        return _back_to("tasks")
    active = 0 if row["active"] else 1
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    conn.execute("UPDATE recurring_tasks SET active = ?, last_run = CASE WHEN ? THEN ? ELSE last_run END"
                 " WHERE id = ?", (active, active, yesterday, rule_id))
    conn.commit()
    conn.close()
    made = run_recurring() if active else []
    flash(f"定期タスク「{row['title']}」を{'再開' if active else '一時停止'}しました"
          + ("（今日の分をタスク一覧に追加しました）" if made else ""), "ok")
    return _back_to("tasks")


@app.route("/recurring/<int:rule_id>/run", methods=["POST"])
def run_recurring_now(rule_id):
    """予定日を待たずに1件作る（臨時の点検など）。前回分が残っていれば作らない"""
    conn = db.get_db()
    row = conn.execute("SELECT * FROM recurring_tasks WHERE id = ?", (rule_id,)).fetchone()
    if not row:
        conn.close()
        flash("定期タスクが見つかりません", "error")
        return _back_to("tasks")
    if recurring.open_instance(conn, rule_id) is not None:
        conn.close()
        flash(f"「{row['title']}」は前回の分がまだ終わっていないため、作りませんでした", "error")
        return _back_to("tasks")
    recurring.create_task(conn, row, datetime.now().date())
    conn.commit()
    conn.close()
    flash(f"「{row['title']}」をタスク一覧に追加しました", "ok")
    return _back_to("tasks")


@app.route("/recurring/<int:rule_id>/delete", methods=["POST"])
def delete_recurring(rule_id):
    """設定だけを消す。これまでに作ったタスクは残す（作業中のものもある）"""
    conn = db.get_db()
    row = conn.execute("SELECT title FROM recurring_tasks WHERE id = ?", (rule_id,)).fetchone()
    conn.execute("UPDATE tasks SET recurring_id = NULL WHERE recurring_id = ?", (rule_id,))
    conn.execute("DELETE FROM recurring_tasks WHERE id = ?", (rule_id,))
    conn.commit()
    conn.close()
    if row:
        flash(f"定期タスク「{row['title']}」を削除しました（作成済みのタスクは残しています）", "ok")
    return _back_to("tasks")


@app.route("/equipment")
def equipment():
    conn = db.get_db()
    rows = conn.execute(f"""
        SELECT e.*, COALESCE({_team_names_sql('e.current_task_id')}, w.name) AS worker_name,
               t.title AS task_title,
               COALESCE(t.started_at, e.updated_at) AS since
        FROM equipment e
        LEFT JOIN workers w ON w.id = e.current_worker_id
        LEFT JOIN tasks t   ON t.id = e.current_task_id
        ORDER BY e.id
    """).fetchall()
    overdue = _overdue_sessions(conn)
    conn.close()
    with _pending_lock:
        pending_modules = dict(_pending_modules)
    return _render("equipment.html", equipment=rows, pending_modules=pending_modules,
                   overdue=overdue, now=datetime.now().strftime("%Y-%m-%dT%H:%M"))


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
        if hostname:
            # 紐付けフォームと同じ後始末。INSERT だけだと未登録一覧に残り、
            # 次のハートビートまでオフライン表示のままになる
            _bind_module(conn, cur.lastrowid, hostname)
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


@app.route("/equipment/<int:eq_id>/update", methods=["POST"])
def update_equipment(eq_id):
    """
    機材名とモジュールIDの変更（新UIの編集欄）。モジュールIDの付け替えは
    /equipment/bind と同じ処理（_bind_module）を通す。
    """
    name = request.form.get("name", "").strip()
    hostname = request.form.get("hostname", "").strip() or None
    conn = db.get_db()
    row = conn.execute("SELECT * FROM equipment WHERE id = ?", (eq_id,)).fetchone()
    if not row:
        conn.close()
        flash("機材が見つかりません", "error")
        return redirect(url_for("equipment"))
    if not name:
        conn.close()
        flash("機材名を入力してください", "error")
        return redirect(url_for("equipment"))

    renamed = name != row["name"]
    if renamed:
        conn.execute("UPDATE equipment SET name = ? WHERE id = ?", (name, eq_id))
        conn.commit()
    rebound = "hostname" in request.form and hostname != row["hostname"]
    if rebound:
        _bind_module(conn, eq_id, hostname)
    eq = conn.execute("SELECT * FROM equipment WHERE id = ?", (eq_id,)).fetchone()
    conn.close()

    # 待機中・停止中のモジュールの画面には機材名が出ているので送り直す。使用中の画面は
    # 作業者とタスクを出しているだけなので触らない。操作中（メニューや問い合わせを
    # 出している最中）に送ると、その表示を消してしまうので送らない
    device_id = _device_id_of(eq)
    if renamed and eq["status"] != "working" and device_id \
            and (eq["online"] or device_id in _virtual_modules):
        with _touch_busy_lock:
            busy = device_id in _touch_busy
        if not busy:
            _sync_module_state(device_id, eq_id)

    if renamed and rebound:
        flash(f"機材名を「{name}」に変更し、モジュールIDを{'「' + hostname + '」に' if hostname else '外し'}ました", "ok")
    elif renamed:
        flash(f"機材名を「{row['name']}」から「{name}」に変更しました", "ok")
    elif rebound:
        flash(f"モジュールID「{hostname}」を紐付けました" if hostname else "紐付けを解除しました", "ok")
    else:
        flash("変更はありませんでした", "ok")
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
    pending = _forget_module(hostname, conn)
    if pending:
        conn.execute(
            """UPDATE equipment SET online = 1, ip = COALESCE(?, ip),
               last_seen = datetime('now','localtime') WHERE id = ?""",
            (pending.get("ip"), eq_id),
        )
    elif not hostname:
        conn.execute("UPDATE equipment SET online = 0 WHERE id = ?", (eq_id,))
    conn.commit()
    if pending:
        # 向こうは「未登録のモジュールです」等を出したまま。待機画面を送っておく
        _sync_module_state(hostname, eq_id)


@app.route("/equipment/<int:eq_id>/delete", methods=["POST"])
def delete_equipment(eq_id):
    conn = db.get_db()
    conn.execute("UPDATE tasks SET equipment_id = NULL WHERE equipment_id = ?", (eq_id,))
    conn.execute("UPDATE recurring_tasks SET equipment_id = NULL WHERE equipment_id = ?", (eq_id,))
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


def _close_session(eq_id, outcome="done", ended_at=None):
    """
    使用中の機材を終わらせる。終了のタッチを忘れたときの後始末で、管理画面の
    「代わりに終了」と、別の機材でタッチしたときの確認（_ask_forgotten_session）が使う。

    outcome:
        "done"  … タスクを完了にして実績を残す。所要時間は ended_at まで
        "abort" … タスクは終わっていない。未着手に戻す（担当者指定なら本人のまま）
    フリー利用なら、どちらでも機材を空けるだけ。
    戻り値: 終わらせた内容の dict / 使用中でなければ None
    """
    ended_at = ended_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = db.get_db()
    eq = conn.execute("""
        SELECT e.*, w.name AS worker_name FROM equipment e
        LEFT JOIN workers w ON w.id = e.current_worker_id
        WHERE e.id = ? AND e.status = 'working'""", (eq_id,)).fetchone()
    if not eq:
        conn.close()
        return None
    task = None
    if eq["current_task_id"]:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (eq["current_task_id"],)).fetchone()
    result = {"equipment": eq["name"], "worker": eq["worker_name"],
              "task": task["title"] if task else None, "duration_sec": None, "work_log_id": None}
    members = _team_members(conn, task["id"]) if task and perms.team_size(task) > 1 else []
    if len(members) > 1:
        result["worker"] = "・".join(m["name"] for m in members)
    if task and task["status"] == "in_progress":
        if outcome == "done":
            duration = None
            if task["started_at"]:
                duration = int((datetime.strptime(ended_at, "%Y-%m-%d %H:%M:%S")
                                - datetime.strptime(task["started_at"], "%Y-%m-%d %H:%M:%S")).total_seconds())
            conn.execute("UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
                         (ended_at, task["id"]))
            # 複数人タスクは全員に実績を残す
            for wid in _log_worker_ids(task, members):
                cur = conn.execute(
                    """INSERT INTO work_logs (task_id, worker_id, equipment_id, started_at, completed_at, duration_sec)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (task["id"], wid, eq_id, task["started_at"], ended_at, duration))
                result["work_log_id"] = result["work_log_id"] or cur.lastrowid
            result["duration_sec"] = duration
        else:
            # 中断。担当者を指定したタスクは本人に残し、そうでなければ誰でも拾えるよう戻す
            conn.execute(
                """UPDATE tasks SET started_at = NULL, ai_note = NULL,
                       status = CASE WHEN designated = 1 AND assigned_worker_id IS NOT NULL
                                     THEN 'assigned' ELSE 'todo' END,
                       assigned_worker_id = CASE WHEN designated = 1 THEN assigned_worker_id END
                   WHERE id = ?""", (task["id"],))
            conn.execute("DELETE FROM task_members WHERE task_id = ?", (task["id"],))
    conn.execute(
        """UPDATE equipment SET status = 'idle', current_worker_id = NULL, current_task_id = NULL,
           updated_at = datetime('now','localtime') WHERE id = ?""", (eq_id,))
    conn.commit()
    conn.close()
    if result["work_log_id"]:
        ai_stub.invalidate_cache()
    _resync_released([eq])
    return result


@app.route("/equipment/<int:eq_id>/finish", methods=["POST"])
def finish_equipment(eq_id):
    """
    終了のタッチを忘れた作業を、管理者が代わりに終わらせる。
    終わった時刻を入れてもらうのは、所要時間が AI の学習データになるため。
    忘れていた時間まで作業時間に数えると、その人が遅いと学習してしまう。
    """
    f = request.form
    outcome = "abort" if f.get("outcome") == "abort" else "done"
    conn = db.get_db()
    eq = conn.execute("""SELECT e.status, e.name, t.started_at FROM equipment e
                         LEFT JOIN tasks t ON t.id = e.current_task_id WHERE e.id = ?""",
                      (eq_id,)).fetchone()
    conn.close()
    if not eq or eq["status"] != "working":
        flash("この機材は使用中ではありません", "error")
        return _back_to("equipment")
    now = datetime.now()
    ended = now
    if f.get("ended_at"):
        try:
            ended = datetime.strptime(f["ended_at"], "%Y-%m-%dT%H:%M")
        except ValueError:
            flash("終わった時刻の形式が正しくありません", "error")
            return _back_to("equipment")
    if ended > now:
        flash("終わった時刻に、これから先の時刻は指定できません", "error")
        return _back_to("equipment")
    if eq["started_at"] and ended < datetime.strptime(eq["started_at"], "%Y-%m-%d %H:%M:%S"):
        flash(f"終わった時刻が、始めた時刻（{eq['started_at'][:16]}）より前になっています", "error")
        return _back_to("equipment")
    result = _close_session(eq_id, outcome, ended.strftime("%Y-%m-%d %H:%M:%S"))
    if result is None:
        flash("この機材は使用中ではありません", "error")
        return _back_to("equipment")
    who = f"{result['worker']} さんの" if result["worker"] else ""
    if not result["task"]:
        what = "フリー利用を終了しました"
    elif outcome == "done":
        what = f"「{result['task']}」を完了にしました"
    else:
        what = f"「{result['task']}」を中断し、もう一度着手できる状態に戻しました"
    print(f"[admin] {result['equipment']}: {who}{what}（代理）")
    flash(f"{result['equipment']}：{who}{what}。機材は空きに戻りました", "ok")
    return _back_to("equipment")


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
        # 実績の中身が変わった。件数と最大idは同じなので、明示的に捨てる
        ai_stub.invalidate_cache()
    return n


def _release_equipment(conn, column, value):
    """
    削除されるタスク・作業者で使用中になっている機材を空きに戻す。column は
    current_task_id か current_worker_id。戻した機材の行（戻す前）を返すので、
    commit した後に _resync_released() でモジュールの画面も戻す。
    """
    rows = conn.execute(f"SELECT * FROM equipment WHERE status IN ('working', 'gathering') AND {column} = ?",
                        (value,)).fetchall()
    for r in rows:
        # 集合待ちならタスクも集合前に戻す。機材だけ空けると、タスクが集合待ちのまま残る
        if r["status"] == "gathering" and r["current_task_id"]:
            _reset_gathering_task(conn, r["current_task_id"])
    if rows:
        conn.execute(
            f"""UPDATE equipment SET status = 'idle', current_worker_id = NULL, current_task_id = NULL,
                updated_at = datetime('now','localtime')
                WHERE status IN ('working', 'gathering') AND {column} = ?""",
            (value,))
    return rows


def _resync_released(rows):
    """空きに戻した機材のモジュールへ待機画面を送る。繋がっていない相手には送らない"""
    for eq in rows:
        device_id = _device_id_of(eq)
        if device_id and (eq["online"] or device_id in _virtual_modules):
            _sync_module_state(device_id, eq["id"])


def _released_note(rows):
    return f"使用中だった {'・'.join(r['name'] for r in rows)} を空きに戻しました" if rows else ""


# ------------------------------------------------ 複数人タスク（集合待ち）
# required_workers >= 2 のタスクは、最初の人（リーダー）が着手を選ぶと機材が「集合待ち」になり、
# 同じ機材で残りの人がタッチして加わる。**ちょうど必要人数**がそろった時点で作業開始になる。
# 必要権限はメンバーの誰か1人が持っていればよい（permissions.team_missing）。
# そろわないまま GATHER_TIMEOUT_SEC が過ぎたら取り消し、機材とタスクを集合前に戻す。
# 作業終了はメンバーの誰がタッチしてもよく、全員分の実績が残る。
GATHER_TIMEOUT_SEC = int(os.environ.get("GEMMBA_GATHER_TIMEOUT_SEC", 180))


def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _team_members(conn, task_id):
    """複数人タスクのメンバー（加わった順）。workers の行。1人で行うタスクは空"""
    return conn.execute("""
        SELECT w.*, m.joined_at FROM task_members m JOIN workers w ON w.id = m.worker_id
        WHERE m.task_id = ? ORDER BY m.joined_at, m.rowid""", (task_id,)).fetchall()


def _log_worker_ids(task, members):
    """完了の実績を残す相手。複数人タスクはメンバー全員、そうでなければ担当者"""
    if members:
        return [m["id"] for m in members]
    return [task["assigned_worker_id"]] if task["assigned_worker_id"] else []


def _gathering(conn, eq_id):
    """集合待ちの機材とそのタスク。集合待ちでなければ None"""
    return conn.execute("""
        SELECT e.id AS eq_id, e.name AS eq_name, e.module_id, e.hostname, e.online,
               e.current_worker_id AS leader_id, e.updated_at AS since,
               t.id AS task_id, t.title, t.priority, t.required_permissions, t.required_workers,
               t.quantity, t.deadline, w.name AS leader_name
        FROM equipment e
        JOIN tasks t ON t.id = e.current_task_id
        LEFT JOIN workers w ON w.id = e.current_worker_id
        WHERE e.id = ? AND e.status = 'gathering'""", (eq_id,)).fetchone()


def _other_gathering(conn, worker_id, task_id=None):
    """task_id 以外の集合待ちに加わっていれば、そのタスクと機材。2か所で同時に待たせない"""
    return conn.execute("""
        SELECT t.id, t.title, e.name AS eq_name FROM task_members m
        JOIN tasks t ON t.id = m.task_id
        LEFT JOIN equipment e ON e.current_task_id = t.id
        WHERE m.worker_id = ? AND t.status = 'gathering' AND t.id IS NOT ?""",
        (worker_id, task_id)).fetchone()


def _reset_gathering_task(conn, task_id):
    """集合待ちのタスクを集合前（割当済 / 未着手）に戻し、集まっていた人を外す。commit は呼び出し側"""
    conn.execute("""UPDATE tasks SET status = CASE WHEN assigned_worker_id IS NOT NULL
                                                  THEN 'assigned' ELSE 'todo' END
                    WHERE id = ? AND status = 'gathering'""", (task_id,))
    conn.execute("DELETE FROM task_members WHERE task_id = ?", (task_id,))


def _begin_gathering(conn, task, worker, equipment):
    """
    api_start_task の複数人タスク版。リーダーだけが加わった集合待ちにする。
    commit / rollback まで行う。戻り値: (応答の dict, HTTPステータス)
    """
    busy = _other_gathering(conn, worker["id"], task["id"])
    if busy:
        conn.rollback()
        return {"error": "already gathering", "equipment": busy["eq_name"], "title": busy["title"]}, 409
    now = _now_str()
    # 候補を見せてから「はい」が押されるまでの間に、他の人が着手・集合を始めていたら断る
    cur = conn.execute("UPDATE tasks SET status = 'gathering' WHERE id = ? AND status IN ('todo', 'assigned')",
                       (task["id"],))
    if cur.rowcount == 0:
        conn.rollback()
        return {"error": "already taken"}, 409
    cur = conn.execute(
        """UPDATE equipment SET status = 'gathering', current_worker_id = ?, current_task_id = ?,
           updated_at = ? WHERE id = ? AND status = 'idle'""",
        (worker["id"], task["id"], now, equipment["id"]))
    if cur.rowcount == 0:
        conn.rollback()
        return {"error": "equipment busy"}, 409
    conn.execute("DELETE FROM task_members WHERE task_id = ?", (task["id"],))
    conn.execute("INSERT INTO task_members (task_id, worker_id, joined_at) VALUES (?, ?, ?)",
                 (task["id"], worker["id"], now))
    conn.commit()
    _schedule_gathering_timeout(equipment["id"], now)
    return {"ok": True, "gathering": True, "have": 1, "need": perms.team_size(task)}, 200


def _join_gathering(eq_id, worker_id):
    """
    集合待ちに1人加える。ちょうど必要人数になったら、その場で作業開始にする。
    戻り値: (結果, 情報の dict)。結果は
        "joined"     … 加わった（まだそろっていない）
        "started"    … 加わってそろったので作業開始
        "already"    … もう加わっている
        "full"       … 既にそろっている
        "busy"       … 別の機材の集合待ちに加わっている
        "permission" … 最後の1人なのに、加わっても必要権限が足りない
        "gone"       … 集合待ちが終わっていた（時間切れ・取り消し）
    """
    conn = db.get_db()
    try:
        # 読んでから書くまでの間に、時間切れの取り消しや別の参加が割り込まないようにする
        conn.execute("BEGIN IMMEDIATE")
        g = _gathering(conn, eq_id)
        if not g:
            return "gone", {}
        members = _team_members(conn, g["task_id"])
        need = perms.team_size(g)
        info = {"title": g["title"], "need": need, "have": len(members), "leader": g["leader_name"]}
        if any(m["id"] == worker_id for m in members):
            return "already", info
        if len(members) >= need:
            return "full", info
        busy = _other_gathering(conn, worker_id, g["task_id"])
        if busy:
            return "busy", dict(info, elsewhere=busy["eq_name"])
        worker = conn.execute("SELECT * FROM workers WHERE id = ?", (worker_id,)).fetchone()
        if not worker:
            return "gone", info
        team = list(members) + [worker]
        lacking = perms.team_missing(team, g)
        if lacking and len(team) == need:
            return "permission", dict(info, missing=perms.labels(lacking))
        now = _now_str()
        conn.execute("INSERT INTO task_members (task_id, worker_id, joined_at) VALUES (?, ?, ?)",
                     (g["task_id"], worker_id, now))
        info["have"] = len(team)
        info["names"] = [m["name"] for m in team]
        if len(team) < need:
            conn.commit()
            return "joined", info
        # そろった。担当者はリーダー（最初にタッチした人）にする
        conn.execute("""UPDATE tasks SET status = 'in_progress', assigned_worker_id = ?, equipment_id = ?,
                        started_at = ? WHERE id = ?""", (g["leader_id"], eq_id, now, g["task_id"]))
        conn.execute("UPDATE equipment SET status = 'working', updated_at = ? WHERE id = ?", (now, eq_id))
        conn.commit()
        return "started", info
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()


def _cancel_gathering(eq_id, since=None):
    """
    集合待ちを取り消し、機材を空きに、タスクを集合前に戻す。
    since を渡すと、その時刻に始まった集合待ちのときだけ取り消す（時間切れのタイマー用。
    一度取り消した後に同じ機材で始まった、次の集合待ちを巻き込まない）。
    戻り値: 取り消した集合待ち / 取り消さなかったら None
    """
    conn = db.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        g = _gathering(conn, eq_id)
        if not g or (since and g["since"] != since):
            return None
        _reset_gathering_task(conn, g["task_id"])
        conn.execute("""UPDATE equipment SET status = 'idle', current_worker_id = NULL, current_task_id = NULL,
                        updated_at = datetime('now','localtime') WHERE id = ?""", (eq_id,))
        conn.commit()
        return g
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()


def _leave_gathering(eq_id, worker_id):
    """集合待ちから1人抜ける。リーダーが抜けると集合待ちごと取り消す。
    戻り値: "left" / "cancelled" / None（集合待ちではなかった）"""
    conn = db.get_db()
    try:
        g = _gathering(conn, eq_id)
        if not g:
            return None
        if g["leader_id"] != worker_id:
            conn.execute("DELETE FROM task_members WHERE task_id = ? AND worker_id = ?",
                         (g["task_id"], worker_id))
            conn.commit()
            return "left"
    finally:
        conn.close()
    return "cancelled" if _cancel_gathering(eq_id) else None


def _schedule_gathering_timeout(eq_id, since):
    timer = threading.Timer(GATHER_TIMEOUT_SEC, _gathering_timed_out, (eq_id, since))
    timer.daemon = True
    timer.start()


def _gathering_timed_out(eq_id, since):
    """そろわないまま時間が過ぎた。取り消して、モジュールにその旨を出す"""
    g = _cancel_gathering(eq_id, since)
    if not g:
        return
    print(f"[{g['module_id']}] 「{g['title']}」は人数がそろわなかったため集合待ちを取り消しました")
    device_id = _device_id_of(g)
    if not device_id or not (g["online"] or device_id in _virtual_modules):
        return
    with _touch_busy_lock:
        if device_id in _touch_busy:
            # 誰かが参加を操作している。その操作が「集合待ちは終わっています」を出す
            return
    _notify_briefly(device_id, g["module_id"],
                    ["人数がそろいませんでした", g["title"], "集合待ちを取り消しました"], "idle", sec=8)


def cancel_stale_gatherings(now=None):
    """
    時間切れを過ぎた集合待ちを取り消す。タイマーは app.py を再起動すると消えるので、
    見回り（start_background_jobs）でも拾う。取り消した機材の id を返す
    """
    now = now or datetime.now()
    conn = db.get_db()
    try:
        rows = conn.execute("SELECT id, updated_at FROM equipment WHERE status = 'gathering'").fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            since = datetime.strptime(r["updated_at"], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            since = None
        if since is None or (now - since).total_seconds() >= GATHER_TIMEOUT_SEC:
            _gathering_timed_out(r["id"], r["updated_at"])
            out.append(r["id"])
    return out


# ------------------------------------------------ API（モジュール/AI連携用）
# ESP32 側からはここを HTTP で叩く想定。WebSocket 化する場合もこの層を置き換えるだけでよい。

@app.route("/api/equipment/<module_id>/status", methods=["GET"])
def api_get_equipment_status(module_id):
    """モジュールからの状態確認。タッチ時にハード側が「開始/終了/ロック中」を判定するために使う"""
    conn = db.get_db()
    eq = conn.execute("SELECT * FROM equipment WHERE module_id = ?", (module_id,)).fetchone()
    if not eq:
        conn.close()
        return jsonify({"error": "unknown module_id"}), 404
    body = dict(eq)
    # 複数人タスクのメンバー。リーダー（current_worker_id）以外も「この機材の使用者」として扱う
    body["member_ids"] = [r["worker_id"] for r in conn.execute(
        "SELECT worker_id FROM task_members WHERE task_id = ?", (eq["current_task_id"],))] \
        if eq["current_task_id"] else []
    conn.close()
    return jsonify(body)


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
               required_permissions, assigned_worker_id, designated, required_workers
        FROM tasks
        WHERE status IN ('todo', 'assigned', 'in_progress')
          AND (assigned_worker_id = ? OR assigned_worker_id IS NULL)
        ORDER BY created_at, id
    """, (w["id"],)).fetchall()]
    logs = _ai_logs(conn)
    # タッチされた機材（?module_id=）。機材指定の無いタスクを「この機材でやったら」で評価する
    eq_row = None
    if request.args.get("module_id"):
        eq_row = conn.execute("SELECT id FROM equipment WHERE module_id = ?",
                              (request.args["module_id"],)).fetchone()
    conn.close()
    # 着手済みは投げ出させないよう先頭に固定する。ここは権限で落とさない。
    # 作業中に資格が取り消されても、完了して機材を解放する経路は残す必要がある。
    in_progress = [t for t in rows if t["status"] == "in_progress"]
    # 未着手の候補は権限（D-2）を満たすものだけ。この1行でモジュール側の
    # 候補提示（C-1/C-2）と他機材への誘導（C-3）の両方に効く。
    rest = perms.eligible_tasks(w, [t for t in rows if t["status"] != "in_progress"])
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

    # 候補は api_next_task で絞ってあるが、このAPIは単体でも叩けるので二重に見る（D-2）。
    # 複数人タスクは、そろう時点でメンバー全体で見る（_join_gathering）
    team = perms.team_size(task)
    lacking = perms.missing(worker, task) if team == 1 else []
    if lacking:
        conn.close()
        print(f"[{data.get('module_id')}] {worker['name']}: 権限不足で着手を拒否 "
              f"({'/'.join(lacking)}) task={task['title']}")
        return jsonify({"error": "permission denied", "missing": lacking,
                        "missing_labels": perms.labels(lacking)}), 403

    # 担当者を指定したタスクは本人しか着手できない（D-3）。api_next_task は本人にしか
    # 出さないが、このAPIは単体でも叩けるので二重に見る
    if task["designated"] and task["assigned_worker_id"] not in (None, worker["id"]):
        conn.close()
        print(f"[{data.get('module_id')}] {worker['name']}: 担当者指定のため着手を拒否 task={task['title']}")
        return jsonify({"error": "designated to another worker"}), 403

    if team > 1:
        body, code = _begin_gathering(conn, task, worker, equipment)
        conn.close()
        if code == 200:
            print(f"[{data.get('module_id')}] {worker['name']}: 「{task['title']}」の集合待ちを開始"
                  f"（{team}人）")
        return jsonify(body), code

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    started_at = task["started_at"] or now
    # 他の人が先に着手していたら断る。候補を見せてから「はい」が押されるまで
    # 最大25秒あり、その間に別の機材で同じタスクに着手されうる。上書きすると
    # 先の人の機材が作業中のまま残り、実績も後の人に付いてしまう。
    # SELECT した後の判定では同時のリクエストを防げないので、UPDATE の条件で見る
    cur = conn.execute(
        """UPDATE tasks SET status = 'in_progress', assigned_worker_id = ?, equipment_id = ?,
           started_at = ? WHERE id = ? AND status != 'done'
             AND (status != 'in_progress' OR assigned_worker_id = ?)""",
        (worker["id"], equipment["id"], started_at, task_id, worker["id"]),
    )
    if cur.rowcount == 0:
        conn.rollback()
        conn.close()
        print(f"[{data.get('module_id')}] {worker['name']}: 着手済み・完了済みのため拒否 "
              f"task={task['title']}")
        return jsonify({"error": "already taken"}), 409
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
                            quantity, deadline, required_workers)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data["title"],
            data.get("description", ""),
            _safe_int(data.get("difficulty"), 3),
            _valid_priority(data.get("priority")),
            perms.dump(data.get("required_permissions")),
            _safe_int(data.get("quantity"), 1) or 1,
            data.get("deadline"),
            _team_size_input(data.get("required_workers")),
        ),
    )
    conn.commit()
    task_id = cur.lastrowid
    conn.close()
    return jsonify({"ok": True, "task_id": task_id}), 201


@app.route("/api/tasks/<int:task_id>/complete", methods=["POST"])
def api_complete_task(task_id):
    """
    モジュールの完了タッチ。所要時間を記録し、AIの学習結果を作り直させる。
    body の worker_id（任意）は終了をタッチした人。複数人タスクでは全員に実績を残し、
    返す work_log_id（難易度フィードバックの宛先）はこの人の分にする
    """
    data = request.get_json(silent=True) or {}
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
    members = _team_members(conn, task_id) if perms.team_size(task) > 1 else []
    toucher = _safe_int(data.get("worker_id"), None)
    work_log_id = None
    for wid in _log_worker_ids(task, members):
        cur = conn.execute(
            """INSERT INTO work_logs (task_id, worker_id, equipment_id, started_at, completed_at, duration_sec)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (task_id, wid, task["equipment_id"], task["started_at"], now, duration),
        )
        if work_log_id is None or wid == toucher:
            work_log_id = cur.lastrowid
    conn.commit()
    # 実績が増えたので次の割り当てで再学習させる。update_model は渡したログを
    # 使わないので、ここで全件を読み込む必要はない
    ai_stub.invalidate_cache()
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
    """
    録音を文字にする。戻り値: テキスト / None（失敗）

    **ロック待ちも含めて VOICE_TIMEOUT 以内に必ず返す。** 起動時の準備
    （start_stt_warmup）はモデルのダウンロード中ずっとロックを握るので、
    素直に待つと RECORD_* の予算を超え、モジュールが先に諦めた後で
    タスクが立つ（＝録り直しで同じタスクが2件）。
    """
    deadline = time.monotonic() + VOICE_TIMEOUT
    if not _stt_lock.acquire(timeout=VOICE_TIMEOUT):
        print("[voice] 文字起こしの準備中・処理中のため時間内に受け付けられませんでした")
        return None
    try:
        proc = _stt["proc"]
        if proc is None or proc.poll() is not None:
            # 起こし直すとモデルの読み込み（手元に無ければダウンロード）を待つことになり、
            # 予算に収まらない。裏で起こしておき、今回は諦める
            print("[voice] 文字起こしが起動していません。準備を始めたので、少し待ってから録り直してください")
            start_stt_warmup()
            return None
        try:
            proc.stdin.write(path + "\n")
            proc.stdin.flush()
        except (OSError, ValueError) as e:
            print(f"[voice] 文字起こしへ送れません: {e}")
            _stt_kill()
            return None
        res = _stt_reply(max(1.0, deadline - time.monotonic()))
        if res is None:
            # どこまで進んだか分からない。次の依頼に前回の答えが混ざらないよう畳む
            print("[voice] 文字起こしが時間内に終わりませんでした")
            _stt_kill()
            return None
        if res.get("error"):
            print(f"[voice] 文字起こしに失敗: {res['error']}")
            return None
        return (res.get("text") or "").strip()
    finally:
        _stt_lock.release()


def start_stt_warmup():
    """
    起動時に文字起こしの常駐を起こしておく（E-2）。

    _transcribe() は常駐が居なければこれを呼んで裏で起こすが、その録音自体は
    諦める（モデルの読み込みを待つと VOICE_TIMEOUT に収まらないため）。
    先に済ませておけば、最初の録音から使える。

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
    pending = _remember_tag(tag_id, module_id)
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
        SELECT e.name, e.status, w.name AS worker_name, t.title AS task_title,
               t.required_workers AS need,
               (SELECT COUNT(*) FROM task_members m WHERE m.task_id = e.current_task_id) AS have
        FROM equipment e
        LEFT JOIN workers w ON w.id = e.current_worker_id
        LEFT JOIN tasks t   ON t.id = e.current_task_id
        WHERE e.id = ?
    """, (eq_id,)).fetchone()
    conn.close()
    if not row:
        return
    if row["status"] == "gathering":
        need = row["need"] or 1
        lines = [row["task_title"] or "?", f"{row['have']}/{need}人 集合待ち",
                 f"あと{max(need - row['have'], 0)}人 社員証をタッチ", "決定ボタン長押しで取り消し"]
        led = "gathering"
    elif row["status"] == "working":
        who = f"{row['worker_name'] or '?'} さん"
        if row["have"] > 1:
            who += f" 他{row['have'] - 1}名"
        lines = [f"{who} 使用中", row["task_title"] or "フリー利用"]
        led = "working" if row["task_title"] else "free"
        # 見込みを大きく超えている。終了のタッチを忘れて立ち去った可能性が高いので、
        # 機材の前を通った人（本人を含む）に分かるよう画面で促す
        if eq_id in _overdue_now():
            lines += ["終わっていたらタッチして", "「作業終了」を選んでください"]
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
    """NFCタッチ（pi/<device_id>/data）。仮想モジュールのタッチもここへ入る。
    ボタンの長押し（{"event": "long_press", "button": "ok"}）も同じトピックで届く"""
    if payload.get("event") == "long_press":
        _handle_long_press(device_id, payload.get("button"))
        return
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


def _handle_long_press(device_id, button):
    """
    問い合わせが出ていないときの、決定ボタンの長押し。集合待ちならそれを取り消す。
    社員証を読まないので誰が押したかは分からないが、取り消しても集合前に戻るだけで
    実績は何も消えないので、その場にいる誰でも取り消せるようにしてある
    （来ない人を待ち続けて機材がふさがるのを、時間切れより先に解きたい）。
    """
    if button != "ok":
        return
    module_id = _resolve_module_id(device_id)
    if not module_id:
        return
    conn = db.get_db()
    eq = conn.execute("SELECT id, status FROM equipment WHERE module_id = ?", (module_id,)).fetchone()
    conn.close()
    if not eq or eq["status"] != "gathering":
        print(f"[{module_id}] 決定の長押し（集合待ちではないので何もしません）")
        return
    with _touch_busy_lock:
        if device_id in _touch_busy:
            # 誰かがタッチして参加・取り消しを操作している。そちらを優先する
            print(f"[{module_id}] 操作中なので決定の長押しを無視しました")
            return
    g = _cancel_gathering(eq["id"])
    if not g:
        return
    print(f"[{module_id}] 決定の長押しで「{g['title']}」の集合待ちを取り消しました")
    _notify_briefly(device_id, module_id, ["集合待ちを取り消しました", g["title"]], "idle", sec=4)


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
            _remember_module(device_id, ip)
        else:
            _forget_module(device_id)
        if online:
            print(f"[{device_id}] 未登録モジュールを検出 (ip={ip}) → 機材管理画面に表示")
        conn.close()
        return

    if device_id in _pending_modules:
        _forget_module(device_id)
    with _pending_lock:
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

    # 複数人タスクの集合待ち。メニューは出さず、参加（メンバーなら取り消し）を尋ねる
    if equipment["status"] == "gathering":
        _gathering_touch(device_id, module_id, worker, equipment)
        return

    # 排他制御。他人が使っている機材には、メニューを出す前に断る。
    # 複数人タスクのメンバーは「他人」ではない（誰でも作業終了できる）
    if equipment["status"] == "working" and not _uses_equipment(worker, equipment):
        print(f"[{module_id}] locked by another worker; rejecting {worker['name']}")
        _notify_briefly(device_id, module_id,
                        ["他の人が使用中です", f"{worker['name']} さんは使用できません"], "working")
        return

    # 別の機材で使用中のまま、こちらへ来た。終了のタッチを忘れている可能性が高い
    closed = _ask_forgotten_session(device_id, module_id, worker, equipment)
    if closed is None:
        return
    if closed:
        # 終わらせたタスクが候補（誘導先の判断材料）に残らないよう取り直す
        try:
            body = requests.get(f"{SELF_URL}/api/workers/{tag_id}/next_task",
                                params={"module_id": module_id}, timeout=HTTP_TIMEOUT).json()
            tasks = body["tasks"] + body.get("elsewhere", [])
        except (requests.RequestException, ValueError, KeyError) as e:
            print(f"[{module_id}] 候補の取り直しに失敗: {e}")
            return

    _show_menu(device_id, module_id, tag_id, worker, equipment, tasks)


def _uses_equipment(worker, equipment):
    """この人が機材の使用者か。複数人タスクならメンバー全員が使用者"""
    return (equipment.get("current_worker_id") == worker["id"]
            or worker["id"] in (equipment.get("member_ids") or []))


JOIN_TIMEOUT = 25     # 「この作業に参加しますか？」の応答待ち秒数


def _gathering_touch(device_id, module_id, worker, equipment):
    """
    集合待ちの機材でのタッチ。
    - まだ加わっていない人 → 参加するか尋ねる。最後の1人ならそこで作業開始
    - 加わっている人       → 取り消すか尋ねる（リーダーなら集合待ちごと取り消す）
    """
    conn = db.get_db()
    try:
        g = _gathering(conn, equipment["id"])
        members = _team_members(conn, g["task_id"]) if g else []
    finally:
        conn.close()
    if not g:
        _sync_module_state(device_id, equipment["id"])
        return
    need = perms.team_size(g)
    status_line = f"{len(members)}/{need}人 集合待ち"

    if any(m["id"] == worker["id"] for m in members):
        is_leader = g["leader_id"] == worker["id"]
        answer = request_confirm(
            device_id, "集合待ちを取り消しますか？" if is_leader else "参加を取り消しますか？",
            lines=[g["title"], status_line],
            timeout=JOIN_TIMEOUT, equipment_name=equipment.get("name"), worker_name=worker["name"],
        )
        if not answer:
            _sync_module_state(device_id, equipment["id"])
            return
        result = _leave_gathering(equipment["id"], worker["id"])
        print(f"[{module_id}] {worker['name']}: 「{g['title']}」の"
              + ("集合待ちを取り消し" if result == "cancelled" else "参加を取り消し"))
        if result == "cancelled":
            _notify_briefly(device_id, module_id, ["集合待ちを取り消しました", g["title"]], "idle", sec=4)
        else:
            _notify_pause(device_id, ["参加を取り消しました", g["title"]], "gathering")
            _sync_module_state(device_id, equipment["id"])
        return

    # 別の機材で使用中のまま来た（終了のタッチ忘れ）なら、先に片付けるか尋ねる
    if _ask_forgotten_session(device_id, module_id, worker, equipment) is None:
        return
    answer = request_confirm(
        device_id, "この作業に参加しますか？",
        lines=[f"{g['leader_name'] or '?'} さんの作業", g["title"], status_line],
        timeout=JOIN_TIMEOUT, equipment_name=equipment.get("name"), worker_name=worker["name"],
    )
    if answer is None:
        _notify_briefly(device_id, module_id, ["応答がありませんでした", "もう一度タッチしてください"], "gathering")
        return
    if not answer:
        _sync_module_state(device_id, equipment["id"])
        return

    result, info = _join_gathering(equipment["id"], worker["id"])
    print(f"[{module_id}] {worker['name']}: 「{g['title']}」への参加 → {result}")
    if result == "started":
        _notify(device_id, [f"{info['need']}人そろいました", info["title"],
                            "・".join(info["names"]), "終了時にもう一度タッチ"], "working")
    elif result == "joined":
        _notify_pause(device_id, ["参加しました", f"あと{info['need'] - info['have']}人です"], "gathering")
        _sync_module_state(device_id, equipment["id"])
    elif result == "permission":
        _notify_briefly(device_id, module_id,
                        ["権限を持つ人が必要です", "・".join(info["missing"])], "error")
    elif result == "busy":
        _notify_briefly(device_id, module_id,
                        ["別の集合待ちに参加中です", info.get("elsewhere") or ""], "error")
    elif result == "full":
        _notify_briefly(device_id, module_id, ["人数はそろっています"], "error")
    else:   # gone / already
        _notify_briefly(device_id, module_id, ["集合待ちは終わっています"], "idle")


FORGOT_TIMEOUT = 25   # 「〇〇で作業中のままです。終了しますか？」の応答待ち秒数


def _ask_forgotten_session(device_id, module_id, worker, equipment):
    """
    本人が別の機材を使用中のまま、この機材でタッチした。終了のタッチ忘れの
    一番よくある形（次の作業へ移った）なので、その場で前の機材を終わらせるか尋ねる。

    - はい   → 前の機材のタスクを完了にして空ける（所要時間は今まで）
    - いいえ → 2台を同時に使っている。何もせずメニューへ進む
    戻り値: 終わらせた機材の数 / None = 無応答（呼び出し側は何もせず終える）
    """
    conn = db.get_db()
    # 複数人タスクのメンバーとして使用中の機材も含める（終了すると全員分が終わる）
    others = conn.execute("""
        SELECT e.id, e.name, t.title AS task_title, t.required_workers FROM equipment e
        LEFT JOIN tasks t ON t.id = e.current_task_id
        WHERE e.status = 'working' AND e.id != ?
          AND (e.current_worker_id = ?
               OR EXISTS (SELECT 1 FROM task_members m
                          WHERE m.task_id = e.current_task_id AND m.worker_id = ?))
        ORDER BY e.updated_at""", (equipment["id"], worker["id"], worker["id"])).fetchall()
    conn.close()
    closed = 0
    for other in others:
        answer = request_confirm(
            device_id, "前の作業を終了しますか？",
            lines=[f"{other['name']} が", "使用中のままです",
                   (other["task_title"] or "フリー利用")
                   + (f"（{other['required_workers']}人作業）" if (other["required_workers"] or 1) > 1 else "")],
            timeout=FORGOT_TIMEOUT,
            equipment_name=equipment.get("name"), worker_name=worker["name"],
        )
        if answer is None:
            print(f"[{module_id}] {worker['name']}: 前の作業の確認で無応答のため中止")
            _notify_briefly(device_id, module_id,
                            ["応答がありませんでした", "もう一度タッチしてください"], "idle")
            return None
        if not answer:
            print(f"[{module_id}] {worker['name']}: {other['name']} は使用中のまま続ける")
            continue
        result = _close_session(other["id"], "done")
        if result:
            closed += 1
            print(f"[{module_id}] {worker['name']}: 終了タッチ忘れの {other['name']} を終了"
                  + (f"（{result['task']}）" if result["task"] else "（フリー利用）"))
            _notify_pause(device_id, [f"{other['name']} を", "終了しました"], "idle")
    return closed


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
                        and _uses_equipment(worker, equipment))
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
    # 管理者が本人を名指ししたタスク（D-3）。「なぜ自分に」が画面で分かるようにする
    if task.get("designated"):
        detail = "あなたが担当 / " + detail
    # 複数人タスク。「はい」で始まるのではなく集合待ちになることを先に知らせる
    if perms.team_size(task) > 1:
        detail = f"{perms.team_size(task)}人作業 / " + detail
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

    # 人手を待っている集合待ちが他の機材にあれば、まずそちらへ呼ぶ
    if _guide_to_gathering(device_id, module_id, worker, equipment):
        return

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
            try:
                res = requests.post(
                    f"{SELF_URL}/api/tasks/{task['id']}/start",
                    json={"nfc_tag_id": tag_id, "module_id": module_id}, timeout=HTTP_TIMEOUT,
                )
            except requests.RequestException as e:
                print(f"[{module_id}] {worker['name']}: 着手の記録に失敗: {e}")
                _notify_briefly(device_id, module_id,
                                ["着手できませんでした", "もう一度タッチしてください"], "error")
                return
            if res.status_code == 409:
                body = res.json() if res.headers.get("content-type", "").startswith("application/json") else {}
                if body.get("error") == "already gathering":
                    print(f"[{module_id}] {worker['name']}: 別の集合待ちに参加中（{body.get('title')}）")
                    _notify_briefly(device_id, module_id,
                                    ["別の集合待ちに参加中です", body.get("equipment") or ""], "error")
                    return
                # 確認している間に、他の人が別の機材で着手した
                print(f"[{module_id}] {worker['name']}: 他の人が着手済み（{task['title']}）")
                _notify_briefly(device_id, module_id,
                                ["他の人が着手済みです", task["title"]], "error")
                return
            # 権限不足(403)など。候補は絞ってあるので通常は起きないが、承認の間に
            # 権限や必要権限が変わることはある。作業中画面を出すとロックした様に見える
            if res.status_code != 200:
                body = res.json() if res.headers.get("content-type", "").startswith("application/json") else {}
                reason = "・".join(body.get("missing_labels") or []) or "着手できませんでした"
                print(f"[{module_id}] {worker['name']}: 着手に失敗 ({res.status_code}) {reason}")
                _notify_briefly(device_id, module_id,
                                ["このタスクには権限が必要です", reason], "error")
                return
            if res.json().get("gathering"):
                # 複数人タスク。残りの人を待つ画面にする（そろうと _gathering_touch が開始する）
                _sync_module_state(device_id, equipment["id"])
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
        try:
            requests.post(
                f"{SELF_URL}/api/equipment/{module_id}/status",
                json={"status": "working", "nfc_tag_id": tag_id}, timeout=HTTP_TIMEOUT,
            ).raise_for_status()
        except requests.RequestException as e:
            print(f"[{module_id}] {worker['name']}: フリー利用の記録に失敗: {e}")
            _notify_briefly(device_id, module_id,
                            ["開始できませんでした", "もう一度タッチしてください"], "error")
            return
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
    # この機材で一番急ぐタスクの順位。これより急ぐものだけを誘導する。見ないと、
    # 至急タスクのある機材へ誘導した先で「高」のタスクへ誘導し返す往復が起きる
    here_best = min((PRIORITY_RANK.get(t.get("priority"), 9) for t in tasks
                     if t.get("equipment_id") in (None, equipment["id"])), default=9)

    for task in others:
        # 優先度が高くないタスクは、この機材でやることが無いときだけ誘導する。
        # そうでないと、ここで作業できるのに毎回よそへ歩かされることになる。
        if task.get("priority") not in GUIDE_PRIORITIES and has_candidates:
            continue
        if has_candidates and PRIORITY_RANK.get(task.get("priority"), 9) >= here_best:
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


def _pick_gathering(conn, worker, equipment):
    """
    この人が加われる、他の機材の集合待ちを1件選ぶ（始まりの古い順）。
    最後の1枠に必要権限が足りないなら、それを補える人だけを呼ぶ。
    戻り値: (集合待ち, いまの人数) / 該当なしなら (None, 0)
    """
    rows = conn.execute("""SELECT id FROM equipment WHERE status = 'gathering' AND id != ?
                           ORDER BY updated_at""", (equipment["id"],)).fetchall()
    for r in rows:
        g = _gathering(conn, r["id"])
        if not g:
            continue
        members = _team_members(conn, g["task_id"])
        need = perms.team_size(g)
        if len(members) >= need or any(m["id"] == worker["id"] for m in members):
            continue
        if len(members) + 1 == need and perms.team_missing(list(members) + [worker], g):
            continue
        return g, len(members)
    return None, 0


def _guide_to_gathering(device_id, module_id, worker, equipment):
    """
    他の機材で人手を待っている集合待ちがあれば、そちらへ移動するか尋ねる。
    戻り値 True = 誘導した。呼び出し側はこの機材で何もせず終了する
    """
    conn = db.get_db()
    try:
        g, have = _pick_gathering(conn, worker, equipment)
        if g and _other_gathering(conn, worker["id"]):
            g = None   # 自分も別の集合待ちに加わっている
    finally:
        conn.close()
    if not g:
        return False
    need = perms.team_size(g)
    answer = request_confirm(
        device_id, f"{g['eq_name']} へ移動しますか？",
        lines=[f"{g['eq_name']} で人手を待っています", g["title"], f"{have}/{need}人 集合待ち"],
        timeout=GUIDE_TIMEOUT,
        equipment_name=equipment.get("name"), worker_name=worker["name"],
    )
    if not answer:
        print(f"[{module_id}] {worker['name']}: 集合待ちへの誘導を辞退（{g['eq_name']} / {g['title']}）")
        return False
    print(f"[{module_id}] {worker['name']} を {g['eq_name']} の集合待ちへ誘導: {g['title']}")
    _notify_briefly(device_id, module_id,
                    [f"{g['eq_name']} へ移動してください", g["title"],
                     "移動先で社員証をタッチ"], "guide", sec=GUIDE_NOTICE_SEC)
    return True


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
        # 完了の記録に失敗しても、下の機材の解放までは必ず進める。ここで例外が
        # 抜けると機材が「作業中」のまま残り、本人以外は誰も使えなくなる
        try:
            res = requests.post(f"{SELF_URL}/api/tasks/{task_id}/complete",
                                json={"worker_id": worker["id"]}, timeout=HTTP_TIMEOUT)
            if res.ok:
                work_log_id = res.json().get("work_log_id")
                task_title = res.json().get("title")
                print(f"[{module_id}] {worker['name']} completed task #{task_id}")
            else:
                print(f"[{module_id}] {worker['name']}: 完了の記録に失敗 ({res.status_code}) task #{task_id}")
        except requests.RequestException as e:
            print(f"[{module_id}] {worker['name']}: 完了の記録に失敗: {e} task #{task_id}")
    else:
        print(f"[{module_id}] {worker['name']} ended free-use")
    # 先に機材を解放する。難易度を答えている間ずっと塞がっていると、
    # 次の人が待たされるうえ、答えなかった場合に解放が漏れる
    try:
        requests.post(
            f"{SELF_URL}/api/equipment/{module_id}/status",
            json={"status": "idle"}, timeout=HTTP_TIMEOUT,
        ).raise_for_status()
    except requests.RequestException as e:
        print(f"[{module_id}] 機材の解放に失敗: {e}")
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


@app.route("/demo-mode", methods=["POST"])
def demo_mode():
    """
    サイドバーのデモモード。on=1 で開始、on=0 で解除する（中身は demo_mode.py）。

    開始するとデモ用DB（宮田・南川／3Dプリンタ・旋盤／至急と通常のタスク）を作り直して
    そちらへ切り替える。本番のDBには触らないので、解除すればそのまま元に戻る。
    """
    turn_on = request.form.get("on") == "1"
    if turn_on == demo.is_active():
        flash("すでにデモモードです" if turn_on else "デモモードではありません", "error")
        return _back_to("dashboard")
    try:
        if turn_on:
            # 以前のデモモードが本番DBに足したタスクと実績が残っていれば、ここで片付ける
            conn = db.get_db()
            try:
                legacy = demo.remove_legacy(conn)
                conn.commit()
            finally:
                conn.close()
            if legacy:
                print(f"[demo] 以前のデモモードのタスク {legacy} 件を本番DBから削除しました")
            added = demo.start()
        else:
            demo.stop()
    except Exception as e:
        print(f"[demo] デモモードを切り替えられませんでした: {e}")
        flash(f"デモモードを切り替えられませんでした（本番のDBは元のままです）: {e}", "error")
        return _back_to("dashboard")

    _after_db_switch()
    if turn_on:
        print(f"[demo] デモモードを開始: {db.DB_PATH.name} に切り替えました")
        flash(f"デモモードを開始しました（作業者 {added['workers']} 人・機材 {added['equipment']} 台・"
              f"タスク {added['tasks']} 件）", "ok")
    else:
        print(f"[demo] デモモードを解除: {db.DB_PATH.name} に戻しました")
        flash("デモモードを解除しました（デモ前の状態に戻しました）", "ok")
    return _back_to("dashboard")


@app.route("/demo-mode/reset", methods=["POST"])
def demo_mode_reset():
    """デモモードの「初期化」。デモ中に行った作業を消して、開始した直後の状態に戻す"""
    if not demo.is_active():
        flash("デモモードではありません", "error")
        return _back_to("dashboard")
    try:
        demo.reset()
    except Exception as e:
        print(f"[demo] デモモードを初期化できませんでした: {e}")
        flash(f"デモモードを初期化できませんでした: {e}", "error")
        return _back_to("dashboard")
    _after_db_switch()
    print("[demo] デモモードを初期化しました")
    flash("デモモードを初期化しました（開始した直後の状態に戻しました）", "ok")
    return _back_to("dashboard")


def _after_db_switch():
    """
    使うDBを切り替えた後に、メモリに持っている写しとモジュールの画面を新しいDBに合わせる。
    未登録タグ・モジュールの一覧は読み直し、繋がっているモジュールには新しいDBでの
    機材名と状態を送り直す（送らないと、前のDBの機材名や作業中の表示が残る）
    """
    with _pending_lock:
        _pending_tags.clear()
        _pending_tag_touched.clear()
        _pending_modules.clear()
    _load_pending()
    ai_stub.invalidate_cache()
    conn = db.get_db()
    rows = conn.execute("SELECT * FROM equipment").fetchall()
    conn.close()
    _resync_released(rows)


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
    return _render("test.html", equipment=equipment_rows, workers=worker_rows,
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
    {"button": "ok", "hold": true} は決定の長押し（問い合わせが無いときだけ。集合待ちの取り消し）。
    """
    data = request.get_json(silent=True) or {}
    button = data.get("button")
    with _virtual_lock:
        vm = _virtual_modules.get(device_id)
        if vm is None:
            return jsonify({"error": "not running"}), 404
        choice = vm["choice"]
        # 実機も、問い合わせ中の長押しは送らない（押した時点で答えになる）
        long_press = bool(data.get("hold")) and button == "ok" and not choice
        if long_press:
            _virtual_log(vm, "up", "決定の長押し")
    if long_press:
        _handle_data(device_id, {"device_id": device_id, "event": "long_press", "button": "ok"})
        return jsonify({"ok": True})
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


# ------------------------------------------------ 定期的な見回り
BACKGROUND_INTERVAL_SEC = 60   # 見回りの間隔。どちらも分単位の精度で足りる

# 終了のタッチ忘れを知らせ済みの使用: {(equipment_id, 使い始めた時刻)}。
# 同じ使用について画面を何度も送り直さない（表示中の問い合わせを消してしまう）
_forgot_notified: set = set()


def check_forgotten_sessions():
    """
    見込みを大きく超えた使用中の機材を探し、モジュールの画面で終了を促す。
    操作中のモジュールには送らない（次の見回りで送る）。知らせた機材の id を返す。
    """
    conn = db.get_db()
    try:
        overdue = _overdue_sessions(conn)
        rows = {r["id"]: r for r in conn.execute(
            "SELECT id, name, hostname, module_id, online FROM equipment WHERE status = 'working'")}
    finally:
        conn.close()
    current = {(eq_id, info["since"]) for eq_id, info in overdue.items()}
    # 終わった使用は忘れる（同じ機材の次の使用を、また知らせられるように）
    _forgot_notified.intersection_update(current)
    notified = []
    for eq_id, info in overdue.items():
        key = (eq_id, info["since"])
        eq = rows.get(eq_id)
        if key in _forgot_notified or eq is None:
            continue
        device_id = _device_id_of(eq)
        if not device_id or not (eq["online"] or device_id in _virtual_modules):
            continue
        with _touch_busy_lock:
            if device_id in _touch_busy:
                continue
        _forgot_notified.add(key)
        print(f"[{device_id}] {eq['name']}: {info['elapsed_sec'] // 60}分 使用中のまま。"
              f"終了タッチ忘れの可能性を画面に出しました")
        _sync_module_state(device_id, eq_id)
        notified.append(eq_id)
    return notified


def start_background_jobs():
    """定期タスクの生成と、終了タッチ忘れの見回り。起動直後にも1回回す"""
    def run():
        while True:
            try:
                run_recurring()
                check_forgotten_sessions()
                cancel_stale_gatherings()
            except Exception as e:   # 見回りを止めない
                print(f"[background] 見回りで例外: {e}")
            time.sleep(BACKGROUND_INTERVAL_SEC)

    threading.Thread(target=run, daemon=True).start()


if __name__ == "__main__":
    db.init_db()
    if demo.resume():
        print(f"[demo] {db.DB_PATH.name} が残っているので、デモモードのまま起動します")
        db.init_db(seed=False)
    _load_pending()
    print(f"[app] デバッグモード: {'ON（開発用。LANに公開しないこと）' if DEBUG else 'OFF'}")
    # 音声タスク登録の後段(E-2)。無くても既定値で登録はできるので落とさず警告だけ
    _intent_ok, _intent_why = intent.available()
    print(f"[voice] 意図分析: {'有効' if _intent_ok else '無効'} - {_intent_why}")
    # debug=True のリローダーは子プロセスで再実行されるため、実際に配信する
    # プロセス(WERKZEUG_RUN_MAIN)でのみ MQTT を起動して二重接続を防ぐ。
    if not DEBUG or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_discovery_responder()
        start_mqtt_bridge()
        start_stt_warmup()
        start_background_jobs()
    app.run(debug=DEBUG, host="0.0.0.0", port=5000, threaded=True)
