"""OpenRouter 官方 SDK 流式客户端（替代 litellm OpenRouter 路径）"""
import hashlib
import json
import re
import time
import traceback
from typing import Any, AsyncGenerator, Dict, List, Optional

from openrouter import OpenRouter
from openrouter.components import (
    ProviderPreferences,
    ProviderSortConfig,
    Reasoning,
    WebSearchPlugin,
)

from app.ai.sampling_clamp import clamp_temperature, clamp_top_p
from app.config import (
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    OPENROUTER_MODEL_CONFIG,
    OPENROUTER_ROUTER_MODEL,
    get_route_key,
)

EFFORT_MAPPING = {
    "minimal": "none",
    "low": "none",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
}


def _build_client() -> OpenRouter:
    """构建 OpenRouter 客户端。中转站路径直连，无需本地代理。"""
    return OpenRouter(
        api_key=OPENROUTER_API_KEY,
        server_url=OPENROUTER_BASE_URL,
    )


def _is_complete_reasoning(rd: list) -> bool:
    """确保 reasoning_details 完整（含 signature/data），防残体写入死锁会话。"""
    if not isinstance(rd, list) or not rd:
        return False
    return all(
        item.get("signature") or item.get("data")
        for item in rd
        if item.get("type") in ("thinking", "reasoning")
    )


def _serialize_rd(rd_objects: list) -> List[Dict[str, Any]]:
    """将 SDK Pydantic reasoning_details 对象序列化为可存储的 dict 列表。"""
    result = []
    for item in rd_objects:
        if hasattr(item, "model_dump"):
            result.append(item.model_dump(by_alias=True, exclude_none=True))
        else:
            result.append(dict(item))
    return result


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


async def call_openrouter_stream(
    messages: List[Dict[str, Any]],
    target_model: str,
    thinking_level: str = "low",
    enable_search: bool = False,
    temperature: float = 0.7,
    top_p: Optional[float] = None,
    conversation_id: str = "",
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    通过 openrouter 官方 SDK 调用 OpenRouter（流式）。
    正确捕获 delta.reasoning_details（含 signature）用于多轮 thinking。

    Yields:
        {"content": ..., "thinking": ..., "thinking_start": ..., "thinking_end": ...,
         "reasoning_details": [...], "usage": {...}, "error": ...}
    """
    route_key = get_route_key(target_model)
    config = OPENROUTER_MODEL_CONFIG.get(route_key, OPENROUTER_MODEL_CONFIG["fast"])
    model = config["model"]

    # Temperature clamp（Claude 模型上限 1.0）
    is_claude = "claude" in model.lower() or model.startswith("anthropic/")
    clamp_provider = "openclaw" if is_claude else "openrouter"
    clamped_temp = clamp_temperature(temperature, clamp_provider)
    if clamped_temp != temperature:
        print(f"⚠️ [OpenRouter] temperature {temperature} → clamp 到 {clamped_temp}（model={model}）")
    temperature = clamped_temp

    if top_p is not None:
        clamped_top_p = clamp_top_p(top_p, "openrouter")
        if clamped_top_p != top_p:
            print(f"⚠️ [OpenRouter] top_p {top_p} → clamp 到 {clamped_top_p}")
        top_p = clamped_top_p

    print(f"📡 [OpenRouter] 请求模型: {model} (路由: {route_key}, thinking: {thinking_level})")
    print(f"🌡️ [OpenRouter] 实际下发 temperature={temperature}, top_p={top_p if top_p is not None else 'default(unset)'}")

    start_time = time.time()
    input_tokens = 0
    output_tokens = 0

    try:
        client = _build_client()

        # ── 构建 provider 偏好 ──
        provider_order = config.get("provider_order", [])
        provider_sort = config.get("provider_sort", "")
        provider_kwargs: Dict[str, Any] = {"allow_fallbacks": True}
        if provider_order:
            provider_kwargs["order"] = provider_order
        if provider_sort:
            provider_kwargs["sort"] = ProviderSortConfig(by=provider_sort)
        provider_pref = ProviderPreferences(**provider_kwargs)

        # ── 模型列表（含 fallback）──
        fallbacks = config.get("fallbacks", [])
        models_list = [model] + fallbacks if fallbacks else None

        # ── Reasoning effort ──
        reasoning_param = None
        effort = EFFORT_MAPPING.get(thinking_level)
        if config.get("supports_reasoning") and effort and effort != "none":
            reasoning_param = Reasoning(effort=effort)

        # ── Web search via plugins ──
        plugins_list = None
        if enable_search and config.get("supports_search"):
            plugins_list = [WebSearchPlugin(id="web")]

        # ── Session ID（OpenRouter 可观测性，哈希保护用户标识）──
        session_id_val = None
        if conversation_id:
            session_id_val = hashlib.sha256(conversation_id.encode()).hexdigest()[:32]

        # ── 消息预处理（不含视觉内容的模型移除图片）──
        final_messages = messages if config.get("supports_vision", True) else _strip_images(messages)

        call_kwargs: Dict[str, Any] = {
            "messages": final_messages,
            "provider": provider_pref,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": temperature,
        }
        if models_list:
            call_kwargs["models"] = models_list
        else:
            call_kwargs["model"] = model
        if top_p is not None:
            call_kwargs["top_p"] = top_p
        if reasoning_param is not None:
            call_kwargs["reasoning"] = reasoning_param
        if plugins_list is not None:
            call_kwargs["plugins"] = plugins_list
        if session_id_val is not None:
            call_kwargs["session_id"] = session_id_val

        thinking_sent = False
        actual_model = model
        last_rd_dicts: Optional[List[Dict[str, Any]]] = None

        async with await client.chat.send_async(**call_kwargs) as stream:
            async for chunk in stream:
                if hasattr(chunk, "model") and chunk.model:
                    actual_model = chunk.model

                if chunk.usage:
                    input_tokens = chunk.usage.prompt_tokens or 0
                    output_tokens = chunk.usage.completion_tokens or 0

                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta

                # 追踪 reasoning_details（含 signature）；完整数组在最后 chunk 出现
                if delta.reasoning_details:
                    last_rd_dicts = _serialize_rd(delta.reasoning_details)

                # thinking 文本（增量流）
                if delta.reasoning:
                    if not thinking_sent:
                        yield {"thinking_start": True}
                        thinking_sent = True
                    yield {"thinking": delta.reasoning}

                # 正文内容
                if delta.content:
                    if thinking_sent:
                        yield {"thinking_end": True}
                        thinking_sent = False
                    yield {"content": delta.content}

        # 循环结束后，一次性 yield 完整 reasoning_details（校验 signature 防残体）
        if last_rd_dicts and _is_complete_reasoning(last_rd_dicts):
            print(f"🧠 [OpenRouter] reasoning_details yielded ({len(last_rd_dicts)} blocks)")
            yield {"reasoning_details": last_rd_dicts}

        latency_ms = int((time.time() - start_time) * 1000)
        print(f"✅ [OpenRouter] 响应结束 | 输入: {input_tokens}, 输出: {output_tokens}, 延迟: {latency_ms}ms")

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
        print(f"❌ [OpenRouter] 调用失败: {error_msg}")
        traceback.print_exc()
        yield {"error": f"OpenRouter API Error: {error_msg}"}


async def call_openrouter_simple(prompt: str, max_tokens: Optional[int] = None) -> str:
    """用于 Soul 进化等后台轻量文本生成任务。

    max_tokens 默认 None（不下发），原 500 在 thinking 模型上会被思考吃光。
    """
    try:
        client = _build_client()
        raw_parts: List[str] = []
        send_kwargs: Dict[str, Any] = {
            "messages": [{"role": "user", "content": prompt}],
            "model": OPENROUTER_ROUTER_MODEL,
            "stream": True,
        }
        if max_tokens is not None:
            send_kwargs["max_tokens"] = max_tokens
        async with await client.chat.send_async(**send_kwargs) as stream:
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                    raw_parts.append(chunk.choices[0].delta.content)
        return "".join(raw_parts)
    except Exception as e:
        print(f"⚠️ [OpenRouter简单调用] 失败: {e}")
        return ""


async def analyze_complexity_with_openrouter(
    content: str,
    has_images: bool = False,
    soul_text: str = "",
) -> dict:
    """
    用 OpenRouter 轻量模型分析消息复杂度，输出路由建议。
    返回与 analyze_complexity_unified 相同的 dict 格式。
    """
    soul_instruction = f"你的性格设定: {soul_text[:100]}\n   " if soul_text else ""

    prompt = f"""分析用户问题，返回 JSON 路由建议。

问题: {content[:300]}
有图片: {"是" if has_images else "否"}

选择规则:
1. model（三个选项）:
   - "lite": 简单问候、闲聊、一句话基础问答（有图片时禁用此选项）
   - "fast": 日常问答、代码、一般分析、图片分析（默认；有图片时最低选此）
   - "pro": 仅用于复杂数学证明、学术研究、系统架构设计、复杂图片分析

2. thinking_level:
   - "minimal": 简单问候如"你好"、"谢谢"、"再见"
   - "low": 普通问答、事实查询
   - "medium": 需要推理、代码问题
   - "high": 复杂分析、算法设计

3. need_search:
   - true: 需要实时信息（天气、新闻、股价、最新事件、当前日期）
   - false: 不需要（默认）

4. thinking_text: 一句简短思考状态（10字以内，不用emoji），和问题内容相关
   {soul_instruction}例如: 代码→"正在编译思路中", 数学→"开始推演计算", 闲聊→"让我想想"

5. temperature:
   - "precise": 代码、数学、翻译、事实查询（需要准确性）
   - "balanced": 普通问答（默认）
   - "creative": 写作、诗歌、头脑风暴、创意任务
   - "wild": highly creative, exploratory tone (temp ~1.3)
   - "chaotic": maximally creative, experimental (temp ~1.8)

6. need_image_gen:
   - true: 用户明确要求生成图片、画画、绘制
   - false: 默认

7. need_image_edit:
   - true: 有图片(has_images=是) 且用户文字中明确包含修改指令（"帮我改"、"修改"、"换颜色"、"去掉背景"、"再生成类似的"等）
   - false: 默认 — 以下情况均为 false：无图片；只发图片没有文字；文字是提问/分析（"这是什么"、"帮我看看"）；没有明确修改词

只返回JSON:
{{"model":"fast","thinking_level":"low","need_search":false,"temperature":"balanced","need_image_gen":false,"need_image_edit":false,"reason":"简短原因","thinking_text":"正在思考"}}"""

    try:
        client = _build_client()
        # SDK 的 send_async 始终返回 EventStreamAsync，stream=False 无效；改为流式收集
        raw_parts: List[str] = []
        # 不设 max_tokens：原 200 在 thinking 模型上会被思考全部消耗，留给 JSON 输出 0 token。
        # 让上游用默认值（通常 4096+），路由 JSON 仅 ~100 token 占用极少。
        async with await client.chat.send_async(
            messages=[{"role": "user", "content": prompt}],
            model=OPENROUTER_ROUTER_MODEL,
            stream=True,
        ) as stream:
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                    raw_parts.append(chunk.choices[0].delta.content)
        raw = "".join(raw_parts)
        json_match = re.search(r'\{.*?\}', raw, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group())
            if has_images and result.get("model") == "lite":
                result["model"] = "fast"
                result["reason"] = result.get("reason", "") + " [图片升级→fast]"
            print(f"🔄 [OR路由] 分析: {result.get('model')} / {result.get('thinking_level')} / reason={result.get('reason')}")
            return result
    except Exception as e:
        print(f"⚠️ [OR路由] 分析失败，降级关键词匹配: {e}")

    from app.ai.router import analyze_complexity_unified
    return analyze_complexity_unified(content, has_images)
