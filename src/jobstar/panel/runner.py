"""面板触发的后台任务：采集、打分。

为什么不能直接在请求里跑：
  - 采集要开真实浏览器逐条抓详情页，打分每个岗位两轮 LLM 调用，一轮下来
    几分钟起步。放进 HTTP 请求里必然超时，而且会占住 FastAPI 的同步线程池。
  - 所以请求只负责「启动」和「查进度」，真正的活在后台线程里干。

为什么全局只允许一个任务在跑（单飞）：
  - 采集驱动的是本机那**一个** Chrome 会话，两轮并发会互相抢标签页；
  - 两轮打分并发会对同一批岗位重复调 LLM，白烧钱；
  - 两个写入方同时写同一个 SQLite 文件，只会更早撞上写锁。
  这个限制是刻意的，不是没做完——需要并行的时候应该先想清楚上面三条。

线程与数据库：后台线程**自己开连接**，绝不借用请求的那个。sqlite3 连接
虽然建库时关掉了同线程检查（见 db.get_conn 的注释），但那是为了适配
「创建和使用不在同一线程」，不代表可以让两个线程同时往同一个连接上写。
"""

from __future__ import annotations

import threading
import traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

MAX_LINES = 400  # 进度日志只留最近这么多行，避免一轮长采集把内存撑大


class AlreadyRunning(RuntimeError):
    """已经有任务在跑。调用方应当转成 HTTP 409。"""


@dataclass
class TaskState:
    name: str  # "collect" | "score"
    status: str  # running | done | failed
    started_at: str
    label: str = ""  # 人能看懂的这轮在干什么，例如「后端开发 等 2 个关键词」
    finished_at: str | None = None
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_LINES))
    summary: dict[str, Any] | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "label": self.label,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "lines": list(self.lines),
            "summary": self.summary,
            "error": self.error,
        }


class TaskRunner:
    """同一时刻只允许一个后台任务。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: TaskState | None = None
        self._thread: threading.Thread | None = None

    def snapshot(self) -> dict[str, Any] | None:
        """当前（或最近一次）任务的状态。没跑过任何任务时返回 None。

        在锁里取，保证读到的不是一个写到一半的状态；`as_dict` 顺手把 deque
        复制成 list，调用方拿到的不会再被后台线程改动。
        """
        with self._lock:
            return self._state.as_dict() if self._state else None

    def is_running(self) -> bool:
        with self._lock:
            return self._state is not None and self._state.status == "running"

    def start(
        self,
        name: str,
        label: str,
        work: Callable[[Callable[[str], None]], dict[str, Any]],
    ) -> dict[str, Any]:
        """启动一个后台任务。`work` 接收一个 `note(str)` 回调，返回摘要字典。

        `AlreadyRunning` 在**取到锁之后**判断，两个几乎同时到达的请求里只有
        一个能赢——这正是单飞要挡住的竞态。
        """
        with self._lock:
            if self._state is not None and self._state.status == "running":
                raise AlreadyRunning(
                    f"「{self._state.label or self._state.name}」还在跑，"
                    "等它结束再开下一轮"
                )
            state = TaskState(
                name=name,
                status="running",
                label=label,
                started_at=_now(),
            )
            self._state = state

        def note(line: str) -> None:
            # deque(maxlen=) 的 append 是原子的，不需要再拿锁——拿锁反而会让
            # 后台线程和每 1.5 秒轮询一次的请求互相等。
            state.lines.append(f"{_now()}  {line}")

        def runner() -> None:
            try:
                summary = work(note)
            except BaseException as exc:  # noqa: BLE001 - 后台线程绝不能让异常逃逸
                # 逃逸出去的异常只会打进 stderr，面板永远停在「运行中」——
                # 那是最坏的一种失败：分不清在跑还是已经死了。
                state.error = f"{type(exc).__name__}: {exc}"
                state.lines.append(f"{_now()}  ✗ {state.error}")
                state.lines.append(traceback.format_exc().rstrip())
                state.status = "failed"
            else:
                state.summary = summary
                state.status = "done"
            finally:
                # 无论如何都要落一个终态和结束时间，否则 is_running() 会永远
                # 为真，面板再也开不了下一轮。
                if state.status == "running":  # 防御：上面两支都没走到
                    state.status = "failed"
                    state.error = state.error or "任务以未知方式结束"
                state.finished_at = _now()

        thread = threading.Thread(target=runner, name=f"jobstar-{name}", daemon=True)
        with self._lock:
            self._thread = thread
        thread.start()
        return state.as_dict()

    def join(self, timeout: float | None = None) -> None:
        """等当前任务结束。只给测试用——生产路径靠面板轮询 /api/run/status。"""
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


# 进程内唯一的实例。面板是单进程的本地工具，不需要跨进程协调——真正的
# 跨进程保护在别处：actions 的状态机保证同一条动作只会被一个执行器认领。
RUNNER = TaskRunner()
