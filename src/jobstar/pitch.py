"""打招呼话术草稿。只拿打分时真正命中的卡片说事。"""

from __future__ import annotations

from jobstar.llm import call_json
from jobstar.models import CapabilityCard, JobRequirements, ScoreResult

# Boss 的打招呼输入框实际限制在 500 字以内，留出余量
MAX_CHARS = 400

SYSTEM = """你替一位求职者写 Boss 直聘的打招呼开场白。

硬性要求：
- 120-200 字，一段话，不分点，不用 markdown
- 必须引用 JD 里出现过的具体关键词作为钩子，让对方一眼看出你读过这条 JD
- 必须点到候选人「命中的能力卡片」里的具体项目和可量化结果
- 开头不要用模板句式（不要以「您好，我看到贵公司」「您好，我对这个岗位」这类
  高频开头起手），每条消息的前 20 个字都应当因岗位而异
- 不要写卡片库里没有的经历，不要编数字
- 不要写「期待回复」「盼复」这类空话结尾
- 语气平实专业，不谄媚，不用感叹号

输出格式（只输出这个 JSON 对象本身，不要 markdown 围栏）：
{"greeting": "……"}"""


def write_pitch(
    *,
    req: JobRequirements,
    result: ScoreResult,
    cards: tuple[CapabilityCard, ...],
    title: str,
    company: str,
) -> str:
    cited: set[str] = set()
    for dim in result.dimensions:
        if dim.score > 0:
            cited.update(dim.card_ids)
    if not cited:
        # Minor 8：score_threshold=0 是合法配置，此时一个所有维度都是 0 分
        # 的岗位也会入队。没有任何证据卡片可引用时，系统提示词仍然要求
        # 「必须点到……具体项目和可量化结果」——矛盾指令 + 空证据集正是
        # 编造捏造经历的温床。这里在 Python 侧把关，不依赖 prompt 自觉。
        raise ValueError("没有任何维度有证据卡片支撑，拒绝生成话术（防止空证据下的编造）")
    card_index = {c.id: c for c in cards}

    card_lines = [
        f"[{cid}] {card_index[cid].capability}｜项目: {card_index[cid].project}"
        f"｜可量化: {'、'.join(card_index[cid].metrics) or '无'}"
        f"｜可讲深度: {card_index[cid].depth}"
        for cid in sorted(cited)
        if cid in card_index
    ]
    hit_lines = [
        f"- {d.name}: {d.score} 分｜{d.reason}"
        for d in result.dimensions
        if d.score > 0 and d.reason
    ]

    user = (
        f"## 岗位\n公司：{company}\n职位：{title}\n"
        f"城市：{req.city or '未知'}｜行业：{req.industry or '未知'}\n"
        f"JD 技能关键词：{'、'.join(req.skills) or '无'}\n\n"
        f"## 命中的维度（总分 {result.total}）\n"
        + ("\n".join(hit_lines) or "无")
        + "\n\n## 可以拿来说事的能力卡片（只能用这些）\n"
        + ("\n".join(card_lines) or "无")
    )

    data = call_json(system=SYSTEM, user=user, tier="strong")
    text = str(data.get("greeting") or "").strip().strip('"').strip("“”").strip()
    if not text:
        raise ValueError("话术生成器返回空文本")
    return text[:MAX_CHARS]
