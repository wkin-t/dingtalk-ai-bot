# -*- coding: utf-8 -*-
import pytest

from app.ai.message_transform import (
    merge_consecutive_same_role,
    rewrite_roles_for_current_agent,
)


def test_other_bot_assistant_becomes_user():
    msgs = [
        {"role": "assistant", "content": "[来自机器人 Gem] hi", "bot_id": "gemini"},
    ]
    result = rewrite_roles_for_current_agent(msgs, current_bot_id="openrouter")
    assert result[0]["role"] == "user"
    assert "bot_id" not in result[0]


def test_current_bot_assistant_preserved():
    msgs = [
        {"role": "assistant", "content": "hi", "bot_id": "openrouter"},
    ]
    result = rewrite_roles_for_current_agent(msgs, current_bot_id="openrouter")
    assert result[0]["role"] == "assistant"
    assert "bot_id" not in result[0]


def test_assistant_without_bot_id_preserved_as_assistant():
    """保守策略：bot_id 为 None 的旧历史保留为 assistant，避免破坏多轮。"""
    msgs = [{"role": "assistant", "content": "old reply", "bot_id": None}]
    result = rewrite_roles_for_current_agent(msgs, current_bot_id="openrouter")
    assert result[0]["role"] == "assistant"


def test_user_messages_unchanged_role():
    msgs = [{"role": "user", "content": "hi", "timestamp": "t1"}]
    result = rewrite_roles_for_current_agent(msgs, current_bot_id="openrouter")
    assert result[0]["role"] == "user"
    assert "timestamp" not in result[0]


def test_system_message_role_preserved():
    msgs = [{"role": "system", "content": "sys"}]
    result = rewrite_roles_for_current_agent(msgs, current_bot_id="openrouter")
    assert result[0]["role"] == "system"


def test_bot_id_field_stripped_after_rewrite():
    msgs = [
        {"role": "user", "content": "u", "bot_id": "gemini", "timestamp": "t1"},
        {"role": "assistant", "content": "a", "bot_id": "openrouter"},
    ]
    result = rewrite_roles_for_current_agent(msgs, current_bot_id="openrouter")
    for msg in result:
        assert "bot_id" not in msg
        assert "timestamp" not in msg
        assert "sender_nick" not in msg
        assert set(msg.keys()) <= {"role", "content"}


def test_merge_consecutive_user_strings():
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "reply"},
    ]
    result = merge_consecutive_same_role(msgs)
    assert len(result) == 2
    assert result[0]["content"] == "first\n\nsecond"
    assert result[1]["content"] == "reply"


def test_merge_consecutive_assistant_strings():
    msgs = [
        {"role": "assistant", "content": "a1"},
        {"role": "assistant", "content": "a2"},
    ]
    result = merge_consecutive_same_role(msgs)
    assert len(result) == 1
    assert result[0]["content"] == "a1\n\na2"


def test_merge_no_consecutive_unchanged():
    msgs = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ]
    result = merge_consecutive_same_role(msgs)
    assert len(result) == 3
    assert [m["content"] for m in result] == ["u1", "a1", "u2"]


def test_system_message_not_merged():
    msgs = [
        {"role": "system", "content": "s1"},
        {"role": "system", "content": "s2"},
    ]
    result = merge_consecutive_same_role(msgs)
    assert len(result) == 2


def test_empty_history_returns_empty():
    assert rewrite_roles_for_current_agent([], "openrouter") == []
    assert merge_consecutive_same_role([]) == []


def test_history_tail_user_plus_current_user_merged():
    """历史尾部 user + 当前 user 必须能合并。"""
    msgs = [
        {"role": "user", "content": "old"},
        {"role": "user", "content": "current"},
    ]
    result = merge_consecutive_same_role(msgs)
    assert len(result) == 1
    assert result[0]["content"] == "old\n\ncurrent"


def test_merge_string_and_list_user_messages():
    msgs = [
        {"role": "user", "content": "history text"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "current with image"},
                {"type": "image_url", "image_url": {"url": "data:..."}},
            ],
        },
    ]
    result = merge_consecutive_same_role(msgs)
    assert len(result) == 1
    merged = result[0]["content"]
    assert isinstance(merged, list)
    assert merged[0]["type"] == "text"
    assert "history text" in merged[0]["text"]
    assert "current with image" in merged[0]["text"]
    assert any(block.get("type") == "image_url" for block in merged)


def test_merge_two_list_messages():
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "first text"},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "second text"},
                {"type": "image_url", "image_url": {"url": "x"}},
            ],
        },
    ]
    result = merge_consecutive_same_role(msgs)
    merged = result[0]["content"]
    assert isinstance(merged, list)
    text_parts = [block["text"] for block in merged if block.get("type") == "text"]
    assert any("first text" in text and "second text" in text for text in text_parts)


def test_message_without_role_treated_as_user():
    msgs = [{"content": "no role"}]
    result = rewrite_roles_for_current_agent(msgs, "openrouter")
    assert result[0]["role"] == "user"


def test_message_without_content_set_to_empty():
    msgs = [{"role": "user"}]
    result = rewrite_roles_for_current_agent(msgs, "openrouter")
    assert result[0]["content"] == ""
