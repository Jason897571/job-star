import json

import pytest

from jobstar.actions import PENDING, list_by_status
from jobstar.config import get_setting, set_setting
from jobstar.db import get_conn, init_db
from jobstar.models import DimensionScore, JobRequirements, ScoreResult
from jobstar.pipeline import (
    clear_for_rescore,
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


# --- Important 4：吸收态与 `jobstar score --rescore` ---
#
# run_score 的查询是「从没打过分 + 门禁通过」才入场（scores.job_id IS NULL
# AND gate_results.passed = 1）。这是正确的默认行为，但也意味着四种状态一旦
# 写入就再也回不来：scoring_failed、pitch_failed、第二遍门禁刷掉、以及已经
# 成功打过分的岗位（设计文档 §5.3 的校准循环要求能重跑同一批标注过的岗位，
# 对照改 prompt/权重前后的一致率）。下面这组测试锁住两件事：不传 --rescore
# 时行为和以前完全一样（吸收态确实是吸收态），传了之后这四种都能重新入场。


def _seed_scorable(conn, job_id: str, *, city: str = "杭州", passed: int = 1) -> None:
    conn.execute(
        "INSERT INTO jobs (platform, job_id, title, company, raw_jd, detail_fetched, city) "
        "VALUES ('boss', ?, '后端开发', 'A公司', 'JD 全文', 1, ?)",
        (job_id, city),
    )
    conn.execute(
        "INSERT INTO gate_results (job_id, passed) VALUES (?, ?)", (job_id, passed)
    )
    conn.commit()


def _req(job_id: str, city: str = "杭州") -> JobRequirements:
    return JobRequirements(
        job_id, city, None, None, None, None, None, (), None, None, None
    )


def test_scoring_failure_is_absorbing_until_rescore_clears_it(conn, monkeypatch):
    """一次 LLM 抽风不该把岗位永久烧掉——但也不该自动重试（那会把一个真正
    坏掉的 JD 变成每轮都烧 token 的无限循环）。重新入场必须是人工显式动作。"""
    from jobstar.llm import LLMSchemaError

    _seed_scorable(conn, "j1")
    calls: list[str] = []

    def flaky_normalize(**kwargs):
        calls.append(kwargs["job_id"])
        if len(calls) == 1:
            raise LLMSchemaError("重试一次后仍不是合法 JSON")
        return _req(kwargs["job_id"])

    monkeypatch.setattr("jobstar.pipeline.normalize", flaky_normalize)
    monkeypatch.setattr("jobstar.pipeline.load_cards", lambda path: ())
    monkeypatch.setattr(
        "jobstar.pipeline.score",
        lambda req, cards, weights: ScoreResult(req.job_id, 80.0, (), "v1"),
    )

    assert run_score(conn).failed == 1
    assert conn.execute(
        "SELECT status FROM jobs WHERE job_id='j1'"
    ).fetchone()["status"] == "scoring_failed"

    again = run_score(conn)
    assert (again.scored, again.failed) == (0, 0), "不传 --rescore 就不该重新入场"
    assert calls == ["j1"], "第二轮根本不该再调用一次归一化"

    assert clear_for_rescore(conn) == 1
    assert run_score(conn).scored == 1
    assert calls == ["j1", "j1"]
    assert conn.execute(
        "SELECT status FROM jobs WHERE job_id='j1'"
    ).fetchone()["status"] == "scored"


def test_rescore_reopens_pitch_failed_job(conn, monkeypatch):
    """话术生成失败时分数是真的（total 不为 NULL），但 scores 行的存在同样
    把岗位挡在 run_score 之外。--rescore 要能把它放回来。"""
    from jobstar.llm import LLMBackendError

    set_setting(conn, "score_threshold", 70)
    _seed_scorable(conn, "j1")
    monkeypatch.setattr("jobstar.pipeline.normalize", lambda **kw: _req(kw["job_id"]))
    monkeypatch.setattr("jobstar.pipeline.load_cards", lambda path: ())
    monkeypatch.setattr(
        "jobstar.pipeline.score",
        lambda req, cards, weights: ScoreResult(req.job_id, 80.0, (), "v1"),
    )

    pitch_calls: list[int] = []

    def flaky_pitch(**kwargs):
        pitch_calls.append(1)
        if len(pitch_calls) == 1:
            raise LLMBackendError("网关 502")
        return "定制开场白"

    monkeypatch.setattr("jobstar.pipeline.write_pitch", flaky_pitch)

    assert run_score(conn).pitch_failed == 1
    row = conn.execute("SELECT * FROM scores WHERE job_id='j1'").fetchone()
    assert row["total"] == 80.0, "分数本身是成立的，不该被话术失败抹掉"
    assert row["error"] is not None

    assert run_score(conn).scored == 0, "scores 行的存在把它挡在门外"

    assert clear_for_rescore(conn) == 1
    report = run_score(conn)
    assert (report.scored, report.enqueued) == (1, 1)
    assert conn.execute(
        "SELECT error FROM scores WHERE job_id='j1'"
    ).fetchone()["error"] is None


def test_rescore_restores_second_pass_gate_rejection(conn, monkeypatch):
    """第二遍门禁把同一行 gate_results 覆盖成 passed=0，此后 run_score 的
    JOIN 永远选不中它——哪怕人工后来把 city_whitelist 放宽了。"""
    _seed_scorable(conn, "j1", city="北京")
    monkeypatch.setattr(
        "jobstar.pipeline.normalize", lambda **kw: _req(kw["job_id"], city="北京")
    )
    monkeypatch.setattr("jobstar.pipeline.load_cards", lambda path: ())
    monkeypatch.setattr(
        "jobstar.pipeline.score",
        lambda req, cards, weights: ScoreResult(req.job_id, 80.0, (), "v1"),
    )

    assert run_score(conn).gated_out == 1
    assert conn.execute(
        "SELECT passed FROM gate_results WHERE job_id='j1'"
    ).fetchone()["passed"] == 0
    assert run_score(conn).gated_out == 0, "已经被刷掉的岗位不会再被门禁看见"

    # 人工放宽规则后重跑
    set_setting(
        conn,
        "gate_rules",
        {**get_setting(conn, "gate_rules"), "city_whitelist": ["杭州", "北京"]},
    )
    assert clear_for_rescore(conn) == 1
    assert run_score(conn).scored == 1


def test_rescore_leaves_never_scored_and_prefilter_rejects_alone(conn):
    """只清「确实卡在吸收态」的岗位：
      - 还没打过分、门禁通过的岗位本来就能入场，不该被动（status 也不能被
        改回 'new' 之外的值覆盖掉）；
      - 第一遍门禁刷掉、从没抓过详情页的岗位（detail_fetched=0）够不着
        run_score，这里也不该去动它——那会让它伪装成一个等待打分的岗位。
    """
    _seed_scorable(conn, "fresh")  # 门禁通过、没有 scores 行
    conn.execute(
        "INSERT INTO jobs (platform, job_id, title, company, detail_fetched, status) "
        "VALUES ('boss', 'prefiltered', 't', 'c', 0, 'gated_out')"
    )
    conn.execute(
        "INSERT INTO gate_results (job_id, passed, reject_reason) "
        "VALUES ('prefiltered', 0, '城市不在白名单：北京')"
    )
    conn.commit()

    assert clear_for_rescore(conn) == 0

    row = conn.execute("SELECT * FROM gate_results WHERE job_id='prefiltered'").fetchone()
    assert row["passed"] == 0
    assert row["reject_reason"] == "城市不在白名单：北京"
    assert conn.execute(
        "SELECT status FROM jobs WHERE job_id='prefiltered'"
    ).fetchone()["status"] == "gated_out"


def test_rescore_with_job_id_only_touches_that_job(conn, monkeypatch):
    from jobstar.llm import LLMSchemaError

    for job_id in ("j1", "j2"):
        _seed_scorable(conn, job_id)

    def boom(**kwargs):
        raise LLMSchemaError("坏了")

    monkeypatch.setattr("jobstar.pipeline.normalize", boom)
    monkeypatch.setattr("jobstar.pipeline.load_cards", lambda path: ())
    assert run_score(conn).failed == 2

    assert clear_for_rescore(conn, job_id="j1") == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM scores").fetchone()["n"] == 1
    assert conn.execute(
        "SELECT job_id FROM scores"
    ).fetchone()["job_id"] == "j2", "只该清掉点名的那一个"


def test_rescore_does_not_resurrect_or_duplicate_an_existing_action(conn, monkeypatch):
    """校准用途（§5.3）要求能重跑已经成功打过分的岗位——包括已经发出过
    招呼的。actions 的 UNIQUE(type, job_id) + enqueue 的 DO NOTHING 保证
    重跑不会造出第二条动作、也不会把一条已经 sent 的动作打回待确认，
    否则一次校准重跑就会给同一个真人再发一遍消息。"""
    from jobstar import actions

    set_setting(conn, "score_threshold", 70)
    _seed_scorable(conn, "j1")
    monkeypatch.setattr("jobstar.pipeline.normalize", lambda **kw: _req(kw["job_id"]))
    monkeypatch.setattr("jobstar.pipeline.load_cards", lambda path: ())
    monkeypatch.setattr("jobstar.pipeline.write_pitch", lambda **kw: "第一版开场白")
    monkeypatch.setattr(
        "jobstar.pipeline.score",
        lambda req, cards, weights: ScoreResult(req.job_id, 80.0, (), "v1"),
    )

    assert run_score(conn).enqueued == 1
    action_id = list_by_status(conn, PENDING)[0]["id"]
    actions.approve(conn, action_id)
    actions.mark_sending(conn, action_id)
    actions.mark_sent(conn, action_id)

    # 改了权重/prompt 之后重跑同一批岗位
    monkeypatch.setattr("jobstar.pipeline.write_pitch", lambda **kw: "第二版开场白")
    assert clear_for_rescore(conn) == 1
    assert run_score(conn).scored == 1

    rows = conn.execute("SELECT * FROM actions WHERE job_id='j1'").fetchall()
    assert len(rows) == 1, "重跑不该造出第二条动作"
    assert rows[0]["status"] == actions.SENT, "已发出的动作不该被打回待确认"
    assert json.loads(rows[0]["payload"])["greeting"] == "第一版开场白", (
        "payload 也不该被新话术覆盖——那会让台账里记的和真正发出去的不一致"
    )
