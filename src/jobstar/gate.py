"""硬门禁：纯规则过滤，不调用 LLM。

设计目标是刷掉 60-80% 明显不合的岗位而不花 token。因此判断偏宽松：
字段为 None（归一化没抽出来）一律放过，交给打分器去看原文。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from jobstar.models import GateResult, JobRequirements

DEGREE_ORDER: dict[str, int] = {
    "不限": 0,
    "中专": 1,
    "高中": 1,
    "大专": 2,
    "本科": 3,
    "硕士": 4,
    "博士": 5,
}


def check(req: JobRequirements, rules: dict[str, Any], company: str = "") -> GateResult:
    """返回是否通过 + 拒绝原因。原因要落库，便于事后检查门禁是否过严。"""
    whitelist = rules.get("city_whitelist") or []
    if whitelist and req.city and req.city not in whitelist:
        return GateResult(False, f"城市不在白名单：{req.city}")

    salary_floor = rules.get("salary_min")
    ceiling = req.salary_max if req.salary_max is not None else req.salary_min
    if salary_floor is not None and ceiling is not None and ceiling < salary_floor:
        return GateResult(
            False, f"薪资上限 {ceiling}K 低于门槛 {salary_floor}K"
        )

    years_max_rule = rules.get("years_max")
    if years_max_rule is not None and req.years_min is not None:
        if req.years_min > years_max_rule:
            return GateResult(
                False, f"要求年限下限 {req.years_min} 年高于上限设置 {years_max_rule} 年"
            )

    years_min_rule = rules.get("years_min")
    if years_min_rule is not None and req.years_max is not None:
        if req.years_max < years_min_rule:
            return GateResult(
                False, f"要求年限上限 {req.years_max} 年低于下限设置 {years_min_rule} 年"
            )

    if rules.get("degree_hard") and req.degree:
        mine = DEGREE_ORDER.get(rules.get("my_degree", "硕士"), 4)
        needed = DEGREE_ORDER.get(req.degree)
        if needed is not None and needed > mine:
            return GateResult(False, f"学历卡死且要求 {req.degree} 高于本人学历")

    for banned in rules.get("company_blacklist") or []:
        if banned and banned in company:
            return GateResult(False, f"公司在黑名单：{banned}")

    return GateResult(True, None)


def save_result(conn: sqlite3.Connection, job_id: str, result: GateResult) -> None:
    conn.execute(
        "INSERT INTO gate_results (job_id, passed, reject_reason) VALUES (?, ?, ?) "
        "ON CONFLICT(job_id) DO UPDATE SET "
        "passed = excluded.passed, "
        "reject_reason = excluded.reject_reason, "
        "checked_at = datetime('now')",
        (job_id, int(result.passed), result.reject_reason),
    )
    conn.commit()
