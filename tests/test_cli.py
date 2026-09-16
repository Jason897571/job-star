"""CLI 输出的格式回归测试：不跑真实执行器，只验证提示文案不会被悄悄改掉。"""

from __future__ import annotations

import pytest

from jobstar.cli import main
from jobstar.collector.boss import LoginRequired
from jobstar.config import get_setting
from jobstar.db import get_conn
from jobstar.executor import ExecutionReport
from jobstar.pipeline import CollectReport, ScoreReport


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


# --- Important 4：`score --rescore` 的命令行接线 ---


def _stub_score_pipeline(monkeypatch, report: ScoreReport | None = None):
    """把 run_score / clear_for_rescore 换成记账用的桩。

    记的是**有序**的调用日志而不是两个独立的布尔标志：清场必须发生在打分
    之前（反过来的话第一轮打分看到的还是吸收态，等于没重跑），而且两次
    调用拿到的 job_id 必须一致——只限定清场范围、让打分全库跑，会对人工
    没点名的岗位调 LLM 写话术、生成待确认动作。独立标志观察不到这两件事。
    """
    calls: list[tuple[str, object]] = []

    def fake_clear(conn, *, job_id=None):
        calls.append(("clear", job_id))
        return 3

    def fake_run_score(conn, *, limit=None, job_id=None):
        calls.append(("score", job_id))
        return report if report is not None else ScoreReport(scored=1)

    monkeypatch.setattr("jobstar.pipeline.clear_for_rescore", fake_clear)
    monkeypatch.setattr("jobstar.pipeline.run_score", fake_run_score)
    return calls


def test_score_without_rescore_never_clears_absorbing_states(
    monkeypatch, tmp_path, capsys
):
    """默认行为必须和以前完全一样：不传 --rescore 就一行都不清。"""
    monkeypatch.setenv("JOBSTAR_DB_PATH", str(tmp_path / "t.db"))
    calls = _stub_score_pipeline(monkeypatch)

    assert main(["score"]) == 0
    assert calls == [("score", None)]
    assert "已清除" not in capsys.readouterr().out


def test_score_rescore_clears_before_scoring(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("JOBSTAR_DB_PATH", str(tmp_path / "t.db"))
    calls = _stub_score_pipeline(monkeypatch)

    assert main(["score", "--rescore"]) == 0
    assert calls == [("clear", None), ("score", None)], "清场必须发生在打分之前"
    assert "已清除 3 个岗位" in capsys.readouterr().out


def test_rescore_job_id_scopes_scoring_too_not_just_clearing(monkeypatch, tmp_path):
    """`--job-id` 的 help 写的是「只重跑这一个岗位」。如果它只传给清场、
    不传给打分，那么库里任何一个「已抓详情、门禁通过、还没打过分」的岗位
    都会被顺带打分并生成待确认招呼——人工被告知只重跑了一个，面板上却
    多出一堆待确认项，误批准的概率被抬高，LLM 花费也在计划之外。"""
    monkeypatch.setenv("JOBSTAR_DB_PATH", str(tmp_path / "t.db"))
    calls = _stub_score_pipeline(monkeypatch)

    assert main(["score", "--rescore", "--job-id", "abc123"]) == 0
    assert calls == [("clear", "abc123"), ("score", "abc123")]


def test_job_id_without_rescore_is_rejected(monkeypatch, tmp_path, capsys):
    """`--job-id` 单独出现时会被静默忽略的话，人工会以为自己只重跑了一个
    岗位，实际上什么都没发生。必须报错而不是装作成功。"""
    monkeypatch.setenv("JOBSTAR_DB_PATH", str(tmp_path / "t.db"))
    _stub_score_pipeline(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        main(["score", "--job-id", "abc123"])
    assert exc.value.code == 2
    assert "--job-id 必须配合 --rescore 使用" in capsys.readouterr().err


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_job_id_is_rejected_instead_of_silently_meaning_all(
    monkeypatch, tmp_path, capsys, blank
):
    """空串是假值，`if args.job_id` 会把它当成没传：既绕过「必须配合
    --rescore」的校验，又在下游被 `? IS NULL` 当成「全部」。人工以为点名了
    一个岗位，实际是全库跑。"""
    monkeypatch.setenv("JOBSTAR_DB_PATH", str(tmp_path / "t.db"))
    calls = _stub_score_pipeline(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        main(["score", "--rescore", "--job-id", blank])
    assert exc.value.code == 2
    assert "--job-id 不能是空串" in capsys.readouterr().err
    assert calls == [], "报错必须发生在任何清场/打分之前"


def test_rescore_reports_retractions_and_skipped_regeneration(
    monkeypatch, tmp_path, capsys
):
    """撤回是机器替人做的状态变更，不能悄悄发生；`already_queued` 也不能被
    折进「入队 N」——那会让人去面板上找根本不存在的待确认项。"""
    monkeypatch.setenv("JOBSTAR_DB_PATH", str(tmp_path / "t.db"))
    _stub_score_pipeline(
        monkeypatch, ScoreReport(scored=5, enqueued=1, already_queued=2, retracted=2)
    )

    assert main(["score", "--rescore"]) == 0
    captured = capsys.readouterr()
    assert "入队 1" in captured.out
    assert "2 条岗位队列里已有招呼动作" in captured.out
    assert "2 条已入队的招呼在重跑后不再合格，已自动撤回成「跳过」" in captured.err


def test_rescore_says_so_when_nothing_was_in_an_absorbing_state(
    monkeypatch, tmp_path, capsys
):
    """清了 0 条是个正常结果（点名的岗位本来就没卡住），但人工必须能看懂
    「这一轮和不加 --rescore 完全一样」，而不是以为重跑生效了。"""
    monkeypatch.setenv("JOBSTAR_DB_PATH", str(tmp_path / "t.db"))

    def fake_clear(conn, *, job_id=None):
        return 0

    monkeypatch.setattr("jobstar.pipeline.clear_for_rescore", fake_clear)
    monkeypatch.setattr(
        "jobstar.pipeline.run_score",
        lambda conn, *, limit=None, job_id=None: ScoreReport(),
    )

    assert main(["score", "--rescore"]) == 0
    assert "和不加 --rescore 完全一样" in capsys.readouterr().err
