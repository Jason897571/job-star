import pytest

from jobstar.db import get_conn, init_db
from jobstar.models import JobRequirements
from jobstar.normalizer import (
    load_requirements,
    normalize,
    parse_salary_raw,
    save_requirements,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("20-35K", (20, 35)),
        ("20-35K·13薪", (20, 35)),
        ("20-35K·16薪", (20, 35)),
        ("30K-50K", (30, 50)),
        ("15K", (15, 15)),
        ("面议", (None, None)),
        ("", (None, None)),
        (None, (None, None)),
        ("1.5-2万", (15, 20)),
        ("8-12万·年", (None, None)),
    ],
)
def test_parse_salary_raw(raw, expected):
    assert parse_salary_raw(raw) == expected


def _stub_llm(monkeypatch, payload):
    calls = []

    def fake(*, system, user, tier):
        calls.append({"system": system, "user": user, "tier": tier})
        return payload

    monkeypatch.setattr("jobstar.normalizer.call_json", fake)
    return calls


def test_normalize_builds_requirements(monkeypatch):
    calls = _stub_llm(
        monkeypatch,
        {
            "city": "杭州",
            "degree": "本科",
            "years_min": 3,
            "years_max": 5,
            "salary_min": 25,
            "salary_max": 40,
            "skills": ["Python", "FastAPI", "RAG"],
            "industry": "人工智能",
            "company_size": "100-499人",
            "category": "后端开发",
        },
    )
    req = normalize(job_id="j1", title="后端工程师", raw_jd="负责……")
    assert isinstance(req, JobRequirements)
    assert req.job_id == "j1"
    assert req.skills == ("Python", "FastAPI", "RAG")
    assert req.years_min == 3
    assert calls[0]["tier"] == "fast", "归一化是结构化抽取，用便宜档"


def test_normalize_prefers_hints_over_llm_guess(monkeypatch):
    """列表页已经给了城市和薪资，比让模型从 JD 里猜更可靠。"""
    _stub_llm(monkeypatch, {"city": "北京", "salary_min": 10, "salary_max": 15})
    req = normalize(
        job_id="j1",
        title="t",
        raw_jd="jd",
        city_hint="杭州",
        salary_hint="30-50K",
    )
    assert req.city == "杭州"
    assert (req.salary_min, req.salary_max) == (30, 50)


def test_normalize_tolerates_missing_fields(monkeypatch):
    _stub_llm(monkeypatch, {"city": "杭州"})
    req = normalize(job_id="j1", title="t", raw_jd="jd")
    assert req.degree is None
    assert req.skills == ()


def test_normalize_coerces_bad_types(monkeypatch):
    """模型偶尔把年限写成字符串或把 skills 写成字符串。"""
    _stub_llm(
        monkeypatch,
        {"years_min": "3", "years_max": "五", "skills": "Python、Go"},
    )
    req = normalize(job_id="j1", title="t", raw_jd="jd")
    assert req.years_min == 3
    assert req.years_max is None
    assert req.skills == ("Python、Go",)


def test_normalize_injects_jd_into_prompt(monkeypatch):
    calls = _stub_llm(monkeypatch, {})
    normalize(job_id="j1", title="高级后端", raw_jd="要求熟悉 LangGraph")
    assert "LangGraph" in calls[0]["user"]
    assert "高级后端" in calls[0]["user"]


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    _stub_llm(monkeypatch, {"city": "杭州", "skills": ["Go"], "salary_min": 30})
    req = normalize(job_id="j1", title="t", raw_jd="jd")
    save_requirements(conn, req)
    save_requirements(conn, req)  # 重跑不炸
    loaded = load_requirements(conn, "j1")
    assert loaded == req


def test_load_requirements_returns_none_when_absent(tmp_path):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    assert load_requirements(conn, "nope") is None
