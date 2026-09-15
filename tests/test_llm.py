import json

import pytest

from jobstar.llm import LLMSchemaError, call_json, extract_json


def test_extract_json_handles_bare_object():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_strips_json_fence():
    """claude CLI 实测会加围栏，即使 system prompt 明确禁止。"""
    raw = '```json\n{"city": "杭州", "salary_min": 30}\n```'
    assert extract_json(raw) == {"city": "杭州", "salary_min": 30}


def test_extract_json_strips_bare_fence():
    assert extract_json("```\n{\"a\": 1}\n```") == {"a": 1}


def test_extract_json_tolerates_surrounding_whitespace():
    assert extract_json('\n\n  {"a": 1}  \n') == {"a": 1}


def test_extract_json_rejects_non_object():
    with pytest.raises(json.JSONDecodeError):
        extract_json("这不是 JSON")


def test_call_json_retries_once_then_raises(monkeypatch):
    calls = []

    def fake_call(*, system, user, tier, timeout=180):
        calls.append(user)
        return "还是不合法"

    monkeypatch.setattr("jobstar.llm._resolve_backend", lambda: fake_call)
    with pytest.raises(LLMSchemaError):
        call_json(system="s", user="u", tier="fast")
    assert len(calls) == 2, "设计文档 §7：重试一次，不是无限重试"
    assert "JSON" in calls[1], "第二次应当把「上次不是合法 JSON」的提示追加进去"


def test_call_json_succeeds_on_retry(monkeypatch):
    outputs = iter(["坏的", '{"ok": true}'])

    def fake_call(*, system, user, tier, timeout=180):
        return next(outputs)

    monkeypatch.setattr("jobstar.llm._resolve_backend", lambda: fake_call)
    assert call_json(system="s", user="u", tier="fast") == {"ok": True}


def test_call_json_rejects_bad_tier(monkeypatch):
    monkeypatch.setattr("jobstar.llm._resolve_backend", lambda: None)
    with pytest.raises(ValueError):
        call_json(system="s", user="u", tier="cheap")


def test_claude_cli_unwraps_envelope(monkeypatch):
    import jobstar.llm.claude_cli as mod

    envelope = {"is_error": False, "result": '{"a": 1}'}
    captured = {}

    class FakeProc:
        returncode = 0
        stdout = json.dumps(envelope)
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        return FakeProc()

    monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    out = mod.call(system="sys", user="用户内容", tier="strong")
    assert out == '{"a": 1}'
    assert captured["input"] == "用户内容", "prompt 必须走 stdin，不走 argv"
    assert "--model" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--model") + 1] == "opus"


def test_claude_cli_raises_on_error_envelope(monkeypatch):
    import jobstar.llm.claude_cli as mod
    from jobstar.llm import LLMBackendError

    class FakeProc:
        returncode = 0
        stdout = json.dumps({"is_error": True, "result": "配额用尽"})
        stderr = ""

    monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kw: FakeProc())
    with pytest.raises(LLMBackendError, match="配额用尽"):
        mod.call(system="s", user="u", tier="fast")


def test_gateway_posts_openai_shape(monkeypatch):
    import jobstar.llm.gateway as mod

    captured = {}

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": '{"a": 1}'}}]}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs["json"]
        captured["headers"] = kwargs["headers"]
        return FakeResp()

    monkeypatch.setenv("JOBSTAR_LLM_BACKEND", "gateway")
    monkeypatch.setenv("JOBSTAR_GATEWAY_BASE_URL", "https://gw.test/v1/")
    monkeypatch.setenv("JOBSTAR_GATEWAY_API_KEY", "sk-test")
    monkeypatch.setenv("JOBSTAR_GATEWAY_MODEL_STRONG", "big-model")
    monkeypatch.setattr(mod.httpx, "post", fake_post)

    assert mod.call(system="s", user="u", tier="strong") == '{"a": 1}'
    assert captured["url"] == "https://gw.test/v1/chat/completions"
    assert captured["json"]["model"] == "big-model"
    assert captured["json"]["messages"][0] == {"role": "system", "content": "s"}
    assert captured["headers"]["Authorization"] == "Bearer sk-test"


def test_gateway_requires_base_url(monkeypatch):
    import jobstar.llm.gateway as mod
    from jobstar.llm import LLMBackendError

    monkeypatch.setenv("JOBSTAR_LLM_BACKEND", "gateway")
    monkeypatch.setenv("JOBSTAR_GATEWAY_BASE_URL", "")
    with pytest.raises(LLMBackendError, match="JOBSTAR_GATEWAY_BASE_URL"):
        mod.call(system="s", user="u", tier="fast")
