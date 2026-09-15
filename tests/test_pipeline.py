import json

import pytest

from jobstar.actions import PENDING, list_by_status
from jobstar.config import set_setting
from jobstar.db import get_conn, init_db
from jobstar.models import DimensionScore, JobRequirements, ScoreResult
from jobstar.pipeline import (
    maybe_enqueue,
    parse_tags,
    prelim_requirements,
    run_collect,
    run_score,
)


@pytest.fixture()
def conn(tmp_path):
    c = get_conn(tmp_path / "t.db")
    init_db(c)
    return c


@pytest.mark.parametrize(
    "tags,expected",
    [
        (["3-5年", "本科"], {"years_min": 3, "years_max": 5, "degree": "本科"}),
        (["5年以上", "硕士"], {"years_min": 5, "years_max": None, "degree": "硕士"}),
        (["经验不限", "学历不限"], {"years_min": 0, "years_max": None, "degree": "不限"}),
        (["1年以内"], {"years_min": 0, "years_max": 1, "degree": None}),
        (["在校/应届"], {"years_min": 0, "years_max": 1, "degree": None}),
        ([], {"years_min": None, "years_max": None, "degree": None}),
    ],
)
def test_parse_tags(tags, expected):
    assert parse_tags(tags) == expected


def test_prelim_requirements_uses_list_page_fields_only(conn):
    conn.execute(
        "INSERT INTO jobs (platform, job_id, title, company, raw_jd, city, salary_raw) "
        "VALUES ('boss', 'j1', 't', 'c', '', '杭州', '25-45K·14薪')"
    )
    conn.commit()
    row = conn.execute("SELECT * FROM jobs WHERE job_id='j1'").fetchone()
    req = prelim_requirements(row, tags=["3-5年", "本科"])
    assert req.city == "杭州"
    assert (req.salary_min, req.salary_max) == (25, 45)
    assert req.years_min == 3
    assert req.degree == "本科"
    assert req.skills == (), "预门禁阶段不做技能抽取"


def test_run_collect_only_fetches_detail_for_gate_survivors(conn):
    """两阶段抓取：详情页请求量应当远小于列表条数。"""
    listed = [
        {
            "job_id": "keep",
            "url": "https://x/job_detail/keep~.html",
            "title": "后端",
            "company": "A",
            "city": "杭州",
            "salary_raw": "30-50K",
            "hr_name": "张",
            "tags": ["3-5年", "本科"],
        },
        {
            "job_id": "drop-city",
            "url": "https://x/job_detail/drop-city~.html",
            "title": "后端",
            "company": "B",
            "city": "北京",
            "salary_raw": "30-50K",
            "hr_name": "李",
            "tags": ["3-5年"],
        },
        {
            "job_id": "drop-salary",
            "url": "https://x/job_detail/drop-salary~.html",
            "title": "后端",
            "company": "C",
            "city": "杭州",
            "salary_raw": "8-12K",
            "hr_name": "王",
            "tags": ["1年以内"],
        },
    ]
    detail_calls = []

    report = run_collect(
        conn,
        keyword="后端",
        city_code="101210100",
        fetch_fn=lambda **kw: listed,
        detail_fn=lambda url: detail_calls.append(url) or {"raw_jd": "JD 全文"},
    )
    assert report.listed == 3
    assert report.gated_out == 2
    assert report.detail_fetched == 1
    assert len(detail_calls) == 1
    assert "keep" in detail_calls[0]

    kept = conn.execute("SELECT raw_jd FROM jobs WHERE job_id='keep'").fetchone()
    assert kept["raw_jd"] == "JD 全文"
    reason = conn.execute(
        "SELECT reject_reason FROM gate_results WHERE job_id='drop-city'"
    ).fetchone()
    assert "城市" in reason["reject_reason"]


def test_run_collect_skips_already_collected(conn):
    item = {
        "job_id": "j1",
        "url": "https://x/job_detail/j1~.html",
        "title": "后端",
        "company": "A",
        "city": "杭州",
        "salary_raw": "30-50K",
        "hr_name": "张",
        "tags": [],
    }
    run_collect(
        conn,
        keyword="k",
        city_code="c",
        fetch_fn=lambda **kw: [item],
        detail_fn=lambda url: {"raw_jd": "第一次"},
    )
    report = run_collect(
        conn,
        keyword="k",
        city_code="c",
        fetch_fn=lambda **kw: [item],
        detail_fn=lambda url: {"raw_jd": "第二次"},
    )
    assert report.new == 0
    assert report.detail_fetched == 0
    row = conn.execute("SELECT raw_jd FROM jobs WHERE job_id='j1'").fetchone()
    assert row["raw_jd"] == "第一次"


def test_run_collect_login_required_propagates_and_stops_mid_batch(conn):
    """登录态失效是会话级别的硬故障：不能被当成普通的单条采集失败吞掉。
    第二个门禁幸存者的详情页请求触发 LoginRequired 后，应当整个往外炸穿，
    且第三个幸存者完全不应该再被尝试。"""
    from jobstar.collector import boss

    listed = [
        {
            "job_id": f"keep{i}",
            "url": f"https://x/job_detail/keep{i}~.html",
            "title": "后端",
            "company": "A",
            "city": "杭州",
            "salary_raw": "30-50K",
            "hr_name": "张",
            "tags": ["3-5年", "本科"],
        }
        for i in range(1, 4)
    ]
    detail_calls = []

    def detail_fn(url):
        detail_calls.append(url)
        if len(detail_calls) == 2:
            raise boss.LoginRequired("登录态失效")
        return {"raw_jd": "JD 全文"}

    with pytest.raises(boss.LoginRequired):
        run_collect(
            conn,
            keyword="后端",
            city_code="101210100",
            fetch_fn=lambda **kw: listed,
            detail_fn=detail_fn,
        )

    assert len(detail_calls) == 2, "第三个幸存者不应该再被尝试详情页请求"


def test_run_score_marks_failure_without_guessing(conn, monkeypatch):
    from jobstar.llm import LLMSchemaError

    conn.execute(
        "INSERT INTO jobs (platform, job_id, title, company, raw_jd, detail_fetched, city) "
        "VALUES ('boss', 'j1', 't', 'c', 'JD', 1, '杭州')"
    )
    conn.execute("INSERT INTO gate_results (job_id, passed) VALUES ('j1', 1)")
    conn.commit()

    def boom(**kwargs):
        raise LLMSchemaError("重试一次后仍不是合法 JSON")

    monkeypatch.setattr("jobstar.pipeline.normalize", boom)
    report = run_score(conn)
    assert report.failed == 1
    assert report.scored == 0
    row = conn.execute("SELECT status FROM jobs WHERE job_id='j1'").fetchone()
    assert row["status"] == "scoring_failed"


def test_maybe_enqueue_does_nothing_when_threshold_is_none(conn):
    """设计文档 §5.3：第一版交付时不设阈值，一条消息都不发。"""
    req = JobRequirements(
        "j1", "杭州", None, None, None, None, None, (), None, None, None
    )
    result = ScoreResult("j1", 95.0, (DimensionScore("skills", 95, ("a",), "", ""),), "v1")
    assert maybe_enqueue(conn, req, result, title="t", company="c") is None
    assert list_by_status(conn, PENDING) == []


def test_maybe_enqueue_creates_pending_action_above_threshold(conn, monkeypatch):
    set_setting(conn, "score_threshold", 70)
    monkeypatch.setattr("jobstar.pipeline.load_cards", lambda path: ())
    monkeypatch.setattr("jobstar.pipeline.write_pitch", lambda **kw: "定制开场白")
    req = JobRequirements(
        "j1", "杭州", None, None, None, None, None, (), None, None, None
    )
    result = ScoreResult("j1", 82.0, (DimensionScore("skills", 82, ("a",), "", ""),), "v1")
    action_id = maybe_enqueue(conn, req, result, title="t", company="c")
    assert action_id is not None
    row = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
    assert row["status"] == PENDING


def test_maybe_enqueue_reuses_passed_in_cards_instead_of_reloading(conn, monkeypatch):
    """run_score 已经加载过一次卡片库；传了 cards 进来就不该再读一遍磁盘。"""
    set_setting(conn, "score_threshold", 70)

    def boom(path):
        raise AssertionError("cards 已经传入，不应该再次从磁盘加载")

    monkeypatch.setattr("jobstar.pipeline.load_cards", boom)
    monkeypatch.setattr("jobstar.pipeline.write_pitch", lambda **kw: "定制开场白")
    req = JobRequirements(
        "j1", "杭州", None, None, None, None, None, (), None, None, None
    )
    result = ScoreResult("j1", 82.0, (DimensionScore("skills", 82, ("a",), "", ""),), "v1")
    action_id = maybe_enqueue(conn, req, result, title="t", company="c", cards=())
    assert action_id is not None
    row = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
    assert json.loads(row["payload"])["greeting"] == "定制开场白"


def test_maybe_enqueue_skips_below_threshold(conn):
    set_setting(conn, "score_threshold", 70)
    req = JobRequirements(
        "j1", "杭州", None, None, None, None, None, (), None, None, None
    )
    result = ScoreResult("j1", 55.0, (), "v1")
    assert maybe_enqueue(conn, req, result, title="t", company="c") is None
