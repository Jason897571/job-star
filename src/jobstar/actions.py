"""待确认队列。状态机是「绝不自动发送」的结构性保证。

pending  → approved → sent
pending  → skipped
approved → failed
approved → skipped
approved → approved（幂等重新确认，允许编辑后再次确认）
skipped  → approved
failed   → approved
failed   → skipped（彻底放弃，例如岗位已下架）

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
    APPROVED: frozenset({PENDING, SKIPPED, FAILED, APPROVED}),
    SKIPPED: frozenset({PENDING, APPROVED, FAILED}),
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
    """仅用于生成可读的报错信息（动作不存在 / 当前状态不允许）。

    不参与并发决策——真正的强制校验在 `_atomic_transition` 的
    `UPDATE ... WHERE status IN (...)` 里，与写入是同一条语句。
    """
    current = _current_status(conn, action_id)
    if current not in _ALLOWED_FROM.get(target, frozenset()):
        raise InvalidTransition(f"动作 {action_id} 不能从 {current} 变成 {target}")


def _atomic_transition(
    conn: sqlite3.Connection,
    action_id: int,
    target: str,
    set_sql: str,
    set_params: tuple,
) -> None:
    """把「来源状态是否允许」折进 UPDATE 的 WHERE 子句，使校验与写入成为
    同一条语句，避免 SELECT 和 UPDATE 分属两个隐式事务时的竞态：

        执行者 _require(SENT) 通过（此刻是 approved）
            → 人工在面板点「跳过」并提交（approved → skipped）
            → 执行者的 UPDATE 仍然落地 → 最终状态变成 sent

    `_ALLOWED_FROM[target]` 是允许来源状态的唯一来源，占位符从它生成，
    而不是在每个写入点各自硬编码一份。
    """
    allowed = _ALLOWED_FROM.get(target, frozenset())
    placeholders = ",".join("?" * len(allowed)) if allowed else "NULL"
    sql = (
        f"UPDATE actions SET {set_sql} "
        f"WHERE id=? AND status IN ({placeholders})"
    )
    cur = conn.execute(sql, (*set_params, action_id, *allowed))
    if cur.rowcount == 0:
        # 没有任何行受影响：要么动作不存在，要么当前状态不允许这次迁移。
        # get_conn 让 sqlite3 保持 isolation_level=''，上面的 UPDATE 在
        # WHERE 求值之前就已经隐式 BEGIN；这里不回滚就直接 raise，会让这次
        # 什么都没改动的事务一直挂在连接上，占着 RESERVED 锁，卡住其他连接
        # 的写入。拒绝是本模块设计要处理的正常结果（竞态守卫按预期触发、或
        # 人工在面板上对一个已经是终态的行又点了一次），所以必须先释放锁再
        # 抛错。用 rollback 而不是 commit：这条 UPDATE 没有改动任何行，且
        # 本模块每个写入函数都是「一次调用即提交」，不会有调用方的工作在途
        # 中被误回滚。
        conn.rollback()
        # _require 重新读一次状态（只读，不会重新开事务），只为了给出可读
        # 的报错信息。
        _require(conn, action_id, target)
        # 并发下另一个连接可能又把状态改回允许值：rowcount==0 时上面的
        # UPDATE 已经确认此刻不允许，但 _require 这次重新读取发生在之后，
        # 可能读到已经被改回允许来源状态的行，因而没有抛出，这里兜底。
        raise InvalidTransition(f"动作 {action_id} 无法变成 {target}")
    conn.commit()


def enqueue(
    conn: sqlite3.Connection, *, type: str, job_id: str, payload: dict
) -> int:
    """入队一个待确认动作。同一 (type, job_id) 重复入队返回已有行的 id（payload 不更新）。"""
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
    if payload_override is not None:
        _atomic_transition(
            conn,
            action_id,
            APPROVED,
            "status=?, decided_at=datetime('now'), payload=?, error=NULL",
            (APPROVED, json.dumps(payload_override, ensure_ascii=False)),
        )
    else:
        _atomic_transition(
            conn,
            action_id,
            APPROVED,
            "status=?, decided_at=datetime('now'), error=NULL",
            (APPROVED,),
        )


def skip(conn: sqlite3.Connection, action_id: int) -> None:
    _atomic_transition(
        conn,
        action_id,
        SKIPPED,
        "status=?, decided_at=datetime('now')",
        (SKIPPED,),
    )


def mark_sent(conn: sqlite3.Connection, action_id: int) -> None:
    _atomic_transition(
        conn,
        action_id,
        SENT,
        "status=?, sent_at=datetime('now')",
        (SENT,),
    )


def mark_failed(conn: sqlite3.Connection, action_id: int, error: str) -> None:
    """设计文档 §4.8：失败不自动重试，写原因等人工决定。"""
    _atomic_transition(
        conn,
        action_id,
        FAILED,
        "status=?, error=?",
        (FAILED, error),
    )


def list_by_status(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM actions WHERE status = ? ORDER BY created_at, id", (status,)
    ).fetchall()


def sent_today(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM actions "
        "WHERE status = ? AND date(sent_at, 'localtime') = date('now', 'localtime')",
        (SENT,),
    ).fetchone()
    return int(row["n"])


def remaining_quota(conn: sqlite3.Connection) -> int:
    from jobstar.config import get_setting

    limit = int(get_setting(conn, "daily_greeting_limit"))
    return max(0, limit - sent_today(conn))
