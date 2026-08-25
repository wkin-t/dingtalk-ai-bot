# -*- coding: utf-8 -*-
"""
Gemini 官方 SDK 客户端 (使用新版 google-genai)
支持提取真实的 thinking 内容
支持 Google Search Grounding (实时搜索)
"""
import os
import time
import asyncio
import re
from typing import List, Dict, Any, Optional, AsyncGenerator
from google import genai
from google.genai import types
from app import gemini_sse_patch
gemini_sse_patch.apply()
from app.ai.sampling_clamp import clamp_top_p
from app.error_safety import safe_error_summary, safe_display_text, safe_model_name
from app.config import (
    GEMINI_API_KEY,
    GEMINI_API_BASE,
    GEMINI_API_BASE_KEY,
    GEMINI_API_BASE_FALLBACK,
    GEMINI_API_BASE_FALLBACK_KEY,
    DEFAULT_MODEL,
    GEMINI_MODEL_LITE,
    GEMINI_MODEL_FAST,
    MODEL_ROUTER_FALLBACK,
    MODEL_LITE_FALLBACK,
    MODEL_FAST_FALLBACK,
    MODEL_PRO_FALLBACK,
    GEMINI_SEARCH_MODEL,
    SEARCH_TIMEOUT_SECONDS,
    ENABLE_THINKING,
    SOCKS_PROXY,
    ENABLE_SEARCH,
)
from app import gemini_circuit

# 配置代理 (仅 Gemini API 使用代理，通过 httpx_client 单独配置)
# 将 socks5h:// 转换为 socks5:// (httpx 格式)
proxy_url = SOCKS_PROXY.replace("socks5h://", "socks5://") if SOCKS_PROXY else None


class _ProviderPreOutputError(Exception):
    """provider 在尚未产生用户可见输出时失败。"""

    def __init__(self, error: BaseException):
        super().__init__()
        self.error = error


class _ProviderStreamError(Exception):
    """provider 在流式处理阶段失败，禁止重放。"""

    def __init__(self, error: BaseException):
        super().__init__()
        self.error = error


def _build_direct_client() -> genai.Client:
    """直连 Google 官方 API 的 client（按需带代理）"""
    if proxy_url:
        import httpx
        print(f"🔗 Gemini SDK 使用代理: {proxy_url}")
        return genai.Client(
            api_key=GEMINI_API_KEY,
            http_options=types.HttpOptions(
                api_version='v1beta',
                httpx_client=httpx.Client(proxy=proxy_url, timeout=60.0),
            )
        )
    print("🔗 Gemini SDK 直连 (无代理)")
    return genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(api_version='v1beta')
    )


if GEMINI_API_BASE:
    # 对话/搜索走中转站（如 sub2api 的 /v1beta 原生协议层）。
    # 中转站在本机/内网，不注入代理；google_search 工具由中转透传（groundingMetadata 已实测可回流）。
    print(f"🔗 Gemini SDK 对话走中转: {GEMINI_API_BASE}")
    client = genai.Client(
        api_key=GEMINI_API_BASE_KEY,
        http_options=types.HttpOptions(
            api_version='v1beta',
            base_url=GEMINI_API_BASE,
        )
    )
    # 生图 (generate_images/:predict) 等端点中转站不覆盖，保持直连（image_gen 使用）
    direct_client = _build_direct_client()
else:
    client = _build_direct_client()
    direct_client = client


# Vertex fallback 只用于文本/搜索调用；生图仍明确使用上面的 direct_client。
fallback_client = None
if GEMINI_API_BASE_FALLBACK:
    if not GEMINI_API_BASE_FALLBACK_KEY:
        print("⚠️ Gemini fallback base 已配置但缺少显式 fallback key，保底路径未启用")
    else:
        try:
            print("🔗 Gemini SDK fallback 已启用（Vertex）")
            fallback_client = genai.Client(
                api_key=GEMINI_API_BASE_FALLBACK_KEY,
                http_options=types.HttpOptions(
                    api_version="v1beta",
                    base_url=GEMINI_API_BASE_FALLBACK,
                )
            )
        except Exception:
            print("⚠️ Gemini fallback client 构建失败，保底路径未启用")


def _select_fallback_model(primary_model: str, route_slot: Optional[str] = None) -> str:
    """按调用方显式 route slot 选择 fallback override，默认沿用主模型名。"""
    overrides = {
        "router": MODEL_ROUTER_FALLBACK,
        "lite": MODEL_LITE_FALLBACK,
        "fast": MODEL_FAST_FALLBACK,
        "pro": MODEL_PRO_FALLBACK,
    }
    return overrides.get(route_slot, "") or primary_model


async def _is_circuit_open_safe() -> bool:
    """熔断状态读取失败时按未熔断处理，不能反向阻断主请求。"""
    try:
        return await gemini_circuit.is_circuit_open_async()
    except asyncio.CancelledError:
        raise
    except Exception:
        print("⚠️ [Gemini 熔断] 状态读取异常，按 fail-open 处理")
        return False


async def _open_circuit_safe() -> None:
    """记录 provider 故障；Redis 失败不能阻断后续 fallback。"""
    try:
        await gemini_circuit.open_circuit_async()
    except asyncio.CancelledError:
        raise
    except Exception:
        print("⚠️ [Gemini 熔断] 状态写入异常，按 fail-open 处理")


_stale_circuit_checked = False


async def warn_stale_circuit_without_fallback() -> None:
    """fallback 未配置时最多探测一次 stale marker，并保持主路径可用。"""
    global _stale_circuit_checked
    if fallback_client is not None or _stale_circuit_checked:
        return
    _stale_circuit_checked = True
    try:
        if await gemini_circuit.is_circuit_open_async():
            print("⚠️ [Gemini 熔断] 检测到 marker 但未配置 fallback，忽略并继续主路径")
    except asyncio.CancelledError:
        raise
    except Exception:
        print("⚠️ [Gemini 熔断] stale marker 检查异常，按 fail-open 处理")


def call_gemini_sync(active_client: genai.Client, model: str, prompt: str) -> str:
    """执行一次同步 Gemini provider 调用，不包含熔断或 fallback 副作用。"""
    response = active_client.models.generate_content(
        model=model,
        contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
        config=types.GenerateContentConfig(temperature=0.7, max_output_tokens=500),
    )
    return response.text or ""


async def analyze_complexity_with_model(content: str, has_images: bool = False, analysis_model: str = None, soul_text: str = "") -> dict:
    """
    使用 Gemini 模型快速分析问题复杂度
    返回推荐的模型、thinking level 和是否需要联网搜索

    Args:
        content: 用户消息内容
        has_images: 是否包含图片
        analysis_model: 用于分析的模型 (默认 gemini-3.1-flash-lite)

    Returns:
        {
            "model": "gemini-3-flash-preview" or "gemini-3.1-pro-preview",
            "thinking_level": "minimal" | "low" | "medium" | "high",
            "need_search": true | false,
            "reason": "分析原因"
        }
    """
    import json

    if analysis_model is None:
        analysis_model = GEMINI_MODEL_LITE

    print(f"🔍 [预分析] 函数被调用，内容: {content[:50]}...")
    print(f"🔍 [预分析] has_images={has_images}")

    # 构造分析提示
    soul_instruction = ""
    if soul_text:
        soul_instruction = f"你的性格设定: {soul_text[:100]}\n   请让思考短语符合这个性格。\n   "

    analysis_prompt = f"""分析用户问题，返回 JSON 路由建议。

问题: {content[:300]}
有图片: {"是" if has_images else "否"}

选择规则:
1. model:
   - "{GEMINI_MODEL_FAST}": 日常问答、代码、一般分析 (默认)
   - "{DEFAULT_MODEL}": 仅用于复杂数学证明、学术研究、系统架构设计

2. thinking_level:
   - "minimal": 简单问候如"你好"、"谢谢"
   - "low": 普通问答、事实查询
   - "medium": 需要一定推理、代码问题
   - "high": 复杂分析、算法设计

3. need_search:
   - true: 需要实时信息（天气、新闻、股价、最新事件、当前日期、现在是几年、今年是哪年）
   - false: 不需要联网（默认）

4. route_slot:
   - "lite": 简单问候、短确认，通常配合 minimal thinking
   - "fast": 日常问答、代码、一般分析
   - "pro": 复杂数学证明、学术研究、系统架构设计

5. thinking_text:
   - 一句简短的思考状态（10字以内，不用emoji），要和问题内容相关，风格符合你的性格
   - {soul_instruction}例如: 代码问题→"正在编译思路中", 数学问题→"开始推演计算", 闲聊→"让我想想"
   - 要有个性、不重复

6. need_image_gen:
   - true: 用户明确要求生成图片、画画、插图、绘制、画一张、生成图片
   - false: 不需要生图（默认）

7. image_gen_params (仅当 need_image_gen=true 时):
   - prompt: 提取用户描述的图片内容，转为英文描述（生图模型只支持英文）
   - aspect_ratio: 解析用户指定的比例 → "1:1" | "3:4" | "4:3" | "9:16" | "16:9"，默认 "1:1"
   - number_of_images: 解析数量 → 1-4，默认 1

8. need_image_edit:
   - true: 有图片(has_images=是) 且用户文字中明确包含修改指令（"帮我改"、"修改"、"换颜色"、"去掉背景"、"再生成类似的"等）
   - false: 默认 — 以下情况均为 false：无图片；只发图片没有文字；文字是提问/分析（"这是什么"、"帮我看看"、"分析一下"）；没有明确修改词

9. temperature:
   - "precise": 代码、数学、翻译、事实查询（需要准确性）
   - "balanced": 普通问答（默认）
   - "creative": 写作、诗歌、头脑风暴、创意任务
   - "wild": highly creative, exploratory tone (temp ~1.3)
   - "chaotic": maximally creative, experimental (temp ~1.8)

重要: 如果问题涉及"今年"、"现在"、"当前时间"等，设置 need_search=true

只返回JSON:
{{"model":"{GEMINI_MODEL_FAST}","route_slot":"fast","thinking_level":"low","need_search":false,"temperature":"balanced","need_image_gen":false,"need_image_edit":false,"reason":"简短原因","thinking_text":"正在思考"}}"""

    async def _collect_analysis(active_client: genai.Client, model: str) -> str:
        """收集完整预分析流；任意 provider iterator 异常都回给 fallback 边界。"""
        raw_parts = []
        try:
            response = await active_client.aio.models.generate_content_stream(
                model=model,
                contents=[types.Content(role="user", parts=[types.Part.from_text(text=analysis_prompt)])],
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    max_output_tokens=300,
                ),
            )
            iterator = response.__aiter__()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise _ProviderPreOutputError(error) from None
        while True:
            try:
                chunk = await iterator.__anext__()
            except StopAsyncIteration:
                break
            except asyncio.CancelledError:
                raise
            except Exception as error:
                raise _ProviderPreOutputError(error) from None

            try:
                if chunk.candidates and chunk.candidates[0].content and chunk.candidates[0].content.parts:
                    for part in chunk.candidates[0].content.parts:
                        if not getattr(part, "thought", False) and part.text:
                            raw_parts.append(part.text)
            except Exception as error:
                print(f"⚠️ [预分析] chunk 处理失败: {safe_error_summary(error, 'analysis')}")

        return "".join(raw_parts)

    try:
        print(f"🔍 [预分析] 准备调用 {analysis_model}...")
        fallback_model = _select_fallback_model(analysis_model, "router")
        using_fallback = False
        primary_error = None

        if fallback_client is None:
            await warn_stale_circuit_without_fallback()
        if fallback_client is not None and await _is_circuit_open_safe():
            active_client = fallback_client
            active_model = fallback_model
            using_fallback = True
        else:
            active_client = client
            active_model = analysis_model

        try:
            result_text = await _collect_analysis(active_client, active_model)
        except _ProviderPreOutputError as failure:
            if using_fallback or fallback_client is None:
                safe_failure = safe_error_summary(failure.error, "analysis")
                if using_fallback:
                    print(f"⚠️ [预分析] Vertex fallback 失败: {safe_failure}")
                else:
                    print(f"⚠️ [预分析] 主模型失败且未配置 fallback: {safe_failure}")
                raise

            primary_error = safe_error_summary(failure.error, "analysis")
            print(f"⚠️ [预分析] 主模型失败，切换 Vertex: {primary_error}")
            await _open_circuit_safe()
            using_fallback = True
            active_client = fallback_client
            active_model = fallback_model
            try:
                result_text = await _collect_analysis(active_client, active_model)
            except _ProviderPreOutputError as fallback_failure:
                fallback_error = safe_error_summary(fallback_failure.error, "fallback")
                print(f"⚠️ [预分析] Vertex fallback 失败: {fallback_error}; 主模型: {primary_error}")
                raise

        print(f"📝 [预分析] 返回摘要: {result_text[:200]}")

        # 解析 JSON（支持嵌套对象）
        json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group())
            # 验证和修正字段
            if result.get("model") not in [GEMINI_MODEL_FAST, DEFAULT_MODEL]:
                result["model"] = GEMINI_MODEL_FAST
            if result.get("thinking_level") not in ["minimal", "low", "medium", "high"]:
                result["thinking_level"] = "low"
            route_slot = result.get("route_slot")
            if route_slot not in {"lite", "fast", "pro"}:
                if result.get("thinking_level") == "minimal":
                    route_slot = "lite"
                else:
                    route_slot = "fast"
            result["route_slot"] = route_slot
            if "need_search" not in result:
                result["need_search"] = False
            if "need_image_gen" not in result:
                result["need_image_gen"] = False
            if "need_image_edit" not in result:
                result["need_image_edit"] = False
            print(f"🤖 预分析结果: {result}")
            return result
        else:
            print("⚠️ 无法从返回中提取 JSON，使用降级默认配置")

    except asyncio.CancelledError:
        raise
    except _ProviderPreOutputError:
        # 上面的 provider 分支已输出安全摘要；这里仅进入原有保守路由。
        pass
    except Exception as error:
        print(f"⚠️ 模型预分析失败: {safe_error_summary(error, 'analysis')}")

    # 降级：返回保守的默认值
    print("⚠️ 使用降级默认配置")
    return {
        "model": GEMINI_MODEL_FAST,
        "route_slot": "fast",
        "thinking_level": "low",
        "need_search": False,
        "reason": "预分析失败，使用默认配置",
        "thinking_text": "正在思考 💭"
    }


def _convert_openai_to_gemini(messages: List[Dict[str, Any]]) -> tuple[Optional[str], List[types.Content]]:
    """
    将 OpenAI 格式的消息转换为 Gemini 格式

    OpenAI 格式:
    [
        {"role": "system", "content": "..."},
        {"role": "user", "content": "..."},
        {"role": "assistant", "content": "..."}
    ]

    Gemini 格式:
    system_instruction: "..."
    contents: [Content(role="user", parts=[...]), Content(role="model", parts=[...])]
    """
    system_instruction = None
    contents = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # 提取 system prompt
        if role == "system":
            if isinstance(content, list):
                # 兼容 list of blocks 形态：提取所有 text part 拼接
                texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                system_instruction = "\n\n".join(t for t in texts if t)
            else:
                system_instruction = content
            continue

        # 转换角色名称: assistant -> model
        gemini_role = "model" if role == "assistant" else "user"

        # 处理内容 (可能是字符串或多模态列表)
        parts = []
        if isinstance(content, str):
            parts.append(types.Part.from_text(text=content))
        elif isinstance(content, list):
            # 多模态内容 (文本 + 图片)
            for item in content:
                if item.get("type") == "text":
                    parts.append(types.Part.from_text(text=item.get("text", "")))
                elif item.get("type") == "image_url":
                    # 从 data URL 提取 base64
                    image_url = item.get("image_url", {}).get("url", "")
                    if image_url.startswith("data:"):
                        try:
                            header, b64_data = image_url.split(",", 1)
                            mime_type = header.split(":")[1].split(";")[0]
                            import base64
                            image_bytes = base64.b64decode(b64_data)
                            parts.append(types.Part.from_bytes(
                                data=image_bytes,
                                mime_type=mime_type
                            ))
                        except asyncio.CancelledError:
                            raise
                        except Exception as error:
                            print(f"⚠️ 解析图片 data URL 失败: {safe_error_summary(error, 'provider')}")
        else:
            parts.append(types.Part.from_text(text=str(content)))

        contents.append(types.Content(role=gemini_role, parts=parts))

    return system_instruction, contents


async def call_gemini_stream(
    messages: List[Dict[str, Any]],
    target_model: str = DEFAULT_MODEL,
    thinking_level: str = "low",
    enable_search: bool = False,
    temperature: float = 0.7,
    top_p: Optional[float] = None,
    route_slot: Optional[str] = None,
) -> AsyncGenerator[Dict[str, str], None]:
    """
    调用 Gemini API 进行流式生成

    Args:
        messages: OpenAI 格式的消息列表
        target_model: 模型名称
        thinking_level: 思考深度
        enable_search: 是否启用 Google Search
        top_p: 核采样参数，None 表示使用 Gemini 默认值
        route_slot: 调用方明确的模型档位，用于选择对应 fallback override

    Yields:
        {"content": "...", "thinking": "..."} 或 {"error": "..."}
    """
    print(f"📡 正在请求 Google Gemini API ({target_model})...")

    # 记录开始时间
    start_time = time.time()
    input_tokens = 0
    output_tokens = 0
    cached_tokens = 0

    try:
        # 转换消息格式
        system_instruction, contents = _convert_openai_to_gemini(messages)

        # 配置工具 (Google Search 由智能路由决定)
        tools = []
        if enable_search:
            tools.append(types.Tool(google_search=types.GoogleSearch()))
            print("🔍 已启用 Google Search (实时搜索)")
        if enable_search:
            yield {
                "search": {
                    "requested": True,
                    "native_enabled": bool(tools),
                    "fallback_injected": False,
                    "reason": "native" if tools else "not_requested",
                }
            }

        # 配置生成参数
        config_kwargs = {
            "temperature": temperature,
            "max_output_tokens": 8192,
            "system_instruction": system_instruction,
            "tools": tools if tools else None,
            "safety_settings": [
                types.SafetySetting(
                    category="HARM_CATEGORY_HARASSMENT",
                    threshold="BLOCK_NONE"
                ),
                types.SafetySetting(
                    category="HARM_CATEGORY_HATE_SPEECH",
                    threshold="BLOCK_NONE"
                ),
                types.SafetySetting(
                    category="HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    threshold="BLOCK_NONE"
                ),
                types.SafetySetting(
                    category="HARM_CATEGORY_DANGEROUS_CONTENT",
                    threshold="BLOCK_NONE"
                ),
            ],
        }
        # 应用 per-provider clamp（Gemini 限 [0, 2.0]）
        from app.ai.sampling_clamp import clamp_temperature
        clamped_temp = clamp_temperature(temperature, "gemini")
        if clamped_temp != temperature:
            print(f"⚠️ [Gemini] temperature {temperature} → clamp 到 {clamped_temp}")
        config_kwargs["temperature"] = clamped_temp

        if top_p is not None:
            clamped_top_p = clamp_top_p(top_p, "gemini")
            if clamped_top_p != top_p:
                print(f"⚠️ [Gemini] top_p {top_p} → clamp 到 {clamped_top_p}")
            config_kwargs["top_p"] = clamped_top_p
        config = types.GenerateContentConfig(**config_kwargs)
        print(f"🌡️ [Gemini] 实际下发 temperature={clamped_temp}, top_p={config_kwargs.get('top_p', 'default(unset)')}")

        # Gemini 3 系列支持 thinking，配置 thinking level
        # thinking_level: minimal (最快) | low | medium | high (最深度)
        if 'gemini-3' in target_model.lower() or 'thinking' in target_model.lower():
            if ENABLE_THINKING and thinking_level != "minimal":
                config.thinking_config = types.ThinkingConfig(
                    thinking_level=thinking_level,
                    include_thoughts=True
                )
                print(f"🧠 已启用 Thinking 模式 (level={thinking_level})")
            else:
                # gemini-3.7 系列不支持 minimal/none（400 THINKING_LEVEL_MINIMAL），
                # low 是被支持档位里最轻量的一档，用它替代
                config.thinking_config = types.ThinkingConfig(
                    thinking_level="low",
                    include_thoughts=False
                )
                print("⚡ Thinking 模式 (level=low, 快速响应)")

        async def _iterate_attempt(
            active_client: genai.Client,
            active_model: str,
            fallback_used: bool,
            circuit_open: bool,
            fallback_error: Optional[str],
            state: Dict[str, Any],
        ):
            """拉取一次 provider 流，首个可见输出前的异常交给外层 fallback。"""
            nonlocal input_tokens, output_tokens, cached_tokens
            thinking_sent = False
            search_executed_sent = False
            actual_model = active_model

            try:
                response = await active_client.aio.models.generate_content_stream(
                    model=active_model,
                    contents=contents,
                    config=config,
                )
                iterator = response.__aiter__()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                raise _ProviderPreOutputError(error) from None

            while True:
                try:
                    chunk = await iterator.__anext__()
                except StopAsyncIteration:
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    if state["output_started"]:
                        raise _ProviderStreamError(error) from None
                    raise _ProviderPreOutputError(error) from None

                try:
                    model_version = getattr(chunk, "model_version", None)
                    if isinstance(model_version, str) and model_version.strip():
                        actual_model = safe_model_name(model_version.strip())

                    if hasattr(chunk, "usage_metadata") and chunk.usage_metadata:
                        usage = chunk.usage_metadata
                        if hasattr(usage, "prompt_token_count"):
                            input_tokens = usage.prompt_token_count or 0
                        if hasattr(usage, "candidates_token_count"):
                            output_tokens = usage.candidates_token_count or 0
                        if hasattr(usage, "cached_content_token_count"):
                            cached = usage.cached_content_token_count or 0
                            cached_tokens = cached if isinstance(cached, int) else 0

                    if not chunk.candidates:
                        continue

                    candidate = chunk.candidates[0]
                    if not search_executed_sent and getattr(candidate, "grounding_metadata", None):
                        search_executed_sent = True
                        print("🌐 [搜索执行] Gemini grounding_metadata 回流，本次真实联网")
                        yield {"search": {"executed": True}}

                    if getattr(candidate, "finish_reason", None):
                        # candidate.finish_reason 是 str 子类枚举，但 str() 会走自定义
                        # __str__ 返回 "FinishReason.STOP" 而非纯值 "STOP"，用 .name 取真正的
                        # 枚举名，否则下面的 in 判断永远不匹配，每次正常结束都会误报"异常"。
                        finish_reason = getattr(candidate.finish_reason, "name", None) or str(candidate.finish_reason)
                        if "SAFETY" in finish_reason:
                            yield {"error": "⚠️ 内容被安全过滤器阻止。可能是图片包含敏感内容，或提示词触发了安全限制。请尝试其他图片或调整提问方式。"}
                            return
                        if finish_reason not in ["STOP", "MAX_TOKENS", ""]:
                            print(f"⚠️ 异常的 finish_reason: {finish_reason}")

                    if not candidate.content or not candidate.content.parts:
                        continue

                    for part in candidate.content.parts:
                        is_thought = getattr(part, "thought", False)
                        text_content = getattr(part, "text", "")
                        if not text_content:
                            continue

                        if is_thought:
                            if ENABLE_THINKING and not thinking_sent:
                                yield {"thinking_start": True}
                                thinking_sent = True
                            if ENABLE_THINKING:
                                state["output_started"] = True
                                yield {"thinking": text_content}
                        else:
                            if thinking_sent:
                                yield {"thinking_end": True}
                                thinking_sent = False
                            state["output_started"] = True
                            yield {"content": text_content}

                except asyncio.CancelledError:
                    raise
                except ValueError as error:
                    print(f"⚠️ Chunk 处理警告: {safe_error_summary(error, 'stream')}")
                except Exception as error:
                    print(f"⚠️ 处理 chunk 异常: {safe_error_summary(error, 'stream')}")

            latency_ms = int((time.time() - start_time) * 1000)
            print(f"✅ 流式响应结束 | 请求模型: {safe_model_name(target_model)}, 实际模型: {safe_model_name(actual_model)}, 输入: {input_tokens} tokens, 输出: {output_tokens} tokens, 延迟: {latency_ms}ms")
            cache_pct = round(cached_tokens / input_tokens * 100) if input_tokens else 0
            print(f"💾 [Cache] Gemini | cached={cached_tokens}/{input_tokens} ({cache_pct}%)")

            if output_tokens == 0 and not state["output_started"]:
                yield {"error": "⚠️ Gemini API 没有返回任何内容。可能原因：\n1. 图片内容触发了安全过滤器\n2. 图片格式不支持或损坏\n3. API 遇到内部错误\n\n请尝试：\n- 更换其他图片\n- 添加文字描述一起发送\n- 稍后重试"}
                return

            usage_payload = {
                "model": safe_model_name(actual_model),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_tokens": cached_tokens,
                "latency_ms": latency_ms,
            }
            if fallback_used:
                usage_payload.update({
                    "requested_model": safe_model_name(target_model),
                    "fallback": True,
                    "fallback_error": fallback_error or "circuit open",
                    "circuit_open": circuit_open,
                })
            yield {"usage": usage_payload}

        primary_model = target_model
        fallback_model = _select_fallback_model(primary_model, route_slot)
        fallback_used = False
        circuit_open = False
        fallback_error = None

        if fallback_client is None:
            await warn_stale_circuit_without_fallback()
        if fallback_client is not None and await _is_circuit_open_safe():
            active_client = fallback_client
            active_model = fallback_model
            fallback_used = True
            circuit_open = True
            fallback_error = "circuit open"
        else:
            active_client = client
            active_model = primary_model

        state = {"output_started": False}
        try:
            async for chunk in _iterate_attempt(
                active_client,
                active_model,
                fallback_used,
                circuit_open,
                fallback_error,
                state,
            ):
                yield chunk
        except _ProviderPreOutputError as failure:
            if fallback_used:
                fallback_safe = safe_error_summary(failure.error, "fallback")
                print(f"❌ [Gemini] Vertex fallback 失败: {fallback_safe}")
                yield {
                    "error": (
                        f"❌ fallback 模型 {safe_model_name(active_model)}:\n{fallback_safe}\n\n"
                        f"主模型 {safe_model_name(primary_model)}:\n{safe_display_text(fallback_error or 'circuit open', 1000)}"
                    )
                }
                return

            if fallback_client is None:
                safe_primary = safe_error_summary(failure.error, "provider")
                print(f"❌ [Gemini] 主模型失败且未配置 fallback: {safe_primary}")
                yield {"error": f"Gemini API Error: {safe_primary}"}
                return

            fallback_error = safe_error_summary(failure.error, "provider")
            print(f"⚠️ [Gemini] 主模型 {safe_model_name(primary_model)} 失败，切换 Vertex: {fallback_error}")
            await _open_circuit_safe()
            fallback_used = True
            state = {"output_started": False}
            input_tokens = output_tokens = cached_tokens = 0
            try:
                async for chunk in _iterate_attempt(
                    fallback_client,
                    fallback_model,
                    True,
                    False,
                    fallback_error,
                    state,
                ):
                    yield chunk
            except (_ProviderPreOutputError, _ProviderStreamError) as fallback_failure:
                fallback_safe = safe_error_summary(fallback_failure.error, "fallback")
                print(f"❌ [Gemini] fallback 失败: {fallback_safe}; 主模型: {fallback_error}")
                yield {
                    "error": (
                        f"❌ fallback 模型 {safe_model_name(fallback_model)}:\n{fallback_safe}\n\n"
                        f"主模型 {safe_model_name(primary_model)}:\n{safe_display_text(fallback_error, 1000)}"
                    )
                }
                return
        except _ProviderStreamError as failure:
            safe_stream_error = safe_error_summary(failure.error, "stream")
            print(f"❌ [Gemini] 流式响应中断: {safe_stream_error}")
            yield {"error": f"Gemini API Error: {safe_stream_error}"}
            return

    except asyncio.CancelledError:
        raise
    except Exception as error:
        safe_error = safe_error_summary(error, "provider")
        print(f"❌ Gemini API 错误: {safe_error}")
        yield {"error": f"Gemini API Error: {safe_error}"}


async def google_search(query: str) -> Optional[str]:
    """
    用 Gemini Flash + Google Search grounding 做实时搜索。
    返回搜索摘要文本，供其他后端注入 prompt（原生搜索不可用时的 fallback）。

    要点：
    - 用 GEMINI_SEARCH_MODEL（真实 Gemini 型号），不借用路由模型名——
      openai/openrouter 后端的路由模型是 gpt-*/claude-* 名，发给 Gemini 搜索接口会 404。
    - 用 generate_content_stream 而非非流式 generate_content：后者在 Python 3.14 +
      同步 httpx 代理下会 Network unreachable（本仓库已发生过的事故）。
    - 外层加超时，代理半死/网络黑洞时避免挂死整个对话流（卡片永远停在 Thinking）。

    Returns:
        搜索结果文本，搜索失败/超时时返回 None
    """
    async def _run() -> Optional[str]:
        parts: List[str] = []
        stream = await client.aio.models.generate_content_stream(
            model=GEMINI_SEARCH_MODEL,
            contents=f"请搜索以下问题并给出简洁的事实性回答，包含关键信息来源：\n\n{query}",
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                max_output_tokens=2048,
            ),
        )
        async for chunk in stream:
            if chunk.text:
                parts.append(chunk.text)
        return "".join(parts) if parts else None

    try:
        text = await asyncio.wait_for(_run(), timeout=SEARCH_TIMEOUT_SECONDS)
        if text:
            print(f"🔍 [Google Search] 搜索完成: {query[:50]}...")
            return text
        return None
    except asyncio.TimeoutError:
        print(f"⚠️ [Google Search] 搜索超时（>{SEARCH_TIMEOUT_SECONDS}s）: {query[:50]}")
        return None
    except asyncio.CancelledError:
        raise
    except Exception as error:
        print(f"⚠️ [Google Search] 搜索失败: {safe_error_summary(error, 'provider')}")
        return None
