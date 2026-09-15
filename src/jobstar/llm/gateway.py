"""后端 B：OpenAI 兼容网关，直调 /chat/completions。"""

from __future__ import annotations

import httpx

from jobstar.llm import LLMBackendError


def call(*, system: str, user: str, tier: str, timeout: int = 180) -> str:
    from jobstar.config import get_settings

    settings = get_settings()
    if not settings.gateway_base_url:
        raise LLMBackendError("gateway 后端需要设置 JOBSTAR_GATEWAY_BASE_URL")
    model = (
        settings.gateway_model_fast
        if tier == "fast"
        else settings.gateway_model_strong
    )
    url = settings.gateway_base_url.rstrip("/") + "/chat/completions"
    try:
        resp = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {settings.gateway_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "response_format": {"type": "json_object"},
            },
            timeout=timeout,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise LLMBackendError(f"网关请求失败：{exc}") from exc
    try:
        return resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as exc:
        raise LLMBackendError(f"网关响应结构异常：{resp.text[:300]!r}") from exc
