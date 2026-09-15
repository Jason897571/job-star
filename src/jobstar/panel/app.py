"""本地 Web 面板：唯一的人工入口。四个视图共用这套 API。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse

from jobstar import actions
from jobstar.config import (
    SETTING_DEFAULTS,
    get_setting,
    get_settings,
    set_setting,
)
from jobstar.db import get_conn, init_db
from jobstar.evidence import load_cards
from jobstar.models import DIMENSION_LABELS

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="jobstar 面板")


def get_db() -> sqlite3.Connection:
    conn = get_conn(get_settings().db_path)
    init_db(conn)
    return conn


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
    failed = conn.execute(
        "SELECT COUNT(*) AS n FROM scores WHERE error IS NOT NULL"
    ).fetchone()["n"]
    uncertain = conn.execute(
        "SELECT COUNT(*) AS n FROM actions WHERE status = ? AND error IS NOT NULL",
        (actions.SENDING,),
    ).fetchone()["n"]
    return {
        "remaining_quota": actions.remaining_quota(conn),
        "sent_today": actions.sent_today(conn),
        "daily_limit": get_setting(conn, "daily_greeting_limit"),
        "score_threshold": get_setting(conn, "score_threshold"),
        "scoring_failed": failed,
        "pending": len(actions.list_by_status(conn, actions.PENDING)),
        "uncertain": uncertain,
    }


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
    return {"items": items, "uncertain": uncertain}


@app.post("/api/actions/{action_id}/approve")
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
    override = None
    if body.get("greeting"):
        payload = json.loads(row["payload"])
        payload["greeting"] = body["greeting"]
        override = payload
    try:
        actions.approve(conn, action_id, override)
    except actions.InvalidTransition as exc:
        raise HTTPException(
            409, _conflict_message(conn, action_id, "确认", exc)
        ) from exc
    return {"ok": True}


@app.post("/api/actions/{action_id}/skip")
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


@app.post("/api/label")
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


@app.put("/api/settings")
def write_settings(
    body: dict = Body(...), conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    unknown = [k for k in body if k not in SETTING_DEFAULTS]
    if unknown:
        raise HTTPException(400, f"未知配置项：{unknown}")
    for key, value in body.items():
        set_setting(conn, key, value)
    return {"ok": True}
