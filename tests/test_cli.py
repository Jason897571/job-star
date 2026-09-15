"""CLI 输出的格式回归测试：不跑真实执行器，只验证提示文案不会被悄悄改掉。"""

from __future__ import annotations

from jobstar.cli import main
from jobstar.collector.boss import LoginRequired
from jobstar.config import get_setting
from jobstar.db import get_conn
from jobstar.executor import ExecutionReport
from jobstar.pipeline import CollectReport


def test_send_surfaces_uncertain_and_login_required_warnings(
    monkeypatch, tmp_path, capsys
):
    """uncertain 和 login_required 各自有专门的警告文案（消息可能已经发出，
    需要人工去 Boss 核实；登录态失效要中止本轮）。不能被悄悄折回普通计数里。"""
    monkeypatch.setenv("JOBSTAR_DB_PATH", str(tmp_path / "t.db"))

    def fake_run_queue(conn):
        return ExecutionReport(
            sent=1, failed=0, skipped=0, uncertain=2, login_required=True
        )

    monkeypatch.setattr("jobstar.executor.run_queue", fake_run_queue)

    exit_code = main(["send"])
    assert exit_code == 0

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "2 条发送结果不确定" in combined
    assert "请到 Boss 对话列表人工核实，不要重新批准这些动作" in combined
    assert "登录态失效，本轮执行已中止，请重新登录 Boss 后再运行 send" in combined


# --- Task 13：collect 子命令要把采集健康状态写进 last_collect_error /
# last_collect_at 这两个 setting，供面板顶部横幅读取（/api/health）。
# run_collect 对两类失败的处理方式不同（见 pipeline.py 与 collector/boss.py）：
#   - LoginRequired（登录态失效）：run_collect 故意让它原样往外炸穿，不吞掉。
#   - 详情页抓取失败（疑似页面结构变更）：run_collect 内部捕获，累积进
#     report.errors，正常返回。
#   - 列表页零结果（关键词真的没匹配 vs 页面被拦截，二者无法区分）：
#     run_collect 把它转成 report.errors 里唯一的一条、report.listed 仍为 0。
# 下面覆盖这三种情况分别写入的 last_collect_error 文案，以及成功且无
# report.errors 时会清空历史错误。


def test_collect_login_required_aborts_and_records_health(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("JOBSTAR_DB_PATH", str(tmp_path / "t.db"))

    def fake_run_collect(conn, *, keyword, city_code, pages):
        raise LoginRequired("检测到登录墙")

    monkeypatch.setattr("jobstar.pipeline.run_collect", fake_run_collect)

    exit_code = main(["collect", "--keyword", "AI 后端"])
    assert exit_code == 2

    captured = capsys.readouterr()
    assert "采集中止：检测到登录墙" in captured.err
    assert "重新扫码登录" in captured.err

    conn = get_conn(tmp_path / "t.db")
    assert "登录态失效" in get_setting(conn, "last_collect_error")
    assert get_setting(conn, "last_collect_at") is not None


def test_collect_detail_fetch_errors_recorded_as_structural_change(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("JOBSTAR_DB_PATH", str(tmp_path / "t.db"))

    def fake_run_collect(conn, *, keyword, city_code, pages):
        return CollectReport(
            listed=5,
            new=2,
            gated_out=1,
            detail_fetched=1,
            errors=["j1: 详情页轮询超时"],
        )

    monkeypatch.setattr("jobstar.pipeline.run_collect", fake_run_collect)

    exit_code = main(["collect", "--keyword", "AI 后端"])
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "! j1: 详情页轮询超时" in captured.err

    conn = get_conn(tmp_path / "t.db")
    err = get_setting(conn, "last_collect_error")
    assert "页面结构变更" in err
    assert "j1: 详情页轮询超时" in err
    assert get_setting(conn, "last_collect_at") is not None


def test_collect_zero_listed_records_ambiguous_health(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("JOBSTAR_DB_PATH", str(tmp_path / "t.db"))

    def fake_run_collect(conn, *, keyword, city_code, pages):
        return CollectReport(
            listed=0,
            new=0,
            gated_out=0,
            detail_fetched=0,
            errors=["AI 后端: 采集失败或本关键词零结果：..."],
        )

    monkeypatch.setattr("jobstar.pipeline.run_collect", fake_run_collect)

    exit_code = main(["collect", "--keyword", "AI 后端"])
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "无法自动区分，请人工核实" in captured.err

    conn = get_conn(tmp_path / "t.db")
    err = get_setting(conn, "last_collect_error")
    assert "无法自动区分" in err


def test_collect_clean_run_clears_previous_health_error(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBSTAR_DB_PATH", str(tmp_path / "t.db"))
    from jobstar.config import set_setting

    conn = get_conn(tmp_path / "t.db")
    from jobstar.db import init_db

    init_db(conn)
    set_setting(conn, "last_collect_error", "上一次的旧错误")

    def fake_run_collect(conn, *, keyword, city_code, pages):
        return CollectReport(listed=3, new=3, gated_out=0, detail_fetched=3, errors=[])

    monkeypatch.setattr("jobstar.pipeline.run_collect", fake_run_collect)

    exit_code = main(["collect", "--keyword", "AI 后端"])
    assert exit_code == 0

    conn2 = get_conn(tmp_path / "t.db")
    assert get_setting(conn2, "last_collect_error") is None
    assert get_setting(conn2, "last_collect_at") is not None


# --- fix round 1（review finding 1）：last_collect_at 记的是「最近一次
# 尝试」，成功失败都前进；last_collect_ok_at 只在真正成功（清空
# last_collect_error 的同一条路径）时才前进。下面三个用例分别覆盖：干净
# 采集会把两者对齐，登录态失效和结构变更两条失败路径都不能推进
# last_collect_ok_at——否则人工点掉错误横幅后，横幅会把失败的那次尝试
# 误报成「未见异常」（review 复现的复合场景）。


def test_collect_clean_run_sets_last_collect_ok_at_equal_to_at(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("JOBSTAR_DB_PATH", str(tmp_path / "t.db"))

    def fake_run_collect(conn, *, keyword, city_code, pages):
        return CollectReport(listed=3, new=3, gated_out=0, detail_fetched=3, errors=[])

    monkeypatch.setattr("jobstar.pipeline.run_collect", fake_run_collect)

    exit_code = main(["collect", "--keyword", "AI 后端"])
    assert exit_code == 0

    conn = get_conn(tmp_path / "t.db")
    at = get_setting(conn, "last_collect_at")
    assert at is not None
    assert get_setting(conn, "last_collect_ok_at") == at


def test_collect_login_required_does_not_advance_ok_at(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("JOBSTAR_DB_PATH", str(tmp_path / "t.db"))
    from jobstar.config import set_setting
    from jobstar.db import init_db

    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    set_setting(conn, "last_collect_ok_at", "2026-09-01 08:00:00")

    def fake_run_collect(conn, *, keyword, city_code, pages):
        raise LoginRequired("检测到登录墙")

    monkeypatch.setattr("jobstar.pipeline.run_collect", fake_run_collect)

    exit_code = main(["collect", "--keyword", "AI 后端"])
    assert exit_code == 2

    conn2 = get_conn(tmp_path / "t.db")
    assert get_setting(conn2, "last_collect_ok_at") == "2026-09-01 08:00:00"
    assert get_setting(conn2, "last_collect_at") != "2026-09-01 08:00:00"


def test_collect_structural_change_does_not_advance_ok_at(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("JOBSTAR_DB_PATH", str(tmp_path / "t.db"))
    from jobstar.config import set_setting
    from jobstar.db import init_db

    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    set_setting(conn, "last_collect_ok_at", "2026-09-01 08:00:00")

    def fake_run_collect(conn, *, keyword, city_code, pages):
        return CollectReport(
            listed=5,
            new=2,
            gated_out=1,
            detail_fetched=1,
            errors=["j1: 详情页轮询超时"],
        )

    monkeypatch.setattr("jobstar.pipeline.run_collect", fake_run_collect)

    exit_code = main(["collect", "--keyword", "AI 后端"])
    assert exit_code == 0

    conn2 = get_conn(tmp_path / "t.db")
    assert get_setting(conn2, "last_collect_ok_at") == "2026-09-01 08:00:00"
    assert get_setting(conn2, "last_collect_at") != "2026-09-01 08:00:00"
