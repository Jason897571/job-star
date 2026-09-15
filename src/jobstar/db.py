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
    """打开数据库连接。行以 sqlite3.Row 返回，支持按列名取值。"""
    db_path = Path(path) if path is not None else DEFAULT_DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
