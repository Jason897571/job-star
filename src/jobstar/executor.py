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
from jobstar.collector import boss

# 对数正态参数：中位数 e^3.6 ≈ 36 秒，长尾能拉到几分钟
_DELAY_MU = 3.6
_DELAY_SIGMA = 0.9
_DELAY_MIN = 5.0
_DELAY_MAX = 600.0


@dataclass
class ExecutionReport:
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    uncertain: int = 0
    quota_hit: bool = False
    login_required: bool = False
    errors: list[str] = field(default_factory=list)


def human_delay(rng: random.Random) -> float:
    """动作之间的随机延迟。对数正态而非均匀分布 —— 均匀间隔本身就是风控信号。"""
    value = math.exp(rng.normalvariate(_DELAY_MU, _DELAY_SIGMA))
    return max(_DELAY_MIN, min(_DELAY_MAX, value))


# 聊天框/发送按钮/我方消息气泡的选择器：直接读 collector.boss.SELECTORS，
# 与列表页/详情页选择器同源，页面改版时在同一个字典里一起改（不再各自维护
# 一份），并标了 UNVERIFIED——校准状态见 boss.py 顶部注释。
# 单独提成常量是为了避免在 f-string 里嵌套三引号 —— 那会提前终止外层字符串。
_FOCUS_INPUT_JS = (
    "const box = document.querySelector("
    f"{boss.SELECTORS['chat_input']!r});"
    "if (!box) return 'no-input';"
    "box.focus();"
    "return 'ok';"
)

_CLICK_SEND_JS = (
    "const btn = document.querySelector("
    f"{boss.SELECTORS['chat_send']!r});"
    "if (!btn) return 'no-send-button';"
    "btn.click();"
    "return 'ok';"
)

# 发送后的读回校验：按钮被点到了不代表消息真的发出去了（Input.insertText
# 打在了没聚焦的元素上，或聊天框还没挂载完，按钮依旧存在、依旧能点）。读
# 回聊天框是否已清空、最新一条我方气泡是否包含刚才发的文案，交给 Python
# 侧比较（JS 只负责取值），比较逻辑才能在不开浏览器的情况下被测试覆盖。
_CONFIRM_SENT_JS = (
    "const box = document.querySelector("
    f"{boss.SELECTORS['chat_input']!r});"
    "const bubbles = document.querySelectorAll("
    f"{boss.SELECTORS['chat_outgoing_bubble']!r});"
    "const last = bubbles.length ? bubbles[bubbles.length - 1] : null;"
    "return JSON.stringify({"
    "composer_text: box ? (box.value !== undefined ? box.value : box.innerText) : null,"
    "last_bubble_text: last ? last.innerText : null"
    "});"
)

# 点击发送按钮之后、读回确认之前打印的标记。发送按钮点没点到，和「点到了
# 之后有没有确认成功」是两件性质完全不同的事——前者什么都没发生，后者
# 消息很可能已经真的发出去了。这个标记出现在捕获到的文本（stdout，失败
# 时还有 CollectError 带出来的部分输出）里，send_greeting/run_queue 就据此
# 分辨一次失败发生在点击之前还是之后，见 SendUncertain。
_CLICK_MARKER = "###CLICKED###"

# Finding 3：上面这个标记是 js(_CLICK_SEND_JS) 那次 Runtime.evaluate 往返
# 成功返回 "ok" 之后才打印的——如果往返本身失败（意外导航把执行上下文
# 干掉、CDP 传输错误……），btn.click() 可能已经在页面里真的执行过了，但
# Python 侧永远等不到那个 "ok"，_CLICK_MARKER 也就永远不会被打印，这次
# 失败就会被误判成「点击之前」，退配额、允许重新批准，酿成重复发送。这个
# 标记改在调用 js() 之前、由 Python 侧单独打印，把「即将发起点击」和「往
# 返是否成功完成」拆成两件事——send_greeting 的点击前/点击后判断改用这
# 个更早的标记兜底，_CLICK_MARKER 仍然保留，只作为往返确实成功的诊断信息。
_PRE_CLICK_MARKER = "###ABOUT_TO_CLICK###"


def _build_send_script(job_url: str, text: str) -> str:
    """组装 browser-harness 脚本：开新标签页、点「立即沟通」、填文案、发送、
    读回确认，finally 里始终关闭标签页。

    登录墙检测复用 collector.boss 的特征串（不重复维护一份），做法是在拿到
    「立即沟通」按钮之前就先打印 page_info() 和一段正文摘要——即使后面因为
    按钮找不到而 raise SystemExit，这两行也已经写进了 stdout，调用方在拿到
    完整输出后统一跑 boss._guard_login 判断是不是登录态失效（哪怕这次
    browser-harness 以非零退出码收场，boss.CollectError 现在也会把已经写出
    的 stdout 带出去，见 send_greeting）。

    点击发送按钮之前先打印 _PRE_CLICK_MARKER（Finding 3）：js(_CLICK_SEND_JS)
    这次 Runtime.evaluate 往返本身可能失败于 btn.click() 已经执行之后，那种
    情况下靠往返成功之后才打印的 _CLICK_MARKER 分不清点没点到，所以边界要
    提前到「即将调用 js()」这一刻。往返成功之后再打印 _CLICK_MARKER，再等
    我方消息气泡选择器（wait_for_element，超时沿用 boss.POLL_TIMEOUT，和上
    面等聊天输入框出现是同一种手法）给异步渲染留出时间，最后才读回确认——
    按钮被点到不等于气泡已经渲染完，读回跟得太紧只会把「气泡还没出现」误
    判成「没发出去」。
    """
    payload = json.dumps({"url": job_url, "text": text}, ensure_ascii=False)
    lines = [
        "import json",
        f"args = json.loads({payload!r})",
        'new_tab(args["url"])',
        "try:",
        "    wait_for_load()",
        "    print('###PAGEINFO###' + str(page_info()))",
        "    body = js('return document.body.innerText.slice(0, 2000);')",
        "    print('###BODY###' + str(body))",
        '    nodes = cdp("Accessibility.getFullAXTree")["nodes"]',
        "    target = None",
        "    for n in nodes:",
        '        name = (n.get("name") or {}).get("value") or ""',
        '        role = (n.get("role") or {}).get("value") or ""',
        '        if role == "button" and ("立即沟通" in name or "继续沟通" in name):',
        '            target = n["backendDOMNodeId"]',
        "            break",
        "    if target is None:",
        '        raise SystemExit("找不到「立即沟通」按钮")',
        '    quad = cdp("DOM.getBoxModel", backendNodeId=target)["model"]["content"]',
        "    click_at_xy(sum(quad[0::2]) / 4, sum(quad[1::2]) / 4)",
        "    wait_for_load()",
        f"    found = wait_for_element({boss.SELECTORS['chat_input']!r}, "
        f"timeout={boss.POLL_TIMEOUT})",
        "    if not found:",
        '        raise SystemExit("聊天输入框在超时内未出现")',
        f"    ok = js({_FOCUS_INPUT_JS!r})",
        '    if ok != "ok":',
        '        raise SystemExit("找不到聊天输入框: " + str(ok))',
        '    cdp("Input.insertText", text=args["text"])',
        f"    print({_PRE_CLICK_MARKER!r})",
        f"    sent = js({_CLICK_SEND_JS!r})",
        '    if sent != "ok":',
        '        raise SystemExit("找不到发送按钮: " + str(sent))',
        f"    print({_CLICK_MARKER!r})",
        f"    wait_for_element({boss.SELECTORS['chat_outgoing_bubble']!r}, "
        f"timeout={boss.POLL_TIMEOUT})",
        f"    confirm_raw = js({_CONFIRM_SENT_JS!r})",
        "    confirm = json.loads(confirm_raw)",
        '    composer_text = (confirm.get("composer_text") or "").strip()',
        '    last_bubble = confirm.get("last_bubble_text") or ""',
        '    if composer_text != "":',
        '        raise SystemExit('
        '"发送后聊天框未清空，怀疑未真正发送: " + composer_text[:200])',
        '    expected_prefix = args["text"][:20]',
        "    if expected_prefix and expected_prefix not in last_bubble:",
        '        raise SystemExit('
        '"发送后最新消息未包含文案前缀，怀疑发送失败: " + last_bubble[:200])',
        '    print("SENT_OK")',
        "finally:",
        "    close_tab()",
    ]
    return "\n".join(lines)


class SendUncertain(RuntimeError):
    """发送按钮已经点击，但发送后确认失败：消息很可能已经真的发出去了，
    仅凭这一端的信息无法百分之百排除，需要人工去 Boss 对话列表核实。

    区别于普通异常——run_queue 收到这个之后不会 mark_failed（那样会把配额
    退还给一条可能已经送达的消息，也允许人工在「消息可能已发出」的情况下
    重新批准，酿成给同一个真人重复发送），而是调用 actions.note_uncertain
    只留一句人工核实提示，行原地留在 sending。
    """


def send_greeting(job_url: str, text: str) -> None:
    """真实浏览器动作：开新标签页，打开岗位详情页，点「立即沟通」，填文案，
    发送，读回确认，最后无论成功失败都关闭这个标签页。

    按钮用无障碍树按名字找而不是靠 CSS 类名 —— Boss 的类名比按钮文案更容易变。

    登录墙检测在成功和失败两条路径上都要跑，且都要先看 SENT_OK 在不在场：
    - 成功路径（退出码 0）：`out` 就是完整 stdout。如果里面已经有 SENT_OK，
      这条消息已经确认送达，不再跑登录墙检测就直接返回——LOGIN_MARKERS
      里有裸词 "login"，成功发送之后的页面正文完全可能无辜地包含它，先认
      SENT_OK 才不会把一条已经送达的消息误判成登录态失效。
    - 失败路径（`boss.CollectError`，来自非零退出码或超时）：`run_script`
      抛出的异常本身丢的是 stdout（Finding 1 之前就是这样）——现在
      `CollectError.stdout` 把它带出来了，这里和异常消息（含 stderr 片段）
      拼成 `combined`。这条路径修复前完全跑不到：session 过期 → 找不到
      「立即沟通」按钮 → SystemExit → 退出码非零 → 登录墙特征串跟着
      stdout 一起被丢弃。

    这条失败路径上的判断顺序是刻意的，不是随手写的先后：
    1. 先认 SENT_OK——`_build_send_script` 的 finally 块在 SENT_OK 打印之后
       才执行 `close_tab()`，如果那一步报错，进程会以非零退出码收场，但消
       息已经确认送达。这不是失败也不是不确定，直接当成功返回（Finding 2），
       不能让纯粹的收尾噪音把一条已经送达的消息打成人工核实提示。
    2. 再看点击标记（`_PRE_CLICK_MARKER` 或 `_CLICK_MARKER`）在不在场，命中
       就抛 `SendUncertain`，**不再**往下跑登录墙检测（Finding 1）。原因：
       能走到「即将点击」这一步，前面已经先后扛过了「立即沟通」按钮查找、
       聊天输入框等待两轮失败点——真正的登录态失效会在那两步之一就
       `SystemExit`，走不到点击这里。`LOGIN_MARKERS` 里的裸词 "login" 完全
       可能无辜地出现在 `_build_send_script` 一开始就打印的 ###BODY### 页面
       正文摘要里（这段摘要在找「立即沟通」按钮之前就已经写进 stdout，点击
       前点击后的失败都带着它），如果登录墙检测跑在点击标记检测之前，会把
       「点击之后才失败、消息可能已经发出」误判成「登录态失效」——那样会
       mark_failed 退配额、允许人工重新批准，酿成给同一个真人重复发送。
       **不要把这个顺序「修」回登录墙检测在前**——那正是本轮要修的洞。
    3. 走到这里说明点击从未发起，才是登录墙检测该管的范围，跑
       `boss._guard_login` 再把原始异常抛出去。

    点击发送按钮之后才失败（例如气泡还没渲染完、chat_outgoing_bubble 选择
    器猜错、close_tab 报错、run_script 超时、往返本身失败于点击已经发起之
    后）和点击之前的失败（例如「立即沟通」按钮本身没找到）语义完全不同——
    前者「消息可能已经发出」。
    """
    try:
        out = boss.run_script(_build_send_script(job_url, text))
    except boss.CollectError as exc:
        combined = f"{exc.stdout}\n{exc}"
        if "SENT_OK" in combined:
            return
        if _PRE_CLICK_MARKER in combined or _CLICK_MARKER in combined:
            raise SendUncertain(str(exc)) from exc
        boss._guard_login(combined)
        raise
    if "SENT_OK" in out:
        return
    boss._guard_login(out)
    raise RuntimeError(f"发送未确认成功：{out[-500:]}")


def _record_application(
    conn: sqlite3.Connection, action_row: sqlite3.Row, greeting: str
) -> None:
    job = conn.execute(
        "SELECT hr_name FROM jobs WHERE job_id = ? AND platform = 'boss'",
        (action_row["job_id"],),
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


def _guarded_write(
    report: ExecutionReport, job_id: str, write: Callable[[], None]
) -> None:
    """执行一次状态记账（mark_sent/mark_failed/note_uncertain），把
    `sqlite3.OperationalError`（db.py 用的是默认 5s busy timeout，本地面板
    并发写同一个数据库文件时会撞上「database is locked」）转成
    `report.errors` 里的一条记录，而不是让它从这里逃出 `run_queue`——那样
    整份 report 和队列里剩下的行会被一起丢掉，正是上一轮修复设法消除的
    那种失败形状。"""
    try:
        write()
    except sqlite3.OperationalError as exc:
        report.errors.append(f"{job_id}: 状态写入失败（数据库繁忙）：{exc}")


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

    # 是否已经有过一行真正调用过 send_fn。延迟只应该出现在两次真实发送
    # 之间——按队列里的行号计（不管这一行会不会真的发送）会让人工每跳过
    # 一行，执行器都白白多等一次 5-600s，见 Minor 3。只在确定要调用
    # send_fn 之前才置 True（认领成功但缺 URL、从未打开浏览器的行不算，
    # 见 Minor 5），否则这种行会替下一行——很可能是第一次真正发送——
    # 白白占用一次本不该有的延迟。
    attempted = False

    for row in queue:
        if actions.remaining_quota(conn) <= 0:
            report.quota_hit = True
            break

        # 认领这一行：把「来源状态是否允许」折进原子 UPDATE，而不是先读一次
        # 状态再决定发不发——两次读写之间人工可能已经在面板上点了「跳过」，
        # 或者另一个执行器进程已经抢先认领了它。认领失败说明这一行此刻已经
        # 不再可发，直接跳过、不调用 send_fn、不消耗一次人类延迟，继续处理
        # 队列里剩下的行。
        try:
            actions.mark_sending(conn, row["id"])
        except actions.InvalidTransition:
            report.skipped += 1
            continue

        payload = json.loads(row["payload"])
        greeting = payload.get("greeting", "")
        job = conn.execute(
            "SELECT url FROM jobs WHERE job_id = ? AND platform = 'boss'",
            (row["job_id"],),
        ).fetchone()
        url = (job["url"] if job else None) or ""
        if not isinstance(greeting, str) or not greeting.strip():
            # 最后一道闸：面板的 approve 已经挡住了空话术，但执行器是真正
            # 按下发送键的那一环，不该依赖上游的校验——payload 也可能来自
            # 手改库文件或将来某个绕开面板的写入点。空消息发出去既没意义，
            # 又会消耗一次每日配额和一个真人的注意力。和缺 URL 同样处理：
            # 不打开浏览器，标记失败留痕，且排在 attempted 之前（这一行从
            # 未真正调用 send_fn，不该替下一行占掉一次人类延迟）。
            _guarded_write(
                report,
                row["job_id"],
                lambda: actions.mark_failed(
                    conn, row["id"], "话术为空，未打开浏览器"
                ),
            )
            report.failed += 1
            report.errors.append(f"{row['job_id']}: 话术为空，拒绝发送")
            continue
        if not url:
            # 缺 URL 就不该打开浏览器再失败——那样真实的 send_fn 会导航到
            # 空地址，白白多等一轮超时才报错。这一行已经被认领成 sending，
            # 所以 mark_failed 从 sending 出发是合法迁移。这一步刻意排在
            # attempted 判断之前：这一行从未真正调用 send_fn，不该占用
            # 「下两次真实发送之间才需要延迟」的名额，否则下一行即使是第一
            # 次真正发送，也会被迫先睡一次 5-600s（Minor 5）。
            _guarded_write(
                report,
                row["job_id"],
                lambda: actions.mark_failed(
                    conn, row["id"], "岗位缺少可用的 URL，未打开浏览器"
                ),
            )
            report.failed += 1
            report.errors.append(f"{row['job_id']}: 岗位缺少可用的 URL")
            continue

        if attempted:
            sleep_fn(human_delay(rng))
        attempted = True

        try:
            send_fn(url, greeting)
        except boss.LoginRequired as exc:
            # 登录墙不是「这一条消息发失败了」，而是整个 Boss 会话失效——
            # 继续跑只会让剩下的每一行都重复同样的失败，还各自耗掉一次
            # 5-600s 的人类延迟。整轮中止，让调用方在报告里挂横幅。
            # 这一行已经认领成 sending，标记失败留痕；不直接改回 approved：
            # 状态机里 sending 只能走向 sent 或 failed，直接跳回 approved
            # 属于「自动恢复」，而登录墙触发的时点无法百分之百排除消息已经
            # 发出的可能——诚实地留给人工核实，而不是自动重新排队。
            _guarded_write(
                report,
                row["job_id"],
                lambda: actions.mark_failed(
                    conn, row["id"], f"登录态失效，运行已中止：{exc}"
                ),
            )
            report.failed += 1
            report.login_required = True
            report.errors.append(f"{row['job_id']}: 登录态失效，运行已中止")
            break
        except SendUncertain as exc:
            # 发送按钮已经点了，读回确认才失败：消息很可能已经真的发出去
            # 了。不能 mark_failed——那会把配额还给一条可能已送达的消息，
            # 也允许人工在「消息可能已发出」的情况下重新批准，酿成给同一
            # 个真人重复发送。行原地留在 sending，只留一句人工核实提示。
            _guarded_write(
                report,
                row["job_id"],
                lambda: actions.note_uncertain(
                    conn,
                    row["id"],
                    f"消息可能已发出，请人工到 Boss 对话列表核实后再决定：{exc}",
                ),
            )
            report.uncertain += 1
            report.errors.append(f"{row['job_id']}: 发送结果不确定，需人工核实")
            continue
        except Exception as exc:  # 任何失败都只留痕，不自动重试（含点击前失败）
            _guarded_write(
                report,
                row["job_id"],
                lambda: actions.mark_failed(conn, row["id"], str(exc)),
            )
            report.failed += 1
            report.errors.append(f"{row['job_id']}: {exc}")
            continue

        _guarded_write(
            report, row["job_id"], lambda: actions.mark_sent(conn, row["id"])
        )
        try:
            _record_application(conn, row, greeting)
        except Exception as exc:
            # 消息已经发出、状态也已经落成 sent，这一步只是写申请台账。
            # 台账写失败不能让异常带着「消息已发出」的事实一起从 run_queue
            # 里逃出去——那样 report 会被整个丢弃，队列里剩下的行也不会
            # 再被处理。留痕即可，继续下一行。
            report.errors.append(f"{row['job_id']}: 申请台账写入失败：{exc}")
        report.sent += 1

    return report
