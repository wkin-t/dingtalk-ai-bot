# -*- coding: utf-8 -*-
"""对话历史格式化工具，保留 bot_id 元数据给消息转换层。"""
from typing import Any, Dict, List


_BOT_SOURCE_NAMES = {
    "gemini": "Gem",
    "openclaw": "Claw",
    "openai": "小G",
    "openrouter": "小克",
}


def format_history_with_meta(history_messages: List[Dict], current_bot_id: str) -> List[Dict]:
    """格式化历史消息，保留 bot_id 给后续 transform 层。

    真人用户消息带时间和昵称；其他 bot 的 assistant 消息加来源标签；当前 bot
    的 assistant 消息保持无前缀，便于模型自然续接自身历史。
    """
    formatted: List[Dict[str, Any]] = []
    for msg in history_messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        timestamp = msg.get("timestamp")
        sender_nick = msg.get("sender_nick")
        bot_id = msg.get("bot_id")

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
            tag = f"[来自机器人 {bot_source}]"
            if not str(content).startswith(tag):
                new_msg["content"] = f"{tag} {content}"
            else:
                new_msg["content"] = content
        else:
            new_msg["content"] = content

        formatted.append(new_msg)

    return formatted
