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
    # 列表页的地点是「市·区·商圈」三段，例如 杭州·滨江区·长河。以前这里只留
    # 第一段就把后两段扔了——而区县恰恰是**在抓详情页之前**就能拿到的唯一
    # 地理信息，扔掉它等于把「按区域筛掉一半再决定抓谁」这个能力也扔了。
    # 段数不固定（有的岗位只给到市），所以逐段取、缺的留空字符串。
    parts = [p for p in clean_text(raw.get("city")).split("·") if p]
    tags = [clean_text(t) for t in (raw.get("tags") or []) if clean_text(t)]
    return {
        "job_id": job_id,
        "url": url,
        "title": title,
        "company": clean_text(raw.get("company")),
        "city": parts[0] if parts else "",
        "district": parts[1] if len(parts) > 1 else "",
        "business_area": parts[2] if len(parts) > 2 else "",
        "salary_raw": clean_text(raw.get("salary")),
        "hr_name": clean_text(raw.get("hr")),
        "tags": tags,
    }


# 中国陆地的大致经纬度范围。超出这个范围的值不是「远」，是解析错了——
# 与其存一个会让距离计算胡说八道的坐标，不如当作没有。
_LNG_RANGE = (73.0, 136.0)
_LAT_RANGE = (3.0, 54.0)


def parse_coords(raw: object) -> tuple[float, float] | None:
    """把详情页的 `data-lat="120.008921,30.282488"` 解析成 (经度, 纬度)。

    属性名是 Boss 起的，里面其实是**经度在前**；坐标系是 GCJ-02（页面上用
    的是高德）。解析不出来或超出中国范围一律返回 None——距离功能宁可不显示，
    也不能显示一个错的。
    """
    parts = clean_text(raw).split(",")
    if len(parts) != 2:
        return None
    try:
        lng, lat = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if not (_LNG_RANGE[0] <= lng <= _LNG_RANGE[1]):
        return None
    if not (_LAT_RANGE[0] <= lat <= _LAT_RANGE[1]):
        return None
    return lng, lat


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
