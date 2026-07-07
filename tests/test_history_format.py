# -*- coding: utf-8 -*-
"""验证 format_history_with_meta 的格式化行为：
- 真人 user 消息：[timestamp] sender_nick: content
- 其他 bot 的 assistant 消息：[timestamp] <other_bot name="X">content</other_bot>（对称）
- 当前 bot 的 assistant 消息：原样保留
"""
from app.ai.history_format import format_history_with_meta


def test_other_bot_assistant_gets_timestamp_prefix():
    """其他 bot 的 assistant 消息应该带 timestamp + <other_bot name="X"> XML 包裹"""
    msgs = [
        {
            "role": "assistant",
            "content": "Python 是一门简洁优雅的高级编程语言",
            "bot_id": "openrouter",
            "timestamp": "2026-05-21 09:07:30",
        },
    ]
    result = format_history_with_meta(msgs, current_bot_id="gemini")
    assert result[0]["content"] == (
        '[2026-05-21 09:07:30] <other_bot name="小克">Python 是一门简洁优雅的高级编程语言</other_bot>'
    )


def test_other_bot_assistant_without_timestamp_no_prefix():
    """旧消息没有 timestamp 时，应只加 XML 包裹不加 timestamp（向后兼容）"""
    msgs = [
        {"role": "assistant", "content": "回答", "bot_id": "gemini"},
    ]
    result = format_history_with_meta(msgs, current_bot_id="openrouter")
    assert result[0]["content"] == '<other_bot name="Gem">回答</other_bot>'


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
    """如果 content 已经以旧版 [来自机器人 X] 开头（旧数据/fixture），不应再次包裹"""
    msgs = [
        {
            "role": "assistant",
            "content": "[来自机器人 Gem] 之前的回答",
            "bot_id": "gemini",
            "timestamp": "2026-05-21 09:07:30",
        },
    ]
    result = format_history_with_meta(msgs, current_bot_id="openrouter")
    # 旧格式数据保留原样，不叠加新的 XML 包裹
    assert result[0]["content"] == "[2026-05-21 09:07:30] [来自机器人 Gem] 之前的回答"


def test_already_xml_tagged_content_not_double_wrapped():
    """新格式（本 bot 专属前缀）已包裹的内容，不应再次包裹"""
    msgs = [
        {
            "role": "assistant",
            "content": '<other_bot name="Gem">之前的回答</other_bot>',
            "bot_id": "gemini",
            "timestamp": "2026-05-21 09:07:30",
        },
    ]
    result = format_history_with_meta(msgs, current_bot_id="openrouter")
    assert result[0]["content"] == (
        '[2026-05-21 09:07:30] <other_bot name="Gem">之前的回答</other_bot>'
    )


def test_content_mentioning_other_bots_tag_syntax_still_gets_wrapped():
    """幂等判断必须按"本 bot 专属前缀"匹配，不能用泛化的 "<other_bot" 前缀。

    否则：如果某个 bot 的回复恰好以讨论/引用了 *另一个* bot 的标签语法开头
    （比如解释这套格式怎么用），泛化前缀检查会误判为"已包裹"而跳过真正的
    归属包裹，导致这条历史消息在别的 bot 眼里看起来像没有来源标记——这正是
    本次要修的"看不出这不是我说的"问题本身。用 bot 专属前缀就不会误判。
    """
    msgs = [
        {
            "role": "assistant",
            # Claude(openrouter) 的真实回复内容，恰好文本上是在讨论 Gemini 的标签写法，
            # 但这条消息本身的来源仍然是 openrouter，不是 gemini。
            "content": '<other_bot name="Gem">这是格式示例</other_bot>',
            "bot_id": "openrouter",
            "timestamp": "2026-05-21 09:07:30",
        },
    ]
    result = format_history_with_meta(msgs, current_bot_id="gemini")
    # 必须仍然被包上 "小克"（openrouter 的来源标签），不能因为内容开头长得像
    # "<other_bot" 就被误判成"已经是 Gem 的标签"而跳过包裹。
    assert result[0]["content"] == (
        '[2026-05-21 09:07:30] <other_bot name="小克">'
        '<other_bot name="Gem">这是格式示例</other_bot>'
        '</other_bot>'
    )


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
    assert result[0]["content"] == '[2026-05-21 09:07:30] <other_bot name="mystery">回答</other_bot>'


def test_xml_tag_survives_rewrite_and_merge():
    """端到端验证：XML 标签在 rewrite_roles_for_current_agent + merge_consecutive_same_role
    之后依然完整、可辨——这是本次要修的 bug 实际发生的那一层，不能只测 format() 本身。
    """
    from app.ai.message_transform import rewrite_roles_for_current_agent, merge_consecutive_same_role

    msgs = [
        {"role": "user", "content": "问题A", "sender_nick": "张三", "timestamp": "t1"},
        {"role": "assistant", "content": "回答A", "bot_id": "gemini", "timestamp": "t2"},
        {"role": "user", "content": "追问B", "sender_nick": "李四", "timestamp": "t3"},
    ]
    formatted = format_history_with_meta(msgs, current_bot_id="openrouter")
    merged = merge_consecutive_same_role(
        rewrite_roles_for_current_agent(formatted, current_bot_id="openrouter")
    )

    assert len(merged) == 1
    assert merged[0]["role"] == "user"
    assert merged[0]["content"] == (
        "[t1] 张三: 问题A\n\n"
        '[t2] <other_bot name="Gem">回答A</other_bot>\n\n'
        "[t3] 李四: 追问B"
    )
