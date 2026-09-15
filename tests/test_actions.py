import pytest

from jobstar import actions as actions_module
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
    with pytest.raises(InvalidTransition):
        mark_sent(conn, action_id)
    with pytest.raises(InvalidTransition):
        mark_failed(conn, action_id, "boom")


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


@pytest.mark.parametrize("prior_state", ["pending", "skipped", "failed", "sent"])
def test_mark_sent_rejected_from_every_non_approved_state(conn, prior_state):
    """安全线的参数化版本：sent 只能从 approved 到达，其余任何状态都必须拒绝。"""
    action_id = _new(conn)
    if prior_state == "skipped":
        skip(conn, action_id)
    elif prior_state == "failed":
        approve(conn, action_id)
        mark_failed(conn, action_id, "boom")
    elif prior_state == "sent":
        approve(conn, action_id)
        mark_sent(conn, action_id)

    with pytest.raises(InvalidTransition):
        mark_sent(conn, action_id)


def test_reenqueue_after_sent_does_not_revive(conn):
    """已发送的行重新入队不应该复活或被新 payload 覆盖。"""
    action_id = _new(conn)
    approve(conn, action_id)
    mark_sent(conn, action_id)
    again = enqueue(
        conn, type="send_greeting", job_id="j1", payload={"greeting": "新文案"}
    )
    assert again == action_id
    row = conn.execute(
        "SELECT status, payload FROM actions WHERE id=?", (action_id,)
    ).fetchone()
    assert row["status"] == SENT
    assert "你好" in row["payload"]
    assert "新文案" not in row["payload"]


def test_reapprove_is_idempotent(conn):
    """approved → approved：重复点击确认，或先确认、发现文案有误、改写后再次确认。"""
    action_id = _new(conn)
    approve(conn, action_id)
    approve(conn, action_id, {"greeting": "改写后的文案"})
    row = conn.execute(
        "SELECT status, payload FROM actions WHERE id=?", (action_id,)
    ).fetchone()
    assert row["status"] == APPROVED
    assert "改写后的文案" in row["payload"]


def test_failed_can_be_given_up_on(conn):
    """failed → skipped：永久失败（例如岗位已下架）时，人工可以彻底放弃。"""
    action_id = _new(conn)
    approve(conn, action_id)
    mark_failed(conn, action_id, "岗位已下架")
    skip(conn, action_id)
    row = conn.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone()
    assert row["status"] == SKIPPED


def test_atomic_guard_blocks_interleaved_send(conn, tmp_path, monkeypatch):
    """Finding 1 的回归测试。

    模拟最坏时序：执行者读到 approved（校验通过）之后、真正把 UPDATE 落盘
    之前，人工在面板上点了「跳过」并提交。用 monkeypatch 把这次竞争写入精确
    插到 `_atomic_transition` 真正执行 UPDATE 语句的前一刻——这正是旧的
    check-then-act 实现里，SELECT 和 UPDATE 分属两个隐式事务之间唯一存在
    的窗口。修复后 UPDATE 的 WHERE 子句会重新校验当前状态，必须失败。
    """
    action_id = _new(conn)
    approve(conn, action_id)

    conn_a = get_conn(tmp_path / "t.db")
    conn_b = get_conn(tmp_path / "t.db")

    row = conn_a.execute(
        "SELECT status FROM actions WHERE id=?", (action_id,)
    ).fetchone()
    assert row["status"] == APPROVED  # 执行者此刻看到的是 approved

    original = actions_module._atomic_transition
    triggered = {"done": False}

    def racing_atomic_transition(conn_arg, aid, target, set_sql, set_params):
        if (
            conn_arg is conn_a
            and aid == action_id
            and target == SENT
            and not triggered["done"]
        ):
            triggered["done"] = True
            skip(conn_b, action_id)  # 人工此刻提交了「跳过」
        return original(conn_arg, aid, target, set_sql, set_params)

    monkeypatch.setattr(actions_module, "_atomic_transition", racing_atomic_transition)

    try:
        with pytest.raises(InvalidTransition):
            mark_sent(conn_a, action_id)
    finally:
        conn_a.close()
        conn_b.close()

    final = conn.execute(
        "SELECT status FROM actions WHERE id=?", (action_id,)
    ).fetchone()
    assert final["status"] == SKIPPED


def test_sent_today_counts_previous_utc_day_current_local_day(conn):
    """Finding 2 的回归测试。

    sent_at 按 UTC 落盘。如果它落在「UTC 的前一天、本地的今天」这个窗口
    （杭州 UTC+8 的凌晨 0-8 点就是这种情况），必须被 sent_today 计入，而不是
    永久漏计。时间戳从 SQLite 自己的 'now' 推导，不写死字面量，这样无论在
    哪个时区跑测试都有意义（旧测试用 2020-01-01，在任何时区都恒为「不是
    今天」，根本没测到跨天边界）。
    """
    row = conn.execute(
        "SELECT strftime('%s','now') AS u, strftime('%s','now','localtime') AS l"
    ).fetchone()
    offset_seconds = int(row["l"]) - int(row["u"])
    if offset_seconds == 0:
        pytest.skip("本机 UTC 偏移为 0，无法构造跨天场景")

    if offset_seconds > 0:
        # 本地时区领先 UTC（例如杭州 +8）：本地零点后一小时换算成 UTC，落在
        # UTC 的前一天。
        formula = "datetime(date('now','localtime'), '+1 hours', 'utc')"
    else:
        # 本地时区落后 UTC（例如本机 EDT -4）：本地明天零点前一小时换算成
        # UTC，落在 UTC 的下一天——同样是「UTC 日期 ≠ 本地日期」的边界。
        formula = "datetime(date('now','localtime','+1 day'), '-1 hours', 'utc')"

    sent_at_value = conn.execute(f"SELECT {formula} AS v").fetchone()["v"]

    check = conn.execute(
        "SELECT date(?) AS raw_date, date(?, 'localtime') AS local_date, "
        "date('now','localtime') AS today",
        (sent_at_value, sent_at_value),
    ).fetchone()
    assert check["raw_date"] != check["today"]
    assert check["local_date"] == check["today"]

    action_id = _new(conn)
    approve(conn, action_id)
    mark_sent(conn, action_id)
    conn.execute("UPDATE actions SET sent_at=? WHERE id=?", (sent_at_value, action_id))
    conn.commit()

    assert sent_today(conn) == 1
