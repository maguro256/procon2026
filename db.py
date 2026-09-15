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
    module_id TEXT UNIQUE,                      -- モジュール(ESP32)の識別子
    status TEXT NOT NULL DEFAULT 'idle',        -- idle / working / stopped / maintenance
    current_worker_id INTEGER REFERENCES workers(id),
    current_task_id INTEGER REFERENCES tasks(id),
    ip TEXT,
    hostname TEXT,                               -- モジュール(ラズパイ)の DEVICE_ID。IPが変わっても不変
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
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 作業実績ログ: NFCタッチで収集する所要時間データ（WariAthena の学習用）
CREATE TABLE IF NOT EXISTS work_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    worker_id INTEGER NOT NULL REFERENCES workers(id),
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


def init_db(seed: bool = True):
    # サンプル投入は「DBファイルが存在しなかった初回」に限る。テーブルが空か
    # どうかで判定すると、意図的に全削除した後の起動でサンプルが復活してしまう。
    first_run = not DB_PATH.exists()
    conn = get_db()
    conn.executescript(SCHEMA)
    migrate(conn)
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
