"""抓取结果的纯后处理。没有 DOM 逻辑 —— 取值在浏览器里用 JS 做。"""

from __future__ import annotations

import re

BASE = "https://www.zhipin.com"

_JOB_ID = re.compile(r"/job_detail/([A-Za-z0-9_-]+)")
_WS = re.compile(r"\s+")


def extract_job_id(url: str | None) -> str | None:
    """从详情页 URL 抠出平台唯一 id。查询串每次刷新都变，必须丢掉。"""
    if not url:
        return None
    match = _JOB_ID.search(url)
    return match.group(1) if match else None


def clean_text(value: object) -> str:
    if value is None:
        return ""
    return _WS.sub(" ", str(value)).strip()


def normalize_list_item(raw: dict) -> dict | None:
    """把一条列表页原始记录整理成入库形状。不合格返回 None。"""
    url = clean_text(raw.get("url"))
    job_id = extract_job_id(url)
    title = clean_text(raw.get("title"))
    if not job_id or not title:
        return None
    if url.startswith("/"):
        url = BASE + url
    city = clean_text(raw.get("city")).split("·")[0]
    tags = [clean_text(t) for t in (raw.get("tags") or []) if clean_text(t)]
    return {
        "job_id": job_id,
        "url": url,
        "title": title,
        "company": clean_text(raw.get("company")),
        "city": city,
        "salary_raw": clean_text(raw.get("salary")),
        "hr_name": clean_text(raw.get("hr")),
        "tags": tags,
    }


def dedup(items: list[dict]) -> list[dict]:
    """按 job_id 去重，保留第一次出现的。"""
    seen: set[str] = set()
    out: list[dict] = []
    for item in items:
        job_id = item.get("job_id")
        if not job_id or job_id in seen:
            continue
        seen.add(job_id)
        out.append(item)
    return out
