"""执行器：只消费 approved 状态的动作，不做任何判断。

与决策器彻底分离带来的性质：执行器可以独立调节流和风控，不受打分逻辑变更影响；
决策器可以离线回放历史岗位调权重，而不可能误发消息。
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable

from jobstar import actions
from jobstar.collector.boss import run_script

# 对数正态参数：中位数 e^3.6 ≈ 36 秒，长尾能拉到几分钟
_DELAY_MU = 3.6
_DELAY_SIGMA = 0.9
_DELAY_MIN = 5.0
_DELAY_MAX = 600.0


@dataclass
class ExecutionReport:
    sent: int = 0
    failed: int = 0
    quota_hit: bool = False
    errors: list[str] = field(default_factory=list)


def human_delay(rng: random.Random) -> float:
    """动作之间的随机延迟。对数正态而非均匀分布 —— 均匀间隔本身就是风控信号。"""
    value = math.exp(rng.normalvariate(_DELAY_MU, _DELAY_SIGMA))
    return max(_DELAY_MIN, min(_DELAY_MAX, value))


# 聊天框与发送按钮的选择器。与 collector.boss.SELECTORS 同源，页面改版时一起改。
# 单独提成常量是为了避免在 f-string 里嵌套三引号 —— 那会提前终止外层字符串。
_FOCUS_INPUT_JS = (
    "const box = document.querySelector("
    "'#chat-input, textarea.input-area, div[contenteditable=true]');"
    "if (!box) return 'no-input';"
    "box.focus();"
    "return 'ok';"
)

_CLICK_SEND_JS = (
    "const btn = document.querySelector('.btn-send, button[type=submit]');"
    "if (!btn) return 'no-send-button';"
    "btn.click();"
    "return 'ok';"
)


def send_greeting(job_url: str, text: str) -> None:
    """真实浏览器动作：打开岗位详情页，点「立即沟通」，填文案，发送。

    按钮用无障碍树按名字找而不是靠 CSS 类名 —— Boss 的类名比按钮文案更容易变。
    """
    payload = json.dumps({"url": job_url, "text": text}, ensure_ascii=False)
    script = "\n".join(
        [
            "import json",
            f"args = json.loads({payload!r})",
            'goto_url(args["url"])',
            "wait_for_load()",
            'nodes = cdp("Accessibility.getFullAXTree")["nodes"]',
            "target = None",
            "for n in nodes:",
            '    name = (n.get("name") or {}).get("value") or ""',
            '    role = (n.get("role") or {}).get("value") or ""',
            '    if role == "button" and ("立即沟通" in name or "继续沟通" in name):',
            '        target = n["backendDOMNodeId"]',
            "        break",
            "if target is None:",
            '    raise SystemExit("找不到「立即沟通」按钮")',
            'quad = cdp("DOM.getBoxModel", backendNodeId=target)["model"]["content"]',
            "click_at_xy(sum(quad[0::2]) / 4, sum(quad[1::2]) / 4)",
            "wait_for_load()",
            f"ok = js({_FOCUS_INPUT_JS!r})",
            'if ok != "ok":',
            '    raise SystemExit("找不到聊天输入框: " + str(ok))',
            'cdp("Input.insertText", text=args["text"])',
            f"sent = js({_CLICK_SEND_JS!r})",
            'if sent != "ok":',
            '    raise SystemExit("找不到发送按钮: " + str(sent))',
            'print("SENT_OK")',
        ]
    )
    out = run_script(script)
    if "SENT_OK" not in out:
        raise RuntimeError(f"发送未确认成功：{out[-500:]}")


def _record_application(
    conn: sqlite3.Connection, action_row: sqlite3.Row, greeting: str
) -> None:
    job = conn.execute(
        "SELECT hr_name FROM jobs WHERE job_id = ?", (action_row["job_id"],)
    ).fetchone()
    score = conn.execute(
        "SELECT total, dimensions, scorer_version FROM scores WHERE job_id = ?",
        (action_row["job_id"],),
    ).fetchone()
    snapshot = (
        {
            "total": score["total"],
            "dimensions": json.loads(score["dimensions"]),
            "scorer_version": score["scorer_version"],
        }
        if score is not None
        else {}
    )
    conn.execute(
        "INSERT INTO applications (job_id, action_id, hr_name, greeting_text, "
        " score_snapshot) VALUES (?, ?, ?, ?, ?)",
        (
            action_row["job_id"],
            action_row["id"],
            job["hr_name"] if job else None,
            greeting,
            json.dumps(snapshot, ensure_ascii=False),
        ),
    )
    conn.commit()


def run_queue(
    conn: sqlite3.Connection,
    *,
    send_fn: Callable[[str, str], None] = send_greeting,
    sleep_fn: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> ExecutionReport:
    rng = rng or random.Random()
    report = ExecutionReport()
    queue = actions.list_by_status(conn, actions.APPROVED)

    for index, row in enumerate(queue):
        if actions.remaining_quota(conn) <= 0:
            report.quota_hit = True
            break
        if index > 0:
            sleep_fn(human_delay(rng))

        payload = json.loads(row["payload"])
        greeting = payload.get("greeting", "")
        job = conn.execute(
            "SELECT url FROM jobs WHERE job_id = ?", (row["job_id"],)
        ).fetchone()
        url = (job["url"] if job else None) or ""

        try:
            send_fn(url, greeting)
        except Exception as exc:  # 任何失败都只留痕，不自动重试
            actions.mark_failed(conn, row["id"], str(exc))
            report.failed += 1
            report.errors.append(f"{row['job_id']}: {exc}")
            continue

        actions.mark_sent(conn, row["id"])
        _record_application(conn, row, greeting)
        report.sent += 1

    return report
