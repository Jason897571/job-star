"""Boss 直聘采集器。通过 browser-harness 驱动本地 Chrome，复用已登录的会话。

两阶段抓取（设计文档 §4.1）：先抓列表页摘要，经硬门禁筛掉大部分后，
只对幸存岗位抓详情页。详情页请求量因此压到约 1/5 —— 既是性能优化也是风控措施。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

from jobstar.collector.parse import dedup, normalize_list_item

BASE = "https://www.zhipin.com"

# 页面改版时只改这里。2026-09-15 用 browser-harness 对活体页面校准过：
# 搜索 "后端开发" / city=101210100（杭州）。
# - card/link/title/company/city/salary/tag：与活体页面一致，已验证。
# - hr：列表卡片当前版本不展示招聘者姓名（只有详情页 .job-boss-info .name 有），
#   这里保留一个不存在的选择器，pick() 会稳定拿到 ''，hr_name 落库为空字符串。
# - salary：真实存在，但 Boss 用自定义字体（fontFamily: kanzhun-mix）把数字字形
#   替换成占位符，innerText 读出来是 "-K"/"-K·薪" 这类脱敏文本，不是真实薪资。
#   这是反爬手段，不在本任务范围内解决；salary_raw 会带着这个已知限制入库。
# - detail_company：详情页唯一带 "company" 字样的选择器，但实测其中是职位名+
#   薪资徽章，不是真正的工商/企业简介。目前 save_detail 也没有落这个字段，
#   影响面很小，先如实记录。
SELECTORS: dict[str, str] = {
    "card": "li.job-card-box",
    "link": "a.job-name",
    "title": ".job-name",
    "company": ".boss-name",
    "city": ".company-location",
    "salary": ".job-salary",
    "hr": ".info-public",
    "tag": ".tag-list li",
    "detail_jd": ".job-sec-text",
    "detail_company": ".company-info",
}

# 登录墙的特征串。命中就中止采集，面板顶部挂横幅（设计文档 §7）。
LOGIN_MARKERS = ("请先登录", "login", "/web/user/?ka=header-login")


class LoginRequired(RuntimeError):
    """Boss 登录态失效。采集中止，不静默失败。"""


class CollectError(RuntimeError):
    pass


def run_script(script: str, timeout: int = 180) -> str:
    """跑一段 browser-harness 脚本，返回 stdout。"""
    exe = shutil.which("browser-harness")
    if exe is None:
        raise CollectError("browser-harness 不在 PATH 上")
    proc = subprocess.run(
        [exe], input=script, capture_output=True, text=True, timeout=timeout
    )
    if proc.returncode != 0:
        raise CollectError(
            f"browser-harness 退出码 {proc.returncode}：{proc.stderr[:800]}"
        )
    return proc.stdout


def _extract_js() -> str:
    return f"""
  const cards = document.querySelectorAll({SELECTORS["card"]!r});
  const pick = (root, sel) => {{
    const el = root.querySelector(sel);
    return el ? el.innerText : '';
  }};
  return JSON.stringify(Array.from(cards).map(card => {{
    const link = card.querySelector({SELECTORS["link"]!r});
    return {{
      url: link ? link.getAttribute('href') : '',
      title: pick(card, {SELECTORS["title"]!r}),
      company: pick(card, {SELECTORS["company"]!r}),
      city: pick(card, {SELECTORS["city"]!r}),
      salary: pick(card, {SELECTORS["salary"]!r}),
      hr: pick(card, {SELECTORS["hr"]!r}),
      tags: Array.from(card.querySelectorAll({SELECTORS["tag"]!r})).map(t => t.innerText),
    }};
  }}));
"""


def _guard_login(page_text: str) -> None:
    lowered = page_text.lower()
    if any(marker.lower() in lowered for marker in LOGIN_MARKERS):
        raise LoginRequired(
            "Boss 登录态失效，采集已中止。请在 Chrome 里重新扫码登录后重跑。"
        )


def search_url(keyword: str, city_code: str, page: int = 1) -> str:
    from urllib.parse import quote

    return (
        f"{BASE}/web/geek/jobs?query={quote(keyword)}"
        f"&city={city_code}&page={page}"
    )


def fetch_list(*, keyword: str, city_code: str, pages: int = 1) -> list[dict]:
    """抓 pages 页搜索结果，返回去重后的列表页摘要。"""
    collected: list[dict] = []
    for page in range(1, pages + 1):
        url = search_url(keyword, city_code, page)
        script = (
            f"new_tab({url!r})\n"
            "wait_for_load()\n"
            "info = page_info()\n"
            "print('###PAGEINFO###' + str(info))\n"
            f"print('###DATA###' + js({_extract_js()!r}))\n"
        )
        out = run_script(script)
        head, _, data = out.partition("###DATA###")
        _guard_login(head)
        try:
            raw = json.loads(data.strip())
        except json.JSONDecodeError as exc:
            raise CollectError(f"第 {page} 页提取结果不是 JSON：{data[:300]!r}") from exc
        collected.extend(i for i in (normalize_list_item(r) for r in raw) if i)
    return dedup(collected)


def fetch_detail(url: str) -> dict:
    """抓单个岗位详情页的 JD 全文。"""
    detail_js = (
        f"const jd = document.querySelector({SELECTORS['detail_jd']!r});"
        f"const co = document.querySelector({SELECTORS['detail_company']!r});"
        "return JSON.stringify({"
        "  raw_jd: jd ? jd.innerText : '',"
        "  company_info: co ? co.innerText : '',"
        "});"
    )
    script = (
        f"goto_url({url!r})\n"
        "wait_for_load()\n"
        "print('###PAGEINFO###' + str(page_info()))\n"
        f"print('###DATA###' + js({detail_js!r}))\n"
    )
    out = run_script(script)
    head, _, data = out.partition("###DATA###")
    _guard_login(head)
    try:
        return json.loads(data.strip())
    except json.JSONDecodeError as exc:
        raise CollectError(f"详情页提取结果不是 JSON：{data[:300]!r}") from exc


def save_jobs(conn: sqlite3.Connection, items: list[dict]) -> int:
    """写入列表页摘要。已存在的 job_id 跳过（设计文档 §4.1 去重规则）。"""
    inserted = 0
    for item in items:
        cursor = conn.execute(
            "INSERT INTO jobs (platform, job_id, title, company, raw_jd, city, "
            " salary_raw, hr_name, url) "
            "VALUES ('boss', ?, ?, ?, '', ?, ?, ?, ?) "
            "ON CONFLICT(platform, job_id) DO NOTHING",
            (
                item["job_id"],
                item["title"],
                item["company"],
                item["city"],
                item["salary_raw"],
                item["hr_name"],
                item["url"],
            ),
        )
        inserted += cursor.rowcount or 0
    conn.commit()
    return inserted


def save_detail(conn: sqlite3.Connection, job_id: str, detail: dict) -> None:
    conn.execute(
        "UPDATE jobs SET raw_jd = ?, detail_fetched = 1 WHERE job_id = ?",
        (detail.get("raw_jd", ""), job_id),
    )
    conn.commit()


def dump_failure(job_id: str, payload: str, out_dir: Path) -> Path:
    """抓取失败时留原始片段，便于事后定位页面改版（设计文档 §7）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{job_id}.txt"
    path.write_text(payload[:20000], encoding="utf-8")
    return path
