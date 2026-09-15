import pytest

from jobstar.models import (
    CapabilityCard,
    DimensionScore,
    JobRequirements,
    ScoreResult,
    Strength,
)
from jobstar.pitch import MAX_CHARS, write_pitch

CARDS = (
    CapabilityCard(
        id="cap-rag",
        capability="RAG",
        synonyms=("检索增强",),
        strength=Strength.STRONG,
        project="电信 OCR + RAG",
        metrics=("准确率",),
        depth="分块策略、召回优化",
        resume_versions=("后端AI版",),
    ),
    CapabilityCard(
        id="cap-gw",
        capability="大模型网关",
        synonyms=("LLM Gateway",),
        strength=Strength.STRONG,
        project="大模型聚合平台",
        metrics=("日均 token",),
        depth="多供应商聚合、限流、计费",
        resume_versions=("后端AI版",),
    ),
)

REQ = JobRequirements(
    job_id="j1",
    city="杭州",
    degree="本科",
    years_min=3,
    years_max=5,
    salary_min=30,
    salary_max=50,
    skills=("RAG", "LangGraph"),
    industry="人工智能",
    company_size="100-499人",
    category="后端开发",
)

RESULT = ScoreResult(
    job_id="j1",
    total=82.0,
    dimensions=(
        DimensionScore("skills", 90, ("cap-rag",), "RAG 命中", "缺 LangGraph"),
        DimensionScore("industry", 85, ("cap-gw",), "同为 AI 平台", "无"),
        DimensionScore("duties", 80, ("cap-gw",), "职责重合", "无"),
        DimensionScore("years", 70, ("cap-rag",), "年限够", "无"),
        DimensionScore("bonus", 0, (), "", "无证据卡片支撑"),
    ),
    scorer_version="v1",
)


def _stub(monkeypatch, text):
    calls = []

    def fake(*, system, user, tier):
        calls.append({"system": system, "user": user, "tier": tier})
        return {"greeting": text}

    monkeypatch.setattr("jobstar.pitch.call_json", fake)
    return calls


def test_returns_greeting_text(monkeypatch):
    _stub(monkeypatch, "您好，看到贵司在做 RAG 检索问答……")
    out = write_pitch(req=REQ, result=RESULT, cards=CARDS, title="AI 后端", company="某司")
    assert out.startswith("您好")


def test_uses_strong_tier(monkeypatch):
    calls = _stub(monkeypatch, "文案")
    write_pitch(req=REQ, result=RESULT, cards=CARDS, title="AI 后端", company="某司")
    assert calls[0]["tier"] == "strong"


def test_prompt_only_includes_cards_that_were_actually_cited(monkeypatch):
    """话术只能拿打分时命中的卡片说事，不能翻出没命中的去吹。"""
    calls = _stub(monkeypatch, "文案")
    write_pitch(req=REQ, result=RESULT, cards=CARDS, title="AI 后端", company="某司")
    assert "cap-rag" in calls[0]["user"]
    assert "cap-gw" in calls[0]["user"]


def test_prompt_excludes_uncited_cards(monkeypatch):
    calls = _stub(monkeypatch, "文案")
    result = ScoreResult(
        job_id="j1",
        total=50,
        dimensions=(DimensionScore("skills", 80, ("cap-rag",), "r", "g"),),
        scorer_version="v1",
    )
    write_pitch(req=REQ, result=result, cards=CARDS, title="AI 后端", company="某司")
    assert "cap-rag" in calls[0]["user"]
    assert "cap-gw" not in calls[0]["user"]


def test_prompt_carries_jd_keywords_as_hooks(monkeypatch):
    """设计文档 §4.6：要引用 JD 中的具体关键词作为钩子。"""
    calls = _stub(monkeypatch, "文案")
    write_pitch(req=REQ, result=RESULT, cards=CARDS, title="AI 后端", company="某司")
    user = calls[0]["user"]
    assert "LangGraph" in user
    assert "某司" in user


def test_system_prompt_forbids_template_openings(monkeypatch):
    """前 20 字相似度是风控信号之一。"""
    calls = _stub(monkeypatch, "文案")
    write_pitch(req=REQ, result=RESULT, cards=CARDS, title="AI 后端", company="某司")
    assert "开头" in calls[0]["system"]


def test_truncates_overlong_output(monkeypatch):
    _stub(monkeypatch, "啊" * (MAX_CHARS + 200))
    out = write_pitch(req=REQ, result=RESULT, cards=CARDS, title="t", company="c")
    assert len(out) <= MAX_CHARS


def test_strips_surrounding_quotes(monkeypatch):
    _stub(monkeypatch, '"您好，我对这个岗位很感兴趣"')
    out = write_pitch(req=REQ, result=RESULT, cards=CARDS, title="t", company="c")
    assert not out.startswith('"')


def test_raises_when_model_returns_empty(monkeypatch):
    _stub(monkeypatch, "   ")
    with pytest.raises(ValueError, match="空"):
        write_pitch(req=REQ, result=RESULT, cards=CARDS, title="t", company="c")
