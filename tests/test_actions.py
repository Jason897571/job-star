import time

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


def test_rejected_mark_sent_releases_lock_for_other_connections(conn, tmp_path):
    """Finding 1 的回归测试。

    `get_conn` 让 sqlite3 保持 isolation_level=''，UPDATE 在 WHERE 求值之前
    就已经隐式 BEGIN。`_atomic_transition` 的 rowcount==0 分支如果不回滚就
    直接 raise，这次什么都没改动的 UPDATE 开启的事务会一直挂在连接上，占
    着 RESERVED 锁，卡住其他连接的所有写入——即使这次拒绝本身完全符合设
    计（pending 不能变 sent）。拒绝是正常结果：竞态守卫按预期触发，或者人
    工在面板上对一个已经是终态的行又点了一次确认/跳过；而架构本来就是多
    进程的——面板进程和执行者进程都会打开这个数据库文件。

    `conn.in_transaction` 是实现细节，只作为诊断信号；真正要保证的用户可
    见行为是下面第二个连接的写入必须能完成——这一条在修复前会因为
    `database is locked` 失败，是这里真正把关的断言。
    """
    action_id = _new(conn)  # 仍是 pending

    with pytest.raises(InvalidTransition):
        mark_sent(conn, action_id)

    assert conn.in_transaction is False

    other = get_conn(tmp_path / "t.db")
    try:
        other.execute("UPDATE actions SET error=? WHERE id=?", ("ping", action_id))
        other.commit()
    finally:
        other.close()

    row = conn.execute("SELECT error FROM actions WHERE id=?", (action_id,)).fetchone()
    assert row["error"] == "ping"


def test_rejected_approve_releases_lock_for_other_connections(conn, tmp_path):
    """Finding 1 的回归测试（覆盖另一个写入函数）。

    `_atomic_transition` 的 rowcount==0 分支只有一份，四个写入函数
    （approve/skip/mark_sent/mark_failed）共用它，但只测 mark_sent 不足以
    防止以后有人往这个分支加新的 raise 路径却忘了回滚。这里再拿 `approve`
    在一个已经是 sent（终态）的行上被拒绝的场景测一遍。
    """
    action_id = _new(conn)
    approve(conn, action_id)
    mark_sent(conn, action_id)

    with pytest.raises(InvalidTransition):
        approve(conn, action_id)  # sent 是终态，approve 必须被拒绝

    assert conn.in_transaction is False

    other = get_conn(tmp_path / "t.db")
    try:
        other.execute("UPDATE actions SET error=? WHERE id=?", ("ping", action_id))
        other.commit()
    finally:
        other.close()

    row = conn.execute("SELECT error FROM actions WHERE id=?", (action_id,)).fetchone()
    assert row["error"] == "ping"


def test_atomic_guard_blocks_interleaved_send(conn, tmp_path):
    """Finding 1 的回归测试（不 monkeypatch 私有函数）。

    两个真实连接指向同一个数据库文件：连接 A 确认（approved），连接 B 抢先
    把状态改成 skipped 并提交，连接 A 接着调用 mark_sent——此时它对状态的
    认知已经过时。修复后的 UPDATE 把「来源状态是否允许」折进同一条语句的
    WHERE 子句，写入前会重新校验当前状态，必须失败，且最终状态仍是
    skipped。

    这里不再像之前那样 monkeypatch `_atomic_transition` 去精确插入竞争写
    入：那个注入点在进入 `_atomic_transition` 之前就已经触发，效果和这里
    直接按顺序调用 approve → skip → mark_sent 完全一样，却让测试耦合到一个
    私有辅助函数的位置签名——一旦它被改名或内联，测试要么报错要么静默退化
    成空操作。改写后的顺序调用测的是同一个属性（写入语句必须重新校验状
    态），但不依赖任何私有实现细节。
    """
    action_id = _new(conn)

    conn_a = get_conn(tmp_path / "t.db")
    conn_b = get_conn(tmp_path / "t.db")
    try:
        approve(conn_a, action_id)
        skip(conn_b, action_id)  # 人工抢先提交了「跳过」

        with pytest.raises(InvalidTransition):
            mark_sent(conn_a, action_id)
    finally:
        conn_a.close()
        conn_b.close()

    final = conn.execute(
        "SELECT status FROM actions WHERE id=?", (action_id,)
    ).fetchone()
    assert final["status"] == SKIPPED


def test_sent_today_counts_previous_utc_day_current_local_day(conn, monkeypatch):
    """Finding 2 的回归测试。

    sent_at 按 UTC 落盘。如果它落在「UTC 的前一天、本地的今天」这个窗口
    （部署时区杭州 UTC+8 的凌晨 0-8 点就是这种情况：本地 01:00 存成 UTC 前
    一天 17:00），必须被 sent_today 计入，而不是永久漏计。

    测试把 TZ 固定成部署时区 Asia/Shanghai，而不是按宿主机的 UTC 偏移分支
    判断——CI/容器宿主机默认是 UTC，偏移为 0 时旧写法会直接跳过整条测试，
    回归永远测不到，输出变成「23 passed, 1 skipped」而不是全绿。SQLite 的
    'localtime' 修饰符通过 libc 解析时区，所以 TZ 环境变量 + time.tzset()
    能让它在任何宿主机上都按 UTC+8 计算，测试因此在哪里跑都确定性成立。
    """
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    try:
        # 本地零点后一小时换算成 UTC，落在 UTC 的前一天（例如本地 01:00
        # 存成 UTC 前一天 17:00）——这正是 bug 实际咬人的那个窗口。
        sent_at_value = conn.execute(
            "SELECT datetime(date('now','localtime'), '+1 hours', 'utc') AS v"
        ).fetchone()["v"]

        # 先断言边界条件本身成立，测试才不会在这个条件根本没构造出来的情况
        # 下空洞地通过。
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
        conn.execute(
            "UPDATE actions SET sent_at=? WHERE id=?", (sent_at_value, action_id)
        )
        conn.commit()

        assert sent_today(conn) == 1
    finally:
        # monkeypatch 在测试结束后才会把 TZ 环境变量还原，如果不在这里主动
        # 还原 + 重新 tzset，进程级别的 C 库时区状态会在还原之前一直是
        # Asia/Shanghai，泄漏给后面的测试。显式 undo + tzset 之后，pytest
        # 自动触发的那次 undo 是幂等的空操作。
        monkeypatch.undo()
        time.tzset()
