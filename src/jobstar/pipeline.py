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
    errors: list[str] = field(default_factory=list)


@dataclass
class ScoreReport:
    scored: int = 0
    gated_out: int = 0
    failed: int = 0
    pitch_failed: int = 0
    enqueued: int = 0
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


def _gate_rules(conn: sqlite3.Connection) -> dict:
    """拼装门禁规则：settings 里的 gate_rules 加上 my_degree（硬性学历项存在
    单独的配置键里，不属于 gate_rules 本身，但门禁判断时要和其余规则一起传入）。"""
    rules = dict(get_setting(conn, "gate_rules"))
    rules.setdefault("my_degree", get_setting(conn, "my_degree"))
    return rules


def run_collect(
    conn: sqlite3.Connection,
    *,
    keyword: str,
    city_code: str,
    pages: int = 1,
    fetch_fn: Callable[..., list[dict]] | None = None,
    detail_fn: Callable[[str], dict] | None = None,
) -> CollectReport:
    from jobstar.collector import boss

    fetch_fn = fetch_fn or boss.fetch_list
    detail_fn = detail_fn or boss.fetch_detail

    report = CollectReport()
    try:
        items = fetch_fn(keyword=keyword, city_code=city_code, pages=pages)
    except boss.CollectError as exc:
        # fetch_list 现在把「零结果」和「页面被拦截」都当 CollectError 抛出
        # （二者在采集器这一层无法区分）。这里不让异常继续往上炸穿多关键词
        # 的调用方——一个关键词恰好没有匹配，不该中止整轮采集；调用方从
        # report.errors 里能看到这条含糊的失败，自己判断要不要去核实。
        report.errors.append(f"{keyword}: 采集失败或本关键词零结果：{exc}")
        return report

    report.listed = len(items)
    report.new = boss.save_jobs(conn, items)

    rules = _gate_rules(conn)

    for item in items:
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (item["job_id"],)
        ).fetchone()
        if row is None or row["detail_fetched"]:
            continue

        req = prelim_requirements(row, item.get("tags"))
        result = gate.check(req, rules, company=row["company"] or "")
        gate.save_result(conn, row["job_id"], result)
        if not result.passed:
            report.gated_out += 1
            conn.execute(
                "UPDATE jobs SET status='gated_out' WHERE job_id=?", (row["job_id"],)
            )
            conn.commit()
            continue

        try:
            detail = detail_fn(row["url"])
        except boss.LoginRequired:
            # 登录态失效是会话级别的硬故障：不能当成这一条岗位的采集失败吞掉、
            # 继续对下一个门禁幸存者发详情页请求——那样只会拿一个已经失效的
            # 会话再打一堆请求，攒出一屏迷惑性的单条错误，而不是一次响亮、
            # 立刻能看懂的“重新登录再跑”信号。让它照原样往外炸穿。
            raise
        except Exception as exc:
            report.errors.append(f"{row['job_id']}: {exc}")
            continue
        boss.save_detail(conn, row["job_id"], detail)
        report.detail_fetched += 1

    return report


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
    if threshold is None or result.total < float(threshold):
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


def run_score(conn: sqlite3.Connection, *, limit: int | None = None) -> ScoreReport:
    """对已抓详情、已过第一遍门禁、还没打过分的岗位做归一化 + 门禁 + 打分。"""
    report = ScoreReport()
    sql = (
        "SELECT j.* FROM jobs j "
        "JOIN gate_results g ON g.job_id = j.job_id AND g.passed = 1 "
        "LEFT JOIN scores s ON s.job_id = j.job_id "
        "WHERE j.detail_fetched = 1 AND s.job_id IS NULL "
        "ORDER BY j.collected_at"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()

    cards = load_cards(get_settings().cards_path)
    weights = get_setting(conn, "dimension_weights")
    rules = _gate_rules(conn)

    for row in rows:
        try:
            req = normalize(
                job_id=row["job_id"],
                title=row["title"],
                raw_jd=row["raw_jd"],
                city_hint=row["city"],
                salary_hint=row["salary_raw"],
            )
        except (LLMSchemaError, LLMBackendError) as exc:
            save_failure(conn, row["job_id"], f"归一化失败：{exc}")
            report.failed += 1
            report.errors.append(f"{row['job_id']}: {exc}")
            continue
        save_requirements(conn, req)

        gate_result = gate.check(req, rules, company=row["company"] or "")
        gate.save_result(conn, row["job_id"], gate_result)
        if not gate_result.passed:
            report.gated_out += 1
            conn.execute(
                "UPDATE jobs SET status='gated_out' WHERE job_id=?", (row["job_id"],)
            )
            conn.commit()
            continue

        try:
            result = score(req, cards, weights)
        except (LLMSchemaError, LLMBackendError) as exc:
            save_failure(conn, row["job_id"], f"打分失败：{exc}")
            report.failed += 1
            report.errors.append(f"{row['job_id']}: {exc}")
            continue

        save_score(conn, result)
        report.scored += 1

        try:
            if maybe_enqueue(
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
