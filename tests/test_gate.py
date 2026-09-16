import pytest

from jobstar.config import SETTING_DEFAULTS
from jobstar.gate import check, haversine_km, save_result
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


# --- 区域与距离 ---
#
# 两者生效的时机完全不同，这是整块功能的关键约束：
#   区县来自列表页 → **抓详情页之前**就知道 → 按它筛选能省下页面请求；
#   坐标来自详情页 → 只能在第二遍门禁里用 → 省的是打分 token，省不了请求。


def _req(city="杭州"):
    return JobRequirements(
        "j1", city, None, None, None, None, None, (), None, None, None
    )


def test_district_whitelist_filters_before_any_detail_request():
    rules = {"district_whitelist": ["滨江区", "西湖区"]}
    assert check(_req(), rules, district="滨江区").passed
    result = check(_req(), rules, district="余杭区")
    assert not result.passed
    assert "余杭区" in result.reject_reason


def test_district_blacklist_only_kills_exact_matches():
    rules = {"district_blacklist": ["余杭区"]}
    assert not check(_req(), rules, district="余杭区").passed
    assert check(_req(), rules, district="滨江区").passed


def test_unknown_district_is_let_through():
    """有的岗位只给到市（`杭州`，没有区段）。门禁的既定原则是字段缺失一律
    放过，交给打分器看原文——按区县把这类岗位全刷掉是过严的。"""
    rules = {"district_whitelist": ["滨江区"]}
    assert check(_req(), rules, district="").passed
    assert check(_req(), rules, district=None).passed


def test_commute_limit_rejects_jobs_that_are_too_far():
    rules = {"max_commute_km": 15}
    assert check(_req(), rules, distance_km=8.2).passed
    result = check(_req(), rules, distance_km=31.7)
    assert not result.passed
    assert "31.7" in result.reject_reason and "15" in result.reject_reason


def test_commute_limit_does_nothing_when_distance_is_unknown():
    """没设家的位置、或这条岗位没抓到坐标时 distance 是 None。这时必须放过——
    把「不知道多远」当成「太远」会悄悄刷掉一大批岗位。"""
    rules = {"max_commute_km": 5}
    assert check(_req(), rules, distance_km=None).passed


def test_haversine_matches_a_known_distance():
    """杭州东站 (120.213,30.291) → 滨江区长河 (120.212,30.206)，约 9.4 公里。"""
    d = haversine_km((120.213, 30.291), (120.212, 30.206))
    assert 9.0 < d < 10.0, d
    assert haversine_km((120.2, 30.2), (120.2, 30.2)) == 0
