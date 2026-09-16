import json
import subprocess

import pytest

from jobstar.collector import boss
from jobstar.collector.boss import (
    CollectError,
    LoginRequired,
    _looks_like_empty_result,
    fetch_detail,
    fetch_list,
    run_script,
    save_detail,
)
from jobstar.db import get_conn, init_db


def test_run_script_wraps_timeout_into_collect_error(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/browser-harness")

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="browser-harness", timeout=180)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(CollectError):
        run_script("print(1)")


def test_looks_like_empty_result_matches_known_markers():
    assert _looks_like_empty_result("换个搜索词试试，或者看看推荐职位")
    assert not _looks_like_empty_result("后端开发工程师 15-30K")


def _seed_job(conn, *, platform, job_id):
    conn.execute(
        "INSERT INTO jobs (platform, job_id, title, company, raw_jd, hr_name, salary_raw) "
        "VALUES (?, ?, 'title', 'company', '', '', '')",
        (platform, job_id),
    )
    conn.commit()


def test_save_detail_scopes_update_by_platform(tmp_path):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    _seed_job(conn, platform="boss", job_id="shared-id")
    _seed_job(conn, platform="other", job_id="shared-id")

    save_detail(conn, "shared-id", {"raw_jd": "真实 JD", "hr_name": "薛先生", "salary_raw": "15-30K"})

    boss_row = conn.execute(
        "SELECT raw_jd, hr_name, salary_raw FROM jobs WHERE platform = 'boss' AND job_id = 'shared-id'"
    ).fetchone()
    other_row = conn.execute(
        "SELECT raw_jd, hr_name, salary_raw FROM jobs WHERE platform = 'other' AND job_id = 'shared-id'"
    ).fetchone()

    assert boss_row["raw_jd"] == "真实 JD"
    assert boss_row["hr_name"] == "薛先生"
    assert boss_row["salary_raw"] == "15-30K"
    assert other_row["raw_jd"] == ""
    assert other_row["hr_name"] == ""
    assert other_row["salary_raw"] == ""


def test_save_detail_does_not_overwrite_real_values_with_empty_string(tmp_path):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    conn.execute(
        "INSERT INTO jobs (platform, job_id, title, company, raw_jd, hr_name, salary_raw) "
        "VALUES ('boss', 'j1', 'title', 'company', '旧 JD', '薛先生', '15-30K')"
    )
    conn.commit()

    save_detail(conn, "j1", {"raw_jd": "新 JD", "hr_name": "", "salary_raw": ""})

    row = conn.execute(
        "SELECT raw_jd, hr_name, salary_raw FROM jobs WHERE job_id = 'j1'"
    ).fetchone()
    assert row["raw_jd"] == "新 JD"
    assert row["hr_name"] == "薛先生"
    assert row["salary_raw"] == "15-30K"


# --- Minor 11：fetch_list/fetch_detail 之前完全没有测试覆盖，§7 承诺的
# 「登录墙检测」「零卡片→CollectError」「坏 JSON→CollectError+dump_failure」
# 「空 JD→CollectError」全部没有回归测试钉住；下面用 monkeypatch 掉
# run_script 来覆盖这几条，同时钉住 fix round 2（Minor 2）的行为：JD 正文
# 里出现裸词 "login" 不该被判成登录墙，真正的登录墙特征串仍然要生效。


def _list_stdout(*, page_info: str = "https://x/web/geek/jobs?query=x", data: dict) -> str:
    return (
        "###FOUND###True\n"
        f"###PAGEINFO###{{'url': '{page_info}'}}\n"
        "###DATA###" + json.dumps(data, ensure_ascii=False)
    )


def _detail_stdout(*, page_info: str = "https://x/job_detail/j1~.html", data: dict) -> str:
    return (
        "###FOUND###True\n"
        f"###PAGEINFO###{{'url': '{page_info}'}}\n"
        "###DATA###" + json.dumps(data, ensure_ascii=False)
    )


def _fake_item(job_id: str = "j1") -> dict:
    return {
        "url": f"/job_detail/{job_id}~.html",
        "title": "后端开发工程师",
        "company": "某某科技",
        "city": "杭州",
        "salary": "-K",
        "hr": "",
        "tags": ["3-5年", "本科"],
    }


def test_fetch_list_returns_normalized_deduped_items(monkeypatch):
    out = _list_stdout(data={"items": [_fake_item("j1"), _fake_item("j1")], "body_snippet": "后端开发 15-30K"})
    monkeypatch.setattr(boss, "run_script", lambda script, **kw: out)
    items = fetch_list(keyword="后端", city_code="101210100")
    assert [i["job_id"] for i in items] == ["j1"]


def test_fetch_list_raises_login_required_on_page_info_redirect(monkeypatch):
    """URL/page_info() 这一侧仍然要能识别跳转到登录页——裸词 "login" 在这里
    是可靠信号（只可能来自跳转到的地址本身）。"""
    out = _list_stdout(
        page_info="https://www.zhipin.com/web/user/?ka=header-login",
        data={"items": [], "body_snippet": ""},
    )
    monkeypatch.setattr(boss, "run_script", lambda script, **kw: out)
    with pytest.raises(LoginRequired):
        fetch_list(keyword="后端", city_code="101210100")


def test_fetch_list_raises_login_required_on_chinese_marker_in_body(monkeypatch):
    out = _list_stdout(data={"items": [], "body_snippet": "请先登录后查看更多职位"})
    monkeypatch.setattr(boss, "run_script", lambda script, **kw: out)
    with pytest.raises(LoginRequired):
        fetch_list(keyword="后端", city_code="101210100")


def test_fetch_list_body_with_bare_login_word_is_not_a_login_wall(monkeypatch):
    """fix round 2（Minor 2）：JD/列表正文里出现裸词 "login"（例如 SSO/OAuth
    login 相关的职位描述）不该被误判成登录态失效——这条列表页请求应该正常
    返回结果，而不是抛 LoginRequired 把整轮采集中止掉。"""
    out = _list_stdout(
        data={
            "items": [_fake_item("j1")],
            "body_snippet": "负责单点登录 login 模块的设计与实现，熟悉 OAuth login 流程",
        }
    )
    monkeypatch.setattr(boss, "run_script", lambda script, **kw: out)
    items = fetch_list(keyword="后端", city_code="101210100")
    assert [i["job_id"] for i in items] == ["j1"]


def test_fetch_list_zero_cards_raises_collect_error_and_dumps_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(boss, "FAILURE_DIR", tmp_path / "collector_failures")
    out = _list_stdout(data={"items": [], "body_snippet": "无关紧要的正文"})
    monkeypatch.setattr(boss, "run_script", lambda script, **kw: out)
    with pytest.raises(CollectError) as exc_info:
        fetch_list(keyword="后端", city_code="101210100")
    message = str(exc_info.value)
    assert "0 条卡片" in message
    dumped = list((tmp_path / "collector_failures").glob("list-p1-zero.txt"))
    assert len(dumped) == 1, "dump_failure 必须真的落盘，且路径要出现在异常信息里"
    assert str(dumped[0]) in message


def test_fetch_list_bad_json_raises_collect_error_and_dumps_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(boss, "FAILURE_DIR", tmp_path / "collector_failures")
    out = "###FOUND###True\n###PAGEINFO###{}\n###DATA###不是 JSON"
    monkeypatch.setattr(boss, "run_script", lambda script, **kw: out)
    with pytest.raises(CollectError) as exc_info:
        fetch_list(keyword="后端", city_code="101210100")
    message = str(exc_info.value)
    assert "不是 JSON" in message
    dumped = list((tmp_path / "collector_failures").glob("list-p1-badjson.txt"))
    assert len(dumped) == 1
    assert str(dumped[0]) in message


def test_fetch_detail_returns_full_payload_on_success(monkeypatch):
    out = _detail_stdout(
        data={
            "raw_jd": "负责后端服务开发",
            "company_info": "某某科技",
            "hr_name": "薛先生",
            "salary_raw": "15-30K·14薪",
            "body_snippet": "负责后端服务开发",
        }
    )
    monkeypatch.setattr(boss, "run_script", lambda script, **kw: out)
    payload = fetch_detail("https://x/job_detail/j1~.html")
    assert payload["raw_jd"] == "负责后端服务开发"
    assert payload["hr_name"] == "薛先生"


def test_fetch_detail_raises_login_required_on_page_info_redirect(monkeypatch):
    out = _detail_stdout(
        page_info="https://www.zhipin.com/web/user/?ka=header-login",
        data={"raw_jd": "", "body_snippet": ""},
    )
    monkeypatch.setattr(boss, "run_script", lambda script, **kw: out)
    with pytest.raises(LoginRequired):
        fetch_detail("https://x/job_detail/j1~.html")


def test_fetch_detail_raises_login_required_on_chinese_marker_in_body(monkeypatch):
    out = _detail_stdout(data={"raw_jd": "", "body_snippet": "请先登录才能查看完整职位描述"})
    monkeypatch.setattr(boss, "run_script", lambda script, **kw: out)
    with pytest.raises(LoginRequired):
        fetch_detail("https://x/job_detail/j1~.html")


def test_fetch_detail_body_with_bare_login_word_is_not_a_login_wall(monkeypatch):
    """fix round 2（Minor 2）核心场景：详情页 JD 正文含裸词 "login"（真实的
    后端/前端岗位描述完全可能写到 SSO/OAuth login）不该触发 LoginRequired，
    这条详情页请求应该正常返回抓到的 JD。"""
    jd = "负责公司内部系统的单点登录（SSO/Login）模块建设，熟悉 OAuth login 协议"
    out = _detail_stdout(data={"raw_jd": jd, "body_snippet": jd})
    monkeypatch.setattr(boss, "run_script", lambda script, **kw: out)
    payload = fetch_detail("https://x/job_detail/j1~.html")
    assert payload["raw_jd"] == jd


def test_fetch_detail_bad_json_raises_collect_error_and_dumps_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(boss, "FAILURE_DIR", tmp_path / "collector_failures")
    out = "###FOUND###True\n###PAGEINFO###{}\n###DATA###不是 JSON"
    monkeypatch.setattr(boss, "run_script", lambda script, **kw: out)
    with pytest.raises(CollectError) as exc_info:
        fetch_detail("https://x/job_detail/j1~.html")
    message = str(exc_info.value)
    assert "不是 JSON" in message
    dumped = list((tmp_path / "collector_failures").glob("detail-j1-badjson.txt"))
    assert len(dumped) == 1
    assert str(dumped[0]) in message


def test_fetch_detail_empty_jd_raises_collect_error_and_dumps_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(boss, "FAILURE_DIR", tmp_path / "collector_failures")
    out = _detail_stdout(data={"raw_jd": "   ", "body_snippet": "无关紧要"})
    monkeypatch.setattr(boss, "run_script", lambda script, **kw: out)
    with pytest.raises(CollectError) as exc_info:
        fetch_detail("https://x/job_detail/j1~.html")
    message = str(exc_info.value)
    assert "JD 正文" in message
    dumped = list((tmp_path / "collector_failures").glob("detail-j1-empty-jd.txt"))
    assert len(dumped) == 1
    assert str(dumped[0]) in message
