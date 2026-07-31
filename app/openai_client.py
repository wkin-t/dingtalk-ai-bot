# -*- coding: utf-8 -*-
"""OpenAI 官方 SDK 客户端 - 用于 OPENAI_API_BASE (sub2api 中转站) 路径"""
import asyncio
import json
import re
import time
import traceback
from collections.abc import Mapping
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

import httpx
import openai
from openai import AsyncOpenAI

from app.config import (
    HTTPX_PROXY,
    MODEL_ROUTER,
    OPENAI_API_BASE,
    OPENAI_API_KEY_CUSTOM,
    SEARCH_FALLBACK_PROVIDER,
    get_litellm_model_config,
    get_route_key,
)
from app.ai.sampling_clamp import clamp_temperature, clamp_top_p
from app.gemini_client import google_search

EFFORT_MAPPING = {
    "minimal": "none",
    "low": "none",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
}

RESPONSES_REASONING_DELTA_EVENTS = frozenset({
    "response.reasoning_text.delta",
    "response.reasoning_summary_text.delta",
})
RESPONSES_SEARCH_ITEM_TYPES = frozenset({"web_search_call"})
RESPONSES_SEARCH_ANNOTATION_TYPES = frozenset({"url_citation", "url"})
RESPONSES_GROUNDING_TEXT_EVENTS = frozenset({
    "response.output_text.delta",
    "response.output_text.done",
})
RESPONSES_SEARCH_ITEM_EVENTS = frozenset({
    "response.output_item.added",
    "response.output_item.done",
})
RESPONSES_SEARCH_ANNOTATION_EVENTS = frozenset({
    "response.output_text.annotation.added",
    "response.output_text.annotation.done",
})
RESPONSES_DIAGNOSTIC_EVENT_TYPES = frozenset({
    "response.created",
    "response.output_item.added",
    "response.output_item.done",
    "response.content_part.added",
    "response.content_part.done",
    "response.output_text.delta",
    "response.output_text.done",
    "response.output_text.annotation.added",
    "response.output_text.annotation.done",
    "response.reasoning_text.delta",
    "response.reasoning_text.done",
    "response.reasoning_summary_text.delta",
    "response.reasoning_summary_text.done",
    "response.completed",
    "response.failed",
})
RESPONSES_SEARCH_CANDIDATE_LIMIT = 64
ANTIGRAVITY_GROUNDING_REDIRECT_PREFIX = (
    "https://vertexaisearch.cloud.google.com/grounding-api-redirect/"
)
ANTIGRAVITY_GROUNDING_SOURCE_LINK_RE = re.compile(
    re.escape(ANTIGRAVITY_GROUNDING_REDIRECT_PREFIX)
    + r"[A-Za-z0-9._~%/-]{32,}"
)
ANTIGRAVITY_GROUNDING_WINDOW_LIMIT = (
    len(ANTIGRAVITY_GROUNDING_REDIRECT_PREFIX) + 256
)


def _response_field(value: Any, name: str) -> Any:
    """读取 SDK 对象或兼容网关字典中的字段。"""
    if isinstance(value, Mapping):
        return value.get(name)
    if value is None:
        return None

    # 不直接对任意对象调用 getattr：测试 double 和部分代理对象会为不存在
    # 的属性动态创建子对象，递归扫描时会因此无限扩张。OpenAI SDK 的响应
    # 模型是 Pydantic 对象，字段会出现在 __dict__ / model_fields 中。
    value_dict = getattr(value, "__dict__", None)
    if isinstance(value_dict, dict) and name in value_dict:
        return value_dict[name]
    model_fields = getattr(type(value), "model_fields", None)
    if isinstance(model_fields, dict) and name in model_fields:
        try:
            return getattr(value, name, None)
        except Exception:
            return None
    legacy_fields = getattr(type(value), "__dict__", {}).get("__fields__")
    if isinstance(legacy_fields, dict) and name in legacy_fields:
        try:
            return getattr(value, name, None)
        except Exception:
            return None
    if hasattr(type(value), name):
        try:
            return getattr(value, name, None)
        except Exception:
            return None
    return None


def _response_candidates(value: Any) -> List[Any]:
    """把单值/列表统一成有限候选集合，不展开字符串。"""
    if value is None or isinstance(value, (str, bytes)):
        return []
    if isinstance(value, (list, tuple)):
        return list(value[:RESPONSES_SEARCH_CANDIDATE_LIMIT])
    return [value]


def _response_type(value: Any) -> str:
    event_type = _response_field(value, "type")
    return event_type if isinstance(event_type, str) else ""


def _find_responses_search_signal(
    value: Any,
    path: str,
    depth: int = 0,
) -> Optional[str]:
    """只在固定 Responses 字段树中寻找 allowlist 搜索证据。

    网关可能把标准 item/annotation 放在 SDK 对象、字典或最终 output 的一层
    嵌套中。这里显式限制字段名和深度，避免对未知响应做全量递归或把普通
    文本中的 ``search`` 字样当成工具执行。
    """
    if depth > 3:
        return None

    if _response_type(value) in RESPONSES_SEARCH_ITEM_TYPES:
        return f"structured:{path}.type"

    for field_name in ("annotation", "annotations"):
        for index, candidate in enumerate(
            _response_candidates(_response_field(value, field_name))
        ):
            if _response_type(candidate) in RESPONSES_SEARCH_ANNOTATION_TYPES:
                suffix = f"{field_name}[{index}].type"
                return f"structured:{path}.{suffix}"

    for field_name in ("item", "output", "content", "response", "delta", "output_text"):
        child = _response_field(value, field_name)
        for index, candidate in enumerate(_response_candidates(child)):
            child_path = f"{path}.{field_name}"
            if isinstance(child, (list, tuple)):
                child_path += f"[{index}]"
            signal = _find_responses_search_signal(candidate, child_path, depth + 1)
            if signal:
                return signal
    return None


def _responses_search_signal(event: Any) -> Optional[str]:
    """提取一个 Responses 事件中的结构化搜索证据。"""
    event_type = _response_type(event)
    if event_type in RESPONSES_SEARCH_ITEM_EVENTS:
        item = _response_field(event, "item")
        if _response_type(item) in RESPONSES_SEARCH_ITEM_TYPES:
            return "structured:item.type"
    if event_type in RESPONSES_SEARCH_ANNOTATION_EVENTS:
        annotation = _response_field(event, "annotation")
        if _response_type(annotation) in RESPONSES_SEARCH_ANNOTATION_TYPES:
            return "structured:annotation.type"

    # 兼容 annotation/搜索 item 被包在 delta、output_text 或 response.output 中的
    # 网关形态；不把任意顶层文本递归进去。
    for path, value in (
        ("event", event),
        ("delta", _response_field(event, "delta")),
        ("output_text", _response_field(event, "output_text")),
        ("response", _response_field(event, "response")),
    ):
        signal = _find_responses_search_signal(value, path)
        if signal:
            return signal
    return None


def _responses_grounding_texts(event: Any) -> List[str]:
    """提取可用于 Grounding 启发式的有限输出文本字段。"""
    event_type = _response_type(event)
    if event_type not in RESPONSES_GROUNDING_TEXT_EVENTS:
        return []

    texts: List[str] = []
    for field_name in ("delta", "text", "output_text"):
        value = _response_field(event, field_name)
        if isinstance(value, str) and value:
            texts.append(value)
    return texts


def _safe_response_event_type(event: Any) -> str:
    """仅保留固定 allowlist 中的事件类型，供诊断摘要使用。"""
    event_type = _response_type(event)
    if event_type in RESPONSES_DIAGNOSTIC_EVENT_TYPES:
        return event_type
    return "other"


def _is_claude_model(model_name: str) -> bool:
    """识别带 provider 前缀或 Antigravity 无前缀的 Claude 模型。"""
    model_lower = model_name.lower()
    model_base = model_lower.rsplit("/", 1)[-1]
    return model_lower.startswith("anthropic/") or model_base.startswith("claude-")


def _last_user_text(messages: List[Dict[str, Any]]) -> str:
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            return "\n".join(part for part in parts if part)
    return ""


async def _build_search_fallback_summary(messages: List[Dict[str, Any]]) -> Optional[str]:
    if SEARCH_FALLBACK_PROVIDER != "gemini":
        return None
    query = _last_user_text(messages)
    if not query:
        return None
    return await google_search(query)


def _inject_search_summary_message(
    messages: List[Dict[str, Any]],
    summary: str,
) -> List[Dict[str, Any]]:
    search_message = {
        "role": "system",
        "content": (
            "## 联网搜索结果\n"
            "以下内容来自实时搜索摘要。回答涉及当前信息时优先使用这些结果；"
            "如果摘要不足以支持结论，请明确说明不确定。\n\n"
            f"{summary}"
        ),
    }
    return [search_message, *messages]


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
    kwargs: Dict[str, Any] = {
        "api_key": OPENAI_API_KEY_CUSTOM or "dummy",
        "http_client": http_client,
    }
    if OPENAI_API_BASE:
        kwargs["base_url"] = OPENAI_API_BASE
    return AsyncOpenAI(**kwargs)


async def _retry_create(create_fn: Callable, max_retries: int = 2) -> Any:
    """上游临时故障（502/503/连接失败）自动重试，指数退避，最多 max_retries 次。
    只在 stream.create() 建立前失败时重试——流式 yield 已开始后不适用。
    """
    for attempt in range(max_retries + 1):
        try:
            return await create_fn()
        except (openai.InternalServerError, openai.APIConnectionError) as e:
            status = getattr(e, "status_code", None) or 0
            retryable = (
                status in (502, 503)
                or "upstream" in str(e).lower()
                or isinstance(e, openai.APIConnectionError)
            )
            if retryable and attempt < max_retries:
                wait = 2 ** attempt  # 1s, 2s
                print(f"⚠️ [重试 {attempt + 1}/{max_retries}] 上游临时错误 ({status or type(e).__name__})，{wait}s 后重试")
                await asyncio.sleep(wait)
                continue
            raise


def _is_complete_reasoning(rd: list) -> bool:
    """确保 reasoning_details 有效，防止残体写入导致多轮 thinking 死锁。"""
    if not isinstance(rd, list) or not rd:
        return False
    return all(
        item.get("signature") or item.get("data")
        for item in rd
        if item.get("type") in ("thinking", "reasoning")
    )


def _convert_block_to_responses_format(block: Dict[str, Any], role: Optional[str]) -> Dict[str, Any]:
    """把 Chat Completions 的 content block 转成 Responses API 词汇表。

    Chat Completions → Responses 映射：
      - user 的 {"type": "text", ...}       → {"type": "input_text", ...}
      - assistant 的 {"type": "text", ...}  → {"type": "output_text", ...}
      - {"type": "image_url", "image_url": {"url": "..."}} → {"type": "input_image", "image_url": "..."}
      - 已经是 Responses 词汇（input_text / input_image / output_text）→ 原样保留
    """
    if not isinstance(block, dict):
        return block
    btype = block.get("type")
    if btype == "text":
        new_type = "output_text" if role == "assistant" else "input_text"
        out = {"type": new_type, "text": block.get("text", "")}
        # cache_control 保留——上游 Anthropic 路径仍可能识别
        if "cache_control" in block:
            out["cache_control"] = block["cache_control"]
        return out
    if btype == "image_url":
        img = block.get("image_url")
        url = img.get("url") if isinstance(img, dict) else img
        return {"type": "input_image", "image_url": url}
    return block


def _split_messages_for_responses(
    messages: List[Dict[str, Any]],
    supports_vision: bool,
    supports_store: bool = True,
):
    """把 chat completions messages 拆成 Responses API 需要的 (instructions, input)。

    - system 角色的 string content（如搜索兜底注入的一次性摘要）→ 恒定拼接到 instructions
      字符串，不受 supports_store 影响——一次性内容不需要占用缓存前缀。
    - system 角色的 list content（Stage B 分段 blocks，带 cache_control）：
        - supports_store=True（GPT，会用 previous_response_id 精简续接）→ 仍拼进
          instructions。instructions 每轮都无条件重发（不受精简路径影响，精简路径只
          精简 input 里的 user/assistant 历史），所以不会丢内容；cache_control 在此路径
          丢失是刻意选择，因为这条路径本来就没有精简路径以外能吃到显式缓存的场景。
        - supports_store=False（Claude/sub2api，没有服务端续接，每轮都全量重发 input）
          → 转换后作为一条 role="system" 消息插到 input 最前面，保留 cache_control，
          让每轮重发都有机会命中缓存断点。
    - 其他角色 → 放进 input 数组；string content 透传；list content 按 Responses
      词汇表逐块转换（text→input_text/output_text，image_url→input_image）
    - 视觉模型保留图片块；非视觉模型剥掉图片
    """
    instructions_parts: List[str] = []
    system_blocks: List[Dict[str, Any]] = []
    input_items: List[Dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "system":
            if isinstance(content, str):
                instructions_parts.append(content)
            elif isinstance(content, list):
                if supports_store:
                    for blk in content:
                        if isinstance(blk, dict) and blk.get("type") == "text":
                            instructions_parts.append(blk.get("text", ""))
                else:
                    for blk in content:
                        system_blocks.append(_convert_block_to_responses_format(blk, "system"))
            continue
        if isinstance(content, list):
            if not supports_vision:
                text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                content = "\n".join(text_parts) if text_parts else "[图片已移除]"
            else:
                content = [_convert_block_to_responses_format(blk, role) for blk in content]
        input_items.append({"role": role, "content": content})
    if system_blocks:
        input_items.insert(0, {"role": "system", "content": system_blocks})
    return "\n\n".join(p for p in instructions_parts if p), input_items


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
    流式调用 OPENAI_API_BASE 中转站。按 upstream 自动选择 API：
      - gemini-* → Chat Completions（sub2api Gemini 适配层不支持 Responses）
      - anthropic/* | openai/* | gpt-* | o1/o3/o4 → Responses API
        （绕开 sub2api Chat Completions 在多轮对话下的 "Invalid Responses API request" bug）

    Yields:
        {"content": "...", "thinking": "...", "usage": {...}, "error": "..."}
    """
    route_key = get_route_key(target_model)
    config = get_litellm_model_config(route_key)
    model_name = config["model"]

    is_claude = _is_claude_model(model_name)
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

    # 路由决策
    is_gemini_upstream = "gemini" in model_name.lower()
    api_kind = "chat" if is_gemini_upstream else "responses"

    print(f"📡 [OpenAI/{api_kind}] 请求模型: {model_name} (路由: {route_key}, thinking: {thinking_level})")
    print(f"🌡️ [OpenAI] 实际下发 temperature={temperature}, top_p={top_p if top_p is not None else 'default(unset)'}")

    start_time = time.time()

    try:
        client = _build_client()
        native_search = enable_search and (not is_gemini_upstream) and bool(config.get("supports_search"))
        fallback_summary = None
        request_messages = messages
        if enable_search and not native_search:
            fallback_summary = await _build_search_fallback_summary(messages)
            if fallback_summary:
                request_messages = _inject_search_summary_message(messages, fallback_summary)
                print("🔍 [OpenAI] 已注入 Gemini 搜索摘要")
            else:
                print("⚠️ [OpenAI] 搜索已请求，但没有可用 fallback 摘要")

        if enable_search:
            yield {
                "search": {
                    "requested": True,
                    "native_enabled": bool(native_search),
                    "fallback_injected": bool(fallback_summary),
                    "reason": "native" if native_search else ("fallback_gemini" if fallback_summary else "unavailable"),
                }
            }

        if is_gemini_upstream:
            async for evt in _stream_via_chat_completions(
                client, model_name, config, request_messages,
                temperature, top_p, thinking_level, start_time,
            ):
                yield evt
        else:
            async for evt in _stream_via_responses(
                client, model_name, config, request_messages,
                temperature, top_p, thinking_level, start_time,
                conversation_id=conversation_id,
                enable_search=enable_search,
            ):
                yield evt

    except Exception as e:
        error_msg = str(e)
        print(f"❌ [OpenAI/{api_kind}] 调用失败: {error_msg}")
        traceback.print_exc()
        yield {"error": f"OpenAI API Error: {error_msg}"}


async def _stream_via_chat_completions(
    client,
    model_name: str,
    config: Dict[str, Any],
    messages: List[Dict[str, Any]],
    temperature: float,
    top_p: Optional[float],
    thinking_level: str,
    start_time: float,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Chat Completions 路径（Gemini 上游专用）。"""
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

    create_kwargs: Dict[str, Any] = {
        "model": model_name,
        "messages": processed_messages,
        "stream": True,
        **extra_params,
    }
    if any(_model_base.startswith(p) for p in ("gpt-", "o1", "o3", "o4", "text-")):
        create_kwargs["stream_options"] = {"include_usage": True}
    stream = await _retry_create(lambda: client.chat.completions.create(**create_kwargs))

    thinking_sent = False
    input_tokens = 0
    output_tokens = 0
    content_chars = 0  # 实际流出的字符数（sub2api Gemini 不带 usage，这是真实证据）
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
            content_chars += len(delta.content)
            yield {"content": delta.content}

        rd = (delta.model_extra or {}).get("reasoning_details")
        if rd and _is_complete_reasoning(rd):
            yield {"reasoning_details": rd}

    if thinking_sent:
        yield {"thinking_end": True}

    latency_ms = int((time.time() - start_time) * 1000)
    print(f"✅ [OpenAI/chat] 响应结束 | 输入: {input_tokens}, 输出: {output_tokens}, 字符: {content_chars}, 延迟: {latency_ms}ms")

    # 判断"无返回"以 content_chars 为准（sub2api Gemini 路径不带 usage，output_tokens 恒为 0）
    if content_chars == 0 and output_tokens == 0:
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


async def _stream_via_responses(
    client,
    model_name: str,
    config: Dict[str, Any],
    messages: List[Dict[str, Any]],
    temperature: float,
    top_p: Optional[float],
    thinking_level: str,
    start_time: float,
    conversation_id: str = "",
    enable_search: bool = False,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Responses API 路径（Claude/GPT 上游）。

    多轮 thinking 通过 previous_response_id 机制保留：
    - 服务端用 store=True 持久化 response
    - 下一轮把上一轮的 response.id 作为 previous_response_id 传回，仅发送新 user 消息
    - 状态保存在 responses_state（Redis + 文件降级，TTL 7 天）
    - previous_response_id 失效时清除状态、回退全量历史重试一次

    Stage B 的 system cache_control 按 _supports_store 分流（见 _split_messages_for_responses）：
    GPT（store=True）走 instructions，每轮无条件刷新，不受精简续接路径影响；
    Claude（store=False）走 input[0] 的 role="system" 消息，保留 cache_control，
    每轮全量重发才有机会命中 sub2api 转译层的缓存。
    """
    from app import responses_state

    # previous_response_id 仅对支持 store 的上游有效（OpenAI 原生），Anthropic 不支持；
    # 同一个信号也决定 system 内容走 instructions 还是 input[0] 的 system 消息——
    # 不是按模型名分叉消息结构，是按这个已有的 store 机制信号分叉（见函数内 docstring）
    _supports_store = not _is_claude_model(model_name)
    full_instructions, full_input_items = _split_messages_for_responses(
        messages, config["supports_vision"], _supports_store
    )

    prev_response_id = (
        responses_state.get_response_id(conversation_id)
        if (conversation_id and _supports_store)
        else None
    )

    def _last_user_only(items: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
        for item in reversed(items):
            if item.get("role") == "user":
                return [item]
        return None

    if prev_response_id:
        slim = _last_user_only(full_input_items)
        if slim:
            input_items = slim
        else:
            prev_response_id = None
            input_items = full_input_items
    else:
        input_items = full_input_items

    def _build_kwargs(use_prev: bool, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        # _supports_store 复用外层闭包变量（Anthropic 没有 Responses API 服务端存储，
        # sub2api 转发 store=True 会 502）
        kw: Dict[str, Any] = {
            "model": model_name,
            "input": items,
            "stream": True,
            "store": _supports_store,
        }
        if full_instructions:
            kw["instructions"] = full_instructions

        _model_base = model_name.split("/")[-1]
        if not (config["supports_reasoning"] or any(
            _model_base.startswith(p) for p in ("gpt-5", "o1", "o3", "o4")
        )):
            kw["temperature"] = temperature
        if top_p is not None:
            kw["top_p"] = top_p

        effort = EFFORT_MAPPING.get(thinking_level)
        # 当前只有 Claude Responses 的生产证据证明 low 可用；GPT 与 Gemini
        # 请求保持历史行为，避免扩大 35002 canary 之外的费用和延迟变化。
        if thinking_level == "low" and _is_claude_model(model_name):
            effort = "low"
        if config["supports_reasoning"] and effort and effort != "none":
            kw["reasoning"] = {"effort": effort}

        if enable_search and config.get("supports_search"):
            kw["tools"] = [{"type": "web_search"}]

        if use_prev and prev_response_id:
            kw["previous_response_id"] = prev_response_id
        return kw

    create_kwargs = _build_kwargs(use_prev=True, items=input_items)

    try:
        stream = await _retry_create(lambda: client.responses.create(**create_kwargs))
    except Exception as e:
        msg = str(e).lower()
        is_prev_id_error = prev_response_id and (
            "previous_response_id" in msg or "previous response" in msg or "not found" in msg
        )
        if is_prev_id_error:
            print(f"⚠️ [OpenAI/responses] previous_response_id 失效，清状态回退全量历史重试")
            if conversation_id:
                responses_state.clear_response_id(conversation_id)
            create_kwargs = _build_kwargs(use_prev=False, items=full_input_items)
            stream = await _retry_create(lambda: client.responses.create(**create_kwargs))
        else:
            raise

    thinking_sent = False
    input_tokens = 0
    output_tokens = 0
    cached_tokens = 0
    content_chars = 0
    actual_model = model_name
    new_response_id: Optional[str] = None
    content_started = False
    # sub2api 的 Antigravity Responses 路径可能只在正文返回完整 Grounding source link，
    # 不透传标准搜索事件。该信号是生产形态启发式证据，不是工具执行的密码学证明。
    # 只在本次确实挂载原生工具时检查，并限制缓冲区大小。
    search_executed_sent = False
    search_evidence_reason: Optional[str] = None
    grounding_tail = ""
    native_search = bool(enable_search and config.get("supports_search"))
    response_event_counts: Dict[str, int] = {}

    def _print_search_probe() -> None:
        """打印不含正文/URL 的 Responses 搜索事件摘要。"""
        if not native_search:
            return
        event_summary = ",".join(
            f"{event_type}={count}"
            for event_type, count in sorted(response_event_counts.items())
        ) or "none"
        print(
            "🔎 [Responses搜索探针] "
            f"native=true executed={'true' if search_executed_sent else 'false'} "
            f"evidence={search_evidence_reason or 'none'} "
            f"events={event_summary}"
        )

    stream_error_after_thinking = object()
    stream_cancel_after_thinking = object()

    async def _events_with_thinking_cleanup():
        """让流异常先收口 thinking，再交给外层保留原有错误 contract。"""
        nonlocal thinking_sent
        try:
            async for event in stream:
                yield event
        except asyncio.CancelledError:
            _print_search_probe()
            if thinking_sent:
                thinking_sent = False
                yield stream_cancel_after_thinking
            raise
        except Exception:
            _print_search_probe()
            if thinking_sent:
                thinking_sent = False
                yield stream_error_after_thinking
            raise

    event_stream = _events_with_thinking_cleanup()
    async for event in event_stream:
        if event is stream_error_after_thinking or event is stream_cancel_after_thinking:
            yield {"thinking_end": True}
            continue

        event_type = _response_type(event)
        event_key = _safe_response_event_type(event)
        if event_key in response_event_counts or len(response_event_counts) < 24:
            response_event_counts[event_key] = response_event_counts.get(event_key, 0) + 1
        elif "other" in response_event_counts:
            response_event_counts["other"] += 1
        else:
            response_event_counts["other"] = 1
        if not event_type:
            continue

        if native_search and not search_executed_sent:
            executed_reason = _responses_search_signal(event)
            if executed_reason:
                search_executed_sent = True
                search_evidence_reason = executed_reason
                print(f"🌐 [搜索执行] Responses {executed_reason} detected，本次真实联网")
                yield {"search": {"executed": True}}

        if native_search and not search_executed_sent:
            for text_fragment in _responses_grounding_texts(event):
                grounding_window = grounding_tail + text_fragment
                if ANTIGRAVITY_GROUNDING_SOURCE_LINK_RE.search(grounding_window):
                    search_executed_sent = True
                    search_evidence_reason = "grounding_redirect"
                    print("🌐 [搜索执行] Responses grounding_redirect detected，本次真实联网")
                    yield {"search": {"executed": True}}
                    break
                grounding_tail = grounding_window[-ANTIGRAVITY_GROUNDING_WINDOW_LIMIT:]

        if event_type == "response.output_text.delta":
            delta = _response_field(event, "delta")
            if not isinstance(delta, str):
                delta = _response_field(event, "text")
            if isinstance(delta, str) and delta:
                content_started = True
                if thinking_sent:
                    yield {"thinking_end": True}
                    thinking_sent = False
                content_chars += len(delta)
                yield {"content": delta}

        elif event_type in RESPONSES_REASONING_DELTA_EVENTS:
            delta = _response_field(event, "delta")
            if isinstance(delta, str) and delta and not content_started:
                if not thinking_sent:
                    yield {"thinking_start": True}
                    thinking_sent = True
                yield {"thinking": delta}

        elif event_type in ("response.created", "response.completed"):
            response = _response_field(event, "response")
            if response is None:
                continue
            rid = _response_field(response, "id")
            if rid:
                new_response_id = rid
            mdl = _response_field(response, "model")
            if mdl:
                actual_model = mdl
            usage = _response_field(response, "usage")
            if usage:
                input_tokens = _response_field(usage, "input_tokens") or 0
                output_tokens = _response_field(usage, "output_tokens") or 0
                _det = _response_field(usage, "input_tokens_details")
                _c = (_response_field(_det, "cached_tokens") or 0) if _det else 0
                cached_tokens = _c if isinstance(_c, int) else 0

        elif event_type == "response.failed":
            _print_search_probe()
            response = _response_field(event, "response")
            err = _response_field(response, "error") if response else None
            err_msg = _response_field(err, "message") or str(err) if err else "Unknown failure"
            if thinking_sent:
                thinking_sent = False
                await event_stream.aclose()
                yield {"thinking_end": True}
            else:
                await event_stream.aclose()
            yield {"error": f"OpenAI Responses Error: {err_msg}"}
            return

    if thinking_sent:
        yield {"thinking_end": True}

    latency_ms = int((time.time() - start_time) * 1000)
    print(f"✅ [OpenAI/responses] 响应结束 | 输入: {input_tokens}, 输出: {output_tokens}, 字符: {content_chars}, 延迟: {latency_ms}ms")
    _print_search_probe()
    _cache_pct = round(cached_tokens / input_tokens * 100) if input_tokens else 0
    print(f"💾 [Cache] OpenAI/responses | cached={cached_tokens}/{input_tokens} ({_cache_pct}%)")

    if content_chars == 0 and output_tokens == 0:
        yield {"error": "⚠️ 模型未返回任何内容，请检查模型名和 API Key 配置"}
        return

    # 成功响应：存 response.id 给下一轮 previous_response_id 用
    if _supports_store and new_response_id and conversation_id:
        responses_state.set_response_id(conversation_id, new_response_id)

    yield {
        "usage": {
            "model": actual_model,
            "cached_tokens": cached_tokens,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency_ms": latency_ms,
        }
    }


async def call_openai_simple(prompt: str, max_tokens: Optional[int] = None) -> str:
    """用于 Soul 进化等后台轻量文本生成任务（非流式）。

    max_tokens 默认 None（不下发），让上游用其默认 budget。原默认 500 在 thinking
    模型（如 Gemini 3.5-flash）上会被思考全部消耗，留给输出 0 token。
    """
    try:
        client = _build_client()
        kwargs: Dict[str, Any] = {
            "model": MODEL_ROUTER,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        response = await client.chat.completions.create(**kwargs)
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
        # 不设 max_tokens：让上游用默认值（通常 4096+）。原本 300 太小会让 thinking 模型
        # （如 Gemini 3.5-flash）把预算全用在思考上、留给输出 0 token，导致 finish_reason=
        # length + 空 content。输出只是短 JSON（~100 token），用上游默认 budget 余量充足。
        response = await client.chat.completions.create(
            model=MODEL_ROUTER,
            messages=[{"role": "user", "content": analysis_prompt}],
            temperature=0.1,
        )
        result_text = response.choices[0].message.content or ""
        if not result_text:
            finish_reason = getattr(response.choices[0], "finish_reason", "?")
            usage = getattr(response, "usage", None)
            ct = getattr(usage, "completion_tokens", "?") if usage else "?"
            print(f"⚠️ [OpenAI路由] content 为空 (finish_reason={finish_reason}, completion_tokens={ct})——可能 thinking token 占满预算或被 safety filter 拦截")
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
