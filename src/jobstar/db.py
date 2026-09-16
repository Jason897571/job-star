"""SQLite 数据层。所有表在 init_db 里一次建好。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "jobstar.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY,
    platform        TEXT NOT NULL DEFAULT 'boss',
    job_id          TEXT NOT NULL,
    title           TEXT NOT NULL,
    company         TEXT NOT NULL,
    raw_jd          TEXT NOT NULL DEFAULT '',
    city            TEXT,
    district        TEXT,          -- 列表页就能拿到：滨江区
    business_area   TEXT,          -- 列表页就能拿到：长河
    address         TEXT,          -- 详情页才有：杭州余杭区乐富海邦园12座201
    lng             REAL,          -- 详情页才有，GCJ-02（高德坐标系）
    lat             REAL,
    salary_raw      TEXT,
    hr_name         TEXT,
    url             TEXT,
    collected_at    TEXT NOT NULL DEFAULT (datetime('now')),
    detail_fetched  INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'new',
    UNIQUE (platform, job_id)
);

CREATE TABLE IF NOT EXISTS requirements (
    job_id        TEXT PRIMARY KEY,
    city          TEXT,
    degree        TEXT,
    years_min     INTEGER,
    years_max     INTEGER,
    salary_min    INTEGER,
    salary_max    INTEGER,
    skills        TEXT NOT NULL DEFAULT '[]',
    industry      TEXT,
    company_size  TEXT,
    category      TEXT
);

CREATE TABLE IF NOT EXISTS gate_results (
    job_id        TEXT PRIMARY KEY,
    passed        INTEGER NOT NULL,
    reject_reason TEXT,
    checked_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS scores (
    job_id         TEXT PRIMARY KEY,
    total          REAL,
    dimensions     TEXT NOT NULL DEFAULT '[]',
    scored_at      TEXT NOT NULL DEFAULT (datetime('now')),
    scorer_version TEXT NOT NULL,
    error          TEXT
);

CREATE TABLE IF NOT EXISTS actions (
    id          INTEGER PRIMARY KEY,
    type        TEXT NOT NULL,
    job_id      TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at  TEXT,
    sent_at     TEXT,
    error       TEXT,
    UNIQUE (type, job_id)
);

CREATE TABLE IF NOT EXISTS applications (
    id             INTEGER PRIMARY KEY,
    job_id         TEXT NOT NULL,
    action_id      INTEGER NOT NULL,
    hr_name        TEXT,
    greeting_text  TEXT NOT NULL,
    score_snapshot TEXT NOT NULL DEFAULT '{}',
    sent_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS labels (
    job_id       TEXT PRIMARY KEY,
    would_apply  INTEGER NOT NULL,
    labeled_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status);
CREATE INDEX IF NOT EXISTS idx_actions_status ON actions (status);
"""


def get_conn(path: Path | None = None) -> sqlite3.Connection:
    """打开数据库连接。行以 sqlite3.Row 返回，支持按列名取值。

    `check_same_thread=False`：Web 面板（Task 12）的同步路由函数由
    FastAPI/Starlette 派发到 anyio 的工作线程池执行，同一个连接对象可能
    先在一个线程里创建、再在另一个线程里被路由函数使用——sqlite3 默认的
    同线程检查会把这种情况直接判成错误。这里没有真正的多线程并发访问
    同一个连接（这套系统里同一个连接对象在任意时刻只被一个调用方使用），
    只是「创建」和「使用」这两步不保证发生在同一个线程，关掉这项检查是
    安全的。CLI/执行器仍然是单线程使用，不受影响。
    """
    db_path = Path(path) if path is not None else DEFAULT_DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# 后加的列。SCHEMA 里的 CREATE TABLE IF NOT EXISTS 对**已经存在**的表什么都
# 不做，所以新列必须单独补——否则老库升级上来会在第一次查询时炸。
# (表, 列, 类型)，顺序即添加顺序。
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("jobs", "district", "TEXT"),
    ("jobs", "business_area", "TEXT"),
    ("jobs", "address", "TEXT"),
    ("jobs", "lng", "REAL"),
    ("jobs", "lat", "REAL"),
)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """给既有库补上后加的列。已经有的跳过，所以可以反复调用。

    用 PRAGMA 查实际列名而不是 try/except ALTER：后者在别的原因失败时会被
    一起吞掉，而这里失败意味着库结构和代码对不上，必须炸出来。
    """
    for table, column, decl in _ADDED_COLUMNS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _add_missing_columns(conn)
    conn.commit()
