from pathlib import Path

import pytest

from jobstar.evidence import (
    CardValidationError,
    FullDumpStore,
    cards_to_prompt_block,
    load_cards,
)
from jobstar.models import Strength

FIXTURES = Path(__file__).parent / "fixtures"


def test_load_cards_maps_chinese_keys_to_ascii_fields():
    cards = load_cards(FIXTURES / "cards_ok.yaml")
    assert len(cards) == 2
    first = cards[0]
    assert first.id == "cap-rag-telecom"
    assert first.capability == "RAG / 文档智能问答"
    assert "向量检索" in first.synonyms
    assert first.strength is Strength.STRONG
    assert first.project == "电信扫描件 OCR + RAG"
    assert first.resume_versions == ("后端AI版", "财务AI专家版")


def test_load_cards_rejects_invalid_strength():
    with pytest.raises(CardValidationError, match="证据强度"):
        load_cards(FIXTURES / "cards_bad.yaml")


def test_load_cards_rejects_duplicate_ids(tmp_path):
    p = tmp_path / "dup.yaml"
    p.write_text(
        "- id: a\n  能力: x\n  同义表述: []\n  证据强度: 强\n  项目: p\n"
        "  可量化: []\n  可讲深度: d\n  关联简历版本: []\n"
        "- id: a\n  能力: y\n  同义表述: []\n  证据强度: 中\n  项目: p\n"
        "  可量化: []\n  可讲深度: d\n  关联简历版本: []\n",
        encoding="utf-8",
    )
    with pytest.raises(CardValidationError, match="重复"):
        load_cards(p)


def test_load_cards_rejects_missing_required_key(tmp_path):
    p = tmp_path / "missing.yaml"
    p.write_text("- id: a\n  能力: x\n", encoding="utf-8")
    with pytest.raises(CardValidationError, match="缺少"):
        load_cards(p)


def test_load_cards_rejects_none_list_item(tmp_path):
    p = tmp_path / "none_item.yaml"
    p.write_text(
        "- id: a\n  能力: x\n  同义表述: []\n  证据强度: 强\n  项目: p\n"
        "  可量化: []\n  可讲深度: d\n  关联简历版本: []\n"
        "-\n",
        encoding="utf-8",
    )
    with pytest.raises(CardValidationError, match="不是合法的映射结构"):
        load_cards(p)


def test_load_cards_rejects_scalar_list_item(tmp_path):
    p = tmp_path / "scalar_item.yaml"
    p.write_text(
        "- id: a\n  能力: x\n  同义表述: []\n  证据强度: 强\n  项目: p\n"
        "  可量化: []\n  可讲深度: d\n  关联简历版本: []\n"
        "- 123\n",
        encoding="utf-8",
    )
    with pytest.raises(CardValidationError, match="不是合法的映射结构"):
        load_cards(p)


def test_load_cards_rejects_bare_string_synonyms(tmp_path):
    p = tmp_path / "bare_synonyms.yaml"
    p.write_text(
        "- id: a\n  能力: x\n  同义表述: 检索增强\n  证据强度: 强\n  项目: p\n"
        "  可量化: []\n  可讲深度: d\n  关联简历版本: []\n",
        encoding="utf-8",
    )
    with pytest.raises(CardValidationError, match="同义表述"):
        load_cards(p)


def test_load_cards_rejects_unquoted_numeric_synonym(tmp_path):
    p = tmp_path / "numeric_synonym.yaml"
    p.write_text(
        "- id: a\n  能力: x\n  同义表述: [429, RAG]\n  证据强度: 强\n  项目: p\n"
        "  可量化: []\n  可讲深度: d\n  关联简历版本: []\n",
        encoding="utf-8",
    )
    with pytest.raises(CardValidationError, match="同义表述"):
        load_cards(p)


def test_load_cards_rejects_blank_scalar_field(tmp_path):
    p = tmp_path / "blank_project.yaml"
    p.write_text(
        "- id: a\n  能力: x\n  同义表述: []\n  证据强度: 强\n  项目:\n"
        "  可量化: []\n  可讲深度: d\n  关联简历版本: []\n",
        encoding="utf-8",
    )
    with pytest.raises(CardValidationError, match="项目"):
        load_cards(p)


def test_load_cards_allows_blank_tuple_field(tmp_path):
    p = tmp_path / "blank_metrics.yaml"
    p.write_text(
        "- id: a\n  能力: x\n  同义表述: []\n  证据强度: 强\n  项目: p\n"
        "  可量化:\n  可讲深度: d\n  关联简历版本: []\n",
        encoding="utf-8",
    )
    cards = load_cards(p)
    assert cards[0].metrics == ()


def test_load_cards_rejects_non_list_top_level(tmp_path):
    p = tmp_path / "not_list.yaml"
    p.write_text("id: a\n能力: x\n", encoding="utf-8")
    with pytest.raises(CardValidationError, match="顶层必须是列表"):
        load_cards(p)


def test_load_cards_strips_padded_capability(tmp_path):
    p = tmp_path / "padded.yaml"
    p.write_text(
        "- id: a\n  能力: '  带空格的能力  '\n  同义表述: []\n  证据强度: 强\n"
        "  项目: p\n  可量化: []\n  可讲深度: d\n  关联简历版本: []\n",
        encoding="utf-8",
    )
    cards = load_cards(p)
    assert cards[0].capability == "带空格的能力"


def test_full_dump_store_ignores_jd_and_returns_all():
    cards = load_cards(FIXTURES / "cards_ok.yaml")
    store = FullDumpStore(cards)
    assert store.retrieve(None) == cards


def test_prompt_block_includes_id_strength_and_synonyms():
    cards = load_cards(FIXTURES / "cards_ok.yaml")
    block = cards_to_prompt_block(cards)
    assert "cap-rag-telecom" in block
    assert "强" in block
    assert "向量检索" in block


def test_prompt_block_stays_small():
    """全量注入的前提是总量小。超了就该重新评估检索方式（设计文档 §5.2）。"""
    cards = load_cards(FIXTURES / "cards_ok.yaml")
    assert len(cards_to_prompt_block(cards)) < 4000


def test_real_cards_file_is_loadable():
    """校对后的真实卡片库必须始终能加载。"""
    from jobstar.config import get_settings

    path = get_settings().cards_path
    if not path.exists():
        pytest.skip("卡片库尚未生成")
    cards = load_cards(path)
    assert len(cards) >= 20, "MASTER.md 至少能提炼出 20 张卡片"
    assert any(c.strength is Strength.STRONG for c in cards)
