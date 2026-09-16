"""维度打分器。

两条约束在代码里强制执行，不依赖 prompt 遵守：
1. 某维度没有引用到真实存在的卡片 → 该维度记 0 分
2. 某维度只引用了「弱」证据卡片 → 该维度分数封顶 WEAK_EVIDENCE_CAP

`score()` 是纯函数：不写库、不碰浏览器，可对历史岗位批量回放。
模块里另外三个函数（`save_score`/`save_failure`/`load_score`）负责读写 scores 表。
"""

from __future__ import annotations

import json
import sqlite3

from jobstar.evidence import cards_to_prompt_block
from jobstar.llm import call_json
from jobstar.models import (
    DIMENSION_LABELS,
    DIMENSIONS,
    CapabilityCard,
    DimensionScore,
    JobRequirements,
    ScoreResult,
    Strength,
)

# 改 prompt 或改权重时手动 bump，用于区分不同版本产生的分数、支持回归对比
SCORER_VERSION = "v1"

WEAK_EVIDENCE_CAP = 40.0

SYSTEM = f"""你是岗位匹配评估器。给定一个岗位的结构化需求和候选人的能力卡片库，
对每个维度打分并给出证据。

维度（必须全部给出，一个都不能少）：
{chr(10).join(f"- {name}: {label}" for name, label in DIMENSION_LABELS.items())}

每个维度输出四个字段：
- score: 0-100 的整数
- card_ids: 支撑这个分数的能力卡片 id 数组。**必须是卡片库里真实存在的 id。**
  找不到能支撑的卡片就给空数组，不要编 id，不要硬凑。
- reason: 命中理由，一两句话，引用卡片里的具体内容
- gap: 缺口说明。JD 要求了但卡片库里没有的东西。没缺口写「无」

打分纪律：
- 每个维度的分数必须有卡片作为证据。没有证据的维度会被判 0 分。
- 「证据强度」是候选人自评的可讲深度。**强=能讲 30 分钟，中=能讲 5 分钟，弱=仅接触。**
  只有「弱」证据支撑的维度不算真匹配，分数会被封顶 {int(WEAK_EVIDENCE_CAP)} 分。
  所以如果某维度只能靠弱卡片撑，如实给低分并在 gap 里写清楚。
- JD 措辞和卡片措辞不一样是常态。先看卡片的「同义表述」再判断是否命中。

输出格式（只输出这个 JSON 对象本身，不要 markdown 围栏，不要解释）：
{{"dimensions": {{"skills": {{"score": 0, "card_ids": [], "reason": "", "gap": ""}}, ...}}}}"""


def _render_requirements(req: JobRequirements) -> str:
    skills = "、".join(req.skills) if req.skills else "未提取到"
    years = (
        f"{req.years_min}-{req.years_max}"
        if req.years_min is not None and req.years_max is not None
        else (f"{req.years_min}年以上" if req.years_min is not None else "不限")
    )
    if req.salary_min is not None and req.salary_max is not None:
        salary = f"{req.salary_min}-{req.salary_max}K"
    elif req.salary_min is not None:
        salary = f"{req.salary_min}K起（未提取到上限）"
    elif req.salary_max is not None:
        salary = f"{req.salary_max}K以下（未提取到下限）"
    else:
        salary = "未提取到"
    return (
        f"岗位类别: {req.category or '未提取到'}\n"
        f"行业: {req.industry or '未提取到'}\n"
        f"城市: {req.city or '未提取到'}\n"
        f"学历要求: {req.degree or '未提取到'}\n"
        f"经验年限: {years}\n"
        f"薪资: {salary}\n"
        f"公司规模: {req.company_size or '未提取到'}\n"
        f"技能要求: {skills}"
    )


def _clean_dimension(
    name: str, raw: object, card_index: dict[str, CapabilityCard]
) -> DimensionScore:
    if not isinstance(raw, dict):
        return DimensionScore(name, 0.0, (), "", "模型未返回该维度（缺失）")

    ids = raw.get("card_ids") or []
    if isinstance(ids, str):
        ids = [ids]
    elif not isinstance(ids, (list, tuple)):
        # 非字符串的标量（如 3、true）不可能是真实存在的卡片 id，
        # 直接当空列表处理，交给下面的过滤逻辑判定为无证据
        ids = []
    valid = tuple(str(i) for i in ids if str(i) in card_index)

    reason = str(raw.get("reason") or "")
    gap = str(raw.get("gap") or "")

    try:
        value = float(raw.get("score") or 0)
    except (TypeError, ValueError):
        value = 0.0
    value = max(0.0, min(100.0, value))

    if not valid:
        # 强制清零时不能沿用模型给出的 reason/gap——那是为非零分数写的话术，
        # 原样展示会让人误以为「0 分但看起来命中了」。这里改写成明确说明零分成因，
        # 模型原话只作为附加信息弱化呈现。
        note = "无证据卡片支撑，已强制记 0 分"
        if gap and gap != "无":
            note = f"{note}（模型给出的缺口说明：{gap}）"
        return DimensionScore(name, 0.0, (), "", note)

    if all(card_index[i].strength == Strength.WEAK for i in valid):
        value = min(value, WEAK_EVIDENCE_CAP)

    return DimensionScore(name, value, valid, reason, gap)


def score(
    req: JobRequirements,
    cards: tuple[CapabilityCard, ...],
    weights: dict[str, float],
    scorer_version: str = SCORER_VERSION,
) -> ScoreResult:
    user = (
        "## 岗位需求\n"
        f"{_render_requirements(req)}\n\n"
        "## 候选人能力卡片库\n"
        f"{cards_to_prompt_block(cards)}"
    )
    data = call_json(system=SYSTEM, user=user, tier="strong")
    raw_dims = data.get("dimensions") or {}
    if not isinstance(raw_dims, dict):
        # call_json 只保证顶层是 dict，dimensions 的形状不受保证——
        # 模型可能返回 [{"name": "skills", ...}] 这种 list-of-objects。
        # 按「未返回任何维度」处理，走后面统一的缺失/清零路径，而不是让 AttributeError 炸穿。
        raw_dims = {}

    card_index = {c.id: c for c in cards}
    dimensions = tuple(
        _clean_dimension(name, raw_dims.get(name), card_index) for name in DIMENSIONS
    )

    weight_sum = sum(weights.get(d.name, 0.0) for d in dimensions)
    if weight_sum <= 0:
        total = 0.0
    else:
        total = (
            sum(d.score * weights.get(d.name, 0.0) for d in dimensions) / weight_sum
        )

    return ScoreResult(
        job_id=req.job_id,
        total=round(total, 2),
        dimensions=dimensions,
        scorer_version=scorer_version,
    )


def save_score(conn: sqlite3.Connection, result: ScoreResult) -> None:
    payload = [
        {
            "name": d.name,
            "score": d.score,
            "card_ids": list(d.card_ids),
            "reason": d.reason,
            "gap": d.gap,
        }
        for d in result.dimensions
    ]
    conn.execute(
        "INSERT INTO scores (job_id, total, dimensions, scorer_version, error) "
        "VALUES (?, ?, ?, ?, NULL) "
        "ON CONFLICT(job_id) DO UPDATE SET "
        "total=excluded.total, dimensions=excluded.dimensions, "
        "scorer_version=excluded.scorer_version, error=NULL, "
        "scored_at=datetime('now')",
        (
            result.job_id,
            result.total,
            json.dumps(payload, ensure_ascii=False),
            result.scorer_version,
        ),
    )
    conn.execute("UPDATE jobs SET status='scored' WHERE job_id=?", (result.job_id,))
    conn.commit()


def save_failure(conn: sqlite3.Connection, job_id: str, error: str) -> None:
    """设计文档 §7：LLM 返回不合 schema 重试仍失败 → 标记 scoring_failed，不猜分数。"""
    conn.execute(
        "INSERT INTO scores (job_id, total, dimensions, scorer_version, error) "
        "VALUES (?, NULL, '[]', ?, ?) "
        "ON CONFLICT(job_id) DO UPDATE SET "
        "total=NULL, dimensions='[]', scorer_version=excluded.scorer_version, "
        "error=excluded.error, scored_at=datetime('now')",
        (job_id, SCORER_VERSION, error),
    )
    conn.execute(
        "UPDATE jobs SET status='scoring_failed' WHERE job_id=?", (job_id,)
    )
    conn.commit()


def load_score(conn: sqlite3.Connection, job_id: str) -> ScoreResult | None:
    row = conn.execute(
        "SELECT * FROM scores WHERE job_id = ? AND total IS NOT NULL", (job_id,)
    ).fetchone()
    if row is None:
        return None
    dimensions = tuple(
        DimensionScore(
            name=d["name"],
            score=d["score"],
            card_ids=tuple(d["card_ids"]),
            reason=d["reason"],
            gap=d["gap"],
        )
        for d in json.loads(row["dimensions"])
    )
    return ScoreResult(
        job_id=row["job_id"],
        total=row["total"],
        dimensions=dimensions,
        scorer_version=row["scorer_version"],
    )
