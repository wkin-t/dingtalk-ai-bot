"""中转模型能力覆盖必须在启动时校验，避免误配置扩大实际思考预算。"""
import pytest


def test_thinking_overrides_accept_exact_models_and_backend_scope(monkeypatch):
    from app.config import _get_thinking_level_overrides

    monkeypatch.setenv("THINKING_LEVEL_OVERRIDES", '{"openai:custom-model":["low","high","max"]}')
    assert _get_thinking_level_overrides() == {"openai:custom-model": ["low", "high", "max"]}


@pytest.mark.parametrize("value", ["[]", "not-json", '{"m":[]}', '{"m":["max","low"]}', '{"m":["low","low"]}', '{"m":["ultra"]}', '{"m":["minimal"]}', '{"m":"high"}', '{"": ["low"]}'])
def test_thinking_overrides_reject_invalid_config_without_echoing_it(monkeypatch, value):
    from app.config import _get_thinking_level_overrides

    monkeypatch.setenv("THINKING_LEVEL_OVERRIDES", value)
    with pytest.raises(ValueError, match="THINKING_LEVEL_OVERRIDES") as caught:
        _get_thinking_level_overrides()
    assert value not in str(caught.value)
