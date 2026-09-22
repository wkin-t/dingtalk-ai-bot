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
from app.error_safety import safe_model_name
from app.antigravity_search import (
    BRIDGE_TOOL_NAME,
    SearchEvidence,
    bridge_ready,
    build_search_tool,
    search_current_web,
)
from app.config import ANTIGRAVITY_CLAUDE_BRIDGE_THINKING_LEVELS
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



def _bridge_route_matches(expected: str, actual: str) -> bool:
    """要求网关返回同一 Claude 模型族，允许 provider 前缀和短版本别名。"""
    if not _is_claude_model(actual):
        return False
    expected_base = expected.lower().rsplit("/", 1)[-1]
    actual_base = actual.lower().rsplit("/", 1)[-1]
    return (
        actual_base == expected_base
        or actual_base.startswith(expected_base + "-")
        or expected_base.startswith(actual_base + "-")
    )

BRIDGE_SAFE_ERROR = "Claude 搜索桥接暂不可用，请稍后重试。"
# antigravity 网关的已知行为：Claude 在搜索场景下会被中转站静默换成非 Claude 模型
# （实测常见 gemini-2.5-flash）。_bridge_route_matches 会拦截这种响应，这里给出区分
# 于通用故障的提示，方便群里使用者和看日志的人都能知道这不是普通的桥接故障。
BRIDGE_MODEL_SWAP_ERROR = (
    "⚠️ 联网搜索触发了中转站的已知行为：Claude 被静默换成了其他模型（如 gemini-2.5-flash），"
    "为避免冒充 Claude 回复，本轮已拦截，请重试或换个问法。"
)
BRIDGE_MAX_CALLS = 8
BRIDGE_MAX_ARGUMENT_BYTES = 8192
BRIDGE_MAX_PENDING_CHARS = 32768


def _bridge_safe_id(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\x00-\x1f\x7f]", "", value)[:128]


def _bridge_merge_field(entry: Dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        entry["invalid"] = True
        return
    value = _bridge_safe_id(value)
    if not value:
        entry["invalid"] = True
        return
    previous = entry.get(key)
    if previous and previous != value:
        entry["invalid"] = True
        return
    entry[key] = value


def _bridge_set_argument(entry: Dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, str) or len(value.encode("utf-8")) > BRIDGE_MAX_ARGUMENT_BYTES:
        entry["invalid"] = True
        return
    previous = entry.get(key)
    if previous is not None and previous != value:
        entry["invalid"] = True
        return
    entry[key] = value


def _bridge_entry(ledger: Dict[str, Dict[str, Any]], item_id: str) -> Dict[str, Any]:
    return ledger.setdefault(
        item_id,
        {
            "item_id": item_id,
            "call_id": "",
            "name": "",
            "fragments": [],
            "done_arguments": None,
            "added_arguments": None,
            "item_arguments": None,
            "completed_arguments": None,
            "invalid": False,
        },
    )


def _bridge_record_item(
    ledger: Dict[str, Dict[str, Any]],
    item: Any,
    source: str,
    errors: list[str],
) -> bool:
    if _response_type(item) != "function_call":
        return False
    item_id = _bridge_safe_id(_response_field(item, "id") or _response_field(item, "item_id"))
    if not item_id:
        errors.append("missing_item_id")
        return True
    if item_id not in ledger and len(ledger) >= BRIDGE_MAX_CALLS:
        errors.append("call_limit")
        return True
    entry = _bridge_entry(ledger, item_id)
    _bridge_merge_field(entry, "call_id", _response_field(item, "call_id"))
    _bridge_merge_field(entry, "name", _response_field(item, "name"))
    arguments = _response_field(item, "arguments")
    _bridge_set_argument(entry, f"{source}_arguments", arguments)
    return True


def _bridge_record_event(
    ledger: Dict[str, Dict[str, Any]],
    event: Any,
    errors: list[str],
) -> bool:
    event_type = _response_type(event)
    if event_type == "response.output_item.added":
        # added 事件可能只携带参数前缀，作为最低优先级候选保存。
        return _bridge_record_item(ledger, _response_field(event, "item"), "added", errors)
    if event_type == "response.output_item.done":
        return _bridge_record_item(ledger, _response_field(event, "item"), "item", errors)
    if event_type == "response.function_call_arguments.delta":
        item_id = _bridge_safe_id(_response_field(event, "item_id"))
        if not item_id:
            errors.append("missing_item_id")
            return True
        entry = _bridge_entry(ledger, item_id)
        _bridge_merge_field(entry, "call_id", _response_field(event, "call_id"))
        delta = _response_field(event, "delta")
        if not isinstance(delta, str):
            entry["invalid"] = True
            errors.append("invalid_delta")
            return True
        fragments = entry["fragments"]
        current_bytes = sum(len(part.encode("utf-8")) for part in fragments)
        if current_bytes + len(delta.encode("utf-8")) > BRIDGE_MAX_ARGUMENT_BYTES:
            entry["invalid"] = True
            errors.append("argument_limit")
        else:
            fragments.append(delta)
        return True
    if event_type == "response.function_call_arguments.done":
        item_id = _bridge_safe_id(_response_field(event, "item_id"))
        if not item_id:
            errors.append("missing_item_id")
            return True
        entry = _bridge_entry(ledger, item_id)
        _bridge_merge_field(entry, "call_id", _response_field(event, "call_id"))
        # v0.1.168 可能只发 done 信号，不能把缺失 arguments 当成空参数。
        _bridge_set_argument(entry, "done_arguments", _response_field(event, "arguments"))
        return True
    if event_type == "response.completed":
        response = _response_field(event, "response")
        output = _response_field(response, "output")
        if not isinstance(output, (list, tuple)):
            return False
        if len(output) > BRIDGE_MAX_CALLS:
            errors.append("call_limit")
        bounded_output = list(output)[:BRIDGE_MAX_CALLS]
        for item in bounded_output:
            _bridge_record_item(ledger, item, "completed", errors)
        return any(_response_type(item) == "function_call" for item in bounded_output)
    return False


def _bridge_fixed_tool_output(reason: str) -> str:
    return SearchEvidence(False, reason=reason).as_tool_output()


def _bridge_parse_calls(
    ledger: Dict[str, Dict[str, Any]],
    errors: list[str],
) -> tuple[list[Dict[str, Any]], Optional[int]]:
    calls: list[Dict[str, Any]] = []
    if errors:
        # 缺 item id 等无法配对的事件不能被静默修复，直接 fail-closed。
        return [], None
    for entry in ledger.values():
        primary_candidates = [
            entry.get("item_arguments"),
            entry.get("completed_arguments"),
            entry.get("done_arguments"),
            "".join(entry.get("fragments") or []),
        ]
        primary_candidates = [
            candidate
            for candidate in primary_candidates
            if isinstance(candidate, str) and candidate
        ]
        if len(set(primary_candidates)) > 1:
            entry["invalid"] = True
        raw_arguments = (
            primary_candidates[0]
            if primary_candidates
            else entry.get("added_arguments") or "{}"
        )
        try:
            parsed = json.loads(raw_arguments)
        except (TypeError, ValueError, RecursionError):
            parsed = None
            entry["invalid"] = True
        if not isinstance(parsed, dict):
            entry["invalid"] = True
        name = entry.get("name") or ""
        call_id = entry.get("call_id") or ""
        if not call_id:
            entry["invalid"] = True
        query = parsed.get("query") if isinstance(parsed, dict) else None
        supported = name == BRIDGE_TOOL_NAME and isinstance(query, str) and bool(query.strip()) and len(query) <= 512
        if entry.get("invalid"):
            status = "invalid_tool_call"
            input_name = BRIDGE_TOOL_NAME if name == BRIDGE_TOOL_NAME else "unsupported_tool"
            input_arguments = "{}"
            supported = False
        elif not supported:
            status = "unsupported_tool"
            input_name = "unsupported_tool"
            input_arguments = raw_arguments
        else:
            status = "pending"
            input_name = BRIDGE_TOOL_NAME
            input_arguments = raw_arguments
        calls.append(
            {
                "item_id": entry["item_id"],
                "call_id": call_id,
                "name": input_name,
                "arguments": input_arguments,
                "query": query if supported else None,
                "supported": supported,
                "status": status,
                "output": _bridge_fixed_tool_output(status),
            }
        )
    if not calls:
        return [], None

    call_counts: Dict[str, int] = {}
    for call in calls:
        call_id = call["call_id"]
        call_counts[call_id] = call_counts.get(call_id, 0) + 1
    duplicate_call_ids = {
        call_id for call_id, count in call_counts.items() if call_id and count > 1
    }
    for call in calls:
        if call["call_id"] in duplicate_call_ids:
            call["supported"] = False
            call["status"] = "duplicate_call_id"
            call["output"] = _bridge_fixed_tool_output("duplicate_call_id")

    selected: Optional[int] = None
    for index, call in enumerate(calls):
        if call["supported"] and selected is None:
            selected = index
        elif call["supported"]:
            call["supported"] = False
            call["status"] = "limit_reached"
            call["output"] = _bridge_fixed_tool_output("limit_reached")
    return calls, selected


def _bridge_tool_input(call: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "function_call",
        "id": call["item_id"],
        "call_id": call["call_id"],
        "name": call["name"],
        "arguments": call["arguments"],
    }


def _bridge_tool_output_input(call: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "function_call_output",
        "call_id": call["call_id"],
        "output": call["output"],
    }



async def _stream_via_claude_bridge(
    client: Any,
    model_name: str,
    config: Dict[str, Any],
    messages: List[Dict[str, Any]],
    temperature: float,
    top_p: Optional[float],
    thinking_level: str,
    start_time: float,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Claude function-tool → 专用 Gemini 搜索 → Claude continuation。"""
    full_instructions, full_input_items = _split_messages_for_responses(
        messages, config["supports_vision"], supports_store=False
    )

    def _kwargs(items: List[Dict[str, Any]], with_tool: bool) -> Dict[str, Any]:
        request: Dict[str, Any] = {
            "model": model_name,
            "input": items,
            "stream": True,
            "store": False,
        }
        if full_instructions:
            request["instructions"] = full_instructions
        model_base = model_name.split("/")[-1]
        if not (config["supports_reasoning"] or any(
            model_base.startswith(prefix) for prefix in ("gpt-5", "o1", "o3", "o4")
        )):
            request["temperature"] = temperature
        if top_p is not None:
            request["top_p"] = top_p
        effort = EFFORT_MAPPING.get(thinking_level)
        if thinking_level == "low":
            effort = "low"
        if config["supports_reasoning"] and effort and effort != "none":
            request["reasoning"] = {"effort": effort}
        if with_tool:
            request["tools"] = [build_search_tool()]
        return request

    async def _consume(
        stream: Any,
        state: Dict[str, Any],
        allow_tools: bool,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        ledger: Dict[str, Dict[str, Any]] = {}
        ledger_errors: list[str] = []
        pending: list[Dict[str, Any]] = []
        pending_chars = 0
        thinking_sent = False
        content_started = False

        def _queue_or_publish(chunk: Dict[str, Any]) -> bool:
            nonlocal pending_chars
            if state["identity_confirmed"]:
                return True
            chunk_size = sum(len(str(value)) for value in chunk.values())
            if pending_chars + chunk_size > BRIDGE_MAX_PENDING_CHARS:
                state["error"] = "prefix_limit"
                pending.clear()
                return False
            pending.append(chunk)
            pending_chars += chunk_size
            return True

        async def _flush_pending() -> AsyncGenerator[Dict[str, Any], None]:
            nonlocal pending_chars
            if not state["identity_confirmed"]:
                return
            for chunk in pending:
                yield chunk
            pending.clear()
            pending_chars = 0

        try:
            async for event in stream:
                event_type = _response_type(event)
                response = _response_field(event, "response")
                response_model = _response_field(response, "model")
                if isinstance(response_model, str) and response_model:
                    if not _bridge_route_matches(model_name, response_model):
                        state["identity_error"] = True
                        state["error"] = "non_claude_model"
                        print(
                            f"🔀 [Claude桥接] 中转站把 {model_name} 换成了 {response_model}"
                            "（antigravity 搜索场景已知行为），已拦截，不会冒充 Claude 回复"
                        )
                        pending.clear()
                        break
                    if state.get("model") and state["model"] != response_model:
                        state["identity_error"] = True
                        state["error"] = "model_conflict"
                        pending.clear()
                        break
                    state["model"] = response_model
                    if event_type == "response.created":
                        state["identity_confirmed"] = True

                usage = _response_field(response, "usage")
                if usage is not None:
                    input_tokens = _response_field(usage, "input_tokens") or 0
                    output_tokens = _response_field(usage, "output_tokens") or 0
                    cached_details = _response_field(usage, "input_tokens_details")
                    cached_tokens = _response_field(cached_details, "cached_tokens") or 0
                    if isinstance(input_tokens, int):
                        state["input_tokens"] = input_tokens
                    if isinstance(output_tokens, int):
                        state["output_tokens"] = output_tokens
                    if isinstance(cached_tokens, int):
                        state["cached_tokens"] = cached_tokens

                if event_type == "response.failed":
                    state["error"] = "provider"
                    if thinking_sent and state["identity_confirmed"]:
                        yield {"thinking_end": True}
                        thinking_sent = False
                    break

                has_tool_event = _bridge_record_event(ledger, event, ledger_errors)
                if has_tool_event and not allow_tools:
                    state["unexpected_tool"] = True
                    if thinking_sent and state["identity_confirmed"]:
                        yield {"thinking_end": True}
                    break

                if event_type == "response.reasoning_text.delta" or event_type == "response.reasoning_summary_text.delta":
                    delta = _response_field(event, "delta")
                    if isinstance(delta, str) and delta and not content_started:
                        if not thinking_sent:
                            thinking_sent = True
                            chunk = {"thinking_start": True}
                            if _queue_or_publish(chunk) and state["identity_confirmed"]:
                                yield chunk
                        chunk = {"thinking": delta}
                        state["visible_chars"] += len(delta)
                        if _queue_or_publish(chunk) and state["identity_confirmed"]:
                            yield chunk

                elif event_type == "response.output_text.delta":
                    delta = _response_field(event, "delta")
                    if not isinstance(delta, str):
                        delta = _response_field(event, "text")
                    if isinstance(delta, str) and delta:
                        content_started = True
                        state["content_chars"] += len(delta)
                        state["visible_chars"] += len(delta)
                        chunks: list[Dict[str, Any]] = []
                        if thinking_sent:
                            thinking_sent = False
                            chunks.append({"thinking_end": True})
                        chunks.append({"content": delta})
                        for chunk in chunks:
                            if _queue_or_publish(chunk) and state["identity_confirmed"]:
                                yield chunk

                if state["identity_confirmed"] and pending:
                    async for chunk in _flush_pending():
                        yield chunk

                if event_type == "response.completed":
                    state["completed"] = True

        except asyncio.CancelledError:
            if thinking_sent and state["identity_confirmed"]:
                yield {"thinking_end": True}
                thinking_sent = False
            raise
        except Exception:
            state["error"] = "stream"

        if state.get("error"):
            if thinking_sent and state["identity_confirmed"]:
                yield {"thinking_end": True}
                thinking_sent = False
            pending.clear()
            return
        if not state.get("identity_confirmed"):
            state["error"] = "missing_identity"
            pending.clear()
            return
        if not state.get("completed"):
            state["error"] = "incomplete"
            pending.clear()
            return
        if thinking_sent:
            yield {"thinking_end": True}
            thinking_sent = False
        state["ledger"] = ledger
        state["ledger_errors"] = ledger_errors
        state["thinking_open"] = thinking_sent

    first_state: Dict[str, Any] = {
        "identity_confirmed": False,
        "identity_error": False,
        "model": "",
        "completed": False,
        "error": None,
        "content_chars": 0,
        "visible_chars": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "unexpected_tool": False,
    }
    try:
        first_stream = await _retry_create(
            lambda: client.responses.create(**_kwargs(full_input_items, with_tool=True))
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        yield {"error": BRIDGE_SAFE_ERROR}
        return

    async for chunk in _consume(first_stream, first_state, allow_tools=True):
        yield chunk
    if first_state.get("error") or first_state.get("identity_error"):
        swap_error = first_state.get("error") == "non_claude_model"
        yield {"error": BRIDGE_MODEL_SWAP_ERROR if swap_error else BRIDGE_SAFE_ERROR}
        return

    if first_state.get("ledger_errors"):
        yield {"error": BRIDGE_SAFE_ERROR}
        return

    calls, selected_index = _bridge_parse_calls(
        first_state.get("ledger", {}), first_state.get("ledger_errors", [])
    )
    if any(call["status"] == "duplicate_call_id" for call in calls):
        yield {"error": BRIDGE_SAFE_ERROR}
        return
    if not calls:
        if first_state["visible_chars"] <= 0:
            yield {"error": BRIDGE_SAFE_ERROR}
            return
        latency_ms = int((time.time() - start_time) * 1000)
        yield {
            "usage": {
                "model": safe_model_name(first_state["model"]),
                "input_tokens": first_state["input_tokens"],
                "output_tokens": first_state["output_tokens"],
                "cached_tokens": first_state["cached_tokens"],
                "latency_ms": latency_ms,
            }
        }
        return

    if selected_index is not None:
        selected_call = calls[selected_index]
        try:
            evidence = await search_current_web(selected_call["query"])
        except asyncio.CancelledError:
            raise
        except Exception:
            evidence = SearchEvidence(False, reason="provider")
        if isinstance(evidence, SearchEvidence) and evidence.success and evidence.summary and evidence.sources:
            selected_call["output"] = evidence.as_tool_output()
            yield {"search": {"executed": True}}
        else:
            selected_call["output"] = (
                evidence.as_tool_output()
                if isinstance(evidence, SearchEvidence)
                else _bridge_fixed_tool_output("provider")
            )

    continuation_items = list(full_input_items)
    for call in calls:
        if not call["call_id"]:
            yield {"error": BRIDGE_SAFE_ERROR}
            return
        continuation_items.append(_bridge_tool_input(call))
        continuation_items.append(_bridge_tool_output_input(call))

    second_state: Dict[str, Any] = {
        "identity_confirmed": False,
        "identity_error": False,
        "model": "",
        "completed": False,
        "error": None,
        "content_chars": 0,
        "visible_chars": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "unexpected_tool": False,
    }
    try:
        second_stream = await _retry_create(
            lambda: client.responses.create(**_kwargs(continuation_items, with_tool=False))
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        yield {"error": BRIDGE_SAFE_ERROR}
        return

    async for chunk in _consume(second_stream, second_state, allow_tools=False):
        yield chunk
    if second_state.get("error") or second_state.get("identity_error") or second_state.get("unexpected_tool"):
        swap_error = second_state.get("error") == "non_claude_model"
        yield {"error": BRIDGE_MODEL_SWAP_ERROR if swap_error else BRIDGE_SAFE_ERROR}
        return
    if second_state["visible_chars"] <= 0:
        yield {"error": BRIDGE_SAFE_ERROR}
        return

    latency_ms = int((time.time() - start_time) * 1000)
    yield {
        "usage": {
            "model": safe_model_name(second_state["model"]),
            "input_tokens": first_state["input_tokens"] + second_state["input_tokens"],
            "output_tokens": first_state["output_tokens"] + second_state["output_tokens"],
            "cached_tokens": first_state["cached_tokens"] + second_state["cached_tokens"],
            "latency_ms": latency_ms,
        }
    }



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


def _inject_claude_search_unavailable_notice(
    messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """搜索桥接关闭时用：让 Claude 自己向用户说明为什么这轮没有联网。

    content 用 list-of-blocks（而非纯字符串）：_split_messages_for_responses 对
    Claude（_supports_store=False）会把 list-content 的 system 消息转成 input[0]
    的 role=system 条目——这条路径已经在生产验证过能把 Soul/persona 正确送到
    Claude；纯字符串 content 走的是 instructions 字段，在 Claude 这条 Responses
    路径上是否被 s2a 转译层转发未经验证，不能拿这条没验证过的路径来发安全提示。
    """
    notice = {
        "role": "system",
        "content": [
            {
                "type": "text",
                "text": (
                    "## 联网搜索当前不可用\n"
                    "这轮对话可能需要联网查询最新信息，但你现在没有可用的搜索工具——"
                    "中转站的已知行为是，一旦触发联网搜索就会把你静默换成非 Claude 模型再作答，"
                    "这会导致你在用户不知情的情况下冒充自己完成了搜索。\n"
                    "如果用户的问题依赖你没有的最新信息，请用自己的话简要说明现在无法联网查询"
                    "（原因是搜索会把你换成别的模型），然后基于已有知识谨慎作答，"
                    "并明确指出这部分内容可能不是最新的；不要假装自己刚刚联网查询过。"
                ),
            }
        ],
    }
    # 放在 messages 末尾（而非最前）：_split_messages_for_responses 按遇到顺序把
    # 所有 list-content system 消息的 block 依次塞进同一个 system_blocks，这条
    # per-turn 可变提示排在 Stage B 稳定/半稳定段前面会让那段本该稳定的 cache
    # 前缀每轮跟着变——系统消息本身会被整体提到 input 最前，不受这里位置影响。
    return [*messages, notice]


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
    search_requested: Optional[bool] = None,
    temperature: float = 0.7,
    top_p: Optional[float] = None,
    conversation_id: str = "",
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    流式调用 OPENAI_API_BASE 中转站。按 upstream 自动选择 API：
      - gemini-* → Chat Completions（sub2api Gemini 适配层不支持 Responses）
      - anthropic/* | openai/* | gpt-* | o1/o3/o4 → Responses API
        （绕开 sub2api Chat Completions 在多轮对话下的 "Invalid Responses API request" bug）

    search_requested: 路由/用户是否真的要求了搜索，区别于 enable_search——全自主模式下
    enable_search 会被 resolve_enable_search 强制为 True（fast/pro 无条件挂搜索工具，
    模型自决），不代表真实需求。None 时退化为 enable_search，兼容旧调用方。

    Yields:
        {"content": "...", "thinking": "...", "usage": {...}, "error": "..."}
    """
    if search_requested is None:
        search_requested = enable_search
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

        # Claude 使用应用层 function bridge；即使配置缺失，也不回退到旧摘要
        # google_search()，避免在用户未授权的共享 Google key 上旁路搜索。
        if is_claude and enable_search:
            bridge_enabled = bool(
                bridge_ready()
                and thinking_level.lower() in ANTIGRAVITY_CLAUDE_BRIDGE_THINKING_LEVELS
            )
            yield {
                "search": {
                    "requested": search_requested,
                    "native_enabled": False,
                    "bridge_enabled": bridge_enabled,
                    "fallback_injected": False,
                    "reason": "claude_tool_bridge" if bridge_enabled else "bridge_disabled",
                }
            }
            if bridge_enabled:
                try:
                    async for evt in _stream_via_claude_bridge(
                        client,
                        model_name,
                        config,
                        messages,
                        temperature,
                        top_p,
                        thinking_level,
                        start_time,
                    ):
                        yield evt
                except asyncio.CancelledError:
                    raise
                except Exception:
                    yield {"error": BRIDGE_SAFE_ERROR}
            else:
                # 桥接关闭时不静默无视用户真实提出的联网请求：注入 system 提示，让
                # Claude 自己说明"搜索工具会把我换成别的模型，所以现在用不了"。
                # 但全自主模式会对几乎每条 fast/pro 消息都把 enable_search 强制为
                # True（模型自决是否搜索）——只有 search_requested 才代表路由/用户
                # 真的判定这轮需要联网，不能对每条无关消息都提一嘴"我不能联网"。
                request_messages = (
                    _inject_claude_search_unavailable_notice(messages)
                    if search_requested
                    else messages
                )
                async for evt in _stream_via_responses(
                    client,
                    model_name,
                    config,
                    request_messages,
                    temperature,
                    top_p,
                    thinking_level,
                    start_time,
                    conversation_id=conversation_id,
                    enable_search=False,
                ):
                    yield evt
            return

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
    except asyncio.CancelledError:
        raise
    except Exception as e:
        if is_claude and enable_search:
            # 搜索桥接路径不把网关异常、响应体或 traceback 暴露给用户。
            yield {"error": BRIDGE_SAFE_ERROR}
            return
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