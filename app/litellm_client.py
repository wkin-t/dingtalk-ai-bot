"""LiteLLM 统一流式客户端"""
import os
import time
import traceback
from typing import Dict, Any, List, AsyncGenerator

from app.config import (
    get_route_key, get_litellm_model_config,
    LITELLM_PROXY, LITELLM_READ_TIMEOUT,
    LITELLM_MAX_RETRIES, OPENAI_API_BASE, OPENAI_API_KEY_CUSTOM,
    VERTEX_PROJECT,
)

# LiteLLM 通过环境变量识别代理
if LITELLM_PROXY:
    os.environ.setdefault("HTTPS_PROXY", LITELLM_PROXY)
    os.environ.setdefault("HTTP_PROXY", LITELLM_PROXY)

EFFORT_MAPPING = {
    "minimal": "none",      # GPT-5: 关闭推理 | Gemini: 映射为 minimal
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",       # GPT-5 独有：极限推理
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
    import warnings
    litellm.suppress_debug_info = True
    warnings.filterwarnings("ignore", message="Pydantic serializer warnings")

    route_key = get_route_key(target_model)
    config = get_litellm_model_config(route_key)
    model = config["model"]

    print(f"📡 [LiteLLM] 请求模型: {model} (路由: {route_key}, thinking: {thinking_level})")

    start_time = time.time()
    input_tokens = 0
    output_tokens = 0

    # 如果需要搜索，用 Gemini Google Search grounding 获取结果后注入消息
    if enable_search:
        try:
            from app.gemini_client import google_search
            # 从最后一条用户消息提取搜索 query
            search_query = ""
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        search_query = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
                    else:
                        search_query = str(content)
                    break

            if search_query:
                search_result = await google_search(search_query)
                if search_result:
                    search_context = f"\n\n## 实时搜索结果（来自 Google Search）\n{search_result}\n\n请基于以上搜索结果回答用户问题。如果搜索结果与你的训练数据冲突，优先使用搜索结果。"
                    # 注入到 system 消息或第一条消息
                    injected = False
                    for msg in messages:
                        if msg.get("role") == "system":
                            msg["content"] = msg.get("content", "") + search_context
                            injected = True
                            break
                    if not injected:
                        messages = [{"role": "system", "content": f"你是一个智能助手。{search_context}"}] + messages
                    print(f"🔍 [LiteLLM] 已注入 Google Search 结果到上下文")
        except Exception as e:
            print(f"⚠️ [LiteLLM] 搜索注入失败，继续无搜索模式: {e}")

    try:
        kwargs = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "num_retries": LITELLM_MAX_RETRIES,
            "timeout": LITELLM_READ_TIMEOUT,
        }

        if config["model"].startswith("vertex_ai/"):
            # Vertex AI 路径 — 互斥，不走 OpenAI 兼容逻辑
            kwargs["vertex_ai_project"] = VERTEX_PROJECT
            kwargs["vertex_ai_location"] = config.get("region", "us-east5")
            # thinking 参数适配
            reasoning_param = config.get("reasoning_param", "openai_effort")
            if reasoning_param == "anthropic_thinking":
                effort = EFFORT_MAPPING.get(thinking_level)
                if effort and effort != "none":
                    budget = {"low": 2048, "medium": 8192, "high": 32768}.get(effort, 8192)
                    kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
        elif OPENAI_API_BASE:
            kwargs["api_base"] = OPENAI_API_BASE
            kwargs["custom_llm_provider"] = "openai"
            if OPENAI_API_KEY_CUSTOM:
                kwargs["api_key"] = OPENAI_API_KEY_CUSTOM
            effort = EFFORT_MAPPING.get(thinking_level)
            if config["supports_reasoning"] and effort is not None:
                kwargs["reasoning_effort"] = effort
        else:
            # 无 api_base 的默认路径
            effort = EFFORT_MAPPING.get(thinking_level)
            if config["supports_reasoning"] and effort is not None:
                kwargs["reasoning_effort"] = effort

        if not config["supports_vision"]:
            kwargs["messages"] = _strip_images(messages)

        response = await litellm.acompletion(**kwargs)

        thinking_sent = False

        async for chunk in response:
            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta

            reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "thinking", None)
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
