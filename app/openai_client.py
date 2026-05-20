# -*- coding: utf-8 -*-
"""
OpenAI 官方 SDK 客户端 - 用于 OPENAI_API_BASE (sub2api 中转站) 路径
替代 LiteLLM 的 OPENAI_API_BASE 路径，正确捕获 delta.model_extra 中的
reasoning_details（含 signature），支持 Anthropic Claude 多轮 thinking。
"""
import time
import traceback
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx
from openai import AsyncOpenAI

from app.config import (
    HTTPX_PROXY,
    OPENAI_API_BASE,
    OPENAI_API_KEY_CUSTOM,
    get_litellm_model_config,
    get_route_key,
)
from app.ai.sampling_clamp import clamp_temperature, clamp_top_p
from app.litellm_client import EFFORT_MAPPING, _strip_images


def _is_complete_reasoning(rd: list) -> bool:
    """确保 reasoning_details 完整（含 signature），防残体写入死锁会话。"""
    if not isinstance(rd, list) or not rd:
        return False
    return all(
        item.get("signature") or item.get("data")
        for item in rd
        if item.get("type") in ("thinking", "reasoning")
    )


def _build_client() -> AsyncOpenAI:
    """构建 AsyncOpenAI 客户端，注入代理与自定义 base_url。"""
    http_client = None
    if HTTPX_PROXY:
        http_client = httpx.AsyncClient(proxy=HTTPX_PROXY)
    return AsyncOpenAI(
        api_key=OPENAI_API_KEY_CUSTOM or "dummy",
        base_url=OPENAI_API_BASE,
        http_client=http_client,
    )


async def call_openai_stream(
    messages: List[Dict[str, Any]],
    target_model: str,
    thinking_level: str = "low",
    enable_search: bool = False,
    temperature: float = 0.7,
    top_p: Optional[float] = None,
    conversation_id: str = "",
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    通过官方 openai.AsyncOpenAI 调用 OPENAI_API_BASE 中转站（流式）。
    正确捕获 delta.model_extra["reasoning_details"]（含 signature）用于多轮 thinking。

    Yields:
        {"content": "...", "thinking": "...", "reasoning_details": [...], "usage": {...}, "error": "..."}
    """
    route_key = get_route_key(target_model)
    config = get_litellm_model_config(route_key)
    model_name = config["model"]

    # Temperature clamp（Claude 模型上限 1.0）
    is_claude = "claude" in model_name.lower() or model_name.startswith("anthropic/")
    clamp_provider = "openclaw" if is_claude else "openai"
    clamped_temp = clamp_temperature(temperature, clamp_provider)
    if clamped_temp != temperature:
        print(f"⚠️ [OpenAI] temperature {temperature} → clamp 到 {clamped_temp}（model={model_name}）")
    temperature = clamped_temp

    if top_p is not None:
        clamped_top_p = clamp_top_p(top_p, "openai")
        if clamped_top_p != top_p:
            print(f"⚠️ [OpenAI] top_p {top_p} → clamp 到 {clamped_top_p}")
        top_p = clamped_top_p

    print(f"📡 [OpenAI] 请求模型: {model_name} (路由: {route_key}, thinking: {thinking_level})")
    print(f"🌡️ [OpenAI] 实际下发 temperature={temperature}, top_p={top_p if top_p is not None else 'default(unset)'}")

    start_time = time.time()

    try:
        client = _build_client()

        create_kwargs: Dict[str, Any] = {
            "model": model_name,
            "messages": messages if config["supports_vision"] else _strip_images(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": temperature,
        }
        if top_p is not None:
            create_kwargs["top_p"] = top_p

        # reasoning_effort 给 Claude / o1 等支持推理的模型
        effort = EFFORT_MAPPING.get(thinking_level)
        if config["supports_reasoning"] and effort is not None:
            create_kwargs["reasoning_effort"] = effort

        # GPT-5/o1/o3/o4 系列只接受 temperature=1；中转站用 drop_params 静默丢弃不支持的参数
        _model_base = model_name.split("/")[-1]
        if config["supports_reasoning"] or any(
            _model_base.startswith(p) for p in ("gpt-5", "o1", "o3", "o4")
        ):
            create_kwargs.pop("temperature", None)

        stream = await client.chat.completions.create(**create_kwargs)

        thinking_sent = False
        last_rd = None  # 追踪最后一个 reasoning_details chunk（循环后校验完整性再 yield）
        input_tokens = 0
        output_tokens = 0
        actual_model = model_name

        async for chunk in stream:
            if hasattr(chunk, "model") and chunk.model:
                actual_model = chunk.model

            if not chunk.choices:
                # 最终 chunk 携带 usage
                if hasattr(chunk, "usage") and chunk.usage:
                    input_tokens = getattr(chunk.usage, "prompt_tokens", 0) or 0
                    output_tokens = getattr(chunk.usage, "completion_tokens", 0) or 0
                continue

            delta = chunk.choices[0].delta
            model_extra = getattr(delta, "model_extra", None) or {}

            # 追踪 reasoning_details（含 signature）；完整数组在最后 chunk 才出现
            rd = model_extra.get("reasoning_details")
            if rd and isinstance(rd, list):
                last_rd = rd

            thinking = (
                getattr(delta, "reasoning_content", None)
                or getattr(delta, "thinking", None)
                or model_extra.get("reasoning_content")
                or model_extra.get("thinking")
            )
            if thinking:
                if not thinking_sent:
                    yield {"thinking_start": True}
                    thinking_sent = True
                yield {"thinking": thinking}

            if delta.content:
                if thinking_sent:
                    yield {"thinking_end": True}
                    thinking_sent = False
                yield {"content": delta.content}

        # 循环结束后，一次性 yield 完整 reasoning_details（校验 signature 防残体）
        if last_rd and _is_complete_reasoning(last_rd):
            print(f"🧠 [OpenAI] reasoning_details yielded ({len(last_rd)} blocks)")
            yield {"reasoning_details": last_rd}

        latency_ms = int((time.time() - start_time) * 1000)
        print(f"✅ [OpenAI] 响应结束 | 输入: {input_tokens}, 输出: {output_tokens}, 延迟: {latency_ms}ms")

        if output_tokens == 0:
            yield {"error": "⚠️ 模型未返回任何内容，请检查模型名和 API Key 配置"}
            return

        yield {
            "usage": {
                "model": actual_model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "latency_ms": latency_ms,
            }
        }

    except Exception as e:
        error_msg = str(e)
        print(f"❌ [OpenAI] 调用失败: {error_msg}")
        traceback.print_exc()
        yield {"error": f"OpenAI API Error: {error_msg}"}
