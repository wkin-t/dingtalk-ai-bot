# -*- coding: utf-8 -*-
from app.litellm_client import _flatten_cache_blocks


def test_list_content_flattened_to_string():
    blocks = [
        {"type": "text", "text": "stable", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "dynamic"},
    ]
    messages = [{"role": "system", "content": blocks}, {"role": "user", "content": "hi"}]
    result = _flatten_cache_blocks(messages)
    assert result[0]["content"] == "stable\ndynamic"
    assert result[1]["content"] == "hi"


def test_string_content_unchanged():
    messages = [{"role": "system", "content": "you are helpful"}, {"role": "user", "content": "hi"}]
    result = _flatten_cache_blocks(messages)
    assert result[0]["content"] == "you are helpful"
    assert result[1]["content"] == "hi"


def test_single_block_no_newline():
    blocks = [{"type": "text", "text": "only block", "cache_control": {"type": "ephemeral"}}]
    messages = [{"role": "system", "content": blocks}]
    result = _flatten_cache_blocks(messages)
    assert result[0]["content"] == "only block"


def test_non_text_blocks_skipped():
    blocks = [
        {"type": "image", "source": {"type": "url", "url": "http://example.com/img.png"}},
        {"type": "text", "text": "描述"},
    ]
    messages = [{"role": "user", "content": blocks}]
    result = _flatten_cache_blocks(messages)
    assert result[0]["content"] == "描述"


def test_all_messages_processed():
    messages = [
        {"role": "system", "content": [{"type": "text", "text": "系统提示", "cache_control": {"type": "ephemeral"}}]},
        {"role": "user", "content": "用户问题"},
        {"role": "assistant", "content": [{"type": "text", "text": "助手回复"}]},
    ]
    result = _flatten_cache_blocks(messages)
    assert result[0]["content"] == "系统提示"
    assert result[1]["content"] == "用户问题"
    assert result[2]["content"] == "助手回复"
