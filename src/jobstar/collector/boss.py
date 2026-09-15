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

from jobstar.collector.parse import dedup, extract_job_id, normalize_list_item

BASE = "https://www.zhipin.com"

# 页面改版时只改这里。2026-09-15 用 browser-harness 对活体页面校准过：
# 搜索 "后端开发" / city=101210100（杭州）。
# - card/link/title/company/city/salary/tag：与活体页面一致，已验证。
# - hr：列表卡片当前版本不展示招聘者姓名（只有详情页 .job-boss-info .name 有），
#   这里保留一个不存在的选择器，pick() 会稳定拿到 ''，hr_name 落库为空字符串。
#   真实的招聘者姓名走 detail_hr（见下）。
# - salary：真实存在，但 Boss 用自定义字体（fontFamily: kanzhun-mix）把数字字形
#   替换成占位符，innerText 读出来是 "-K"/"-K·薪" 这类脱敏文本，不是真实薪资。
#   这是反爬手段，列表页破解字体映射不在本任务范围内；真实薪资走 detail_salary。
# - detail_company：详情页唯一带 "company" 字样的选择器，但实测其中是职位名+
#   薪资徽章，不是真正的工商/企业简介。save_detail 也没有落这个字段，影响面
#   很小，如实记录。
# - detail_hr / detail_salary（2026-09-15 新增，CALIBRATED）：详情页 job_detail/
#   269040704e3bcd3e0ndy3d28EVpQ.html 实测校准。detail_hr 命中 "薛先生"；
#   detail_salary 命中 "15-30K·14薪"（真实、明文，未被 kanzhun-mix 混淆）。
#   detail_salary 特意加了 .company-info 前缀限定作用域：不加前缀的裸 .badge
#   在当前页面唯一，但 .company-info .badge 更贴近语义、更不容易在未来改版时
#   撞上别的 "热门"/推荐徽章。
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
    "detail_hr": ".job-boss-info .name",
    "detail_salary": ".company-info .badge",
}

# 登录墙的特征串。命中就中止采集，面板顶部挂横幅（设计文档 §7）。
LOGIN_MARKERS = ("请先登录", "login", "/web/user/?ka=header-login")

# Boss 的职位列表是 Vue 异步渲染的，wait_for_load() 在 load 事件那一刻就返回，
# 早于卡片渲染完成。轮询卡片选择器，给够时间等异步渲染跑完。
POLL_TIMEOUT = 15.0

# 防御性兜底：如果轮询超时后 0 条卡片，且页面文本里出现这些"没有结果"提示语，
# 就当作合法的零结果处理，不当异常抛出。
# 未在活体页面上验证过真正命中 —— 2026-09-15 实测过，Boss 对无意义关键词
# （如"保字荒钶九拖拉机"）会退化成模糊/推荐匹配而不是真零结果，超范围的
# page 参数也会静默回退到第 1 页，两种方式都没能真正触发空结果页。所以这条
# 列表目前只是"如果未来某天真的出现空结果页，能被正确识别"的保险丝，
# 而不是"零卡片=一定是合法空结果"的信号源；没有命中就按异常处理。
EMPTY_RESULT_MARKERS = ("没有找到相关职位", "换个搜索词试试", "暂无相关职位", "暂无职位")

# 抓取异常时的原始片段落盘目录（设计文档 §7）。data/fixtures/live/ 已在 .gitignore。
FAILURE_DIR = Path(__file__).resolve().parents[3] / "data" / "fixtures" / "live" / "collector_failures"


class LoginRequired(RuntimeError):
    """Boss 登录态失效。采集中止，不静默失败。"""


class CollectError(RuntimeError):
    pass


def run_script(script: str, timeout: int = 180) -> str:
    """跑一段 browser-harness 脚本，返回 stdout。"""
    exe = shutil.which("browser-harness")
    if exe is None:
        raise CollectError("browser-harness 不在 PATH 上")
    try:
        proc = subprocess.run(
            [exe], input=script, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise CollectError(f"browser-harness 执行超时（>{timeout}s）") from exc
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
  const items = Array.from(cards).map(card => {{
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
  }});
  return JSON.stringify({{items: items, body_snippet: document.body.innerText.slice(0, 2000)}});
"""


def _detail_extract_js() -> str:
    return (
        f"const jd = document.querySelector({SELECTORS['detail_jd']!r});"
        f"const co = document.querySelector({SELECTORS['detail_company']!r});"
        f"const hr = document.querySelector({SELECTORS['detail_hr']!r});"
        f"const sal = document.querySelector({SELECTORS['detail_salary']!r});"
        "return JSON.stringify({"
        "  raw_jd: jd ? jd.innerText : '',"
        "  company_info: co ? co.innerText : '',"
        "  hr_name: hr ? hr.innerText : '',"
        "  salary_raw: sal ? sal.innerText : '',"
        "  body_snippet: document.body.innerText.slice(0, 2000),"
        "});"
    )


def _guard_login(page_text: str) -> None:
    lowered = page_text.lower()
    if any(marker.lower() in lowered for marker in LOGIN_MARKERS):
        raise LoginRequired(
            "Boss 登录态失效，采集已中止。请在 Chrome 里重新扫码登录后重跑。"
        )


def _looks_like_empty_result(body_snippet: str) -> bool:
    return any(marker in body_snippet for marker in EMPTY_RESULT_MARKERS)


def search_url(keyword: str, city_code: str, page: int = 1) -> str:
    from urllib.parse import quote

    return (
        f"{BASE}/web/geek/jobs?query={quote(keyword)}"
        f"&city={city_code}&page={page}"
    )


def _list_script(url: str) -> str:
    return (
        f"new_tab({url!r})\n"
        "try:\n"
        "    wait_for_load()\n"
        f"    found = wait_for_element({SELECTORS['card']!r}, timeout={POLL_TIMEOUT})\n"
        "    print('###FOUND###' + str(found))\n"
        "    print('###PAGEINFO###' + str(page_info()))\n"
        f"    print('###DATA###' + js({_extract_js()!r}))\n"
        "finally:\n"
        "    close_tab()\n"
    )


def fetch_list(*, keyword: str, city_code: str, pages: int = 1) -> list[dict]:
    """抓 pages 页搜索结果，返回去重后的列表页摘要。"""
    collected: list[dict] = []
    for page in range(1, pages + 1):
        url = search_url(keyword, city_code, page)
        out = run_script(_list_script(url))
        head, _, data = out.partition("###DATA###")
        _guard_login(head)
        try:
            payload = json.loads(data.strip())
        except json.JSONDecodeError as exc:
            dump_path = dump_failure(f"list-p{page}-badjson", out, FAILURE_DIR)
            raise CollectError(
                f"第 {page} 页提取结果不是 JSON：{data[:300]!r}；"
                f"原始输出已存至 {dump_path}"
            ) from exc
        raw_items = payload.get("items", [])
        body_snippet = payload.get("body_snippet", "")
        _guard_login(body_snippet)
        normalized = [i for i in (normalize_list_item(r) for r in raw_items) if i]
        if not normalized:
            if _looks_like_empty_result(body_snippet):
                continue
            dump_path = dump_failure(f"list-p{page}-zero", out, FAILURE_DIR)
            raise CollectError(
                f"第 {page} 页轮询 {SELECTORS['card']!r} 后解析出 0 条卡片，"
                "既不是已知的登录墙特征，也不是已知的零结果提示语——疑似页面"
                f"被拦截或选择器需要重新校准；原始输出已存至 {dump_path}"
            )
        collected.extend(normalized)
    return dedup(collected)


def fetch_detail(url: str) -> dict:
    """抓单个岗位详情页：JD 全文、招聘者姓名、明文薪资。

    列表页的 hr_name 恒为空、salary_raw 被反爬字体混淆，真实值只在详情页能拿到
    （见 SELECTORS 顶部注释），所以这里额外把 hr_name / salary_raw 抽出来，
    交给 save_detail 回填 jobs 表。
    """
    job_id = extract_job_id(url) or "unknown"
    script = (
        f"new_tab({url!r})\n"
        "try:\n"
        "    wait_for_load()\n"
        f"    found = wait_for_element({SELECTORS['detail_jd']!r}, timeout={POLL_TIMEOUT})\n"
        "    print('###FOUND###' + str(found))\n"
        "    print('###PAGEINFO###' + str(page_info()))\n"
        f"    print('###DATA###' + js({_detail_extract_js()!r}))\n"
        "finally:\n"
        "    close_tab()\n"
    )
    out = run_script(script)
    head, _, data = out.partition("###DATA###")
    _guard_login(head)
    try:
        payload = json.loads(data.strip())
    except json.JSONDecodeError as exc:
        dump_path = dump_failure(f"detail-{job_id}-badjson", out, FAILURE_DIR)
        raise CollectError(
            f"详情页提取结果不是 JSON：{data[:300]!r}；原始输出已存至 {dump_path}"
        ) from exc
    _guard_login(payload.get("body_snippet", ""))
    if not payload.get("raw_jd", "").strip():
        dump_path = dump_failure(f"detail-{job_id}-empty-jd", out, FAILURE_DIR)
        raise CollectError(
            f"详情页轮询 {SELECTORS['detail_jd']!r} 后仍未提取到 JD 正文，"
            f"疑似被拦截或选择器需要重新校准；原始输出已存至 {dump_path}"
        )
    return payload


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
    """写入详情页抓取结果。按 (platform='boss', job_id) 定位，与 save_jobs 的
    去重键保持一致。hr_name / salary_raw 只在详情页抓到真实（非空）值时才覆盖，
    避免用空字符串冲掉列表页阶段已经落库的值。"""
    hr_name = detail.get("hr_name") or ""
    salary_raw = detail.get("salary_raw") or ""
    conn.execute(
        "UPDATE jobs SET raw_jd = ?, detail_fetched = 1, "
        "hr_name = CASE WHEN ? <> '' THEN ? ELSE hr_name END, "
        "salary_raw = CASE WHEN ? <> '' THEN ? ELSE salary_raw END "
        "WHERE job_id = ? AND platform = 'boss'",
        (detail.get("raw_jd", ""), hr_name, hr_name, salary_raw, salary_raw, job_id),
    )
    conn.commit()


def dump_failure(job_id: str, payload: str, out_dir: Path) -> Path:
    """抓取失败时留原始片段，便于事后定位页面改版（设计文档 §7）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{job_id}.txt"
    path.write_text(payload[:20000], encoding="utf-8")
    return path
