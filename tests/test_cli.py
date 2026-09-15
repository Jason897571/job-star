"""CLI 输出的格式回归测试：不跑真实执行器，只验证提示文案不会被悄悄改掉。"""

from __future__ import annotations

from jobstar.cli import main
from jobstar.executor import ExecutionReport


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
