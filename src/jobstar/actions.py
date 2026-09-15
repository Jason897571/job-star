"""待确认队列。状态机是「绝不自动发送」的结构性保证。

pending → approved → sent
pending → skipped
approved → failed

只有 approved 能变 sent，而 approved 只由面板上的人工点击写入。
"""

from __future__ import annotations

import json
import sqlite3

PENDING = "pending"
APPROVED = "approved"
SKIPPED = "skipped"
SENT = "sent"
FAILED = "failed"

# 目标状态 -> 允许的来源状态
_ALLOWED_FROM: dict[str, frozenset[str]] = {
    APPROVED: frozenset({PENDING, SKIPPED, FAILED}),
    SKIPPED: frozenset({PENDING, APPROVED}),
    SENT: frozenset({APPROVED}),
    FAILED: frozenset({APPROVED}),
}


class InvalidTransition(RuntimeError):
    """状态机不允许的迁移，或动作不存在。"""


def _current_status(conn: sqlite3.Connection, action_id: int) -> str:
    row = conn.execute(
        "SELECT status FROM actions WHERE id = ?", (action_id,)
    ).fetchone()
    if row is None:
        raise InvalidTransition(f"动作 {action_id} 不存在")
    return row["status"]


def _require(conn: sqlite3.Connection, action_id: int, target: str) -> None:
    current = _current_status(conn, action_id)
    if current not in _ALLOWED_FROM[target]:
        raise InvalidTransition(f"动作 {action_id} 不能从 {current} 变成 {target}")


def enqueue(
    conn: sqlite3.Connection, *, type: str, job_id: str, payload: dict
) -> int:
    """入队一个待确认动作。同一 (type, job_id) 重复入队返回已有行的 id。"""
    blob = json.dumps(payload, ensure_ascii=False)
    conn.execute(
        "INSERT INTO actions (type, job_id, payload) VALUES (?, ?, ?) "
        "ON CONFLICT(type, job_id) DO NOTHING",
        (type, job_id, blob),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM actions WHERE type = ? AND job_id = ?", (type, job_id)
    ).fetchone()
    return int(row["id"])


def approve(
    conn: sqlite3.Connection, action_id: int, payload_override: dict | None = None
) -> None:
    _require(conn, action_id, APPROVED)
    if payload_override is not None:
        conn.execute(
            "UPDATE actions SET status=?, decided_at=datetime('now'), "
            "payload=?, error=NULL WHERE id=?",
            (APPROVED, json.dumps(payload_override, ensure_ascii=False), action_id),
        )
    else:
        conn.execute(
            "UPDATE actions SET status=?, decided_at=datetime('now'), error=NULL "
            "WHERE id=?",
            (APPROVED, action_id),
        )
    conn.commit()


def skip(conn: sqlite3.Connection, action_id: int) -> None:
    _require(conn, action_id, SKIPPED)
    conn.execute(
        "UPDATE actions SET status=?, decided_at=datetime('now') WHERE id=?",
        (SKIPPED, action_id),
    )
    conn.commit()


def mark_sent(conn: sqlite3.Connection, action_id: int) -> None:
    _require(conn, action_id, SENT)
    conn.execute(
        "UPDATE actions SET status=?, sent_at=datetime('now') WHERE id=?",
        (SENT, action_id),
    )
    conn.commit()


def mark_failed(conn: sqlite3.Connection, action_id: int, error: str) -> None:
    """设计文档 §4.8：失败不自动重试，写原因等人工决定。"""
    _require(conn, action_id, FAILED)
    conn.execute(
        "UPDATE actions SET status=?, error=? WHERE id=?", (FAILED, error, action_id)
    )
    conn.commit()


def list_by_status(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM actions WHERE status = ? ORDER BY created_at", (status,)
    ).fetchall()


def sent_today(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM actions "
        "WHERE status = ? AND date(sent_at) = date('now', 'localtime')",
        (SENT,),
    ).fetchone()
    return int(row["n"])


def remaining_quota(conn: sqlite3.Connection) -> int:
    from jobstar.config import get_setting

    limit = int(get_setting(conn, "daily_greeting_limit"))
    return max(0, limit - sent_today(conn))
