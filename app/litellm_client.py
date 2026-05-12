"""LiteLLM 统一流式客户端"""
import os
import time
import traceback
from typing import Dict, Any, List, AsyncGenerator

from app.config import (
    get_route_key, get_litellm_model_config,
    LITELLM_PROXY, LITELLM_READ_TIMEOUT,
    LITELLM_MAX_RETRIES, OPENAI_API_BASE, OPENAI_API_KEY_CUSTOM,
)

# LiteLLM 通过环境变量识别代理
if LITELLM_PROXY:
    os.environ.setdefault("HTTPS_PROXY", LITELLM_PROXY)
    os.environ.setdefault("HTTP_PROXY", LITELLM_PROXY)

EFFORT_MAPPING = {
    "minimal": None,
    "low": "low",
    "medium": "medium",
    "high": "high",
}


def _strip_images(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """过滤掉图片内容，只保留文本"""
    cleaned = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts = [p.get("text", "") for p in content if p.get("type") == "text"]
            content = "\n".join(text_parts) if text_parts else "[图片已移除]"
        cleaned.append({**msg, "content": content})
    return cleaned


async def call_litellm_stream(
    messages: List[Dict[str, Any]],
    target_model: str,
    thinking_level: str = "low",
    enable_search: bool = False,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    通过 LiteLLM 调用任意 OpenAI 兼容模型（流式）

    Args:
        messages: OpenAI 格式消息列表
        target_model: 智能路由输出的模型名（如 gemini-3-flash-preview）
        thinking_level: minimal/low/medium/high
        enable_search: 是否启用联网搜索

    Yields:
        {"content": "...", "thinking": "...", "usage": {...}, "error": "..."}
    """
    import litellm
    litellm.suppress_debug_info = True

    route_key = get_route_key(target_model)
    config = get_litellm_model_config(route_key)
    model = config["model"]

    print(f"📡 [LiteLLM] 请求模型: {model} (路由: {route_key}, thinking: {thinking_level})")

    start_time = time.time()
    input_tokens = 0
    output_tokens = 0

    try:
        kwargs = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "num_retries": LITELLM_MAX_RETRIES,
            "timeout": LITELLM_READ_TIMEOUT,
        }

        if OPENAI_API_BASE:
            kwargs["api_base"] = OPENAI_API_BASE
        if OPENAI_API_KEY_CUSTOM:
            kwargs["api_key"] = OPENAI_API_KEY_CUSTOM

        effort = EFFORT_MAPPING.get(thinking_level)
        if config["supports_reasoning"] and effort is not None:
            kwargs["reasoning_effort"] = effort

        if config["supports_search"] and enable_search:
            kwargs["tools"] = [{"googleSearch": {}}]

        if not config["supports_vision"]:
            kwargs["messages"] = _strip_images(messages)

        response = await litellm.acompletion(**kwargs)

        thinking_sent = False

        chunk_count = 0
        async for chunk in response:
            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta
            chunk_count += 1
            if chunk_count <= 3:
                print(f"🔍 [LiteLLM Debug] chunk#{chunk_count} delta attrs: {list(vars(delta).keys())}, content={getattr(delta, 'content', None)!r:.100}, reasoning={getattr(delta, 'reasoning_content', None)!r:.100}")

            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                if not thinking_sent:
                    yield {"thinking_start": True}
                    thinking_sent = True
                yield {"thinking": reasoning}

            content = getattr(delta, "content", None)
            if content:
                if thinking_sent:
                    yield {"thinking_end": True}
                    thinking_sent = False
                yield {"content": content}

            if hasattr(chunk, "usage") and chunk.usage:
                input_tokens = getattr(chunk.usage, "prompt_tokens", 0) or 0
                output_tokens = getattr(chunk.usage, "completion_tokens", 0) or 0

        latency_ms = int((time.time() - start_time) * 1000)
        print(f"✅ [LiteLLM] 响应结束 | 输入: {input_tokens}, 输出: {output_tokens}, 延迟: {latency_ms}ms")

        if output_tokens == 0:
            yield {"error": "⚠️ 模型未返回任何内容，请检查模型名和 API Key 配置"}
            return

        yield {
            "usage": {
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "latency_ms": latency_ms,
            }
        }

    except Exception as e:
        error_msg = str(e)
        print(f"❌ [LiteLLM] 调用失败: {error_msg}")
        traceback.print_exc()
        yield {"error": f"LiteLLM API Error: {error_msg}"}
