"""后端 A：调本地 claude CLI 的 headless 模式，复用 Claude Code 的登录态。

不需要 ANTHROPIC_API_KEY。代价是每次往返约 4 秒，且 CLI 固定会带上自己的
工具定义（约 23K token）。批量跑几百个岗位时建议改用 gateway 后端。
"""

from __future__ import annotations

import json
import shutil
import subprocess

from jobstar.llm import LLMBackendError

_MODEL = {"fast": "sonnet", "strong": "opus"}

# 这些参数把 CLI 的默认上下文剥到最小：不加载全局 CLAUDE.md、skills、MCP、工具。
_TRIM_FLAGS = [
    "--exclude-dynamic-system-prompt-sections",
    "--setting-sources",
    "",
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
    "--allowed-tools",
    "",
]


def call(*, system: str, user: str, tier: str, timeout: int = 180) -> str:
    exe = shutil.which("claude")
    if exe is None:
        raise LLMBackendError(
            "claude CLI 不在 PATH 上。安装 Claude Code，或把 "
            "JOBSTAR_LLM_BACKEND 改成 gateway。"
        )
    cmd = [
        exe,
        "-p",
        "--output-format",
        "json",
        "--model",
        _MODEL[tier],
        "--system-prompt",
        system,
        *_TRIM_FLAGS,
    ]
    try:
        proc = subprocess.run(
            cmd, input=user, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise LLMBackendError(f"claude CLI 超时（{timeout}s）") from exc
    if proc.returncode != 0:
        raise LLMBackendError(
            f"claude CLI 退出码 {proc.returncode}：{proc.stderr[:500]}"
        )
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise LLMBackendError(
            f"claude CLI 输出不是 JSON 信封：{proc.stdout[:300]!r}"
        ) from exc
    if envelope.get("is_error"):
        raise LLMBackendError(f"claude CLI 报错：{str(envelope.get('result'))[:500]}")
    return envelope["result"]
