"""待确认队列。状态机是「绝不自动发送」的结构性保证。

pending  → approved → sending → sent
pending  → skipped
approved → skipped
approved → failed
approved → approved（幂等重新确认，允许编辑后再次确认）
sending  → failed
skipped  → approved
failed   → approved
failed   → skipped（彻底放弃，例如岗位已下架）

只有 approved 能变 sending，只有 sending 能变 sent——执行器发送前必须先把
这一行原子地「认领」成 sending（CAS：同一条 UPDATE ... WHERE status='approved'
只可能有 0 或 1 行受影响）。这样人工在发送前一刻点「跳过」永远赢：认领失败
时执行器直接跳过这一行，绝不会把消息发给一个已经被人回绝的人；两个执行器
抢同一行时也是恰好一个赢。

sending 是「消息可能已经发出、但还没确认」的诚实中间态，不会被自动回收：
进程被打断（Ctrl-C、宕机）留在 sending 的行，下一轮执行器读不到它（它已经
不是 approved），因此不会被重发；面板把它留给人工核实。

sending 现在承载两种含义：一种是上面说的「正在发送中」（进程被打断，行
就停在这里）；另一种是「已经跑完，但结果不确定，等人工核实」——执行器
点击发送按钮之后才失败（例如我方消息气泡渲染慢/选择器猜错/关闭标签页报
错），消息很可能已经真的发出去了。这种情况不会被 mark_failed：那样会把
配额退回去（sent_today 不再计它），也允许人工在「消息可能已发出」的情况
下重新批准，酿成给同一个真人重复发送。这类行改由 note_uncertain 写一条
人工核实提示到 error 列，状态原地留在 sending——反正 sending 本来就不允
许被 approve/skip 当来源状态，行天然就不可再批准。
"""

from __future__ import annotations

import json
import sqlite3

PENDING = "pending"
APPROVED = "approved"
SKIPPED = "skipped"
SENDING = "sending"
SENT = "sent"
FAILED = "failed"

# 目标状态 -> 允许的来源状态
_ALLOWED_FROM: dict[str, frozenset[str]] = {
    APPROVED: frozenset({PENDING, SKIPPED, FAILED, APPROVED}),
    SKIPPED: frozenset({PENDING, APPROVED, FAILED}),
    SENDING: frozenset({APPROVED}),
    SENT: frozenset({SENDING}),
    FAILED: frozenset({APPROVED, SENDING}),
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


def mark_sending(conn: sqlite3.Connection, action_id: int) -> None:
    """执行器发送前的原子认领：approved → sending。

    与 mark_sent/mark_failed 共用 `_atomic_transition`：谁的 UPDATE 先落地
    谁就赢得这一行，另一个只拿到 InvalidTransition——两个执行器抢同一行，
    或执行器与人工的「跳过」抢同一行，都不会两边都把消息发出去。

    这里顺带盖 sent_at（而不是等 mark_sent 才盖）：sent_today/remaining_quota
    把 sending 也计入今日配额（见 sent_today 的注释），复用同一个 sent_at
    列，而不是新开一列只为了这一个状态。mark_sent 成功后会再盖一次，语义
    上是「确认发送时间」，同一天内不影响配额计数。
    """
    _atomic_transition(
        conn,
        action_id,
        SENDING,
        "status=?, sent_at=datetime('now')",
        (SENDING,),
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


def note_uncertain(conn: sqlite3.Connection, action_id: int, error: str) -> None:
    """记录一次「发送结果不确定」的说明，不做状态迁移——行留在 sending。

    区别于 mark_failed：这里不经过 `_atomic_transition`（它要求给一个目标
    状态；这里没有目标状态，只是把 error 列换成人工核实提示）。但写入仍然
    按状态范围（Minor 4）：`WHERE id=? AND status=?`（SENDING）而不是裸
    `WHERE id=?`——今天唯一的调用点（run_queue 里处理 `SendUncertain`）传
    进来的 id 一定是自己刚 `mark_sending` 认领的，本来就安全；但这个写入函
    数本身没有任何校验，未来一旦有新调用点传错一个不在 sending 的行 id，
    会静默覆盖那一行的 error 而不报错。`rowcount == 0` 时回滚并抛
    `InvalidTransition`，和模块里其他写入函数遇到不允许的写入时的处理方式
    保持一致。
    """
    cur = conn.execute(
        "UPDATE actions SET error=? WHERE id=? AND status=?",
        (error, action_id, SENDING),
    )
    if cur.rowcount == 0:
        conn.rollback()
        raise InvalidTransition(f"动作 {action_id} 不在 {SENDING} 状态，无法记录不确定结果")
    conn.commit()


def list_by_status(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM actions WHERE status = ? ORDER BY created_at, id", (status,)
    ).fetchall()


def sent_today(conn: sqlite3.Connection) -> int:
    """今天已发送、或可能已发送（sending：进程被打断时的诚实中间态）的数量。

    sending 必须计入，否则一次崩溃的运行会把配额还给可能已经发出去的消息——
    执行器下一轮不会重发 sending 行，但如果配额没被占住，就会去发送队列里
    下一批 approved 的行，实际发送量就超过了每日上限。sending 和 sent 共用
    同一个 sent_at 列（mark_sending 认领时盖一次，mark_sent 成功后再刷新一
    次），所以这里按 date(sent_at,'localtime') 筛今天的写法不用变。
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM actions "
        "WHERE status IN (?, ?) AND date(sent_at, 'localtime') = date('now', 'localtime')",
        (SENT, SENDING),
    ).fetchone()
    return int(row["n"])


def remaining_quota(conn: sqlite3.Connection) -> int:
    from jobstar.config import get_setting

    limit = int(get_setting(conn, "daily_greeting_limit"))
    return max(0, limit - sent_today(conn))
