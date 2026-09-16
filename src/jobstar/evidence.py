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
    missing = [
        k
        for k, field in _KEY_MAP.items()
        if k not in raw or (raw[k] is None and field not in _TUPLE_FIELDS)
    ]
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
            for item in value or ():
                if not isinstance(item, str):
                    raise CardValidationError(
                        f"第 {index + 1} 张卡片的「{cn_key}」列表项必须是字符串，"
                        f"实际写的是 {item!r}（类型 {type(item).__name__}）"
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


def validate_cards(data: Any) -> tuple[CapabilityCard, ...]:
    """把「YAML 解析出来的东西」校验成卡片元组。

    从 `load_cards` 里拆出来，好让面板的卡片编辑器在**写盘之前**跑同一套
    校验——两条路径必须共用一份规则，否则从面板存进去的卡片可能是下次
    启动时才炸的坏数据。
    """
    if not isinstance(data, list):
        raise CardValidationError(f"顶层必须是列表，实际是 {type(data).__name__}")
    if not data:
        raise CardValidationError("卡片库是空的——打分器会把每个维度都判 0 分")
    cards = tuple(_build_card(raw, i) for i, raw in enumerate(data))
    seen: set[str] = set()
    for card in cards:
        if card.id in seen:
            raise CardValidationError(f"卡片 id 重复：{card.id}")
        seen.add(card.id)
    return cards


def load_cards(path: Path) -> tuple[CapabilityCard, ...]:
    try:
        return validate_cards(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
    except CardValidationError as exc:
        # 带上文件路径——面板和 CLI 都可能指向不同的卡片库
        raise CardValidationError(f"{path}: {exc}") from exc


def card_to_raw(card: CapabilityCard) -> dict[str, Any]:
    """卡片 -> 中文键的映射，键序和 _KEY_MAP 一致（也就是文件里的书写顺序）。"""
    raw: dict[str, Any] = {}
    for cn_key, field in _KEY_MAP.items():
        value = getattr(card, field)
        if field in _TUPLE_FIELDS:
            raw[cn_key] = list(value)
        elif field == "strength":
            raw[cn_key] = value.value
        else:
            raw[cn_key] = value
    return raw


def dump_cards(cards: tuple[CapabilityCard, ...]) -> str:
    """序列化回 YAML。`allow_unicode` 必须开——否则中文全被转义成 \\uXXXX，
    这份文件就再也不能由本人手工校对了，而手工校对正是它存在的理由。"""
    return yaml.safe_dump(
        [card_to_raw(c) for c in cards],
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=None,
        width=100,
    )


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
