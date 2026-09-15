import random
import statistics

import pytest

from jobstar.actions import APPROVED, FAILED, SENT, approve, enqueue
from jobstar.config import set_setting
from jobstar.db import get_conn, init_db
from jobstar.executor import ExecutionReport, human_delay, run_queue


@pytest.fixture()
def conn(tmp_path):
    c = get_conn(tmp_path / "t.db")
    init_db(c)
    return c


def _approved(conn, job_id, greeting="你好"):
    conn.execute(
        "INSERT INTO jobs (platform, job_id, title, company, raw_jd, url, hr_name) "
        "VALUES ('boss', ?, ?, 'c', 'jd', ?, '张女士')",
        (job_id, f"岗位{job_id}", f"https://www.zhipin.com/job_detail/{job_id}~.html"),
    )
    conn.commit()
    action_id = enqueue(
        conn, type="send_greeting", job_id=job_id, payload={"greeting": greeting}
    )
    approve(conn, action_id)
    return action_id


def test_sends_all_approved_actions(conn):
    for i in range(3):
        _approved(conn, f"j{i}")
    sent = []
    report = run_queue(
        conn,
        send_fn=lambda url, text: sent.append((url, text)),
        sleep_fn=lambda _: None,
    )
    assert isinstance(report, ExecutionReport)
    assert report.sent == 3
    assert len(sent) == 3
    rows = conn.execute("SELECT status FROM actions").fetchall()
    assert {r["status"] for r in rows} == {SENT}


def test_ignores_pending_actions(conn):
    """绝不自动发送：pending 的动作执行器根本看不见。"""
    conn.execute(
        "INSERT INTO jobs (platform, job_id, title, company, raw_jd) "
        "VALUES ('boss', 'p1', 't', 'c', 'jd')"
    )
    conn.commit()
    enqueue(conn, type="send_greeting", job_id="p1", payload={"greeting": "你好"})
    calls = []
    report = run_queue(
        conn, send_fn=lambda u, t: calls.append(u), sleep_fn=lambda _: None
    )
    assert report.sent == 0
    assert calls == []


def test_stops_at_daily_quota(conn):
    set_setting(conn, "daily_greeting_limit", 2)
    for i in range(5):
        _approved(conn, f"j{i}")
    report = run_queue(conn, send_fn=lambda u, t: None, sleep_fn=lambda _: None)
    assert report.sent == 2
    assert report.quota_hit is True
    left = conn.execute(
        "SELECT COUNT(*) AS n FROM actions WHERE status=?", (APPROVED,)
    ).fetchone()
    assert left["n"] == 3, "超额的动作留在队列里，不丢"


def test_failure_marks_failed_and_continues(conn):
    _approved(conn, "bad")
    _approved(conn, "good")

    def flaky(url, text):
        if "bad" in url:
            raise RuntimeError("找不到打招呼按钮")

    report = run_queue(conn, send_fn=flaky, sleep_fn=lambda _: None)
    assert report.sent == 1
    assert report.failed == 1
    assert any("打招呼按钮" in e for e in report.errors)
    row = conn.execute(
        "SELECT status, error FROM actions WHERE job_id='bad'"
    ).fetchone()
    assert row["status"] == FAILED
    assert "打招呼按钮" in row["error"]


def test_failure_is_not_auto_retried(conn):
    """设计文档 §4.8：失败不自动重试，人工在面板决定。"""
    _approved(conn, "bad")
    calls = []

    def always_fail(url, text):
        calls.append(url)
        raise RuntimeError("boom")

    run_queue(conn, send_fn=always_fail, sleep_fn=lambda _: None)
    assert len(calls) == 1


def test_writes_application_ledger(conn):
    _approved(conn, "j1", greeting="定制化的开场白")
    conn.execute(
        "INSERT INTO scores (job_id, total, dimensions, scorer_version) "
        "VALUES ('j1', 82.0, '[]', 'v1')"
    )
    conn.commit()
    run_queue(conn, send_fn=lambda u, t: None, sleep_fn=lambda _: None)
    row = conn.execute("SELECT * FROM applications WHERE job_id='j1'").fetchone()
    assert row is not None
    assert row["greeting_text"] == "定制化的开场白"
    assert row["hr_name"] == "张女士"
    assert "82" in row["score_snapshot"]


def test_sleeps_between_sends_but_not_before_first(conn):
    for i in range(3):
        _approved(conn, f"j{i}")
    delays = []
    run_queue(conn, send_fn=lambda u, t: None, sleep_fn=delays.append)
    assert len(delays) == 2, "3 次发送之间只有 2 个间隔"
    assert all(d > 0 for d in delays)


def test_human_delay_is_long_tailed_not_uniform():
    """设计文档 §5.4：延迟分布要模拟人类长尾，不是均匀间隔。"""
    rng = random.Random(42)
    samples = [human_delay(rng) for _ in range(2000)]
    assert min(samples) >= 5.0
    assert max(samples) <= 600.0
    median = statistics.median(samples)
    mean = statistics.mean(samples)
    assert mean > median * 1.15, "均值应显著高于中位数（右偏长尾）"


def test_human_delay_is_reproducible_with_seed():
    assert human_delay(random.Random(7)) == human_delay(random.Random(7))


def test_empty_queue_is_a_noop(conn):
    report = run_queue(conn, send_fn=lambda u, t: None, sleep_fn=lambda _: None)
    assert report == ExecutionReport(sent=0, failed=0, quota_hit=False, errors=[])
