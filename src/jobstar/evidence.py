"""能力卡片库：把 MASTER.md 提炼出的 YAML 加载成结构化卡片。

YAML 用中文键是刻意的 —— 这份文件要由本人逐张校对「证据强度」列。
中文键到 ASCII 字段名的映射只存在于本文件的 _KEY_MAP 一处。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from jobstar.models import CapabilityCard, JobRequirements, Strength

_KEY_MAP = {
    "id": "id",
    "能力": "capability",
    "同义表述": "synonyms",
    "证据强度": "strength",
    "项目": "project",
    "可量化": "metrics",
    "可讲深度": "depth",
    "关联简历版本": "resume_versions",
}

_TUPLE_FIELDS = {"synonyms", "metrics", "resume_versions"}


class CardValidationError(ValueError):
    """卡片 YAML 结构不合法。宁可启动即失败，也不要带着坏卡片去打分。"""


def _build_card(raw: dict[str, Any], index: int) -> CapabilityCard:
    if not isinstance(raw, dict):
        raise CardValidationError(
            f"第 {index + 1} 张卡片不是合法的映射结构，实际是 {type(raw).__name__}"
        )
    missing = [k for k in _KEY_MAP if k not in raw]
    if missing:
        raise CardValidationError(f"第 {index + 1} 张卡片缺少字段：{missing}")
    fields: dict[str, Any] = {}
    for cn_key, field in _KEY_MAP.items():
        value = raw[cn_key]
        if field in _TUPLE_FIELDS:
            if value is not None and not isinstance(value, list):
                raise CardValidationError(
                    f"第 {index + 1} 张卡片的「{cn_key}」必须是列表，实际写的是 {value!r}"
                )
            fields[field] = tuple(value or ())
        elif field == "strength":
            try:
                fields[field] = Strength(str(value).strip())
            except ValueError as exc:
                allowed = [s.value for s in Strength]
                raise CardValidationError(
                    f"第 {index + 1} 张卡片的证据强度是 {value!r}，只能是 {allowed}"
                ) from exc
        else:
            fields[field] = str(value).strip()
    return CapabilityCard(**fields)


def load_cards(path: Path) -> tuple[CapabilityCard, ...]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise CardValidationError(f"{path} 顶层必须是列表，实际是 {type(data).__name__}")
    cards = tuple(_build_card(raw, i) for i, raw in enumerate(data))
    seen: set[str] = set()
    for card in cards:
        if card.id in seen:
            raise CardValidationError(f"卡片 id 重复：{card.id}")
        seen.add(card.id)
    return cards


@dataclass(frozen=True)
class FullDumpStore:
    """第一版检索实现：忽略 jd，返回全部卡片。

    设计文档 §5.2：当前数据量下全量注入优于 top-k 召回。卡片数超过 200 张或
    摘要总量超过 30KB 时，换成 VectorStore，打分器无需改动。
    """

    cards: tuple[CapabilityCard, ...]

    def retrieve(self, jd: JobRequirements | None) -> tuple[CapabilityCard, ...]:
        return self.cards


def cards_to_prompt_block(cards: tuple[CapabilityCard, ...]) -> str:
    """渲染成注入 prompt 的紧凑文本。同义表述必须保留 —— 它是语义对齐的主力。"""
    lines: list[str] = []
    for card in cards:
        synonyms = "、".join(card.synonyms) if card.synonyms else "无"
        metrics = "、".join(card.metrics) if card.metrics else "无"
        lines.append(
            f"[{card.id}] {card.capability}｜证据强度:{card.strength.value}\n"
            f"  同义表述: {synonyms}\n"
            f"  项目: {card.project}\n"
            f"  可量化: {metrics}\n"
            f"  可讲深度: {card.depth}"
        )
    return "\n".join(lines)
