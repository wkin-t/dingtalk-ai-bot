"""统一后端入口 — handler 和 dingtalk_bot 都调用此模块"""
from typing import Dict, Any, List, AsyncGenerator

import app.config as cfg


def _get_backend_name() -> str:
    return cfg.AI_BACKEND


async def create_backend_stream(
    messages: List[Dict[str, Any]],
    target_model: str,
    thinking_level: str = "low",
    enable_search: bool = False,
    **kwargs,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    统一后端入口，根据 AI_BACKEND 选择调用链

    Args:
        messages: OpenAI 格式消息列表
        target_model: 智能路由输出的模型名
        thinking_level: minimal/low/medium/high
        enable_search: 是否启用联网搜索
        **kwargs: 后端特定参数（openclaw 需要 conversation_id, sender_id, sender_nick, image_data_list）

    Yields:
        {"content": "...", "thinking": "...", "usage": {...}, "error": "..."}
    """
    backend = cfg.AI_BACKEND

    if backend == "openclaw":
        from app.openclaw_client import call_openclaw_stream
        stream = call_openclaw_stream(
            messages,
            conversation_id=kwargs.get("conversation_id", ""),
            sender_id=kwargs.get("sender_id", ""),
            sender_nick=kwargs.get("sender_nick", ""),
            model=target_model,
            image_data_list=kwargs.get("image_data_list"),
        )
    elif backend in ("openai", "openrouter"):
        from app.litellm_client import call_litellm_stream
        stream = call_litellm_stream(
            messages,
            target_model=target_model,
            thinking_level=thinking_level,
            enable_search=enable_search,
        )
    else:
        from app.gemini_client import call_gemini_stream
        stream = call_gemini_stream(
            messages,
            target_model=target_model,
            thinking_level=thinking_level,
            enable_search=enable_search,
        )

    async for chunk in stream:
        yield chunk
