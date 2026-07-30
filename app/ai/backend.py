"""统一后端入口 — handler 和 dingtalk_bot 都调用此模块"""
from typing import Dict, Any, List, AsyncGenerator, Optional

import app.config as cfg


def _get_backend_name() -> str:
    return cfg.AI_BACKEND


def resolve_enable_search(backend: str, target_model: str, requested: bool) -> bool:
    """全自主搜索策略（SEARCH_AUTONOMOUS）：路由显式要求搜索时原样放行；否则
    fast/pro 档只要当前路径原生支持搜索工具就强制挂载，让模型自决是否搜索
    （不搜不产生搜索费用）。

    豁免场景（保持原路由门控，防止成本/行为回退）：
    - lite 档：问候闲聊，不挂工具零成本
    - openclaw：不支持搜索工具
    - openai 后端的 gemini 上游或 supports_search=False 的模型：强制开会落入
      fallback 注入路径——每条消息真实执行一次 google 搜索，烧 Gemini 配额
    """
    if requested:
        return True
    if not cfg.SEARCH_AUTONOMOUS:
        return False
    if backend == "openclaw":
        return False
    route_key = cfg.get_route_key(target_model)
    if route_key == "lite":
        return False
    if backend == "gemini":
        return True
    if backend == "openrouter":
        config = cfg.OPENROUTER_MODEL_CONFIG.get(route_key, cfg.OPENROUTER_MODEL_CONFIG["fast"])
        return bool(config.get("supports_search"))
    # openai 后端：gemini 上游走 Chat Completions，无原生搜索工具可挂
    config = cfg.get_litellm_model_config(route_key)
    model_name = str(config.get("model") or "")
    if "gemini" in model_name.lower():
        return False
    return bool(config.get("supports_search"))


def should_show_search_icon(search_info: Optional[Dict[str, Any]]) -> bool:
    """是否点亮 🌐 图标：仅当本次真的执行了搜索。

    - executed：原生路径回流了搜索信号（Gemini grounding_metadata / Responses
      web_search_call 或 url_citation / OpenRouter annotations）
    - fallback_injected：gemini fallback 确实执行了搜索并注入了摘要

    挂了工具但模型没搜（native_enabled 单独为真）不点亮——否则全自主下每条
    fast/pro 回复都常亮，图标失去"本次引用了网络"的信号价值。
    """
    if not search_info:
        return False
    return bool(search_info.get("executed") or search_info.get("fallback_injected"))


async def create_backend_stream(
    messages: List[Dict[str, Any]],
    target_model: str,
    thinking_level: str = "low",
    enable_search: bool = False,
    temperature: float = 0.7,
    top_p: Optional[float] = None,
    **kwargs,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    统一后端入口，根据 AI_BACKEND 选择调用链

    Args:
        messages: OpenAI 格式消息列表
        target_model: 智能路由输出的模型名
        thinking_level: minimal/low/medium/high
        enable_search: 是否启用联网搜索（SEARCH_AUTONOMOUS 开启时，fast/pro 档
            原生支持搜索的路径会被 resolve_enable_search 强制为 True）
        top_p: 核采样参数，None 表示后端默认
        **kwargs: 后端特定参数（openclaw 需要 conversation_id, sender_id, sender_nick, image_data_list）

    Yields:
        {"content": "...", "thinking": "...", "reasoning_details": [...], "usage": {...}, "error": "..."}
    """
    backend = cfg.AI_BACKEND

    resolved_search = resolve_enable_search(backend, target_model, enable_search)
    if resolved_search and not enable_search:
        print(f"🔍 [全自主搜索] {target_model} 挂载原生搜索工具（模型自决是否搜索）")
    enable_search = resolved_search

    if backend == "openclaw":
        from app.openclaw_client import call_openclaw_stream
        stream = call_openclaw_stream(
            messages,
            conversation_id=kwargs.get("conversation_id", ""),
            sender_id=kwargs.get("sender_id", ""),
            sender_nick=kwargs.get("sender_nick", ""),
            model=target_model,
            image_data_list=kwargs.get("image_data_list"),
            top_p=top_p,
        )
    elif backend == "openrouter":
        from app.openrouter_client import call_openrouter_stream
        stream = call_openrouter_stream(
            messages,
            target_model=target_model,
            thinking_level=thinking_level,
            enable_search=enable_search,
            temperature=temperature,
            top_p=top_p,
            conversation_id=kwargs.get("conversation_id", ""),
        )
    elif backend == "openai":
        # OPENAI_API_BASE 路径改用官方 SDK，正确捕获 reasoning_details（含 signature）
        from app.openai_client import call_openai_stream
        stream = call_openai_stream(
            messages,
            target_model=target_model,
            thinking_level=thinking_level,
            enable_search=enable_search,
            temperature=temperature,
            top_p=top_p,
            conversation_id=kwargs.get("conversation_id", ""),
        )
    else:
        from app.gemini_client import call_gemini_stream
        gemini_kwargs = {
            "messages": messages,
            "target_model": target_model,
            "thinking_level": thinking_level,
            "enable_search": enable_search,
            "temperature": temperature,
            "top_p": top_p,
        }
        if kwargs.get("route_slot") in {"router", "lite", "fast", "pro"}:
            gemini_kwargs["route_slot"] = kwargs["route_slot"]
        stream = call_gemini_stream(
            **gemini_kwargs,
        )

    async for chunk in stream:
        yield chunk
