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
from jobstar.models import JobRequirements
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

    rules = dict(get_setting(conn, "gate_rules"))
    rules.setdefault("my_degree", get_setting(conn, "my_degree"))

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
) -> int | None:
    """总分达到阈值才生成待确认动作。阈值为 None（冷启动期）时永远不生成。"""
    threshold = get_setting(conn, "score_threshold")
    if threshold is None or result.total < float(threshold):
        return None
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
    rules = dict(get_setting(conn, "gate_rules"))
    rules.setdefault("my_degree", get_setting(conn, "my_degree"))

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
                conn, req, result, title=row["title"], company=row["company"] or ""
            ) is not None:
                report.enqueued += 1
        except (LLMSchemaError, LLMBackendError, ValueError) as exc:
            report.errors.append(f"{row['job_id']} 话术生成失败: {exc}")

    return report
