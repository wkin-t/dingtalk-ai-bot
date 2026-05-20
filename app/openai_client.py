# -*- coding: utf-8 -*-
"""OpenAI 官方 SDK 客户端 - 用于 OPENAI_API_BASE (sub2api 中转站) 路径"""
import json
import re
import time
import traceback
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx
from openai import AsyncOpenAI

from app.config import (
    HTTPX_PROXY,
    MODEL_ROUTER,
    OPENAI_API_BASE,
    OPENAI_API_KEY_CUSTOM,
    get_litellm_model_config,
    get_route_key,
)
from app.ai.sampling_clamp import clamp_temperature, clamp_top_p

EFFORT_MAPPING = {
    "minimal": "none",
    "low": "none",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
}


def _strip_images(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """过滤掉图片内容，只保留文本。"""
    cleaned = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts = [p.get("text", "") for p in content if p.get("type") == "text"]
            content = "\n".join(text_parts) if text_parts else "[图片已移除]"
        cleaned.append({**msg, "content": content})
    return cleaned



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


def _is_complete_reasoning(rd: list) -> bool:
    """确保 reasoning_details 有效，防止残体写入导致多轮 thinking 死锁。"""
    if not isinstance(rd, list) or not rd:
        return False
    return all(
        item.get("signature") or item.get("data")
        for item in rd
        if item.get("type") in ("thinking", "reasoning")
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
    通过 openai.AsyncOpenAI Chat Completions 调用 OPENAI_API_BASE 中转站（流式）。
    支持 Claude/Gemini/GPT 全量 upstream，兼容 reasoning_content/thinking delta 字段。

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

        extra_params: Dict[str, Any] = {}
        _model_base = model_name.split("/")[-1]
        if not (config["supports_reasoning"] or any(
            _model_base.startswith(p) for p in ("gpt-5", "o1", "o3", "o4")
        )):
            extra_params["temperature"] = temperature
        if top_p is not None:
            extra_params["top_p"] = top_p

        effort = EFFORT_MAPPING.get(thinking_level)
        if config["supports_reasoning"] and effort and effort != "none":
            extra_params["extra_body"] = {"reasoning": {"effort": effort}}

        processed_messages: Any = messages if config["supports_vision"] else _strip_images(messages)

        stream = await client.chat.completions.create(
            model=model_name,
            messages=processed_messages,
            stream=True,
            stream_options={"include_usage": True},
            **extra_params,
        )

        thinking_sent = False
        input_tokens = 0
        output_tokens = 0
        actual_model = model_name

        async for chunk in stream:
            if not chunk.choices:
                if hasattr(chunk, "usage") and chunk.usage:
                    input_tokens = chunk.usage.prompt_tokens or 0
                    output_tokens = chunk.usage.completion_tokens or 0
                continue

            if chunk.model:
                actual_model = chunk.model

            delta = chunk.choices[0].delta

            thinking = (
                getattr(delta, "reasoning_content", None)
                or getattr(delta, "thinking", None)
                or (delta.model_extra or {}).get("reasoning_content")
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

            rd = (delta.model_extra or {}).get("reasoning_details")
            if rd and _is_complete_reasoning(rd):
                yield {"reasoning_details": rd}

        if thinking_sent:
            yield {"thinking_end": True}

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


async def call_openai_simple(prompt: str, max_tokens: int = 500) -> str:
    """用于 Soul 进化等后台轻量文本生成任务（非流式）。"""
    try:
        client = _build_client()
        response = await client.chat.completions.create(
            model=MODEL_ROUTER,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content or ""
    except Exception as e:
        print(f"⚠️ [OpenAI简单调用] 失败: {e}")
        return ""


async def analyze_complexity_with_openai(
    content: str,
    has_images: bool = False,
    soul_text: str = "",
) -> dict:
    """
    用 OpenAI 轻量模型分析消息复杂度，输出路由建议。
    返回与 analyze_complexity_unified 相同的 dict 格式。
    """
    soul_instruction = f"你的性格设定: {soul_text[:100]}\n   请让思考短语符合这个性格。\n   " if soul_text else ""

    analysis_prompt = f"""分析用户问题，返回 JSON 路由建议。

问题: {content[:300]}
有图片: {"是" if has_images else "否"}

选择规则:
1. model（三个选项）:
   - "lite": 简单问候、闲聊、一句话基础问答（有图片时禁用此选项）
   - "fast": 日常问答、代码、一般分析、图片分析（默认；有图片时最低选此）
   - "pro": 仅用于复杂数学证明、学术研究、系统架构设计

2. thinking_level:
   - "minimal": 简单问候如"你好"、"谢谢"
   - "low": 普通问答、事实查询
   - "medium": 需要一定推理、代码问题
   - "high": 复杂分析、算法设计

3. need_search:
   - true: 需要实时信息（天气、新闻、股价、最新事件、当前日期）
   - false: 不需要（默认）

4. thinking_text: 一句简短思考状态（10字以内，不用emoji），和问题内容相关
   {soul_instruction}例如: 代码→"正在编译思路中", 数学→"开始推演计算", 闲聊→"让我想想"

5. temperature:
   - "precise": 代码、数学、翻译、事实查询
   - "balanced": 普通问答（默认）
   - "creative": 写作、诗歌、头脑风暴

6. need_image_gen:
   - true: 用户明确要求生成图片、画画、绘制
   - false: 默认

7. need_image_edit:
   - true: 有图片(has_images=是) 且用户文字中明确包含修改指令
   - false: 默认

只返回JSON:
{{"model":"fast","thinking_level":"low","need_search":false,"temperature":"balanced","need_image_gen":false,"need_image_edit":false,"reason":"简短原因","thinking_text":"正在思考"}}"""

    try:
        client = _build_client()
        response = await client.chat.completions.create(
            model=MODEL_ROUTER,
            messages=[{"role": "user", "content": analysis_prompt}],
            temperature=0.1,
            max_tokens=300,
        )
        result_text = response.choices[0].message.content or ""
        print(f"📝 [OpenAI路由] 原始返回: {result_text[:200]}")

        json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group())
            if result.get("model") not in ["lite", "fast", "pro"]:
                result["model"] = "fast"
            if result.get("thinking_level") not in ["minimal", "low", "medium", "high"]:
                result["thinking_level"] = "low"
            result.setdefault("need_search", False)
            result.setdefault("need_image_gen", False)
            result.setdefault("need_image_edit", False)
            result.setdefault("temperature", "balanced")
            if has_images and result.get("model") == "lite":
                result["model"] = "fast"
            print(f"🤖 [OpenAI路由] 结果: {result}")
            return result
        print(f"⚠️ [OpenAI路由] 无法提取 JSON: {result_text}")
    except Exception as e:
        print(f"⚠️ [OpenAI路由] 分析失败，降级关键词匹配: {e}")

    from app.ai.router import analyze_complexity_unified
    return analyze_complexity_unified(content, has_images)
