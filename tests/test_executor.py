import random
import statistics

import pytest

from jobstar import actions
from jobstar.actions import APPROVED, FAILED, SENT, SKIPPED, approve, enqueue, skip
from jobstar.collector import boss
from jobstar.config import set_setting
from jobstar.db import get_conn, init_db
from jobstar.executor import ExecutionReport, _build_send_script, human_delay, run_queue


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


def test_ignores_skipped_actions(conn):
    """test_ignores_pending_actions 只覆盖了 pending；skipped 同样绝不能被发送。"""
    conn.execute(
        "INSERT INTO jobs (platform, job_id, title, company, raw_jd) "
        "VALUES ('boss', 's1', 't', 'c', 'jd')"
    )
    conn.commit()
    aid = enqueue(conn, type="send_greeting", job_id="s1", payload={"greeting": "你好"})
    skip(conn, aid)
    calls = []
    report = run_queue(
        conn, send_fn=lambda u, t: calls.append(u), sleep_fn=lambda _: None
    )
    assert report.sent == 0
    assert calls == []


def test_row_skipped_mid_run_is_not_sent_and_run_continues(conn):
    """Finding 1（Critical）的直接回归测试。

    executor 在快照时看到 approved 的一行，如果在轮到它之前被人工在面板上
    点了「跳过」，绝不能把消息发给它。

    修复前：run_queue 只在开头读一次快照，后面不再看状态就直接 send_fn，
    这一行的 send_fn 会被正常调用（这条断言先失败）；就算断言侥幸不失败，
    随后的 mark_sent 对一个已经是 skipped 的行做 CAS 也会失败并抛出
    InvalidTransition，这个异常在 try 块之外，会直接从 run_queue 里逃出去，
    这条测试会报错而不是通过——两条路径都证明修复前这个场景是坏的。
    """
    _approved(conn, "aaa")
    id_b = _approved(conn, "bbb")
    sent = []

    def send_fn(url, text):
        if url.endswith("/aaa~.html"):
            skip(conn, id_b)  # 模拟人工在 aaa 发送之后、bbb 轮到之前点了跳过
        sent.append(url)

    report = run_queue(conn, send_fn=send_fn, sleep_fn=lambda _: None)

    assert all(not u.endswith("/bbb~.html") for u in sent), (
        "bbb 被人工跳过后绝不能再发消息"
    )
    assert len(sent) == 1
    row_b = conn.execute("SELECT status FROM actions WHERE id=?", (id_b,)).fetchone()
    assert row_b["status"] == SKIPPED
    assert report.sent == 1
    assert report.skipped == 1


def test_concurrent_executor_claims_row_first_this_run_skips_without_sending(
    conn, tmp_path
):
    """两个执行器抢同一行：mark_sending 的原子 CAS 保证恰好一个赢，另一个
    直接跳过、绝不调用 send_fn。"""
    _approved(conn, "x")
    id_y = _approved(conn, "y")
    other = get_conn(tmp_path / "t.db")
    try:
        sent = []

        def send_fn(url, text):
            if "x" in url:
                # 模拟另一个执行器进程，在本执行器处理到 y 之前抢先认领了它。
                actions.mark_sending(other, id_y)
            sent.append(url)

        report = run_queue(conn, send_fn=send_fn, sleep_fn=lambda _: None)

        assert all("y" not in u for u in sent)
        assert len(sent) == 1
        row_y = conn.execute(
            "SELECT status FROM actions WHERE id=?", (id_y,)
        ).fetchone()
        assert row_y["status"] == actions.SENDING
        assert report.sent == 1
        assert report.skipped == 1
    finally:
        other.close()


def test_interrupted_send_leaves_row_sending_and_is_not_resent(conn):
    """进程被打断（Ctrl-C/KeyboardInterrupt）留在 sending 的行，下一轮
    执行器读不到它（它已经不是 approved），因此不会被重发。"""
    aid = _approved(conn, "interrupt-me")

    def boom(url, text):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        run_queue(conn, send_fn=boom, sleep_fn=lambda _: None)

    row = conn.execute("SELECT status FROM actions WHERE id=?", (aid,)).fetchone()
    assert row["status"] == actions.SENDING

    calls = []
    report = run_queue(
        conn, send_fn=lambda u, t: calls.append(u), sleep_fn=lambda _: None
    )
    assert calls == []
    assert report.sent == 0


def test_failure_from_sending_state_does_not_abort_run(conn):
    """mark_failed 现在从 sending 出发是合法迁移；修复前这一步会因为行已经
    被认领而抛出 InvalidTransition，把整个 run_queue 带崩。"""
    _approved(conn, "bad")
    _approved(conn, "good")

    def flaky(url, text):
        if "bad" in url:
            raise RuntimeError("boom")

    report = run_queue(conn, send_fn=flaky, sleep_fn=lambda _: None)
    assert report.sent == 1
    assert report.failed == 1
    row = conn.execute(
        "SELECT status, error FROM actions WHERE job_id='bad'"
    ).fetchone()
    assert row["status"] == FAILED
    assert row["error"] == "boom"


def test_login_required_aborts_run_and_leaves_remaining_rows_untouched(conn):
    _approved(conn, "aaa")
    id_b = _approved(conn, "bbb")
    id_c = _approved(conn, "ccc")
    calls = []

    def send_fn(url, text):
        calls.append(url)
        if url.endswith("/bbb~.html"):
            raise boss.LoginRequired("登录态失效")

    report = run_queue(conn, send_fn=send_fn, sleep_fn=lambda _: None)

    assert report.login_required is True
    assert len(calls) == 2, "b 触发登录墙之后，c 从未被尝试"
    row_c = conn.execute("SELECT status FROM actions WHERE id=?", (id_c,)).fetchone()
    assert row_c["status"] == APPROVED
    row_b = conn.execute("SELECT status FROM actions WHERE id=?", (id_b,)).fetchone()
    assert row_b["status"] == FAILED


def test_quota_recheck_happens_every_iteration_not_computed_once(conn):
    """quota 检查如果在运行开始时算好一次就不再变，这条测试会失败：
    send_fn 把上限调到 1 之后，第二次迭代必须立刻停下，而不是再发 2 条。"""
    set_setting(conn, "daily_greeting_limit", 10)
    for i in range(3):
        _approved(conn, f"q{i}")
    calls = []

    def send_fn(url, text):
        calls.append(url)
        set_setting(conn, "daily_greeting_limit", 1)  # 人工中途调低配额

    report = run_queue(conn, send_fn=send_fn, sleep_fn=lambda _: None)
    assert len(calls) == 1, "配额是逐次读取的，不是运行开始时算好一次"
    assert report.sent == 1
    assert report.quota_hit is True


def test_sleep_delays_are_exactly_human_delay_with_seeded_rng(conn):
    """`all(d > 0)` 对固定间隔也能通过；这里验证延迟确实来自 human_delay
    本身，而不只是「大于零」——传入相同种子的 rng，观测值必须逐个相等。"""
    for i in range(3):
        _approved(conn, f"d{i}")
    observed = []
    expected_rng = random.Random(123)
    expected = [human_delay(expected_rng) for _ in range(2)]

    run_queue(
        conn,
        send_fn=lambda u, t: None,
        sleep_fn=observed.append,
        rng=random.Random(123),
    )

    assert observed == expected


def test_missing_job_url_fails_before_touching_browser(conn):
    conn.execute(
        "INSERT INTO jobs (platform, job_id, title, company, raw_jd) "
        "VALUES ('boss', 'nourl', 't', 'c', 'jd')"
    )
    conn.commit()
    aid = enqueue(
        conn, type="send_greeting", job_id="nourl", payload={"greeting": "你好"}
    )
    approve(conn, aid)
    calls = []
    report = run_queue(
        conn, send_fn=lambda u, t: calls.append(u), sleep_fn=lambda _: None
    )
    assert calls == [], "URL 缺失时不该打开浏览器"
    assert report.failed == 1
    row = conn.execute(
        "SELECT status, error FROM actions WHERE id=?", (aid,)
    ).fetchone()
    assert row["status"] == FAILED
    assert "URL" in row["error"]


def test_jobs_lookup_is_scoped_by_platform(conn):
    """jobs 表的键是 UNIQUE(platform, job_id)；同一个 job_id 在别的平台下
    也可能存在一行，executor 不能读错平台的 URL/hr_name。"""
    conn.execute(
        "INSERT INTO jobs (platform, job_id, title, company, raw_jd, url, hr_name) "
        "VALUES ('other', 'shared', 't', 'c', 'jd', "
        "'https://other.example/shared', '别人')"
    )
    conn.commit()
    _approved(conn, "shared")  # 插入 platform='boss' 的同 job_id 行并 approve
    sent = []
    run_queue(conn, send_fn=lambda u, t: sent.append(u), sleep_fn=lambda _: None)
    assert sent == ["https://www.zhipin.com/job_detail/shared~.html"]
    row = conn.execute(
        "SELECT hr_name FROM applications WHERE job_id='shared'"
    ).fetchone()
    assert row["hr_name"] == "张女士"


def test_send_script_compiles_and_round_trips_payload():
    """不开浏览器也能验证 send_greeting 生成的脚本本身合法，且引号/换行/
    反斜杠这类内容不会把 JSON 载荷搞散架。"""
    text = '带双引号" 和换行\n还有反斜杠\\结尾'
    url = "https://www.zhipin.com/job_detail/x~.html"
    script = _build_send_script(url, text)

    compile(script, "<generated>", "exec")  # 语法必须合法

    ns: dict = {}
    first_two_lines = "\n".join(script.splitlines()[:2])
    exec(first_two_lines, ns)
    assert ns["args"] == {"url": url, "text": text}
