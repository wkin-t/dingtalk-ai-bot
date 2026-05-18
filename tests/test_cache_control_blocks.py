# -*- coding: utf-8 -*-
from app.litellm_client import _inject_cache_control


def test_string_content_wrapped_to_single_cache_block_for_anthropic():
    messages = [{"role": "system", "content": "you are helpful"}, {"role": "user", "content": "hi"}]
    result = _inject_cache_control(messages, "anthropic/claude-sonnet-4")
    assert isinstance(result[0]["content"], list)
    assert result[0]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_list_content_passed_through_untouched():
    blocks = [
        {"type": "text", "text": "stable", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "dynamic"},
    ]
    messages = [{"role": "system", "content": blocks}, {"role": "user", "content": "hi"}]
    result = _inject_cache_control(messages, "anthropic/claude-sonnet-4")
    # 应该原样透传（不能给每个 block 都加 cache_control）
    assert result[0]["content"] == blocks


def test_non_anthropic_model_unchanged():
    messages = [{"role": "system", "content": "x"}, {"role": "user", "content": "hi"}]
    result = _inject_cache_control(messages, "openai/gpt-4o")
    assert result == messages
