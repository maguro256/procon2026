"""
db.py - SQLite データベース層
標準ライブラリの sqlite3 のみ使用。追加インストール不要。
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "gemmba.db"

SCHEMA = """
-- 作業者: スライドの方針通り、事前登録は「勤続年数」1項目のみ + 名前
CREATE TABLE IF NOT EXISTS workers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    years_of_service REAL NOT NULL DEFAULT 0,   -- 勤続年数（AIの初期文脈ベクトルに使用）
    role TEXT NOT NULL DEFAULT 'member',        -- 役職。permissions.ROLES のコード
    permissions TEXT NOT NULL DEFAULT '',       -- 個別付与の保有権限。カンマ区切り
    nfc_tag_id TEXT UNIQUE,                     -- 腕輪のICチップID（後で紐付け可能）
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 機材: 各機材の横に設置するモジュールと1対1で対応
CREATE TABLE IF NOT EXISTS equipment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    -- 機材コード。**機材そのもの**の識別子で、APIの宛先になる（/api/equipment/<ここ>/…）。
    -- モジュール側の識別子は下の hostname。名前が紛らわしいので画面では「機材コード」と呼ぶ
    module_id TEXT UNIQUE,
    status TEXT NOT NULL DEFAULT 'idle',        -- idle / working / stopped / maintenance
    current_worker_id INTEGER REFERENCES workers(id),
    current_task_id INTEGER REFERENCES tasks(id),
    ip TEXT,
    -- モジュールID。ラズパイの GEMMBA_DEVICE_ID がそのまま入る。IPが変わっても不変
    hostname TEXT,
    last_seen TEXT,                              -- 最終通信時刻
    online INTEGER DEFAULT 0,                    -- 1=接続中。MQTTのLWT/ハートビートで自動更新
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- タスク
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    difficulty INTEGER NOT NULL DEFAULT 3,      -- 難易度 1-5（AIの文脈ベクトルに使用）
    priority TEXT NOT NULL DEFAULT 'normal',    -- urgent / high / normal / low
    required_permissions TEXT NOT NULL DEFAULT '',  -- 必要権限。カンマ区切りで全部必要。空なら誰でも可
    quantity INTEGER DEFAULT 1,
    deadline TEXT,                              -- 期限 (YYYY-MM-DD)
    status TEXT NOT NULL DEFAULT 'todo',        -- todo / assigned / in_progress / done
    assigned_worker_id INTEGER REFERENCES workers(id),
    equipment_id INTEGER REFERENCES equipment(id),
    started_at TEXT,
    completed_at TEXT,
    -- 直近の AI割当（WariAthena）の根拠。JSON。手で担当者を変えたら NULL に戻す
    ai_note TEXT,
    -- 1 = 管理者が担当者を指定した（D-3）。AI割当では担当者を変えず、本人以外には
    -- 提示しない。0 なら assigned_worker_id は AI割当などの「割当結果」
    designated INTEGER NOT NULL DEFAULT 0,
    -- 定期タスク（recurring_tasks）から自動で作ったタスクなら、その元の id。
    -- 外部キーにしないのは、元の設定を消しても作ったタスクは残すため
    recurring_id INTEGER,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 定期タスクの設定。recurring.py が予定日になると tasks に1件ずつ作る
CREATE TABLE IF NOT EXISTS recurring_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    difficulty INTEGER NOT NULL DEFAULT 3,
    priority TEXT NOT NULL DEFAULT 'normal',
    required_permissions TEXT NOT NULL DEFAULT '',
    quantity INTEGER DEFAULT 1,
    equipment_id INTEGER REFERENCES equipment(id),
    worker_id INTEGER REFERENCES workers(id),   -- 指定する担当者。空なら誰でも
    frequency TEXT NOT NULL DEFAULT 'weekly',   -- daily / weekly / monthly
    weekdays TEXT NOT NULL DEFAULT '',          -- weekly のときの曜日。0=月〜6=日のカンマ区切り
    month_day INTEGER,                          -- monthly のときの日。月末より後なら月末
    deadline_days INTEGER NOT NULL DEFAULT 0,   -- 期限 = 予定日 + この日数
    active INTEGER NOT NULL DEFAULT 1,
    last_run TEXT,                              -- 最後に処理した予定日 (YYYY-MM-DD)
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 未登録のICタグ・モジュール。管理画面で登録・紐付けされるまで残す。
-- メモリだけに持っていると app.py の再起動で消え、タッチし直すまで気づけない（G-3）
CREATE TABLE IF NOT EXISTS pending_tags (
    tag_id TEXT PRIMARY KEY,
    module_id TEXT,                             -- タッチされた機材の機材コード
    seen_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS pending_modules (
    device_id TEXT PRIMARY KEY,
    ip TEXT,
    seen_at TEXT
);

-- 作業実績ログ: NFCタッチで収集する所要時間データ（WariAthena の学習用）
CREATE TABLE IF NOT EXISTS work_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- NOT NULL を付けないこと。タスク・作業者・機材が削除されても実績は残す設計で、
    -- app.py の AI_LOG_SQL はそれを前提に LEFT JOIN している。NOT NULL だと
    -- 外部キー制約で「実績のある作業者は削除できない」状態になる（実際にそうなっていた）
    task_id INTEGER REFERENCES tasks(id),
    worker_id INTEGER REFERENCES workers(id),
    equipment_id INTEGER REFERENCES equipment(id),
    started_at TEXT,
    completed_at TEXT,
    duration_sec INTEGER,                       -- 実所要時間（報酬 r の計算に使用）
    -- 完了直後にモジュールの画面で本人が答えた体感難易度: easy / normal / hard。
    -- 答えなかった（時間切れ）場合は NULL。tasks.difficulty とは別物で、
    -- こちらは「その人がどう感じたか」。AIの文脈ベクトルには入れていない
    -- （入れると過去ログの再生結果が変わる。TODO.md の D-1 / D-2 を参照）。
    felt_difficulty TEXT
);
"""


# CREATE TABLE IF NOT EXISTS は既存テーブルを更新しないため、SCHEMA にカラムを
# 足しても運用中の gemmba.db には反映されない。追加したカラムはここにも1行書く。
MIGRATIONS = {
    "workers": {
        # D-2: 役職・保有権限。既存DBにも入るよう空文字を既定にしてある
        "role": "TEXT NOT NULL DEFAULT 'member'",
        "permissions": "TEXT NOT NULL DEFAULT ''",
    },
    "tasks": {
        "required_permissions": "TEXT NOT NULL DEFAULT ''",
        "ai_note": "TEXT",
        "designated": "INTEGER NOT NULL DEFAULT 0",
        "recurring_id": "INTEGER",
    },
    "work_logs": {
        # 完了後の難易度フィードバック。既存の実績は答えていないので NULL のまま
        "felt_difficulty": "TEXT",
    },
    "equipment": {
        "ip": "TEXT",
        "hostname": "TEXT",
        "last_seen": "TEXT",
        "online": "INTEGER DEFAULT 0",
    },
}


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 複数プロセス/複数スレッドが同時に書き込むと sqlite3 は即座に
    # "database is locked" を投げる。Flaskのリクエストスレッドが複数同時に
    # 書き込む場合や、simulate_shift.py がサーバーと並行してDBに触る場合に
    # 起きるので、短時間はリトライさせて凌ぐ。
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def migrate(conn):
    """SCHEMA との差分カラムを既存テーブルへ追加する（冪等）"""
    for table, columns in MIGRATIONS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, decl in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                print(f"[db] migrate: {table}.{column} を追加しました")
    _relax_work_logs(conn)


def _relax_work_logs(conn):
    """
    work_logs.task_id / worker_id の NOT NULL を外す（冪等）。

    付いたままだと、実績のある作業者・タスク・機材を削除しようとした時点で
    外部キー制約に弾かれて 500 になる。実績は消さずに残す設計なので、
    参照先が消えたら NULL にできる必要がある。

    SQLite は ALTER COLUMN で NOT NULL を外せないので、テーブルを作り直す。
    1つのトランザクションで入れ替えるため、途中で落ちても中途半端にならない。
    """
    info = conn.execute("PRAGMA table_info(work_logs)").fetchall()
    if not info:
        return
    # row = (cid, name, type, notnull, dflt_value, pk)
    if not any(r[1] in ("task_id", "worker_id") and r[3] for r in info):
        return

    columns = [r[1] for r in info]
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(f"""
        BEGIN;
        CREATE TABLE work_logs__new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER REFERENCES tasks(id),
            worker_id INTEGER REFERENCES workers(id),
            equipment_id INTEGER REFERENCES equipment(id),
            started_at TEXT,
            completed_at TEXT,
            duration_sec INTEGER,
            felt_difficulty TEXT
        );
        INSERT INTO work_logs__new ({', '.join(columns)})
            SELECT {', '.join(columns)} FROM work_logs;
        DROP TABLE work_logs;
        ALTER TABLE work_logs__new RENAME TO work_logs;
        COMMIT;
    """)
    conn.execute("PRAGMA foreign_keys = ON")
    n = conn.execute("SELECT COUNT(*) FROM work_logs").fetchone()[0]
    print(f"[db] migrate: work_logs の task_id / worker_id を NULL 可にしました（{n}件そのまま）")


# 一覧画面の相関サブクエリが毎行テーブルを全走査しないための索引。
# migrate() の _relax_work_logs は work_logs を作り直す（索引も消える）ので、
# SCHEMA ではなく migrate の後に張る。
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_work_logs_task ON work_logs(task_id, id);
CREATE INDEX IF NOT EXISTS idx_tasks_worker ON tasks(assigned_worker_id, status);
"""


def backup_to(dst):
    """
    DBを dst へ丸ごと写す。WAL では本体ファイルに未反映の更新が -wal 側に
    残っているので、ファイルのコピー（shutil.copy2）だと最新の内容が抜ける。
    """
    src = get_db()
    out = sqlite3.connect(dst)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()


def init_db(seed: bool = True):
    # サンプル投入は「DBファイルが存在しなかった初回」に限る。テーブルが空か
    # どうかで判定すると、意図的に全削除した後の起動でサンプルが復活してしまう。
    first_run = not DB_PATH.exists()
    conn = get_db()
    # WAL にすると読み込み（管理画面のポーリング）と書き込み（MQTTの死活・タッチ）が
    # 互いを待たなくなる。設定はDBファイルに残るので、一度当てれば以後ずっと有効
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.executescript(INDEXES)
    # 初回のみサンプルデータを投入（動作確認用。不要なら seed=False で呼ぶ）
    if seed and first_run:
        conn.executescript("""
        INSERT INTO workers (name, years_of_service, role, permissions, nfc_tag_id) VALUES
            ('田中 太郎', 12.0, 'supervisor', 'forklift,crane', 'TAG-0001'),
            ('佐藤 花子', 3.5,  'leader',     'welding',        'TAG-0002'),
            ('鈴木 一郎', 0.5,  'member',     '',               NULL);
        INSERT INTO equipment (name, module_id, status,ip,hostname) VALUES
            ('レーザー加工機 #1', 'MOD-A-01', 'working',NULL,NULL),
            ('旋盤 #2',          'MOD-A-02', 'idle','192.168.137.212','pi01'),
            ('プレス機 #1',      'MOD-B-01', 'stopped',NULL,NULL);
        INSERT INTO tasks (title, difficulty, priority, required_permissions, quantity, deadline, status) VALUES
            ('製品A 組立', 3, 'urgent', '',      30, date('now', '+1 day'), 'in_progress'),
            ('旋盤メンテ #2', 2, 'normal', '',      1, date('now', '+3 day'), 'todo'),
            ('製品B 加工', 4, 'high',   'welding', 20, date('now', '+2 day'), 'todo');
        UPDATE equipment SET current_worker_id = 1, current_task_id = 1 WHERE id = 1;
        UPDATE tasks SET assigned_worker_id = 1, equipment_id = 1 WHERE id = 1;
        """)
    conn.commit()
    conn.close()
