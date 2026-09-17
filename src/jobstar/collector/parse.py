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


# Boss 列表页的薪资用自定义字体做混淆：数字被换成 Unicode 私用区码位，
# 靠 kanzhun-mix 这个字体渲染成人眼可读的数字。innerText 取到的就是这些
# 码位，看起来像 "-K·薪"——数字不是被抠掉了，是在那儿但不是 ASCII 数字。
#
# 映射是 0xE031+n → n。2026-09-16 在两个独立会话上各验证过一遍（库里存量
# 15 条 + 现场重新加载的 16 条），31/31 全部解码成合理薪资。
#
# 但这是**逆向出来的常量，不是契约**：Boss 随时可能换映射。所以解码之后
# 一定要验，验不过就当「不知道薪资」，让门禁按字段缺失放过，退化成这个功能
# 上线之前的行为。绝不能把一个解错的数字交出去——门禁会照着它筛，错的薪资
# 比没有薪资危险得多。
_PUA_DIGITS = {chr(0xE031 + n): str(n) for n in range(10)}
_PUA_START, _PUA_END = 0xE000, 0xF8FF
# 见过的形状：25-45K·14薪 / 30-50K。日薪、"1.5-3万" 这类一律验不过 → None。
#
# 这个正则是**全锚定、字符集受限**的，这一点承担着安全职责：出现没见过的
# 私用区码位时，那个字符解不出数字、会原样留在串里，于是必然匹配不上。
# 换句话说「残留未知码位」这种情况由它一并挡住，不需要单独再查一遍。
# 改这个正则的人注意：一旦放宽成带 .* 之类的形式，就要把那道单独的检查加回来。
_SALARY_SHAPE = re.compile(r"^(\d{1,3})-(\d{1,3})K(?:·(\d{1,2})薪)?$")
# 「几薪」的现实范围。这是单条校验里最强的一道闸：映射一旦平移，带「薪」的
# 串会解出 20 以上这种不可能的月数，当场露馅。没有它的话，平移后的
# `36-56K·25薪` 会因为形状合法而被当成真薪资交出去。
_MONTHS_RANGE = (12, 18)


def decode_obfuscated_salary(text: str) -> str | None:
    """把私用区编码的薪资解成明文。不可信时返回 None。

    两道闸：形状不认识（连带挡掉「出现了没见过的码位」，见 _SALARY_SHAPE 的
    注释）、数值不合理（下限不小于上限、月数离谱），任一不过就返回 None。
    """
    if not text:
        return None
    decoded = "".join(_PUA_DIGITS.get(ch, ch) for ch in text)
    match = _SALARY_SHAPE.match(decoded)
    if not match:
        return None
    low, high = int(match.group(1)), int(match.group(2))
    if not 0 < low < high <= 999:
        return None
    if match.group(3) is not None:
        months = int(match.group(3))
        if not _MONTHS_RANGE[0] <= months <= _MONTHS_RANGE[1]:
            return None
    return decoded


def has_obfuscated_digits(text: str) -> bool:
    return any(_PUA_START <= ord(ch) <= _PUA_END for ch in text or "")


def decode_salaries(items: list[dict]) -> list[dict]:
    """整批解码一页的薪资，并在整批这一层判断映射还可不可信。

    为什么不是逐条决定：单条校验挡不住「整套映射被换了」——平移之后
    `30-50K` 会变成 `41-61K`，形状和数值都挑不出毛病。但在一整页上，带
    「薪」的那些会解出不可能的月数而失败。所以这里看的是**成功率**：一页里
    混淆过的薪资超过半数解不出来，就认定映射变了，整批退回原串，让门禁按
    「不知道薪资」放过——退化成这个功能上线之前的行为。

    原地改传进来的 dict（和 normalize_list_item 的产物是同一批），返回同一个
    列表只是为了调用处读起来顺。
    """
    obfuscated = [i for i in items if has_obfuscated_digits(i.get("salary_raw", ""))]
    if not obfuscated:
        return items
    decoded = [decode_obfuscated_salary(i["salary_raw"]) for i in obfuscated]
    if sum(1 for d in decoded if d) * 2 < len(obfuscated):
        return items
    for item, value in zip(obfuscated, decoded):
        if value:
            item["salary_raw"] = value
    return items


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
        # 原样留着。解码在 decode_salaries 里按整页做——信任决策要放在证据
        # 最多的地方，单条看不出「整套映射被换了」。
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
