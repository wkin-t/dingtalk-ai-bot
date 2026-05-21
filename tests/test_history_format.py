# -*- coding: utf-8 -*-
"""验证 format_history_with_meta 的格式化行为：
- 真人 user 消息：[timestamp] sender_nick: content
- 其他 bot 的 assistant 消息：[timestamp] [来自机器人 X] content（对称）
- 当前 bot 的 assistant 消息：原样保留
"""
from app.ai.history_format import format_history_with_meta


def test_other_bot_assistant_gets_timestamp_prefix():
    """其他 bot 的 assistant 消息应该带 timestamp + [来自机器人 X] 前缀"""
    msgs = [
        {
            "role": "assistant",
            "content": "Python 是一门简洁优雅的高级编程语言",
            "bot_id": "openrouter",
            "timestamp": "2026-05-21 09:07:30",
        },
    ]
    result = format_history_with_meta(msgs, current_bot_id="gemini")
    assert result[0]["content"] == "[2026-05-21 09:07:30] [来自机器人 小克] Python 是一门简洁优雅的高级编程语言"


def test_other_bot_assistant_without_timestamp_no_prefix():
    """旧消息没有 timestamp 时，应只加 tag 不加 timestamp（向后兼容）"""
    msgs = [
        {"role": "assistant", "content": "回答", "bot_id": "gemini"},
    ]
    result = format_history_with_meta(msgs, current_bot_id="openrouter")
    assert result[0]["content"] == "[来自机器人 Gem] 回答"


def test_current_bot_assistant_keeps_raw_content():
    """当前 bot 自己的 assistant 消息不加任何前缀（让模型自然续接自身历史）"""
    msgs = [
        {
            "role": "assistant",
            "content": "我之前说过的话",
            "bot_id": "openrouter",
            "timestamp": "2026-05-21 09:07:30",
        },
    ]
    result = format_history_with_meta(msgs, current_bot_id="openrouter")
    assert result[0]["content"] == "我之前说过的话"


def test_already_tagged_content_not_double_tagged():
    """如果 content 已经以 [来自机器人 X] 开头（旧数据/fixture），不应再次加 tag"""
    msgs = [
        {
            "role": "assistant",
            "content": "[来自机器人 Gem] 之前的回答",
            "bot_id": "gemini",
            "timestamp": "2026-05-21 09:07:30",
        },
    ]
    result = format_history_with_meta(msgs, current_bot_id="openrouter")
    # 不应该出现 [来自机器人 Gem] [来自机器人 Gem]
    assert result[0]["content"] == "[2026-05-21 09:07:30] [来自机器人 Gem] 之前的回答"


def test_user_message_format_unchanged():
    """真人 user 消息格式保持 [timestamp] sender_nick: content"""
    msgs = [
        {
            "role": "user",
            "content": "用 100 字介绍 Python",
            "sender_nick": "我司曾工",
            "timestamp": "2026-05-21 09:07:05",
        },
    ]
    result = format_history_with_meta(msgs, current_bot_id="openrouter")
    assert result[0]["content"] == "[2026-05-21 09:07:05] 我司曾工: 用 100 字介绍 Python"


def test_other_bot_unknown_bot_id_uses_raw_id():
    """未知 bot_id 应回退到 bot_id 本身作为来源名"""
    msgs = [
        {
            "role": "assistant",
            "content": "回答",
            "bot_id": "mystery",
            "timestamp": "2026-05-21 09:07:30",
        },
    ]
    result = format_history_with_meta(msgs, current_bot_id="gemini")
    assert result[0]["content"] == "[2026-05-21 09:07:30] [来自机器人 mystery] 回答"
