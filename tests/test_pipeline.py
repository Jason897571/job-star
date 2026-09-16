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


# --- 重跑的作用域与旧动作的处置 ---
#
# clear_for_rescore 只清 scores/gate_results/jobs.status，动作表原封不动。
# 这在「重跑后结论没变」时是对的（不重复入队、不覆盖已发出的话术），但
# 「重跑后结论变了」时会留下一条按旧结论生成的招呼躺在待确认队列里——
# 面板不显示 jobs.status，人工看不出它已经作废，点一下确认就发出去了。


def test_run_score_job_id_scopes_the_scoring_not_just_the_clearing(conn, monkeypatch):
    """只限定清场范围而让打分全库跑，等于对人工没点名的岗位调 LLM 写话术、
    生成待确认动作——而命令行还在说「只重跑这一个」。"""
    from jobstar.llm import LLMSchemaError

    set_setting(conn, "score_threshold", 70)
    for job_id in ("broken", "other1", "other2"):
        _seed_scorable(conn, job_id)

    def boom(**kwargs):
        raise LLMSchemaError("坏了")

    monkeypatch.setattr("jobstar.pipeline.normalize", boom)
    monkeypatch.setattr("jobstar.pipeline.load_cards", lambda path: ())
    # 第一轮只让 broken 进去，other1/other2 保持「从没打过分」
    assert run_score(conn, job_id="broken").failed == 1

    normalized: list[str] = []
    pitched: list[str] = []

    def ok_normalize(**kwargs):
        normalized.append(kwargs["job_id"])
        return _req(kwargs["job_id"])

    def ok_pitch(**kwargs):
        pitched.append(kwargs["req"].job_id)
        return "开场白"

    monkeypatch.setattr("jobstar.pipeline.normalize", ok_normalize)
    monkeypatch.setattr("jobstar.pipeline.write_pitch", ok_pitch)
    monkeypatch.setattr(
        "jobstar.pipeline.score",
        lambda req, cards, weights: ScoreResult(req.job_id, 80.0, (), "v1"),
    )

    assert clear_for_rescore(conn, job_id="broken") == 1
    report = run_score(conn, job_id="broken")

    assert (report.scored, report.enqueued) == (1, 1)
    assert normalized == ["broken"], "没点名的岗位不该被归一化"
    assert pitched == ["broken"], "没点名的岗位不该被调 LLM 写话术"
    assert [r["job_id"] for r in conn.execute("SELECT job_id FROM actions")] == [
        "broken"
    ]


def test_clear_for_rescore_resets_job_status_to_new(conn, monkeypatch):
    """jobs.status 留在 scoring_failed 的话，面板 /api/health 的「打分失败
    N 条」会一直把一个已经清干净、正等着重跑的岗位算进去。"""
    from jobstar.llm import LLMSchemaError

    _seed_scorable(conn, "j1")

    def boom(**kwargs):
        raise LLMSchemaError("坏了")

    monkeypatch.setattr("jobstar.pipeline.normalize", boom)
    monkeypatch.setattr("jobstar.pipeline.load_cards", lambda path: ())
    run_score(conn)
    assert conn.execute(
        "SELECT status FROM jobs WHERE job_id='j1'"
    ).fetchone()["status"] == "scoring_failed"

    clear_for_rescore(conn)
    assert conn.execute(
        "SELECT status FROM jobs WHERE job_id='j1'"
    ).fetchone()["status"] == "new"


def _rescore_setup(conn, monkeypatch, *, second_total: float = 80.0):
    """先跑出一条 pending 招呼，再把桩换成第二轮的样子。返回动作 id。"""
    set_setting(conn, "score_threshold", 70)
    _seed_scorable(conn, "j1")
    monkeypatch.setattr("jobstar.pipeline.normalize", lambda **kw: _req(kw["job_id"]))
    monkeypatch.setattr("jobstar.pipeline.load_cards", lambda path: ())
    monkeypatch.setattr("jobstar.pipeline.write_pitch", lambda **kw: "第一版开场白")
    monkeypatch.setattr(
        "jobstar.pipeline.score",
        lambda req, cards, weights: ScoreResult(req.job_id, 90.0, (), "v1"),
    )
    assert run_score(conn).enqueued == 1
    action_id = list_by_status(conn, PENDING)[0]["id"]

    monkeypatch.setattr(
        "jobstar.pipeline.score",
        lambda req, cards, weights: ScoreResult(req.job_id, second_total, (), "v1"),
    )
    return action_id


def test_rescore_retracts_a_queued_greeting_the_gate_now_rejects(conn, monkeypatch):
    """人工把公司拉黑之后重跑：岗位被第二遍门禁刷掉，但队列里那条招呼
    仍然是 pending、卡片上还是旧话术，面板不显示 jobs.status——人工点一下
    确认就发给了刚被自己拉黑的公司。"""
    from jobstar import actions

    action_id = _rescore_setup(conn, monkeypatch)
    set_setting(
        conn,
        "gate_rules",
        {**get_setting(conn, "gate_rules"), "company_blacklist": ["A公司"]},
    )

    assert clear_for_rescore(conn) == 1
    report = run_score(conn)
    assert (report.gated_out, report.retracted) == (1, 1)

    row = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
    assert row["status"] == actions.SKIPPED
    assert "重跑后被门禁刷掉" in row["error"]
    assert "公司在黑名单：A公司" in row["error"]
    assert list_by_status(conn, PENDING) == [], "不该再出现在待确认队列里"


def test_rescore_retracts_a_queued_greeting_that_now_scores_below_threshold(
    conn, monkeypatch
):
    """调高阈值或改了权重之后分数掉下来：旧招呼是按 90 分写的，现在只有
    40 分，队列里却照旧摆着，卡片上的分数和话术出自两套互不相干的依据。"""
    from jobstar import actions

    action_id = _rescore_setup(conn, monkeypatch, second_total=40.0)

    assert clear_for_rescore(conn) == 1
    report = run_score(conn)
    assert (report.scored, report.enqueued, report.retracted) == (1, 0, 1)

    row = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
    assert row["status"] == actions.SKIPPED
    assert "低于阈值" in row["error"]


def test_retraction_is_reversible_by_the_human(conn, monkeypatch):
    """撤回的方向是安全的（只会少发不会多发），但不能是不可逆的——人工
    看过之后要能再点确认恢复。"""
    from jobstar import actions

    action_id = _rescore_setup(conn, monkeypatch, second_total=40.0)
    clear_for_rescore(conn)
    run_score(conn)

    actions.approve(conn, action_id)
    assert conn.execute(
        "SELECT status FROM actions WHERE id=?", (action_id,)
    ).fetchone()["status"] == actions.APPROVED


@pytest.mark.parametrize("already_sent", [True, False])
def test_rescore_never_retracts_an_action_that_may_already_have_been_sent(
    conn, monkeypatch, already_sent
):
    """sending 是「消息可能已经发出、但还没确认」的诚实中间态，sent 是确实
    发出去了。把它们改成「跳过」只会让台账和事实不符——而且 skipped 是允许
    再被批准的来源状态，等于给同一个真人开了重发的口子。"""
    from jobstar import actions

    action_id = _rescore_setup(conn, monkeypatch, second_total=40.0)
    actions.approve(conn, action_id)
    actions.mark_sending(conn, action_id)
    if already_sent:
        actions.mark_sent(conn, action_id)
    expected = actions.SENT if already_sent else actions.SENDING

    assert clear_for_rescore(conn) == 1
    assert run_score(conn).retracted == 0, f"{expected} 不该被撤回"
    assert conn.execute(
        "SELECT status FROM actions WHERE id=?", (action_id,)
    ).fetchone()["status"] == expected


def test_rescore_does_not_regenerate_a_pitch_for_an_action_that_already_exists(
    conn, monkeypatch
):
    """重跑结论没变时，enqueue 是 DO NOTHING——话术会被生成出来然后原样
    丢弃，白烧一次 LLM 调用；而 report.enqueued 还会把这次空操作计成入队，
    让人去面板找根本不存在的待确认项。"""
    action_id = _rescore_setup(conn, monkeypatch)

    pitches: list[str] = []
    monkeypatch.setattr(
        "jobstar.pipeline.write_pitch",
        lambda **kw: pitches.append(kw["req"].job_id) or "第二版开场白",
    )

    assert clear_for_rescore(conn) == 1
    report = run_score(conn)

    assert (report.scored, report.enqueued, report.already_queued) == (1, 0, 1)
    assert pitches == [], "队列里已有动作，不该再调一次 LLM 写话术"
    row = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
    assert json.loads(row["payload"])["greeting"] == "第一版开场白"


# --- 协作式停止 -------------------------------------------------------------
#
# 检查点只放在循环顶部。中途插一个会留下半截状态，最糟的是「分数写了、
# 话术没写」——scores 行一旦存在，这个岗位就落进 run_score 的吸收态，不加
# --rescore 再也回不来了。停在边界上则干净：没轮到的岗位没有 scores 行，
# 下次跑 score 会照常被捡起来。


def test_run_score_stops_at_a_job_boundary_and_keeps_what_it_finished(conn, monkeypatch):
    set_setting(conn, "score_threshold", 70)
    for job_id in ("j1", "j2", "j3", "j4"):
        _seed_scorable(conn, job_id)

    done: list[str] = []
    monkeypatch.setattr("jobstar.pipeline.load_cards", lambda path: ())
    monkeypatch.setattr("jobstar.pipeline.write_pitch", lambda **kw: "开场白")
    monkeypatch.setattr(
        "jobstar.pipeline.normalize",
        lambda **kw: (done.append(kw["job_id"]), _req(kw["job_id"]))[1],
    )
    monkeypatch.setattr(
        "jobstar.pipeline.score",
        lambda req, cards, weights: ScoreResult(req.job_id, 80.0, (), "v1"),
    )

    # 处理完两个之后请求停止
    report = run_score(conn, should_stop=lambda: len(done) >= 2)

    assert done == ["j1", "j2"], "第三个岗位根本不该开始"
    assert report.scored == 2
    assert (report.stopped, report.remaining) == (True, 2)

    scored = {r["job_id"] for r in conn.execute("SELECT job_id FROM scores")}
    assert scored == {"j1", "j2"}, "已经打完的分必须留下"
    assert {r["job_id"] for r in conn.execute("SELECT job_id FROM actions")} == {"j1", "j2"}


def test_jobs_skipped_by_a_stop_are_picked_up_by_the_next_run(conn, monkeypatch):
    """被停掉的那些岗位不需要 --rescore——它们没有 scores 行，本来就还在
    run_score 的取数范围里。这条是「停止不会烧掉岗位」的保证。"""
    for job_id in ("j1", "j2", "j3"):
        _seed_scorable(conn, job_id)

    done: list[str] = []
    monkeypatch.setattr("jobstar.pipeline.load_cards", lambda path: ())
    monkeypatch.setattr(
        "jobstar.pipeline.normalize",
        lambda **kw: (done.append(kw["job_id"]), _req(kw["job_id"]))[1],
    )
    monkeypatch.setattr(
        "jobstar.pipeline.score",
        lambda req, cards, weights: ScoreResult(req.job_id, 80.0, (), "v1"),
    )

    assert run_score(conn, should_stop=lambda: len(done) >= 1).remaining == 2
    assert run_score(conn).scored == 2, "剩下的两个不用 --rescore 就该被捡起来"
    assert done == ["j1", "j2", "j3"]


def test_not_passing_should_stop_keeps_the_old_behaviour(conn, monkeypatch):
    """CLI 不传 should_stop，行为必须和以前完全一样。"""
    for job_id in ("j1", "j2"):
        _seed_scorable(conn, job_id)
    monkeypatch.setattr("jobstar.pipeline.load_cards", lambda path: ())
    monkeypatch.setattr("jobstar.pipeline.normalize", lambda **kw: _req(kw["job_id"]))
    monkeypatch.setattr(
        "jobstar.pipeline.score",
        lambda req, cards, weights: ScoreResult(req.job_id, 80.0, (), "v1"),
    )
    report = run_score(conn)
    assert (report.scored, report.stopped, report.remaining) == (2, False, 0)


def test_run_collect_stops_before_opening_the_next_detail_page(conn):
    """采集的停止点同样在边界上：一个岗位要么门禁结果和详情页都落库，
    要么根本没开始，不会留下门禁写了、详情页没抓的半截状态。"""
    listed = [
        {
            "job_id": f"j{i}",
            "url": f"https://x/job_detail/j{i}~.html",
            "title": "后端",
            "company": "A",
            "city": "杭州",
            "salary_raw": "30-50K",
            "hr_name": "张",
            "tags": ["3-5年", "本科"],
        }
        for i in range(1, 5)
    ]
    fetched: list[str] = []

    report = run_collect(
        conn,
        keyword="后端",
        city_code="101210100",
        fetch_fn=lambda **kw: listed,
        detail_fn=lambda url: (fetched.append(url), {"raw_jd": "JD 全文"})[1],
        should_stop=lambda: len(fetched) >= 2,
    )

    assert len(fetched) == 2, "第三个岗位不该再被打开"
    assert (report.stopped, report.detail_fetched) == (True, 2)
    assert report.remaining == 2
    # 门禁结果是**全部**先算完的（run_list 阶段，纯计算不发请求），停止只
    # 掐断详情页那一段。这样被停掉的岗位仍然带着门禁结论留在候选列表里，
    # 人工回头能直接挑它们继续抓，不用重拉一遍列表页。
    assert conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 4
    assert conn.execute("SELECT COUNT(*) n FROM gate_results").fetchone()["n"] == 4


# --- 两阶段采集：先拉列表，人工挑，再抓详情 ---------------------------------
#
# 详情页请求是整条链路上唯一一个「按岗位数量线性增长、真正打在 Boss 上」的
# 动作。拆成两段是为了让人在中间插一脚，决定要对谁发请求。


def _listed(n=3, city="杭州"):
    return [
        {
            "job_id": f"j{i}",
            "url": f"https://x/job_detail/j{i}~.html",
            "title": f"后端{i}",
            "company": "A",
            "city": city,
            "salary_raw": "-K·薪",
            "hr_name": "",
            "tags": ["3-5年", "本科"],
        }
        for i in range(1, n + 1)
    ]


def test_run_list_never_opens_a_detail_page(conn):
    """这是拆分的全部意义：拉列表这一步必须零详情页请求。"""
    from jobstar.pipeline import run_list

    report = run_list(
        conn, keyword="后端", city_code="101210100", fetch_fn=lambda **kw: _listed(3)
    )
    assert (report.listed, report.new, report.candidates, report.gated_out) == (3, 3, 3, 0)
    assert conn.execute(
        "SELECT COUNT(*) n FROM jobs WHERE detail_fetched=1"
    ).fetchone()["n"] == 0
    # 门禁结论已经落库，候选列表就是靠它区分「待选」和「被刷掉」
    assert conn.execute("SELECT COUNT(*) n FROM gate_results").fetchone()["n"] == 3


def test_run_list_records_why_a_job_was_pre_gated_out(conn):
    from jobstar.pipeline import run_list

    report = run_list(
        conn, keyword="后端", city_code="1", fetch_fn=lambda **kw: _listed(2, city="北京")
    )
    assert (report.candidates, report.gated_out) == (0, 2)
    row = conn.execute("SELECT * FROM gate_results WHERE job_id='j1'").fetchone()
    assert row["passed"] == 0
    assert "北京" in row["reject_reason"], "候选列表要把理由显示给人看"


def test_run_details_only_opens_the_jobs_it_was_given(conn):
    from jobstar.pipeline import run_details, run_list

    run_list(conn, keyword="后端", city_code="1", fetch_fn=lambda **kw: _listed(5))
    opened: list[str] = []

    report = run_details(
        conn,
        ["j2", "j4"],
        detail_fn=lambda url: (opened.append(url), {"raw_jd": "JD 全文"})[1],
    )

    assert len(opened) == 2, "没点名的岗位一个都不该被打开"
    assert all("j2" in u or "j4" in u for u in opened)
    assert report.fetched == 2
    fetched = {
        r["job_id"]
        for r in conn.execute("SELECT job_id FROM jobs WHERE detail_fetched=1")
    }
    assert fetched == {"j2", "j4"}


def test_run_details_does_not_reopen_a_job_it_already_has(conn):
    """重复请求既浪费，也是白给的风控信号。"""
    from jobstar.pipeline import run_details, run_list

    run_list(conn, keyword="后端", city_code="1", fetch_fn=lambda **kw: _listed(2))
    opened: list[str] = []
    detail_fn = lambda url: (opened.append(url), {"raw_jd": "JD"})[1]  # noqa: E731

    run_details(conn, ["j1"], detail_fn=detail_fn)
    report = run_details(conn, ["j1", "j2"], detail_fn=detail_fn)

    assert len(opened) == 2, "j1 不该被打开第二次"
    assert (report.fetched, report.skipped) == (1, 1)


def test_run_details_can_be_stopped_between_jobs(conn):
    from jobstar.pipeline import run_details, run_list

    run_list(conn, keyword="后端", city_code="1", fetch_fn=lambda **kw: _listed(5))
    opened: list[str] = []

    report = run_details(
        conn,
        ["j1", "j2", "j3", "j4", "j5"],
        detail_fn=lambda url: (opened.append(url), {"raw_jd": "JD"})[1],
        should_stop=lambda: len(opened) >= 2,
    )
    assert len(opened) == 2
    assert (report.stopped, report.remaining, report.fetched) == (True, 3, 2)


def test_override_gate_flips_a_pre_gated_job_so_scoring_can_reach_it(conn):
    """人工在候选列表里手动勾了被刷掉的岗位 = 明确要覆盖规则。只抓详情页
    是不够的：run_score 要求 gate_results.passed=1，不翻门禁的话抓回来的
    详情永远等不到打分，勾选等于什么都没发生。"""
    from jobstar.pipeline import override_gate, run_list

    run_list(conn, keyword="后端", city_code="1", fetch_fn=lambda **kw: _listed(2, city="北京"))
    flipped = override_gate(conn, ["j1"])

    assert len(flipped) == 1
    assert "城市不在白名单：北京" in flipped[0], "翻转要把原因带出来写进日志"
    row = conn.execute("SELECT * FROM gate_results WHERE job_id='j1'").fetchone()
    assert (row["passed"], row["reject_reason"]) == (1, None)
    assert conn.execute(
        "SELECT status FROM jobs WHERE job_id='j1'"
    ).fetchone()["status"] == "new"
    # 没点名的那个一动不动
    assert conn.execute(
        "SELECT passed FROM gate_results WHERE job_id='j2'"
    ).fetchone()["passed"] == 0


def test_override_gate_leaves_jobs_that_passed_alone(conn):
    """已经通过的岗位不在覆盖范围里——否则重复勾选会把正常的门禁记录也
    改写一遍，白白抹掉信息。"""
    from jobstar.pipeline import override_gate, run_list

    run_list(conn, keyword="后端", city_code="1", fetch_fn=lambda **kw: _listed(2))
    assert override_gate(conn, ["j1", "j2"]) == []


def test_run_collect_still_does_the_whole_thing_in_one_go(conn):
    """命令行走的是一把梭：拉列表 → 预门禁 → 给所有幸存者抓详情。拆分成
    run_list + run_details 之后，`jobstar collect` 的行为不能变。"""
    listed = _listed(3, city="杭州") + [
        {**_listed(1, city="北京")[0], "job_id": "beijing", "title": "北京的岗位"}
    ]
    opened: list[str] = []

    report = run_collect(
        conn,
        keyword="后端",
        city_code="1",
        fetch_fn=lambda **kw: listed,
        detail_fn=lambda url: (opened.append(url), {"raw_jd": "JD"})[1],
    )

    assert (report.listed, report.new, report.gated_out) == (4, 4, 1)
    assert report.detail_fetched == 3, "被预门禁刷掉的那个不该被打开"
    assert len(opened) == 3
    assert not any("beijing" in u for u in opened)
