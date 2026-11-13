"""统一入口按实际发送的模型约束深度，而非路由别名或档位名称。"""
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("backend,model,requested,expected", [
    ("gemini", "gemini-4-hydrogen-low", "xhigh", "xhigh"),
    ("gemini", "gemini-4-carbon-high", "xhigh", "high"),
    ("gemini", "gemini-4-argon-low", "max", "high"),
    ("openai", "claude-sonnet-5-5-low", "max", "max"),
    ("openai", "gpt-6.1-sol", "max", "max"),
    ("openai", "gpt-6-luna", "none", "none"),
    ("openai", "gpt-6.1-terra", "none", "none"),
    ("openai", "gpt-6.1-sol", "none", "low"),
])
async def test_backend_dispatches_supported_independent_thinking(backend, model, requested, expected, monkeypatch):
    from app import config
    from app.ai.backend import create_backend_stream

    monkeypatch.setattr(config, "AI_BACKEND", backend)
    monkeypatch.setattr(config, "get_route_key", lambda _target: "fast")
    monkeypatch.setattr(config, "get_litellm_model_config", lambda _slot: {"model": model, "supports_search": False, "supports_reasoning": True})
    captured = {}

    async def fake(*args, **kwargs):
        captured.update(kwargs)
        yield {"content": "ok"}

    module = "app.gemini_client.call_gemini_stream" if backend == "gemini" else "app.openai_client.call_openai_stream"
    with patch(module, side_effect=fake):
        result = [chunk async for chunk in create_backend_stream([], target_model=model if backend == "gemini" else "fast", thinking_level=requested)]
    assert result == [{"content": "ok"}]
    assert captured["thinking_level"] == expected


def test_route_thinking_resolves_actual_slot_model(monkeypatch):
    from app import config
    from app.ai.backend import resolve_thinking_for_route

    monkeypatch.setattr(config, "get_route_key", lambda _target: "pro")
    monkeypatch.setattr(config, "get_litellm_model_config", lambda _slot: {"model": "gemini-4-carbon-low", "supports_reasoning": True})
    assert resolve_thinking_for_route("openai", "pro", "max") == "high"


def test_keyword_fallback_preserves_pro_slot_even_when_model_aliases_equal(monkeypatch):
    from app.ai import router

    monkeypatch.setattr(router, "DEFAULT_MODEL", "same-model")
    monkeypatch.setattr(router, "GEMINI_MODEL_FAST", "same-model")
    result = router.analyze_complexity_unified("请证明算法的正确性并推导时间复杂度")
    assert result["route_slot"] == "pro" and result["model"] == "same-model"


def test_disabled_transport_reports_default_not_fake_off(monkeypatch):
    from app import config
    from app.ai.backend import resolve_thinking_for_route

    monkeypatch.setattr(config, "get_route_key", lambda _: "lite")
    monkeypatch.setattr(config, "get_litellm_model_config", lambda _: {"model": "gpt-6-luna", "supports_reasoning": False})
    assert resolve_thinking_for_route("openai", "lite", "none") == "default"
