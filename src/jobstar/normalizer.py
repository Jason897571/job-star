"""把 JD 自由文本抽成结构化的 JobRequirements。一次 LLM 调用，便宜档。"""

from __future__ import annotations

import json
import re
import sqlite3

from jobstar.llm import call_json
from jobstar.models import JobRequirements

SYSTEM = """你是招聘 JD 的结构化抽取器。读一段岗位描述，输出一个 JSON 对象。

字段与取值约定：
- city: 工作城市，只写城市名（如「杭州」），抽不到写 null
- degree: 学历要求，只能是 "不限"/"大专"/"本科"/"硕士"/"博士" 之一，抽不到写 null
- years_min / years_max: 经验年限的下限和上限，整数。「3-5年」→ 3 和 5；
  「3年以上」→ 3 和 null；「不限」→ 0 和 null；抽不到写 null
- salary_min / salary_max: 月薪，单位千元。「20-35K」→ 20 和 35。抽不到写 null
- skills: 技能要求列表，字符串数组。只写 JD 里明确提到的技术名词，不要推断
- industry: 公司所属行业，抽不到写 null
- company_size: 公司规模，抽不到写 null
- category: 岗位类别（如「后端开发」「解决方案架构师」），抽不到写 null

只输出 JSON 对象本身。不要 markdown 围栏，不要解释。抽不到的字段写 null，不要编。"""

# 第一个 K 是可选的：Boss 上「20-35K」和「30K-50K」两种写法都有
_SALARY_K = re.compile(r"(\d+(?:\.\d+)?)\s*[Kk千]?\s*[-~]\s*(\d+(?:\.\d+)?)\s*[Kk千]")
_SALARY_K_SINGLE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[Kk千]")
_SALARY_W = re.compile(r"(\d+(?:\.\d+)?)\s*[-~]\s*(\d+(?:\.\d+)?)\s*万")


def parse_salary_raw(text: str | None) -> tuple[int | None, int | None]:
    """把 Boss 的薪资串解析成千元/月区间。解析不出来返回 (None, None)。

    年薪（「8-12万·年」）故意不解析 —— 换算口径不确定，交给 LLM 从 JD 里看。
    """
    if not text:
        return (None, None)
    if "年" in text:
        return (None, None)
    match = _SALARY_K.search(text)
    if match:
        return (int(float(match.group(1))), int(float(match.group(2))))
    match = _SALARY_W.search(text)
    if match:
        return (int(float(match.group(1)) * 10), int(float(match.group(2)) * 10))
    match = _SALARY_K_SINGLE.match(text)
    if match:
        value = int(float(match.group(1)))
        return (value, value)
    return (None, None)


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return None
    return None


def _as_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v).strip() for v in value if str(v).strip())
    return ()


def normalize(
    *,
    job_id: str,
    title: str,
    raw_jd: str,
    city_hint: str | None = None,
    salary_hint: str | None = None,
) -> JobRequirements:
    """抽取结构化需求。列表页给的 city_hint / salary_hint 优先于模型的推断。"""
    user = f"岗位名称：{title}\n\n岗位描述：\n{raw_jd}"
    data = call_json(system=SYSTEM, user=user, tier="fast")

    salary_min, salary_max = parse_salary_raw(salary_hint)
    if salary_min is None:
        salary_min = _as_int(data.get("salary_min"))
        salary_max = _as_int(data.get("salary_max"))

    return JobRequirements(
        job_id=job_id,
        city=_as_str(city_hint) or _as_str(data.get("city")),
        degree=_as_str(data.get("degree")),
        years_min=_as_int(data.get("years_min")),
        years_max=_as_int(data.get("years_max")),
        salary_min=salary_min,
        salary_max=salary_max,
        skills=_as_tuple(data.get("skills")),
        industry=_as_str(data.get("industry")),
        company_size=_as_str(data.get("company_size")),
        category=_as_str(data.get("category")),
    )


def save_requirements(conn: sqlite3.Connection, req: JobRequirements) -> None:
    conn.execute(
        "INSERT INTO requirements "
        "(job_id, city, degree, years_min, years_max, salary_min, salary_max, "
        " skills, industry, company_size, category) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(job_id) DO UPDATE SET "
        "city=excluded.city, degree=excluded.degree, "
        "years_min=excluded.years_min, years_max=excluded.years_max, "
        "salary_min=excluded.salary_min, salary_max=excluded.salary_max, "
        "skills=excluded.skills, industry=excluded.industry, "
        "company_size=excluded.company_size, category=excluded.category",
        (
            req.job_id,
            req.city,
            req.degree,
            req.years_min,
            req.years_max,
            req.salary_min,
            req.salary_max,
            json.dumps(list(req.skills), ensure_ascii=False),
            req.industry,
            req.company_size,
            req.category,
        ),
    )
    conn.commit()


def load_requirements(
    conn: sqlite3.Connection, job_id: str
) -> JobRequirements | None:
    row = conn.execute(
        "SELECT * FROM requirements WHERE job_id = ?", (job_id,)
    ).fetchone()
    if row is None:
        return None
    return JobRequirements(
        job_id=row["job_id"],
        city=row["city"],
        degree=row["degree"],
        years_min=row["years_min"],
        years_max=row["years_max"],
        salary_min=row["salary_min"],
        salary_max=row["salary_max"],
        skills=tuple(json.loads(row["skills"])),
        industry=row["industry"],
        company_size=row["company_size"],
        category=row["category"],
    )
