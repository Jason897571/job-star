import pytest

from jobstar.actions import (
    APPROVED,
    FAILED,
    PENDING,
    SENT,
    SKIPPED,
    InvalidTransition,
    approve,
    enqueue,
    list_by_status,
    mark_failed,
    mark_sent,
    remaining_quota,
    sent_today,
    skip,
)
from jobstar.config import set_setting
from jobstar.db import get_conn, init_db


@pytest.fixture()
def conn(tmp_path):
    c = get_conn(tmp_path / "t.db")
    init_db(c)
    return c


def _new(conn, job_id="j1"):
    return enqueue(
        conn, type="send_greeting", job_id=job_id, payload={"greeting": "你好"}
    )


def test_enqueue_starts_pending(conn):
    action_id = _new(conn)
    row = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
    assert row["status"] == PENDING
    assert row["decided_at"] is None


def test_enqueue_is_idempotent_per_job_and_type(conn):
    first = _new(conn)
    second = _new(conn)
    assert first == second
    assert len(list_by_status(conn, PENDING)) == 1


def test_approve_moves_to_approved_and_stamps_decided_at(conn):
    action_id = _new(conn)
    approve(conn, action_id)
    row = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
    assert row["status"] == APPROVED
    assert row["decided_at"] is not None


def test_approve_can_rewrite_payload(conn):
    """面板上「改写后确认」走这条路径。"""
    action_id = _new(conn)
    approve(conn, action_id, {"greeting": "改写后的文案"})
    row = conn.execute("SELECT payload FROM actions WHERE id=?", (action_id,)).fetchone()
    assert "改写后的文案" in row["payload"]


def test_skip_moves_to_skipped(conn):
    action_id = _new(conn)
    skip(conn, action_id)
    row = conn.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone()
    assert row["status"] == SKIPPED


def test_pending_cannot_jump_to_sent(conn):
    """这是「绝不自动发送」的结构性保证：只有 approved 能变 sent。"""
    action_id = _new(conn)
    with pytest.raises(InvalidTransition):
        mark_sent(conn, action_id)


def test_approved_can_be_sent(conn):
    action_id = _new(conn)
    approve(conn, action_id)
    mark_sent(conn, action_id)
    row = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
    assert row["status"] == SENT
    assert row["sent_at"] is not None


def test_approved_can_fail_with_reason(conn):
    action_id = _new(conn)
    approve(conn, action_id)
    mark_failed(conn, action_id, "找不到打招呼按钮")
    row = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
    assert row["status"] == FAILED
    assert "打招呼按钮" in row["error"]


def test_failed_can_be_re_approved(conn):
    """失败不自动重试，但人工可以在面板上重新批准。"""
    action_id = _new(conn)
    approve(conn, action_id)
    mark_failed(conn, action_id, "boom")
    approve(conn, action_id)
    row = conn.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone()
    assert row["status"] == APPROVED


def test_sent_is_terminal(conn):
    action_id = _new(conn)
    approve(conn, action_id)
    mark_sent(conn, action_id)
    with pytest.raises(InvalidTransition):
        approve(conn, action_id)
    with pytest.raises(InvalidTransition):
        skip(conn, action_id)


def test_skipped_can_be_reopened(conn):
    action_id = _new(conn)
    skip(conn, action_id)
    approve(conn, action_id)
    row = conn.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone()
    assert row["status"] == APPROVED


def test_sent_today_counts_only_today(conn):
    for i in range(3):
        aid = _new(conn, job_id=f"j{i}")
        approve(conn, aid)
        mark_sent(conn, aid)
    conn.execute("UPDATE actions SET sent_at='2020-01-01 00:00:00' WHERE job_id='j0'")
    conn.commit()
    assert sent_today(conn) == 2


def test_remaining_quota_respects_setting(conn):
    set_setting(conn, "daily_greeting_limit", 2)
    assert remaining_quota(conn) == 2
    aid = _new(conn)
    approve(conn, aid)
    mark_sent(conn, aid)
    assert remaining_quota(conn) == 1


def test_remaining_quota_never_negative(conn):
    set_setting(conn, "daily_greeting_limit", 1)
    for i in range(3):
        aid = _new(conn, job_id=f"j{i}")
        approve(conn, aid)
        mark_sent(conn, aid)
    assert remaining_quota(conn) == 0


def test_unknown_action_id_raises(conn):
    with pytest.raises(InvalidTransition, match="不存在"):
        approve(conn, 9999)
