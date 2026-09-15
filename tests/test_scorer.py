import pytest

from jobstar.config import SETTING_DEFAULTS
from jobstar.db import get_conn, init_db
from jobstar.models import CapabilityCard, JobRequirements, Strength
from jobstar.scorer import (
    SCORER_VERSION,
    WEAK_EVIDENCE_CAP,
    load_score,
    save_failure,
    save_score,
    score,
)

WEIGHTS = SETTING_DEFAULTS["dimension_weights"]

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
        id="cap-k8s",
        capability="Kubernetes",
        synonyms=("k8s",),
        strength=Strength.WEAK,
        project="内部工具部署",
        metrics=(),
        depth="跑通过 deployment",
        resume_versions=(),
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
    skills=("RAG", "Kubernetes"),
    industry="人工智能",
    company_size="100-499人",
    category="后端开发",
)


def _dims(**overrides):
    base = {
        name: {"score": 80, "card_ids": ["cap-rag"], "reason": "命中", "gap": "无"}
        for name in WEIGHTS
    }
    base.update(overrides)
    return {"dimensions": base}


def _stub(monkeypatch, payload):
    calls = []

    def fake(*, system, user, tier):
        calls.append({"system": system, "user": user, "tier": tier})
        return payload

    monkeypatch.setattr("jobstar.scorer.call_json", fake)
    return calls


def test_score_computes_weighted_total(monkeypatch):
    _stub(monkeypatch, _dims())
    result = score(REQ, CARDS, WEIGHTS)
    assert result.total == pytest.approx(80.0)
    assert result.scorer_version == SCORER_VERSION
    assert {d.name for d in result.dimensions} == set(WEIGHTS)


def test_score_uses_strong_tier(monkeypatch):
    calls = _stub(monkeypatch, _dims())
    score(REQ, CARDS, WEIGHTS)
    assert calls[0]["tier"] == "strong"


def test_dimension_without_evidence_is_zeroed(monkeypatch):
    """设计文档 §7：某维度无证据卡片则记 0 分。这条在代码里强制，不靠 prompt。"""
    _stub(
        monkeypatch,
        _dims(skills={"score": 95, "card_ids": [], "reason": "感觉很搭", "gap": ""}),
    )
    result = score(REQ, CARDS, WEIGHTS)
    skills = next(d for d in result.dimensions if d.name == "skills")
    assert skills.score == 0.0


def test_unknown_card_id_is_dropped_and_zeroes_dimension(monkeypatch):
    """模型编了一个不存在的卡片 id，等价于无证据。"""
    _stub(
        monkeypatch,
        _dims(
            industry={
                "score": 90,
                "card_ids": ["cap-does-not-exist"],
                "reason": "编的",
                "gap": "",
            }
        ),
    )
    result = score(REQ, CARDS, WEIGHTS)
    industry = next(d for d in result.dimensions if d.name == "industry")
    assert industry.card_ids == ()
    assert industry.score == 0.0


def test_weak_only_evidence_is_capped(monkeypatch):
    """弱证据卡片不得作为某维度的主要匹配依据，只能是加分项。"""
    _stub(
        monkeypatch,
        _dims(
            skills={"score": 95, "card_ids": ["cap-k8s"], "reason": "会 k8s", "gap": ""}
        ),
    )
    result = score(REQ, CARDS, WEIGHTS)
    skills = next(d for d in result.dimensions if d.name == "skills")
    assert skills.score == WEAK_EVIDENCE_CAP


def test_weak_plus_strong_evidence_is_not_capped(monkeypatch):
    _stub(
        monkeypatch,
        _dims(
            skills={
                "score": 95,
                "card_ids": ["cap-k8s", "cap-rag"],
                "reason": "两项都命中",
                "gap": "",
            }
        ),
    )
    result = score(REQ, CARDS, WEIGHTS)
    skills = next(d for d in result.dimensions if d.name == "skills")
    assert skills.score == 95.0


def test_missing_dimension_becomes_zero(monkeypatch):
    payload = _dims()
    del payload["dimensions"]["bonus"]
    _stub(monkeypatch, payload)
    result = score(REQ, CARDS, WEIGHTS)
    bonus = next(d for d in result.dimensions if d.name == "bonus")
    assert bonus.score == 0.0
    assert "缺失" in bonus.gap


def test_out_of_range_scores_are_clamped(monkeypatch):
    _stub(
        monkeypatch,
        _dims(
            skills={"score": 130, "card_ids": ["cap-rag"], "reason": "", "gap": ""},
            years={"score": -20, "card_ids": ["cap-rag"], "reason": "", "gap": ""},
        ),
    )
    result = score(REQ, CARDS, WEIGHTS)
    by_name = {d.name: d.score for d in result.dimensions}
    assert by_name["skills"] == 100.0
    assert by_name["years"] == 0.0


def test_prompt_contains_cards_and_jd(monkeypatch):
    calls = _stub(monkeypatch, _dims())
    score(REQ, CARDS, WEIGHTS)
    user = calls[0]["user"]
    assert "cap-rag" in user
    assert "检索增强" in user, "同义表述必须进 prompt —— 它是语义对齐的主力"
    assert "后端开发" in user


def test_prompt_states_the_evidence_rule(monkeypatch):
    calls = _stub(monkeypatch, _dims())
    score(REQ, CARDS, WEIGHTS)
    assert "证据" in calls[0]["system"]
    assert "弱" in calls[0]["system"]


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    _stub(monkeypatch, _dims())
    result = score(REQ, CARDS, WEIGHTS)
    save_score(conn, result)
    save_score(conn, result)  # 重跑不炸
    loaded = load_score(conn, "j1")
    assert loaded == result


def test_save_failure_marks_job_scoring_failed(tmp_path):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    conn.execute(
        "INSERT INTO jobs (platform, job_id, title, company, raw_jd) "
        "VALUES ('boss', 'j1', 't', 'c', 'jd')"
    )
    conn.commit()
    save_failure(conn, "j1", "重试一次后仍不是合法 JSON")
    row = conn.execute("SELECT * FROM scores WHERE job_id='j1'").fetchone()
    assert row["total"] is None
    assert "JSON" in row["error"]
    job = conn.execute("SELECT status FROM jobs WHERE job_id='j1'").fetchone()
    assert job["status"] == "scoring_failed"


def test_load_score_ignores_failed_rows(tmp_path):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    save_failure(conn, "j1", "boom")
    assert load_score(conn, "j1") is None
