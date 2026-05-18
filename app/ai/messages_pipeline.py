# -*- coding: utf-8 -*-
"""后端消息准备入口：按后端能力决定是否做角色重塑。"""
from typing import Any, Dict, List

from app.config import AI_BACKEND, ENABLE_ROLE_REWRITE, OPENCLAW_GATEWAY_TRANSPORT
from app.ai.message_transform import merge_consecutive_same_role, rewrite_roles_for_current_agent


def _strip_model_unsafe_fields(messages_raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"role": msg.get("role", "user"), "content": msg.get("content", "")} for msg in messages_raw]


def prepare_messages_for_backend(messages_raw: List[Dict[str, Any]], current_bot_id: str) -> List[Dict[str, Any]]:
    """把 raw messages 转换成可直接送后端 SDK 的消息。

    OpenClaw WebSocket 只使用最后一条用户输入组装 Gateway payload，跳过角色重塑以保留原有行为。
    """
    if AI_BACKEND == "openclaw" and OPENCLAW_GATEWAY_TRANSPORT == "ws":
        return _strip_model_unsafe_fields(messages_raw)

    if not ENABLE_ROLE_REWRITE:
        return _strip_model_unsafe_fields(messages_raw)

    rewritten = rewrite_roles_for_current_agent(messages_raw, current_bot_id)
    return merge_consecutive_same_role(rewritten)
