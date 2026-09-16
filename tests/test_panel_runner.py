"""面板触发的后台任务（runner.py + /api/run/*）。

两条不能破的性质：
  1. 同一时刻只有一个任务在跑。采集驱动的是本机那**一个** Chrome 会话，
     两轮并发会互相抢标签页；两轮打分并发会对同一批岗位重复调 LLM。
  2. 任务无论怎么结束都必须落一个终态。停在「运行中」是最坏的失败——
     分不清在跑还是已经死了，而且面板从此再也开不了下一轮。
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from jobstar.db import get_conn, init_db
from jobstar.panel.app import app, get_db, get_runner
from jobstar.panel.runner import AlreadyRunning, TaskRunner

PANEL_HEADERS = {"X-Jobstar-Panel": "1"}


@pytest.fixture()
def runner():
    r = TaskRunner()
    yield r
    r.join(timeout=5)


def test_a_finished_task_records_its_summary(runner):
    runner.start("score", "打分", lambda note: (note("干活"), {"scored": 3})[1])
    runner.join(timeout=5)
    snap = runner.snapshot()
    assert snap["status"] == "done"
    assert snap["summary"] == {"scored": 3}
    assert snap["finished_at"] is not None
    assert any("干活" in line for line in snap["lines"])


def test_a_crashing_task_lands_in_failed_not_stuck_in_running(runner):
    """异常逃出后台线程只会打进 stderr，面板会永远停在「运行中」。"""

    def boom(note):
        note("开始")
        raise RuntimeError("浏览器没开")

    runner.start("collect", "采集", boom)
    runner.join(timeout=5)
    snap = runner.snapshot()
    assert snap["status"] == "failed"
    assert "浏览器没开" in snap["error"]
    assert snap["finished_at"] is not None
    assert not runner.is_running()


def test_even_a_baseexception_leaves_a_terminal_state(runner):
    """KeyboardInterrupt/SystemExit 不是 Exception 的子类。只 except
    Exception 的话，这两种照样会让任务永远卡在「运行中」。"""

    def boom(note):
        raise KeyboardInterrupt()

    runner.start("score", "打分", boom)
    runner.join(timeout=5)
    assert runner.snapshot()["status"] == "failed"
    assert not runner.is_running()


def test_only_one_task_at_a_time(runner):
    release = threading.Event()
    runner.start("collect", "第一个", lambda note: (release.wait(5), {})[1])
    try:
        with pytest.raises(AlreadyRunning) as exc:
            runner.start("score", "第二个", lambda note: {})
        assert "第一个" in str(exc.value)
    finally:
        release.set()
    runner.join(timeout=5)
    assert runner.snapshot()["label"] == "第一个", "被拒绝的任务不该覆盖掉在跑的那个"


def test_a_new_task_can_start_once_the_previous_one_finished(runner):
    runner.start("collect", "第一个", lambda note: {})
    runner.join(timeout=5)
    runner.start("score", "第二个", lambda note: {})
    runner.join(timeout=5)
    assert runner.snapshot()["label"] == "第二个"


def test_log_is_capped_so_a_long_run_cannot_grow_without_bound(runner):
    from jobstar.panel.runner import MAX_LINES

    def chatty(note):
        for i in range(MAX_LINES + 50):
            note(f"第 {i} 条")
        return {}

    runner.start("collect", "话多", chatty)
    runner.join(timeout=10)
    lines = runner.snapshot()["lines"]
    assert len(lines) == MAX_LINES
    assert not any("第 0 条" in x for x in lines), "最早的那些应该被挤掉"
    assert any(f"第 {MAX_LINES + 49} 条" in x for x in lines), "最新的一条必须在"


def test_snapshot_is_a_copy_not_a_live_view(runner):
    """快照交给 JSON 序列化时，后台线程可能正在往 deque 里写。拿到的必须
    是当时的副本，否则会撞上「迭代时被修改」。"""
    release = threading.Event()

    def slow(note):
        note("一")
        release.wait(5)
        note("二")
        return {}

    runner.start("collect", "慢活", slow)
    for _ in range(200):
        snap = runner.snapshot()
        if snap["lines"]:
            break
    before = snap["lines"]
    release.set()
    runner.join(timeout=5)
    assert before == snap["lines"], "快照拿到之后不该再被后台线程改动"


# --- HTTP 层 ---------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBSTAR_DB_PATH", str(tmp_path / "t.db"))
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    # 每个测试一个全新的 runner：共用模块级单例的话，上一个测试跑完留下的
    # 状态会让这一个收到 409，或者读到别人的任务快照。
    task_runner = TaskRunner()
    app.dependency_overrides[get_db] = lambda: conn
    app.dependency_overrides[get_runner] = lambda: task_runner
    c = TestClient(app)
    c.conn = conn
    c.runner = task_runner
    yield c
    task_runner.join(timeout=10)
    app.dependency_overrides.clear()
    conn.close()


@pytest.mark.parametrize(
    "body,message",
    [
        ({"keywords": [], "city": "101210100", "pages": 1}, "至少要填一个搜索关键词"),
        ({"keywords": ["  "], "city": "101210100", "pages": 1}, "至少要填一个搜索关键词"),
        ({"keywords": ["后端"], "city": "杭州", "pages": 1}, "城市码必须是数字"),
        ({"keywords": ["后端"], "city": "101210100", "pages": 0}, "页数只能是 1-10"),
        ({"keywords": ["后端"], "city": "101210100", "pages": 99}, "页数只能是 1-10"),
    ],
)
def test_collect_rejects_bad_input_before_opening_a_browser(client, body, message):
    resp = client.post("/api/run/collect", json=body, headers=PANEL_HEADERS)
    assert resp.status_code == 400
    assert message in resp.json()["detail"]
    assert client.get("/api/run/status").json()["task"] is None, "不该真的起一个任务"


def test_collect_remembers_the_search_so_the_form_prefills_next_time(client, monkeypatch):
    from jobstar.config import get_setting

    started = threading.Event()
    monkeypatch.setattr(
        client.runner, "start",
        lambda name, label, work: (started.set(), {"name": name, "label": label})[1],
    )
    resp = client.post(
        "/api/run/collect",
        json={"keywords": ["后端开发", "Python"], "city": "101020100", "pages": 2},
        headers=PANEL_HEADERS,
    )
    assert resp.status_code == 200
    assert started.is_set()
    assert get_setting(client.conn, "last_search") == {
        "keywords": ["后端开发", "Python"],
        "city": "101020100",
        "pages": 2,
    }
    assert resp.json()["task"]["label"] == "后端开发 等 2 个关键词"


def test_starting_a_second_run_while_one_is_going_gives_409(client, monkeypatch):
    def busy(name, label, work):
        raise AlreadyRunning("「采集」还在跑，等它结束再开下一轮")

    monkeypatch.setattr(client.runner, "start", busy)
    resp = client.post("/api/run/score", json={}, headers=PANEL_HEADERS)
    assert resp.status_code == 409
    assert "还在跑" in resp.json()["detail"]


def test_run_endpoints_require_the_panel_header(client):
    for path in ("/api/run/collect", "/api/run/score"):
        resp = client.post(path, json={"keywords": ["x"], "city": "1", "pages": 1})
        assert resp.status_code == 403, path


def test_score_runs_the_pipeline_and_reports_counts(client, monkeypatch):
    """走真实的 RUNNER（不是桩），确认后台线程真的把 run_score 跑起来了、
    摘要能落回 /api/run/status。"""
    from jobstar.pipeline import ScoreReport

    monkeypatch.setattr(
        "jobstar.pipeline.run_score",
        lambda conn, *, limit=None, on_progress=None: (
            on_progress("80 分 · 后端开发"),
            ScoreReport(scored=1, enqueued=1),
        )[1],
    )
    resp = client.post("/api/run/score", json={}, headers=PANEL_HEADERS)
    assert resp.status_code == 200

    client.runner.join(timeout=10)
    task = client.get("/api/run/status").json()["task"]
    assert task["status"] == "done", task.get("error")
    assert task["summary"]["scored"] == 1
    assert task["summary"]["enqueued"] == 1
    assert any("80 分 · 后端开发" in line for line in task["lines"])


def test_score_rescore_clears_absorbing_states_first(client, monkeypatch):
    from jobstar.pipeline import ScoreReport

    calls: list[str] = []
    monkeypatch.setattr(
        "jobstar.pipeline.clear_for_rescore",
        lambda conn, **kw: (calls.append("clear"), 2)[1],
    )
    monkeypatch.setattr(
        "jobstar.pipeline.run_score",
        lambda conn, **kw: (calls.append("score"), ScoreReport())[1],
    )
    client.post("/api/run/score", json={"rescore": True}, headers=PANEL_HEADERS)

    client.runner.join(timeout=10)
    assert calls == ["clear", "score"], "清场必须发生在打分之前"
    task = client.get("/api/run/status").json()["task"]
    assert any("已清除 2 个岗位的吸收态" in line for line in task["lines"])


def test_login_failure_aborts_the_whole_collect_run_and_says_so(client, monkeypatch):
    """登录态失效是会话级硬故障：剩下的关键词再跑也只是拿一个已经失效的
    会话反复撞墙。整轮中止，而且要在健康状态里留痕给横幅读。"""
    from jobstar.collector.boss import LoginRequired
    from jobstar.config import get_setting

    seen: list[str] = []

    def fake_collect(conn, *, keyword, city_code, pages, on_progress=None):
        seen.append(keyword)
        raise LoginRequired("扫码登录已过期")

    monkeypatch.setattr("jobstar.pipeline.run_collect", fake_collect)
    client.post(
        "/api/run/collect",
        json={"keywords": ["后端", "前端"], "city": "101210100", "pages": 1},
        headers=PANEL_HEADERS,
    )

    client.runner.join(timeout=10)
    assert seen == ["后端"], "第二个关键词不该再被尝试"
    task = client.get("/api/run/status").json()["task"]
    assert task["status"] == "failed"
    assert "登录态失效" in task["error"]
    assert "登录态失效" in get_setting(client.conn, "last_collect_error")
