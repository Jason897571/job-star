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


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
