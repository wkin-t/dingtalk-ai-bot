# -*- coding: utf-8 -*-
"""对话历史格式化工具，保留 bot_id 元数据给消息转换层。"""
from typing import Any, Dict, List, Optional


_BOT_SOURCE_NAMES = {
    "gemini": "Gem",
    "openclaw": "Claw",
    "openai": "小G",
    "openrouter": "小克",
}


def format_history_with_meta(
    history_messages: List[Dict],
    current_bot_id: str,
    cutoff_at: Optional[str] = None,
) -> List[Dict]:
    """格式化历史消息，保留 bot_id 给后续 transform 层。

    真人用户消息带时间和昵称；其他 bot 的 assistant 消息加来源标签；当前 bot
    的 assistant 消息保持无前缀，便于模型自然续接自身历史。

    cutoff_at: "%Y-%m-%d %H:%M:%S" 字符串。若提供，过滤掉 timestamp <= cutoff 的消息
    （当前 agent /clear 后的"软清空"）。timestamp 缺失的消息保留（保守策略）。
    """
    from app import config as cfg
    backend_supports_thinking = cfg.AI_BACKEND in ("openrouter", "openai")

    formatted: List[Dict[str, Any]] = []
    for msg in history_messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        timestamp = msg.get("timestamp")
        sender_nick = msg.get("sender_nick")
        bot_id = msg.get("bot_id")

        # /clear cutoff 过滤：字符串比较（固定宽度 ISO 格式可靠）
        if cutoff_at and timestamp and timestamp <= cutoff_at:
            continue

        new_msg: Dict[str, Any] = {"role": role}
        if bot_id is not None:
            new_msg["bot_id"] = bot_id

        if role == "user":
            if timestamp:
                if sender_nick and not str(content).startswith(f"{sender_nick}:"):
                    new_msg["content"] = f"[{timestamp}] {sender_nick}: {content}"
                else:
                    new_msg["content"] = f"[{timestamp}] {content}"
            else:
                new_msg["content"] = content
        elif role == "assistant" and bot_id is not None and bot_id != current_bot_id:
            bot_source = _BOT_SOURCE_NAMES.get(bot_id, bot_id)
            content_str = str(content)
            old_tag = f"[来自机器人 {bot_source}]"
            new_tag_prefix = f'<other_bot name="{bot_source}">'
            # 幂等判断必须按"本 bot 专属前缀"匹配，不能用泛化的 "<other_bot" 前缀：
            # system prompt 会教会所有模型这套语法，如果某条历史消息恰好是在讨论/
            # 引用别的 bot 的标签写法，泛化前缀会误判成"已包裹"而漏加真正的归属
            # 标签——这正是本次要修的"看不出这不是我说的"问题本身。
            if content_str.startswith(new_tag_prefix) or content_str.startswith(old_tag):
                body = content
            else:
                body = f'<other_bot name="{bot_source}">{content}</other_bot>'
            # 时间戳留在标签外，与真人 user 消息 "[ts] 昵称: 内容" 对称
            if timestamp:
                new_msg["content"] = f"[{timestamp}] {body}"
            else:
                new_msg["content"] = body
        else:
            new_msg["content"] = content

        # 仅当 backend 支持 thinking blocks 时才传递 reasoning_details
        if backend_supports_thinking and role == "assistant":
            rd = msg.get("reasoning_details")
            if rd:
                new_msg["reasoning_details"] = rd

        formatted.append(new_msg)

    return formatted
