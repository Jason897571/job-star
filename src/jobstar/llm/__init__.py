"""LLM 调用的唯一入口。上层只认 call_json，不关心后端是哪个。"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Protocol

VALID_TIERS = ("fast", "strong")

_RETRY_HINT = "\n\n上一次输出不是合法 JSON。只输出 JSON 对象本身，不要 markdown 围栏，不要任何解释文字。"


class LLMBackendError(RuntimeError):
    """后端本身失败：CLI 退出码非零、网关 5xx、配置缺失。"""


class LLMSchemaError(RuntimeError):
    """重试一次后仍拿不到合法 JSON。设计文档 §7：不猜测，交人工处理。"""


class Backend(Protocol):
    def __call__(
        self, *, system: str, user: str, tier: str, timeout: int = 180
    ) -> str: ...


_FENCE = re.compile(r"\A\s*```(?:json)?\s*\n(.*?)\n?\s*```\s*\Z", re.DOTALL)


def extract_json(text: str) -> dict[str, Any]:
    """剥掉可能存在的 markdown 围栏后解析。claude CLI 实测常带围栏。"""
    match = _FENCE.match(text)
    if match is not None:
        text = match.group(1)
    return json.loads(text.strip())


def _resolve_backend() -> Callable[..., str]:
    from jobstar.config import get_settings

    settings = get_settings()
    if settings.llm_backend == "claude_cli":
        from jobstar.llm import claude_cli

        return claude_cli.call
    from jobstar.llm import gateway

    return gateway.call


def call_json(*, system: str, user: str, tier: str) -> dict[str, Any]:
    """调用 LLM 并返回解析后的 JSON 对象。不合 schema 重试一次，再失败就抛。"""
    if tier not in VALID_TIERS:
        raise ValueError(f"tier 只能是 {VALID_TIERS} 之一，实际是 {tier!r}")
    backend = _resolve_backend()
    prompt = user
    last_error: Exception | None = None
    last_raw = ""
    for _ in range(2):
        last_raw = backend(system=system, user=prompt, tier=tier)
        try:
            return extract_json(last_raw)
        except json.JSONDecodeError as exc:
            last_error = exc
            prompt = user + _RETRY_HINT
    raise LLMSchemaError(
        f"重试一次后仍不是合法 JSON。最后一次输出前 300 字：{last_raw[:300]!r}"
    ) from last_error
