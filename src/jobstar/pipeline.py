"""管道编排：把采集、门禁、归一化、打分、话术串起来。

采集阶段跑两遍门禁：
  第一遍用列表页字段拼的「预备版」需求，刷掉大部分，省下详情页请求和 token；
  第二遍用完整归一化结果，刷掉列表页看不出来的（比如 JD 里才写的学历硬要求）。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Callable

from jobstar import actions, gate
from jobstar.config import get_setting, get_settings
from jobstar.evidence import load_cards
from jobstar.llm import LLMBackendError, LLMSchemaError
from jobstar.models import CapabilityCard, JobRequirements
from jobstar.normalizer import (
    normalize,
    parse_salary_raw,
    save_requirements,
)
from jobstar.pitch import write_pitch
from jobstar.scorer import save_failure, save_score, score

_YEARS_RANGE = re.compile(r"(\d+)\s*[-~]\s*(\d+)\s*年")
_YEARS_MIN = re.compile(r"(\d+)\s*年以上")
_YEARS_MAX = re.compile(r"(\d+)\s*年以内")
_DEGREES = ("博士", "硕士", "本科", "大专", "中专", "高中")


@dataclass
class CollectReport:
    listed: int = 0
    new: int = 0
    gated_out: int = 0
    detail_fetched: int = 0
    # 人工中途点了停止。这两个字段必须存在：只报「抓了 3 条」而不说还剩
    # 27 条没碰，看起来就像正常跑完了。
    stopped: bool = False
    remaining: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class ListReport:
    """只拉列表 + 跑预门禁的结果。不含任何详情页请求。"""

    listed: int = 0
    new: int = 0
    gated_out: int = 0
    candidates: int = 0  # 过了预门禁、等着人工挑的
    errors: list[str] = field(default_factory=list)


@dataclass
class DetailReport:
    """按人工点名的岗位抓详情页的结果。"""

    fetched: int = 0
    skipped: int = 0  # 已经抓过了，不重复打开
    stopped: bool = False
    remaining: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class ScoreReport:
    scored: int = 0
    gated_out: int = 0
    failed: int = 0
    pitch_failed: int = 0
    enqueued: int = 0
    # 以下两项只可能在 `--rescore` 重跑里非零（普通一轮里岗位从没打过分，
    # 也就不可能已经有动作）：
    already_queued: int = 0  # 队列里已经有这个岗位的动作，本轮没有重新生成话术
    retracted: int = 0  # 重跑后不再合格，队列里那条旧动作被撤回成「跳过」
    # 人工中途点了停止，以及还剩几个岗位没轮到。已经处理过的岗位都已落库，
    # 不会丢；没轮到的下次跑 score 还会被捡起来（它们没有 scores 行）。
    stopped: bool = False
    remaining: int = 0
    errors: list[str] = field(default_factory=list)


def parse_tags(tags: list[str]) -> dict:
    """从列表页标签里抠年限和学历。抠不出来的字段是 None。"""
    out: dict[str, object] = {"years_min": None, "years_max": None, "degree": None}
    for tag in tags:
        text = str(tag).strip()
        if "学历不限" in text:
            out["degree"] = "不限"
        else:
            for degree in _DEGREES:
                if degree in text:
                    out["degree"] = degree
                    break
        if "经验不限" in text:
            out["years_min"] = 0
        elif "应届" in text or "在校" in text:
            out["years_min"], out["years_max"] = 0, 1
        else:
            match = _YEARS_RANGE.search(text)
            if match:
                out["years_min"] = int(match.group(1))
                out["years_max"] = int(match.group(2))
                continue
            match = _YEARS_MIN.search(text)
            if match:
                out["years_min"] = int(match.group(1))
                continue
            match = _YEARS_MAX.search(text)
            if match:
                out["years_min"], out["years_max"] = 0, int(match.group(1))
    return out


def prelim_requirements(
    row: sqlite3.Row, tags: list[str] | None = None
) -> JobRequirements:
    """只用列表页字段拼一个预备版需求，给第一遍门禁用。不调 LLM。"""
    parsed = parse_tags(tags or [])
    salary_min, salary_max = parse_salary_raw(row["salary_raw"])
    return JobRequirements(
        job_id=row["job_id"],
        city=row["city"],
        degree=parsed["degree"],
        years_min=parsed["years_min"],
        years_max=parsed["years_max"],
        salary_min=salary_min,
        salary_max=salary_max,
        skills=(),
        industry=None,
        company_size=None,
        category=None,
    )


def home_coords(conn: sqlite3.Connection) -> tuple[float, float] | None:
    """家的坐标 (经度, 纬度)，没设或设坏了都返回 None。

    没设不是错误：距离是可选功能，不设就整个不显示，而不是拿一个猜的
    坐标算出一堆看起来精确的假距离。
    """
    from jobstar.collector.parse import parse_coords

    raw = get_setting(conn, "home_location")
    return parse_coords(raw) if raw else None


def job_distance_km(row: sqlite3.Row, home: tuple[float, float] | None) -> float | None:
    """岗位到家的直线距离。缺任何一端的坐标都返回 None（不是 0，也不是很大
    的数——那两种都会被下游当成真实距离用）。"""
    if home is None:
        return None
    try:
        lng, lat = row["lng"], row["lat"]
    except (IndexError, KeyError):
        return None
    if lng is None or lat is None:
        return None
    return gate.haversine_km(home, (lng, lat))


def _gate_rules(conn: sqlite3.Connection) -> dict:
    """拼装门禁规则：settings 里的 gate_rules 加上 my_degree（硬性学历项存在
    单独的配置键里，不属于 gate_rules 本身，但门禁判断时要和其余规则一起传入）。"""
    rules = dict(get_setting(conn, "gate_rules"))
    rules.setdefault("my_degree", get_setting(conn, "my_degree"))
    return rules


def run_list(
    conn: sqlite3.Connection,
    *,
    keyword: str,
    city_code: str,
    pages: int = 1,
    fetch_fn: Callable[..., list[dict]] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> ListReport:
    """第一阶段：只拉列表页 + 跑预门禁，**一个详情页请求都不发**。

    这一阶段和详情页阶段分开，是为了让人工在两者之间插一脚：先看到这一页
    有哪些岗位，再挑要对谁发详情页请求。详情页请求是整条链路上唯一一个
    「按岗位数量线性增长的、打在 Boss 服务器上的」动作，也是最该由人来
    决定量的地方。

    预门禁必须留在这一阶段：它要用列表页标签里的年限/学历（`item["tags"]`），
    而那几个字段没有落进 jobs 表，出了这个函数就拿不到了。
    """
    from jobstar.collector import boss

    fetch_fn = fetch_fn or boss.fetch_list
    note = on_progress or (lambda _: None)

    report = ListReport()
    try:
        items = fetch_fn(keyword=keyword, city_code=city_code, pages=pages)
    except boss.CollectError as exc:
        # fetch_list 把「零结果」和「页面被拦截」都当 CollectError 抛出
        # （二者在采集器这一层无法区分）。这里不让异常继续往上炸穿多关键词
        # 的调用方——一个关键词恰好没有匹配，不该中止整轮；调用方从
        # report.errors 里能看到这条含糊的失败，自己判断要不要去核实。
        report.errors.append(f"{keyword}: 采集失败或本关键词零结果：{exc}")
        return report

    report.listed = len(items)
    report.new = boss.save_jobs(conn, items)
    note(f"「{keyword}」列表 {report.listed} 条，新增 {report.new} 条")

    rules = _gate_rules(conn)
    for item in items:
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (item["job_id"],)
        ).fetchone()
        if row is None or row["detail_fetched"]:
            continue

        req = prelim_requirements(row, item.get("tags"))
        result = gate.check(
            req, rules, company=row["company"] or "", district=row["district"]
        )
        gate.save_result(conn, row["job_id"], result)
        if result.passed:
            report.candidates += 1
        else:
            report.gated_out += 1
            conn.execute(
                "UPDATE jobs SET status='gated_out' WHERE job_id=?", (row["job_id"],)
            )
    conn.commit()
    note(f"预门禁：{report.candidates} 条待选，{report.gated_out} 条被刷掉")
    return report


def override_gate(conn: sqlite3.Connection, job_ids: list[str]) -> list[str]:
    """人工在候选列表里手动勾选了被预门禁刷掉的岗位。

    光抓详情页是不够的：`run_score` 要求 `gate_results.passed = 1`，不把门禁
    翻过来的话，强行采到的详情页永远等不到打分，勾选等于什么都没发生。这里
    把门禁改判为通过，让「人工的判断压过规则」这件事真的生效。

    返回被翻转的 job_id 和原先的拒绝理由，交给调用方写进日志——这是一次
    人工覆盖规则的动作，不该悄悄发生。
    """
    flipped = []
    for job_id in job_ids:
        row = conn.execute(
            "SELECT reject_reason FROM gate_results WHERE job_id = ? AND passed = 0",
            (job_id,),
        ).fetchone()
        if row is None:
            continue
        conn.execute(
            "UPDATE gate_results SET passed = 1, reject_reason = NULL WHERE job_id = ?",
            (job_id,),
        )
        conn.execute(
            "UPDATE jobs SET status = 'new' WHERE job_id = ?", (job_id,)
        )
        flipped.append(f"{job_id}（原本：{row['reject_reason']}）")
    conn.commit()
    return flipped


def run_details(
    conn: sqlite3.Connection,
    job_ids: list[str],
    *,
    detail_fn: Callable[[str], dict] | None = None,
    on_progress: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> DetailReport:
    """第二阶段：只对点名的岗位抓详情页。

    `should_stop()` 为真时在**下一个岗位开始之前**收工——一个岗位要么详情
    完整落库，要么根本没开始。
    """
    from jobstar.collector import boss

    detail_fn = detail_fn or boss.fetch_detail
    note = on_progress or (lambda _: None)
    stop = should_stop or (lambda: False)

    report = DetailReport()
    for index, job_id in enumerate(job_ids):
        if stop():
            report.stopped = True
            report.remaining = len(job_ids) - index
            note(f"已停止，还有 {report.remaining} 条没抓（已抓到的都已入库）")
            break

        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            report.errors.append(f"{job_id}: 库里没有这个岗位")
            continue
        if row["detail_fetched"]:
            # 已经抓过的不重复打开——重复请求既浪费也是白给的风控信号
            report.skipped += 1
            continue

        try:
            detail = detail_fn(row["url"])
        except boss.LoginRequired:
            # 登录态失效是会话级别的硬故障：不能当成这一条岗位的采集失败吞掉、
            # 继续对下一个发详情页请求——那样只会拿一个已经失效的会话再打一堆
            # 请求，攒出一屏迷惑性的单条错误，而不是一次响亮、立刻能看懂的
            # “重新登录再跑”信号。让它照原样往外炸穿。
            raise
        except Exception as exc:
            note(f"详情页抓取失败：{row['title']}（{exc}）")
            report.errors.append(f"{job_id}: {exc}")
            continue
        boss.save_detail(conn, job_id, detail)
        report.fetched += 1
        note(f"已抓详情：{row['title']} · {row['company']}")

    return report


def pending_candidates(conn: sqlite3.Connection) -> list[str]:
    """过了预门禁、还没抓详情页的岗位。`run_collect` 用它复原「一把梭」行为。"""
    return [
        r["job_id"]
        for r in conn.execute(
            "SELECT j.job_id FROM jobs j "
            "JOIN gate_results g ON g.job_id = j.job_id AND g.passed = 1 "
            "WHERE j.detail_fetched = 0 ORDER BY j.id"
        )
    ]


def run_collect(
    conn: sqlite3.Connection,
    *,
    keyword: str,
    city_code: str,
    pages: int = 1,
    fetch_fn: Callable[..., list[dict]] | None = None,
    detail_fn: Callable[[str], dict] | None = None,
    on_progress: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> CollectReport:
    """一把梭：拉列表 → 预门禁 → 给所有幸存者抓详情页。命令行走这条。

    面板走的是拆开的两步（run_list → 人工挑 → run_details），好让详情页
    请求的数量由人决定。这里把两步接起来，保持 `jobstar collect` 的行为不变。
    """
    listed = run_list(
        conn,
        keyword=keyword,
        city_code=city_code,
        pages=pages,
        fetch_fn=fetch_fn,
        on_progress=on_progress,
    )
    report = CollectReport(
        listed=listed.listed,
        new=listed.new,
        gated_out=listed.gated_out,
        errors=list(listed.errors),
    )
    if not listed.listed:
        return report

    detail = run_details(
        conn,
        pending_candidates(conn),
        detail_fn=detail_fn,
        on_progress=on_progress,
        should_stop=should_stop,
    )
    report.detail_fetched = detail.fetched
    report.stopped = detail.stopped
    report.remaining = detail.remaining
    report.errors.extend(detail.errors)
    return report


def threshold_reached(conn: sqlite3.Connection, total: float) -> bool:
    """总分是否达到入队阈值。阈值为 None（冷启动期）时恒为 False。

    抽成独立函数是因为 `run_score` 也要问同一个问题——重跑时分数可能从阈值
    之上掉到之下，那条已经躺在待确认队列里的旧动作必须被撤回，而不是靠
    `maybe_enqueue` 返回 None 默默带过（见 `_retract_stale_action`）。两处
    必须用同一套判断，否则会出现「不入队但也不撤回」的夹缝。
    """
    threshold = get_setting(conn, "score_threshold")
    if threshold is not None and (
        not isinstance(threshold, (int, float)) or isinstance(threshold, bool)
    ):
        # Minor 12：写入路径上 validate_setting_value 已经把关，正常情况下
        # 不会出现非数字/非 null 的 score_threshold；但这里读的是数据库里的
        # 原始值，不受 set_setting 校验的保护范围（例如手改过库文件、或者
        # 未来出现绕开 set_setting 的写入点）。不加这道防线的话，下面的
        # `float(threshold)` 会抛 TypeError——这个类型不在 run_score 对
        # maybe_enqueue 的 except 子句范围内，会带着整个批次一起炸穿，后面
        # 排队的岗位全部得不到处理。改成 ValueError，让它和其他"这个岗位
        # 出问题了"的失败走同一条本地化、不影响其余岗位的路径。
        raise ValueError(
            f"score_threshold 配置已损坏（{threshold!r} 既不是数字也不是 "
            "null），拒绝据此决定是否入队；请到面板设置里修正"
        )
    return threshold is not None and total >= float(threshold)


def maybe_enqueue(
    conn: sqlite3.Connection,
    req: JobRequirements,
    result,
    *,
    title: str,
    company: str,
    cards: tuple[CapabilityCard, ...] | None = None,
) -> int | None:
    """总分达到阈值才生成待确认动作。阈值为 None（冷启动期）时永远不生成。

    `cards` 让调用方复用已经加载过的卡片库（run_score 每轮只加载一次），不传时
    退化为自己读一遍——直接调用 maybe_enqueue 的既有测试不用改。
    """
    if not threshold_reached(conn, result.total):
        return None
    if cards is None:
        cards = load_cards(get_settings().cards_path)
    greeting = write_pitch(
        req=req, result=result, cards=cards, title=title, company=company
    )
    return actions.enqueue(
        conn,
        type="send_greeting",
        job_id=req.job_id,
        payload={"greeting": greeting, "title": title, "company": company},
    )


def _save_pitch_failure(conn: sqlite3.Connection, job_id: str, error: str) -> None:
    """Minor 3：`maybe_enqueue`（写话术）失败时的记录方式。

    这个岗位的分数是真实、有效的——`save_score` 已经提交过——不能像
    `scorer.save_failure` 那样把 `total`/`dimensions` 清空，那会让一个已经
    成立的打分结果凭空消失。这里只在 `scores.error` 上追加一句话术失败的
    说明，`total`/`dimensions` 保持不动；`jobs.status` 改成 'pitch_failed'，
    在面板/`/api/health` 上和真正的打分失败（`jobs.status='scoring_failed'`，
    `scores.total` 恒为 NULL）区分开——两者各有独立的计数字段，不会被混进
    同一条"打分失败"提示里，人工也不会被引导去怀疑一个其实是正确的分数。

    选择复用 `scores.error` 而不是新开一列：`total IS NULL` 这个既有信号
    已经能把"真正打分失败"和"打分成功但话术失败"两种情况分开，不需要为
    这第二种情况改表结构。"""
    conn.execute("UPDATE scores SET error = ? WHERE job_id = ?", (error, job_id))
    conn.execute(
        "UPDATE jobs SET status = 'pitch_failed' WHERE job_id = ?", (job_id,)
    )
    conn.commit()


def clear_for_rescore(conn: sqlite3.Connection, *, job_id: str | None = None) -> int:
    """Minor 4：`jobstar score --rescore` 用的清场函数。

    `run_score` 的查询要求 `scores.job_id IS NULL` 且 `gate_results.passed = 1`——
    这是"只处理从未打过分的岗位"的正确默认行为，但也意味着三种状态一旦
    写入就永远拿不到重新入场的机会：
      - scoring_failed（归一化/打分失败，`scores.total` 恒为 NULL）
      - pitch_failed（打分成功但话术生成失败，`scores.total` 不为 NULL）
      - 已经成功打过分的岗位（校准 §5.3：改了 prompt/权重后，要能
        重跑同一批标注过的岗位，对照新分数和标注的一致率）
      - 第二遍门禁刷掉（`run_score` 自己的 `gate.check` 把同一行
        `gate_results` 覆盖成 `passed=0`，此后 `run_score` 的 JOIN 永远
        选不中它，哪怕后来放宽了 `gate_rules`）

    这里只清掉阻止 `run_score` 重新处理的吸收态本身——删掉 `scores` 行、
    把被第二遍门禁刷掉的 `gate_results.passed` 改回 1——不重新算分；重新
    算分交给调用方紧接着再跑一次签名不变的 `run_score`（它会用当前的
    `gate_rules`/`dimension_weights` 重新走一遍归一化 + 门禁 + 打分）。

    只处理确实处于吸收态的岗位（有 `scores` 行，或 `gate_results.passed=0`），
    不会误伤那些第一遍门禁就被刷掉、从未抓过详情页的岗位（`detail_fetched=0`
    卡在最外层条件上，本来就够不着 `run_score`，这里也不该去动它们）。

    传 `job_id` 只清这一条；不传清所有满足条件的行。返回被清掉的岗位数。
    """
    rows = conn.execute(
        "SELECT j.job_id FROM jobs j "
        "LEFT JOIN scores s ON s.job_id = j.job_id "
        "LEFT JOIN gate_results g ON g.job_id = j.job_id "
        "WHERE j.detail_fetched = 1 "
        "AND (? IS NULL OR j.job_id = ?) "
        "AND (s.job_id IS NOT NULL OR (g.job_id IS NOT NULL AND g.passed = 0))",
        (job_id, job_id),
    ).fetchall()
    ids = [row["job_id"] for row in rows]
    if not ids:
        return 0

    placeholders = ",".join("?" * len(ids))
    conn.execute(f"DELETE FROM scores WHERE job_id IN ({placeholders})", ids)
    conn.execute(
        f"UPDATE gate_results SET passed = 1, reject_reason = NULL "
        f"WHERE passed = 0 AND job_id IN ({placeholders})",
        ids,
    )
    conn.execute(
        f"UPDATE jobs SET status = 'new' WHERE job_id IN ({placeholders})", ids
    )
    conn.commit()
    return len(ids)


def _existing_action(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    """这个岗位在待确认队列里已有的招呼动作（actions 对 (type, job_id) 唯一）。"""
    return conn.execute(
        "SELECT id, status FROM actions WHERE type = 'send_greeting' AND job_id = ?",
        (job_id,),
    ).fetchone()


def _retract_stale_action(conn: sqlite3.Connection, job_id: str, reason: str) -> bool:
    """重跑后岗位不再合格时，撤回队列里那条按旧结论生成的动作。

    为什么必须撤回而不是放着不管：`clear_for_rescore` 只清 scores/gate_results，
    动作表原封不动。于是「人工把公司拉黑 → --rescore → 第二遍门禁刷掉它」
    之后，队列里仍然躺着一条 pending 的招呼，卡片上还是旧话术，面板不显示
    jobs.status，人工看不出这条已经作废——点一下确认就发给了刚被自己拉黑的
    公司。分数从阈值之上掉到之下也是同一个夹缝。

    只撤回 pending 和 approved：
      - pending 还没有人做过决定，撤回不覆盖任何人的判断；
      - approved 是人工批准过的，但「把公司拉黑 / 调高阈值」同样是人工的
        决定，而且是更晚的那一个——让后一个决定生效，方向上也只会少发不会
        多发。撤回成 skipped 之后人工随时能再点确认改回来（状态机允许
        skipped → approved），不是不可逆的。
      - sending/sent 不动：消息可能已经或确实已经发出去了，撤回只会让台账
        和事实不符。
    撤回原因写进 actions.error，不让它成为一次静默的状态变更。
    """
    row = _existing_action(conn, job_id)
    if row is None or row["status"] not in (actions.PENDING, actions.APPROVED):
        return False
    actions.skip(conn, row["id"])
    conn.execute(
        "UPDATE actions SET error = ? WHERE id = ?", (reason, row["id"])
    )
    conn.commit()
    return True


def run_score(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    job_id: str | None = None,
    on_progress: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> ScoreReport:
    """对已抓详情、已过第一遍门禁、还没打过分的岗位做归一化 + 门禁 + 打分。

    `job_id` 只处理这一个岗位。`clear_for_rescore` 有同名参数，两者必须一起
    传——只限定清场范围而让打分全库跑，等于对着一堆人工没点名的岗位调 LLM
    写话术、生成待确认动作，而命令行还在说「只重跑这一个」。

    `on_progress` 每处理完一个岗位调用一次（每个岗位两轮 LLM 调用，几十条
    就是几分钟），给面板做进度条用。CLI 不传，行为完全不变。

    `should_stop()` 为真时在**下一个岗位开始之前**收工。检查点只放在循环
    顶部：中途插一个检查点会留下半截状态——最糟的是「分数写了、话术没写」，
    因为 scores 行一旦存在，这个岗位就落进 run_score 的吸收态，不加
    --rescore 再也回不来了。停在边界上则干净：没轮到的岗位没有 scores 行，
    下次跑 score 会照常被捡起来。
    """
    report = ScoreReport()
    note = on_progress or (lambda _: None)
    stop = should_stop or (lambda: False)
    sql = (
        "SELECT j.* FROM jobs j "
        "JOIN gate_results g ON g.job_id = j.job_id AND g.passed = 1 "
        "LEFT JOIN scores s ON s.job_id = j.job_id "
        "WHERE j.detail_fetched = 1 AND s.job_id IS NULL "
        "AND (? IS NULL OR j.job_id = ?) "
        "ORDER BY j.collected_at"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, (job_id, job_id)).fetchall()
    note(f"待处理 {len(rows)} 个岗位")

    cards = load_cards(get_settings().cards_path)
    weights = get_setting(conn, "dimension_weights")
    rules = _gate_rules(conn)
    home = home_coords(conn)

    for index, row in enumerate(rows):
        if stop():
            report.stopped = True
            report.remaining = len(rows) - index
            note(f"已停止，还有 {report.remaining} 个岗位没处理（已打的分都已入库）")
            break

        try:
            req = normalize(
                job_id=row["job_id"],
                title=row["title"],
                raw_jd=row["raw_jd"],
                city_hint=row["city"],
                salary_hint=row["salary_raw"],
            )
        except (LLMSchemaError, LLMBackendError) as exc:
            note(f"归一化失败：{row['title']}（{exc}）")
            save_failure(conn, row["job_id"], f"归一化失败：{exc}")
            report.failed += 1
            report.errors.append(f"{row['job_id']}: {exc}")
            continue
        save_requirements(conn, req)

        gate_result = gate.check(
            req,
            rules,
            company=row["company"] or "",
            district=row["district"],
            distance_km=job_distance_km(row, home),
        )
        gate.save_result(conn, row["job_id"], gate_result)
        if not gate_result.passed:
            note(f"门禁刷掉：{row['title']}（{gate_result.reject_reason}）")
            report.gated_out += 1
            conn.execute(
                "UPDATE jobs SET status='gated_out' WHERE job_id=?", (row["job_id"],)
            )
            conn.commit()
            if _retract_stale_action(
                conn,
                row["job_id"],
                f"重跑后被门禁刷掉（{gate_result.reject_reason}），"
                "这条招呼按的是旧结论，已自动撤回；确认无误可以再点确认恢复",
            ):
                report.retracted += 1
            continue

        try:
            result = score(req, cards, weights)
        except (LLMSchemaError, LLMBackendError) as exc:
            note(f"打分失败：{row['title']}（{exc}）")
            save_failure(conn, row["job_id"], f"打分失败：{exc}")
            report.failed += 1
            report.errors.append(f"{row['job_id']}: {exc}")
            continue

        save_score(conn, result)
        report.scored += 1
        note(f"{result.total:g} 分 · {row['title']} · {row['company']}")

        try:
            qualifies = threshold_reached(conn, result.total)
            if not qualifies:
                # 重跑把分数打到了阈值之下——队列里那条按旧分数生成的招呼
                # 现在是过期结论，撤回它（普通一轮里到不了这里：没打过分的
                # 岗位不可能已经有动作）。
                if _retract_stale_action(
                    conn,
                    row["job_id"],
                    f"重跑后总分 {result.total} 低于阈值，这条招呼按的是旧分数，"
                    "已自动撤回；确认无误可以再点确认恢复",
                ):
                    report.retracted += 1
            elif _existing_action(conn, row["job_id"]) is not None:
                # 队列里已经有这个岗位的动作。actions 的 UNIQUE(type, job_id)
                # 决定了再入队是 DO NOTHING——话术会生成出来然后被原样丢弃，
                # 白烧一次 LLM 调用；旧 payload 也不会被覆盖（那是对的：台账
                # 里记的必须是真正发出去的那一版）。所以这里根本不去生成，
                # 单独计数，而不是混进 enqueued 里谎报「入队 N」。
                report.already_queued += 1
            elif maybe_enqueue(
                conn,
                req,
                result,
                title=row["title"],
                company=row["company"] or "",
                cards=cards,
            ) is not None:
                report.enqueued += 1
        except (LLMSchemaError, LLMBackendError, ValueError) as exc:
            _save_pitch_failure(conn, row["job_id"], f"话术生成失败：{exc}")
            report.pitch_failed += 1
            report.errors.append(f"{row['job_id']} 话术生成失败: {exc}")

    return report
