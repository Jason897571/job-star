"""本地 Web 面板：唯一的人工入口。四个视图共用这套 API。"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from jobstar import actions
from jobstar.config import (
    SETTING_DEFAULTS,
    get_setting,
    get_settings,
    set_setting,
)
from jobstar.db import get_conn, init_db
from jobstar.evidence import (
    CardValidationError,
    dump_cards,
    load_cards,
    validate_cards,
)
from jobstar.models import DIMENSION_LABELS
from jobstar.panel.runner import RUNNER, AlreadyRunning, TaskRunner

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Minor 10：建表只需要在进程启动时做一次。放进 lifespan 而不是每个请求
    的依赖里——`executescript` 每次都会在这份数据库文件上取一次短暂的写锁，
    而这个文件同时被执行器进程写；请求量一高，纯粹为了"确保表存在"这件事
    就会和真正的写入抢锁。用 `@app.on_event("startup")` 会在这个 FastAPI/
    Starlette 版本上打出 DeprecationWarning（与"0 警告"要求冲突），改用
    `lifespan` 上下文管理器。"""
    conn = get_conn(get_settings().db_path)
    try:
        init_db(conn)
    finally:
        conn.close()
    yield


app = FastAPI(title="jobstar 面板", lifespan=_lifespan)

PANEL_HEADER = "X-Jobstar-Panel"


def require_panel_request(request: Request) -> None:
    """CSRF 防护（review finding 1）。

    面板绑定在 127.0.0.1，挡得住远程攻击者，但挡不住同一台机器、同一个
    浏览器里另一个页面发起的跨源请求：一个不带自定义头的简单 POST 不需要
    CORS 预检，浏览器照样会把它发出去——即使那个恶意页面读不到响应内容，
    请求本身已经在服务端产生了副作用（比如把一条消息批准发送）。这正是
    这个面板存在的意义要防住的事：只有面板自己的按钮点击才能批准发送。

    这里要求所有变更状态的路由都带上一个只有面板自己的 JS 会发送的自定义
    请求头。浏览器对「携带自定义头的跨源 fetch」要求先发一次 CORS 预检
    (OPTIONS)，而这个服务器没有配置 CORS、不会满足预检条件，恶意页面的
    真实请求根本到不了下面的处理函数——所以自定义头本身不需要保密，它挡
    的是浏览器的同源策略机制，不是靠猜不到。

    额外校验 Origin：如果浏览器带了 Origin 头（同源请求在部分浏览器里会
    省略它，另一些浏览器会照样带上本面板自己的源，两种都要放行），但它
    不是本面板自己的源，说明这个请求源头本来就不对，直接拒绝。
    """
    if request.headers.get(PANEL_HEADER) != "1":
        raise HTTPException(403, "缺少面板请求头，拒绝执行（疑似跨站请求）")
    origin = request.headers.get("origin")
    if origin is not None:
        expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
        if origin != expected:
            raise HTTPException(403, "请求来源不是本面板，拒绝执行")


def get_db() -> Iterator[sqlite3.Connection]:
    """每个请求一个连接，请求结束后关闭（Minor 10：原来的实现是普通函数，
    FastAPI 对普通函数依赖不做任何收尾，每个请求都会泄漏一个 sqlite3.Connection）。

    改成 `yield` 式依赖后，测试仍然可以用
    `app.dependency_overrides[get_db] = lambda: conn` 整体替换掉这个依赖——
    FastAPI 按„覆盖后实际使用的那个可调用对象是不是生成器函数”来决定要不要
    做收尾；测试传入的 lambda 不是生成器函数，所以下面这个 `finally: close()`
    根本不会对测试用的共享连接执行，测试夹具自己控制关闭时机的既有模式不受影响。
    """
    conn = get_conn(get_settings().db_path)
    try:
        yield conn
    finally:
        conn.close()


def _conflict_message(
    conn: sqlite3.Connection, action_id: int, verb: str, exc: Exception
) -> str:
    """把 `InvalidTransition` 的内部措辞换成人工能看懂的提示。

    `actions.InvalidTransition` 的默认信息是给开发者看的（"动作 X 不能从
    sending 变成 skipped"）。面板要处理的一个具体场景是：执行器已经把这
    一行原子认领成 sending（正在发送中，或发送完但结果还不确定），这时人
    工在面板上点「确认」/「跳过」必然落空——sending 不允许被 approve 或
    skip 当来源状态（见 actions._ALLOWED_FROM），这是故意的：正在发送/
    结果不确定的消息不能再被人工的决定改变。这种情况必须清楚地说「来不
    及」，而不是把上面那句内部措辞原样透传成一个看不懂的 409。

    行不存在、或处于其他任何终态（sent/等）时，仍然把 `str(exc)` 原样返回——
    那些情况不是本函数要处理的目标场景。
    """
    row = conn.execute(
        "SELECT status FROM actions WHERE id = ?", (action_id,)
    ).fetchone()
    if row is not None and row["status"] == actions.SENDING:
        return f"已在发送中，来不及{verb}"
    return str(exc)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    # Minor 3：`total IS NULL` 是区分两类失败的既有信号——真正的打分/归一化
    # 失败（scorer.save_failure）从不写分数，`total` 恒为 NULL；话术生成
    # 失败（pipeline._save_pitch_failure）发生在分数已经真实写入之后，
    # `total` 不为 NULL。两者都在 `scores.error` 上留话，但不能混进同一个
    # "打分失败"计数里——那会让一条分数其实成立的岗位被人工误当成需要
    # 重新打分去核实。
    failed = conn.execute(
        "SELECT COUNT(*) AS n FROM scores WHERE error IS NOT NULL AND total IS NULL"
    ).fetchone()["n"]
    pitch_failed = conn.execute(
        "SELECT COUNT(*) AS n FROM scores WHERE error IS NOT NULL AND total IS NOT NULL"
    ).fetchone()["n"]
    uncertain = conn.execute(
        "SELECT COUNT(*) AS n FROM actions WHERE status = ? AND error IS NOT NULL",
        (actions.SENDING,),
    ).fetchone()["n"]
    # Minor 5：执行器在 mark_sending 认领之后、真正调用 send_fn 之前被打断
    # （Ctrl-C、宕机）留下的行——status='sending' 且 error IS NULL。这类行
    # 既不在待确认队列（不是 pending），也不会被上面 uncertain 的查询计入
    # （它专门筛 error IS NOT NULL），此前在任何视图里都看不到。这里单独
    # 计数，配合 /api/queue 里对应的列表和面板的 resolve 按钮，让它不再是
    # 一条"消失"的岗位。
    interrupted = conn.execute(
        "SELECT COUNT(*) AS n FROM actions WHERE status = ? AND error IS NULL",
        (actions.SENDING,),
    ).fetchone()["n"]
    # 下一个本地零点：配额按本地日期计数（actions.remaining_quota 同一时区
    # 假设），这里只是把这个边界暴露给面板显示，不改变配额本身的计算方式。
    tomorrow = (datetime.now() + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return {
        "remaining_quota": actions.remaining_quota(conn),
        "sent_today": actions.sent_today(conn),
        "daily_limit": get_setting(conn, "daily_greeting_limit"),
        "quota_resets_at": tomorrow.strftime("%Y-%m-%d %H:%M:%S"),
        "score_threshold": get_setting(conn, "score_threshold"),
        "scoring_failed": failed,
        "pitch_failed": pitch_failed,
        "pending": len(actions.list_by_status(conn, actions.PENDING)),
        "uncertain": uncertain,
        "interrupted": interrupted,
        # 采集健康状态（Task 13）：由 CLI 的 collect 子命令写入。这是被动
        # 信号——只反映上一次真的跑过 collect 时观察到的结果，没有主动的
        # 活体探测（探测本身要消耗一次页面请求，对账号风控而言不划算）。
        # last_collect_at 是「最近一次尝试」，last_collect_ok_at 是「最近
        # 一次成功」——二者不相等时，说明最近一次尝试其实失败了（fix round
        # 1，review finding 1：不能让人工点掉错误横幅后，横幅转头就用失败
        # 那次的时间戳宣称「未见异常」）。
        "last_collect_error": get_setting(conn, "last_collect_error"),
        "last_collect_at": get_setting(conn, "last_collect_at"),
        "last_collect_ok_at": get_setting(conn, "last_collect_ok_at"),
    }


@app.post(
    "/api/health/clear-error", dependencies=[Depends(require_panel_request)]
)
def clear_collect_error(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """人工点了横幅上的「知道了」。只清错误文案，不动 last_collect_at——
    那是「上次跑 collect 的时间」的记录，跟这条错误是否已经被人工看过是
    两件事。"""
    set_setting(conn, "last_collect_error", None)
    return {"ok": True}


@app.get("/api/queue")
def queue(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    rows = conn.execute(
        "SELECT a.id, a.job_id, a.payload, j.title, j.company, j.city, "
        "       j.salary_raw, j.url, s.total "
        "FROM actions a "
        "JOIN jobs j ON j.job_id = a.job_id "
        "LEFT JOIN scores s ON s.job_id = a.job_id "
        "WHERE a.status = ? ORDER BY s.total DESC NULLS LAST, a.created_at",
        (actions.PENDING,),
    ).fetchall()
    items = []
    for row in rows:
        payload = json.loads(row["payload"])
        items.append(
            {
                "action_id": row["id"],
                "job_id": row["job_id"],
                "title": row["title"],
                "company": row["company"],
                "city": row["city"],
                "salary_raw": row["salary_raw"],
                "url": row["url"],
                "total": row["total"],
                "greeting": payload.get("greeting", ""),
            }
        )

    # 「发送结果不确定」的行（sending 且带 error）不属于待确认队列——它们
    # 既不是 pending，也不应该被允许重新批准/跳过（sending 是故意不可逆
    # 的，见 _conflict_message）。但也不能让它们在队列视图之外彻底消失：
    # 人工需要看到岗位信息和错误提示，才能去 Boss 对话列表核实。这里单独
    # 返回一份只读列表，前端不为它渲染确认/跳过按钮。
    uncertain_rows = conn.execute(
        "SELECT a.id, a.job_id, a.error, j.title, j.company, j.url "
        "FROM actions a JOIN jobs j ON j.job_id = a.job_id "
        "WHERE a.status = ? AND a.error IS NOT NULL "
        "ORDER BY a.sent_at",
        (actions.SENDING,),
    ).fetchall()
    uncertain = [
        {
            "action_id": row["id"],
            "job_id": row["job_id"],
            "title": row["title"],
            "company": row["company"],
            "url": row["url"],
            "error": row["error"],
        }
        for row in uncertain_rows
    ]

    # Minor 5：另一种 sending——执行器 mark_sending 认领之后、真正调用
    # send_fn 之前被打断（Ctrl-C、宕机），行停在 status='sending' 且
    # error IS NULL。这类行此前不在 uncertain_rows 的查询范围内（它筛
    # error IS NOT NULL），也不是 pending，此前在任何视图里都彻底看不到。
    # 和 uncertain 用同样的形状单独返回一份，前端分开渲染、分开措辞，
    # 但都配「resolve」按钮——都需要人工去 Boss 核实之后记录结果。
    interrupted_rows = conn.execute(
        "SELECT a.id, a.job_id, j.title, j.company, j.url "
        "FROM actions a JOIN jobs j ON j.job_id = a.job_id "
        "WHERE a.status = ? AND a.error IS NULL "
        "ORDER BY a.sent_at",
        (actions.SENDING,),
    ).fetchall()
    interrupted = [
        {
            "action_id": row["id"],
            "job_id": row["job_id"],
            "title": row["title"],
            "company": row["company"],
            "url": row["url"],
        }
        for row in interrupted_rows
    ]
    return {"items": items, "uncertain": uncertain, "interrupted": interrupted}


@app.post(
    "/api/actions/{action_id}/approve", dependencies=[Depends(require_panel_request)]
)
def approve_action(
    action_id: int,
    body: dict = Body(default={}),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    row = conn.execute(
        "SELECT payload FROM actions WHERE id = ?", (action_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "动作不存在")
    # 「键不存在」和「键存在但是空串」是两件完全不同的事，不能都当成
    # 「不覆盖」：
    #   - 键不存在 = 调用方没打算改话术（程序化批准），保留库里那版；
    #   - 键存在但为空 = 人工把话术框清空了。原来的 `if body.get(...)` 是
    #     真值判断，空串是假值，于是 override 保持 None、批准的是库里那版
    #     LLM 原稿——人工看着空框点了「确认发送」，发出去的却是他没看过、
    #     更没同意的那段文字。这直接违反「人工批准的就是发出去的那段」。
    # 空话术也不能直接放行：给 HR 发一条空消息既没意义，又会白白烧掉一次
    # 每日配额和一个真人的注意力。这里报错，让人工自己决定是重写还是跳过。
    override = None
    if "greeting" in body:
        greeting = body["greeting"]
        if not isinstance(greeting, str):
            raise HTTPException(
                400, f"话术必须是字符串，收到的是 {type(greeting).__name__}"
            )
        if not greeting.strip():
            raise HTTPException(
                400, "话术是空的，拒绝按库里的原稿发送；请重写话术，或者点「跳过」"
            )
        payload = json.loads(row["payload"])
        payload["greeting"] = greeting
        override = payload
    try:
        actions.approve(conn, action_id, override)
    except actions.InvalidTransition as exc:
        raise HTTPException(
            409, _conflict_message(conn, action_id, "确认", exc)
        ) from exc
    return {"ok": True}


@app.post(
    "/api/actions/{action_id}/skip", dependencies=[Depends(require_panel_request)]
)
def skip_action(
    action_id: int, conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    try:
        actions.skip(conn, action_id)
    except actions.InvalidTransition as exc:
        raise HTTPException(
            409, _conflict_message(conn, action_id, "跳过", exc)
        ) from exc
    return {"ok": True}


@app.post(
    "/api/actions/{action_id}/resolve", dependencies=[Depends(require_panel_request)]
)
def resolve_action(
    action_id: int,
    body: dict = Body(...),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Minor 5：`sending` 状态目前没有人工出口——面板只暴露 approve/skip，
    两者都不允许以 sending 为来源状态（这是故意的：正在发送/结果不确定的
    消息不能被人工的确认/跳过决定改变，见 actions._ALLOWED_FROM）。这条路
    由让人工在自己去 Boss 对话列表核实之后，记录他们看到的结果——`sending
    → sent`（确认已送达）或 `sending → failed`（确认未送达）。两个迁移在
    `actions._ALLOWED_FROM` 里本来就合法（`SENT` 的来源包含 `SENDING`，
    `FAILED` 的来源也包含 `SENDING`），这里只是把面板对这两个已有写入函数
    的调用权限打开——不创建任何新的发送路径，纯粹是把人工已经在 Boss 上
    验证过的事实记录进数据库。
    """
    outcome = body.get("outcome")
    if outcome not in ("sent", "failed"):
        raise HTTPException(400, "outcome 必须是 'sent' 或 'failed'")
    try:
        if outcome == "sent":
            actions.mark_sent(conn, action_id)
        else:
            note = body.get("note") or "人工核实：确认此消息未送达"
            actions.mark_failed(conn, action_id, note)
    except actions.InvalidTransition as exc:
        # 这里不复用 `_conflict_message`：那个助手是为「人工的 approve/skip
        # 撞上了已经被执行器认领成 sending 的行」这个场景写的，专门把
        # sending 特判成「来不及」。resolve 的方向正相反——它要解决的正是
        # 「这一行现在是不是 sending」，失败通常意味着它已经不在 sending
        # 了（比如已经被解决过、或者从来就不是 sending），`InvalidTransition`
        # 的原始措辞（"动作 X 不能从 Y 变成 Z"）在这里就是人工能看懂的信息。
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True}


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    job = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if job is None:
        raise HTTPException(404, "岗位不存在")
    score_row = conn.execute(
        "SELECT * FROM scores WHERE job_id = ?", (job_id,)
    ).fetchone()
    gate_row = conn.execute(
        "SELECT * FROM gate_results WHERE job_id = ?", (job_id,)
    ).fetchone()

    card_index = {c.id: c for c in load_cards(get_settings().cards_path)}
    dimensions = []
    for dim in json.loads(score_row["dimensions"]) if score_row else []:
        dimensions.append(
            {
                "name": dim["name"],
                "label": DIMENSION_LABELS.get(dim["name"], dim["name"]),
                "score": dim["score"],
                "reason": dim["reason"],
                "gap": dim["gap"],
                "cards": [
                    {
                        "id": cid,
                        "capability": card_index[cid].capability,
                        "strength": card_index[cid].strength.value,
                        "project": card_index[cid].project,
                        "depth": card_index[cid].depth,
                    }
                    for cid in dim["card_ids"]
                    if cid in card_index
                ],
            }
        )
    return {
        "job_id": job_id,
        "title": job["title"],
        "company": job["company"],
        "city": job["city"],
        "salary_raw": job["salary_raw"],
        "url": job["url"],
        "raw_jd": job["raw_jd"],
        "status": job["status"],
        "total": score_row["total"] if score_row else None,
        "scorer_version": score_row["scorer_version"] if score_row else None,
        "error": score_row["error"] if score_row else None,
        "gate_passed": bool(gate_row["passed"]) if gate_row else None,
        "gate_reason": gate_row["reject_reason"] if gate_row else None,
        "dimensions": dimensions,
    }


@app.get("/api/label/next")
def label_next(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    row = conn.execute(
        "SELECT j.job_id, j.title, j.company, j.city, j.salary_raw, j.raw_jd, "
        "       j.url, s.total "
        "FROM jobs j JOIN scores s ON s.job_id = j.job_id "
        "LEFT JOIN labels l ON l.job_id = j.job_id "
        "WHERE s.total IS NOT NULL AND l.job_id IS NULL "
        "ORDER BY j.collected_at LIMIT 1"
    ).fetchone()
    if row is None:
        return {"job_id": None}
    return dict(row)


@app.post("/api/label", dependencies=[Depends(require_panel_request)])
def write_label(
    body: dict = Body(...), conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    job_id = body.get("job_id")
    if not job_id:
        raise HTTPException(400, "缺少 job_id")
    conn.execute(
        "INSERT INTO labels (job_id, would_apply) VALUES (?, ?) "
        "ON CONFLICT(job_id) DO UPDATE SET "
        "would_apply = excluded.would_apply, labeled_at = datetime('now')",
        (job_id, int(bool(body.get("would_apply")))),
    )
    conn.commit()
    return {"ok": True}


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "min": None, "max": None, "mean": None}
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "mean": round(sum(values) / len(values), 2),
    }


@app.get("/api/threshold")
def threshold(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """标注分布，用于反推阈值（设计文档 §5.3 第 3 步）。"""
    rows = conn.execute(
        "SELECT l.would_apply, s.total FROM labels l "
        "JOIN scores s ON s.job_id = l.job_id WHERE s.total IS NOT NULL"
    ).fetchall()
    yes = [r["total"] for r in rows if r["would_apply"]]
    no = [r["total"] for r in rows if not r["would_apply"]]
    return {
        "labeled_total": len(rows),
        "would_apply": _stats(yes),
        "would_not_apply": _stats(no),
        "scores_yes": sorted(yes),
        "scores_no": sorted(no),
    }


@app.get("/api/settings")
def read_settings(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    return {key: get_setting(conn, key) for key in SETTING_DEFAULTS}


# --- 能力卡片编辑器 ---------------------------------------------------------
#
# 这份文件（data/capability_cards.yaml）是整套系统防止简历吹牛的唯一防线，
# 也是唯一一份含个人隐私、被 .gitignore 整目录排除的数据。面板改它的三条
# 纪律：写盘前跑和启动时同一套校验；每次覆盖前留一份带时间戳的备份；写入
# 用临时文件 + 原子替换，中途失败不会留下一个半截的卡片库。


def _cards_dir_backup(path: Path) -> Path:
    backups = path.parent / "card-backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return backups / f"{path.stem}-{stamp}{path.suffix}"


@app.get("/api/cards")
def read_cards() -> dict:
    """卡片库当前内容。文件坏掉时不抛 500，而是把错误交给前端显示——
    否则人工会卡在一个「打不开也修不了」的死角里。前端在 error 非空时
    会禁用保存按钮，避免用一个空列表把 40 张卡片覆盖掉。"""
    path = get_settings().cards_path
    try:
        cards = load_cards(path)
    except (CardValidationError, OSError, yaml.YAMLError) as exc:
        return {"path": str(path), "cards": [], "error": str(exc)}
    return {
        "path": str(path),
        "error": None,
        "cards": [
            {
                "id": c.id,
                "capability": c.capability,
                "synonyms": list(c.synonyms),
                "strength": c.strength.value,
                "project": c.project,
                "metrics": list(c.metrics),
                "depth": c.depth,
                "resume_versions": list(c.resume_versions),
            }
            for c in cards
        ],
    }


@app.put("/api/cards", dependencies=[Depends(require_panel_request)])
def write_cards(body: dict = Body(...), conn: sqlite3.Connection = Depends(get_db)) -> dict:
    incoming = body.get("cards")
    if not isinstance(incoming, list):
        raise HTTPException(400, "cards 必须是列表")

    # 前端送的是 ASCII 字段名，文件里是中文键——换回来，顺便把「这一张卡片
    # 的哪个字段不对」的定位留给 evidence 那套既有校验。
    raw = []
    for i, card in enumerate(incoming):
        if not isinstance(card, dict):
            raise HTTPException(400, f"第 {i + 1} 张卡片不是对象")
        raw.append(
            {
                cn: card.get(field)
                for cn, field in (
                    ("id", "id"),
                    ("能力", "capability"),
                    ("同义表述", "synonyms"),
                    ("证据强度", "strength"),
                    ("项目", "project"),
                    ("可量化", "metrics"),
                    ("可讲深度", "depth"),
                    ("关联简历版本", "resume_versions"),
                )
            }
        )
    try:
        cards = validate_cards(raw)
    except CardValidationError as exc:
        raise HTTPException(400, str(exc)) from exc

    path = get_settings().cards_path
    text = dump_cards(cards)
    try:
        if path.exists():
            shutil.copy2(path, _cards_dir_backup(path))
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)  # 同目录内的原子替换
    except OSError as exc:
        raise HTTPException(500, f"写入卡片库失败：{exc}") from exc

    strong = sum(1 for c in cards if c.strength.value == "强")
    return {"ok": True, "count": len(cards), "strong": strong}


# --- 面板触发的采集 / 打分 ---------------------------------------------------


def get_runner() -> TaskRunner:
    """后台任务运行器。做成依赖而不是直接引用模块级单例，是为了让测试能
    用 `app.dependency_overrides` 换成一个干净实例——共用单例的话，前一个
    测试留下的状态会让后一个测试收到 409。"""
    return RUNNER


@app.get("/api/run/status")
def run_status(runner: TaskRunner = Depends(get_runner)) -> dict:
    return {"task": runner.snapshot()}


def _busy(exc: AlreadyRunning) -> HTTPException:
    return HTTPException(409, str(exc))


def _search_params(body: dict) -> tuple[list[str], str, int]:
    """校验搜索条件。拉列表和一把梭采集共用——两处各写一份的话，迟早只有
    一处会跟着改，另一处就成了绕过校验的后门。"""
    keywords = [
        k.strip() for k in (body.get("keywords") or []) if isinstance(k, str) and k.strip()
    ]
    if not keywords:
        raise HTTPException(400, "至少要填一个搜索关键词")
    city = str(body.get("city") or "").strip()
    if not city.isdigit():
        raise HTTPException(400, "城市码必须是数字（Boss 的 city code，杭州是 101210100）")
    raw_pages = body.get("pages")
    try:
        # 不能写 `body.get("pages") or 1`：0 是假值，会被悄悄当成 1——
        # 人工填了个非法值，系统却装作他填的是 1。只有「没填」才用默认值。
        pages = 1 if raw_pages is None else int(raw_pages)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "页数必须是整数") from exc
    if not 1 <= pages <= 10:
        raise HTTPException(400, "页数只能是 1-10")
    return keywords, city, pages


@app.post("/api/run/stop", dependencies=[Depends(require_panel_request)])
def run_stop(runner: TaskRunner = Depends(get_runner)) -> dict:
    """请求停止正在跑的任务。

    只置标志就返回，不等后台线程收工——任务当前多半正阻塞在一次 LLM 调用
    或一次详情页抓取上（各自 180 秒超时），在这个请求里等会把 HTTP 也挂住。
    面板靠轮询看到 `stop_requested` 为真、状态仍是 running，显示「正在停止…」。
    """
    if not runner.request_stop():
        raise HTTPException(409, "现在没有正在跑的任务")
    return {"ok": True}


@app.get("/api/candidates")
def candidates(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """已经拉到列表、但还没抓详情页的岗位，供面板做勾选。

    `salary_raw` 在这一步基本是废的：Boss 列表页用反爬字体把数字抠掉了
    （抓下来长这样 `-K·薪`），真实薪资只有详情页才有。照原样返回而不是
    藏起来——藏起来人会以为是系统丢了字段。
    """
    rows = conn.execute(
        "SELECT j.job_id, j.title, j.company, j.city, j.salary_raw, j.url, "
        "       j.collected_at, g.passed, g.reject_reason "
        "FROM jobs j LEFT JOIN gate_results g ON g.job_id = j.job_id "
        "WHERE j.detail_fetched = 0 "
        "ORDER BY j.collected_at DESC, j.id"
    ).fetchall()
    return {
        "items": [
            {
                "job_id": r["job_id"],
                "title": r["title"],
                "company": r["company"],
                "city": r["city"],
                "salary_raw": r["salary_raw"],
                "url": r["url"],
                "collected_at": r["collected_at"],
                # passed 为 None = 还没跑过预门禁（理论上不该出现，防御性保留）
                "passed": None if r["passed"] is None else bool(r["passed"]),
                "reject_reason": r["reject_reason"],
            }
            for r in rows
        ]
    }


@app.post("/api/run/list", dependencies=[Depends(require_panel_request)])
def run_list_task(
    body: dict = Body(...),
    conn: sqlite3.Connection = Depends(get_db),
    runner: TaskRunner = Depends(get_runner),
) -> dict:
    """第一步：只拉列表页 + 跑预门禁，一个详情页请求都不发。"""
    keywords, city, pages = _search_params(body)
    set_setting(conn, "last_search", {"keywords": keywords, "city": city, "pages": pages})
    label = f"{keywords[0]} 等 {len(keywords)} 个关键词" if len(keywords) > 1 else keywords[0]

    def work(note, should_stop):
        from jobstar.collector.boss import LoginRequired
        from jobstar.pipeline import run_list

        task_conn = get_conn(get_settings().db_path)
        totals = {"listed": 0, "new": 0, "gated_out": 0, "candidates": 0,
                  "stopped": False, "errors": []}
        try:
            for keyword in keywords:
                if should_stop():
                    totals["stopped"] = True
                    note("已停止，剩下的关键词不再拉取")
                    break
                note(f"▶ 拉取「{keyword}」列表（{pages} 页）")
                try:
                    report = run_list(
                        task_conn,
                        keyword=keyword,
                        city_code=city,
                        pages=pages,
                        on_progress=note,
                    )
                except LoginRequired as exc:
                    set_setting(
                        task_conn, "last_collect_error", f"Boss 登录态失效，已中止：{exc}"
                    )
                    set_setting(task_conn, "last_collect_at", _stamp())
                    raise RuntimeError(
                        f"Boss 登录态失效，已中止：{exc}。请在 Chrome 里重新扫码登录后再试"
                    ) from exc
                for key in ("listed", "new", "gated_out", "candidates"):
                    totals[key] += getattr(report, key)
                totals["errors"].extend(report.errors)
            _record_collect_health(task_conn, totals)
            note("列表拉完了。到下面的候选列表里挑要抓详情页的岗位。")
            return totals
        finally:
            task_conn.close()

    try:
        return {"task": runner.start("list", f"拉列表：{label}", work)}
    except AlreadyRunning as exc:
        raise _busy(exc) from exc


@app.post("/api/run/detail", dependencies=[Depends(require_panel_request)])
def run_detail_task(
    body: dict = Body(...),
    runner: TaskRunner = Depends(get_runner),
) -> dict:
    """第二步：只对人工点名的岗位抓详情页。"""
    job_ids = body.get("job_ids")
    if not isinstance(job_ids, list) or not job_ids:
        raise HTTPException(400, "至少要选一个岗位")
    if not all(isinstance(j, str) and j.strip() for j in job_ids):
        raise HTTPException(400, "job_ids 必须是非空字符串列表")
    job_ids = [j.strip() for j in job_ids]
    if len(job_ids) > 200:
        # 一次点名 200 个以上，多半是误操作（比如全选了好几轮攒下来的候选）。
        # 详情页请求是打在 Boss 上的真实流量，宁可让人分批。
        raise HTTPException(400, f"一次最多 200 个，当前选了 {len(job_ids)} 个")

    def work(note, should_stop):
        from jobstar.pipeline import override_gate, run_details

        task_conn = get_conn(get_settings().db_path)
        try:
            # 人工勾了被预门禁刷掉的岗位 = 明确要覆盖规则。不把门禁翻过来的话，
            # 抓回来的详情页永远等不到打分，勾选等于什么都没发生。
            flipped = override_gate(task_conn, job_ids)
            for line in flipped:
                note(f"人工覆盖预门禁：{line}")
            note(f"▶ 开始抓 {len(job_ids)} 个岗位的详情页")
            report = run_details(
                task_conn, job_ids, on_progress=note, should_stop=should_stop
            )
            return {
                "stopped": report.stopped,
                "remaining": report.remaining,
                "detail_fetched": report.fetched,
                "already_fetched": report.skipped,
                "errors": report.errors,
            }
        finally:
            task_conn.close()

    try:
        return {"task": runner.start("detail", f"抓 {len(job_ids)} 个详情页", work)}
    except AlreadyRunning as exc:
        raise _busy(exc) from exc


@app.post("/api/run/collect", dependencies=[Depends(require_panel_request)])
def run_collect_task(
    body: dict = Body(...),
    conn: sqlite3.Connection = Depends(get_db),
    runner: TaskRunner = Depends(get_runner),
) -> dict:
    keywords, city, pages = _search_params(body)
    # 记住这次的搜索条件，下次打开面板直接预填
    set_setting(conn, "last_search", {"keywords": keywords, "city": city, "pages": pages})

    label = f"{keywords[0]} 等 {len(keywords)} 个关键词" if len(keywords) > 1 else keywords[0]

    def work(note, should_stop):
        from jobstar.collector.boss import LoginRequired
        from jobstar.pipeline import run_collect

        task_conn = get_conn(get_settings().db_path)
        totals = {
            "listed": 0, "new": 0, "gated_out": 0, "detail_fetched": 0,
            "stopped": False, "remaining": 0, "errors": [],
        }
        try:
            for keyword in keywords:
                # 关键词之间也是一个停止边界——不然点了停止还要把剩下的
                # 关键词整个跑完。
                if should_stop():
                    totals["stopped"] = True
                    note("已停止，剩下的关键词不再采集")
                    break
                note(f"▶ 开始采集「{keyword}」（{pages} 页）")
                try:
                    report = run_collect(
                        task_conn,
                        keyword=keyword,
                        city_code=city,
                        pages=pages,
                        on_progress=note,
                        should_stop=should_stop,
                    )
                except LoginRequired as exc:
                    # 登录态失效是会话级硬故障：剩下的关键词再跑也只是拿一个
                    # 已经失效的会话反复撞墙。整轮中止，写进健康状态让横幅显示。
                    set_setting(
                        task_conn, "last_collect_error", f"Boss 登录态失效，采集已中止：{exc}"
                    )
                    set_setting(task_conn, "last_collect_at", _stamp())
                    raise RuntimeError(
                        f"Boss 登录态失效，采集已中止：{exc}。"
                        "请在 Chrome 里重新扫码登录后再试"
                    ) from exc
                for key in ("listed", "new", "gated_out", "detail_fetched"):
                    totals[key] += getattr(report, key)
                totals["errors"].extend(report.errors)
                if report.stopped:
                    totals["stopped"] = True
                    totals["remaining"] += report.remaining
                    break
            _record_collect_health(task_conn, totals)
            return totals
        finally:
            task_conn.close()

    try:
        return {"task": runner.start("collect", label, work)}
    except AlreadyRunning as exc:
        raise _busy(exc) from exc


@app.post("/api/run/score", dependencies=[Depends(require_panel_request)])
def run_score_task(
    body: dict = Body(default={}),
    runner: TaskRunner = Depends(get_runner),
) -> dict:
    rescore = bool(body.get("rescore"))
    limit = body.get("limit")
    if limit is not None:
        try:
            limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "条数上限必须是整数") from exc
        if limit < 1:
            raise HTTPException(400, "条数上限至少是 1")

    def work(note, should_stop):
        from jobstar.pipeline import clear_for_rescore, run_score

        task_conn = get_conn(get_settings().db_path)
        try:
            if rescore:
                cleared = clear_for_rescore(task_conn)
                note(
                    f"已清除 {cleared} 个岗位的吸收态"
                    "（打分失败／话术失败／第二遍门禁刷掉／已打过分）"
                )
                if cleared == 0:
                    note("没有岗位处于吸收态——这一轮和不勾「重跑」完全一样")
            report = run_score(
                task_conn, limit=limit, on_progress=note, should_stop=should_stop
            )
            return {
                "stopped": report.stopped,
                "remaining": report.remaining,
                "scored": report.scored,
                "gated_out": report.gated_out,
                "failed": report.failed,
                "pitch_failed": report.pitch_failed,
                "enqueued": report.enqueued,
                "already_queued": report.already_queued,
                "retracted": report.retracted,
                "errors": report.errors,
            }
        finally:
            task_conn.close()

    try:
        return {"task": runner.start("score", "重跑打分" if rescore else "打分", work)}
    except AlreadyRunning as exc:
        raise _busy(exc) from exc


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _record_collect_health(conn: sqlite3.Connection, totals: dict) -> None:
    """和 CLI 的 collect 写同一套健康状态，横幅才认得（见 cli.py 的注释）。

    last_collect_at 记「最近一次尝试」，成功失败都前进；last_collect_ok_at
    只在真正干净的那次前进——两者不相等时横幅就不能宣称「未见异常」。
    """
    now = _stamp()
    if totals["listed"] == 0 and totals["errors"]:
        set_setting(
            conn,
            "last_collect_error",
            "本次没有抓到任何列表条目——可能是关键词真的零匹配，"
            "也可能是页面被拦截，无法自动区分，请人工核实",
        )
    elif totals["errors"]:
        set_setting(
            conn,
            "last_collect_error",
            f"{len(totals['errors'])} 个岗位抓取失败（可能是页面结构变更）："
            + "；".join(totals["errors"][:3]),
        )
    else:
        set_setting(conn, "last_collect_error", None)
        set_setting(conn, "last_collect_ok_at", now)
    set_setting(conn, "last_collect_at", now)


@app.put("/api/settings", dependencies=[Depends(require_panel_request)])
def write_settings(
    body: dict = Body(...), conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    unknown = [k for k in body if k not in SETTING_DEFAULTS]
    if unknown:
        raise HTTPException(400, f"未知配置项：{unknown}")
    for key, value in body.items():
        try:
            set_setting(conn, key, value)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    return {"ok": True}
