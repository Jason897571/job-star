"""跨模块共享的数据结构。所有字段都是不可变的。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# 打分维度，顺序固定；权重配置和 LLM 输出都以此为准
DIMENSIONS: tuple[str, ...] = ("skills", "industry", "duties", "years", "bonus")

DIMENSION_LABELS: dict[str, str] = {
    "skills": "技能匹配",
    "industry": "行业匹配",
    "duties": "岗位职责匹配",
    "years": "经验年限",
    "bonus": "加分项",
}


class Strength(str, Enum):
    """能力卡片的证据强度。这是本系统防止简历吹牛的唯一防线。"""

    STRONG = "强"  # 能讲 30 分钟
    MEDIUM = "中"  # 能讲 5 分钟
    WEAK = "弱"  # 仅接触


@dataclass(frozen=True)
class CapabilityCard:
    id: str
    capability: str  # 能力
    synonyms: tuple[str, ...]  # 同义表述
    strength: Strength  # 证据强度
    project: str  # 项目
    metrics: tuple[str, ...]  # 可量化
    depth: str  # 可讲深度
    resume_versions: tuple[str, ...]  # 关联简历版本


@dataclass(frozen=True)
class JobRequirements:
    """JD 自由文本的结构化抽取结果。薪资单位是千元/月。"""

    job_id: str
    city: str | None
    degree: str | None  # 不限 / 大专 / 本科 / 硕士 / 博士
    years_min: int | None
    years_max: int | None
    salary_min: int | None
    salary_max: int | None
    skills: tuple[str, ...]
    industry: str | None
    company_size: str | None
    category: str | None


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reject_reason: str | None


@dataclass(frozen=True)
class DimensionScore:
    name: str  # DIMENSIONS 之一
    score: float  # 0-100
    card_ids: tuple[str, ...]  # 为空则该维度记 0 分
    reason: str  # 命中理由
    gap: str  # 缺口说明


@dataclass(frozen=True)
class ScoreResult:
    job_id: str
    total: float  # 加权总分 0-100
    dimensions: tuple[DimensionScore, ...]
    scorer_version: str
