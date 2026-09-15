import pytest

from jobstar.config import SETTING_DEFAULTS
from jobstar.gate import check, save_result
from jobstar.db import get_conn, init_db
from jobstar.models import JobRequirements

RULES = SETTING_DEFAULTS["gate_rules"]


def make_req(**overrides) -> JobRequirements:
    base = dict(
        job_id="j1",
        city="杭州",
        degree="本科",
        years_min=3,
        years_max=5,
        salary_min=30,
        salary_max=50,
        skills=("Python",),
        industry="互联网",
        company_size="100-499人",
        category="后端开发",
    )
    base.update(overrides)
    return JobRequirements(**base)


def test_passes_a_matching_job():
    assert check(make_req(), RULES).passed is True


def test_rejects_city_outside_whitelist():
    result = check(make_req(city="北京"), RULES)
    assert result.passed is False
    assert "城市" in result.reject_reason


def test_rejects_salary_below_floor():
    """薪资上限低于门槛才算不合格 —— 20-24K 上限 24 < 25，拒。"""
    result = check(make_req(salary_min=20, salary_max=24), RULES)
    assert result.passed is False
    assert "薪资" in result.reject_reason


def test_accepts_salary_whose_ceiling_clears_floor():
    """18-35K 上限够得着，不拒 —— 谈薪空间留给人工判断。"""
    assert check(make_req(salary_min=18, salary_max=35), RULES).passed is True


def test_rejects_years_above_ceiling():
    result = check(make_req(years_min=12, years_max=None), RULES)
    assert result.passed is False
    assert "年限" in result.reject_reason


def test_rejects_blacklisted_company():
    rules = {**RULES, "company_blacklist": ["某黑名单公司"]}
    result = check(make_req(), rules, company="某黑名单公司杭州分部")
    assert result.passed is False
    assert "黑名单" in result.reject_reason


def test_degree_not_enforced_when_soft():
    """degree_hard 关着时，学历不参与判断。"""
    rules = {**RULES, "degree_hard": False}
    assert check(make_req(degree="博士"), rules).passed is True


def test_degree_enforced_when_hard():
    rules = {**RULES, "degree_hard": True, "my_degree": "硕士"}
    assert check(make_req(degree="本科"), rules).passed is True
    result = check(make_req(degree="博士"), rules)
    assert result.passed is False
    assert "学历" in result.reject_reason


def test_unknown_fields_do_not_reject():
    """归一化失败留下的 None 不应当被当成不合格 —— 宁可放过去让打分器看。"""
    req = make_req(city=None, salary_min=None, salary_max=None, years_min=None)
    assert check(req, RULES).passed is True


def test_reject_reason_is_none_when_passed():
    assert check(make_req(), RULES).reject_reason is None


def test_save_result_persists_and_is_idempotent(tmp_path):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    result = check(make_req(city="北京"), RULES)
    save_result(conn, "j1", result)
    save_result(conn, "j1", result)  # 重跑不应炸
    row = conn.execute("SELECT * FROM gate_results WHERE job_id='j1'").fetchone()
    assert row["passed"] == 0
    assert "城市" in row["reject_reason"]
