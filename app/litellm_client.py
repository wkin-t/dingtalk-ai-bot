"""LiteLLM 统一流式客户端"""
import os
import time
import traceback
from typing import Dict, Any, List, AsyncGenerator, Optional

import litellm

from app.ai.sampling_clamp import clamp_top_p
from app.config import (
    get_route_key, get_litellm_model_config,
    LITELLM_PROXY, LITELLM_READ_TIMEOUT,
    LITELLM_MAX_RETRIES, OPENAI_API_BASE, OPENAI_API_KEY_CUSTOM,
    VERTEX_PROJECT,
    OPENROUTER_API_KEY, OPENROUTER_BASE_URL, OPENROUTER_MODEL_CONFIG, OPENROUTER_ROUTER_MODEL,
)

# LiteLLM 通过环境变量识别代理
if LITELLM_PROXY:
    os.environ.setdefault("HTTPS_PROXY", LITELLM_PROXY)
    os.environ.setdefault("HTTP_PROXY", LITELLM_PROXY)

EFFORT_MAPPING = {
    "minimal": "none",      # GPT-5: 关闭推理 | Gemini: 映射为 minimal
    "low": "none",          # 普通问答不开启 extended thinking，medium 及以上才开
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


def _inject_cache_control(messages: List[Dict[str, Any]], model: str) -> List[Dict[str, Any]]:
    """为 Anthropic 模型的 system 消息注入 cache_control breakpoint。

    兼容两种 system content 形态：
      - str: 整体作为单个 ephemeral block（向下兼容）
      - list of blocks: 原样透传（调用方已自行设好 cache_control）
    """
    if not (model.startswith("anthropic/") or "claude" in model.lower()):
        return messages
    result = []
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                msg = {**msg, "content": [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]}
            # list 形态原样透传（保留调用方的 per-block cache_control 设置）
        result.append(msg)
    return result


async def call_litellm_stream(
    messages: List[Dict[str, Any]],
    target_model: str,
    thinking_level: str = "low",
    enable_search: bool = False,
    temperature: float = 0.7,
    top_p: Optional[float] = None,
    conversation_id: str = "",
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    通过 LiteLLM 调用任意 OpenAI 兼容模型（流式）

    Args:
        messages: OpenAI 格式消息列表
        target_model: 智能路由输出的模型名（lite/fast/pro 或 Gemini 模型名，经 ROUTE_KEY_MAP 归一化）
        thinking_level: minimal/low/medium/high
        enable_search: 是否启用联网搜索
        top_p: 核采样参数，None 表示使用 provider 默认值

    Yields:
        {"content": "...", "thinking": "...", "usage": {...}, "error": "..."}
    """
    import warnings
    litellm.suppress_debug_info = True
    warnings.filterwarnings("ignore", message="Pydantic serializer warnings")

    route_key = get_route_key(target_model)
    if OPENROUTER_API_KEY:
        config = OPENROUTER_MODEL_CONFIG.get(route_key, OPENROUTER_MODEL_CONFIG["fast"])
        provider = "openrouter"
    else:
        config = get_litellm_model_config(route_key)
        provider = "openai"
    model = config["model"]
    # 应用 per-provider clamp（OpenRouter→Claude 路由特殊：上限 1.0）
    from app.ai.sampling_clamp import clamp_temperature
    is_claude = "claude" in model.lower() or model.startswith("anthropic/")
    clamp_provider = "openclaw" if is_claude else provider  # 复用 openclaw 的 [0, 1.0] 限制给 Claude
    clamped_temp = clamp_temperature(temperature, clamp_provider)
    if clamped_temp != temperature:
        print(f"⚠️ [LiteLLM/{provider}] temperature {temperature} → clamp 到 {clamped_temp}（model={model}）")
    temperature = clamped_temp

    if top_p is not None:
        clamped_top_p = clamp_top_p(top_p, provider)
        if clamped_top_p != top_p:
            print(f"⚠️ [LiteLLM/{provider}] top_p {top_p} → clamp 到 {clamped_top_p}")
        top_p = clamped_top_p

    print(f"📡 [LiteLLM] 请求模型: {model} (路由: {route_key}, thinking: {thinking_level})")
    print(f"🌡️ [LiteLLM] 实际下发 temperature={temperature}, top_p={top_p if top_p is not None else 'default(unset)'}")

    start_time = time.time()
    input_tokens = 0
    output_tokens = 0

    # OpenRouter 原生搜索工具更可靠，跳过 Gemini 搜索注入
    if enable_search and not OPENROUTER_API_KEY:
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
            "temperature": temperature,
        }
        if top_p is not None:
            kwargs["top_p"] = top_p

        if OPENROUTER_API_KEY:
            # OpenRouter 路径：模型回退 + 供应商路由 + 原生 Web Search
            extra_body = {}
            fallbacks = config.get("fallbacks", [])
            if fallbacks:
                extra_body["models"] = [model] + fallbacks
                extra_body["route"] = "fallback"
            provider_dict: dict = {}
            provider_order = config.get("provider_order", [])
            if provider_order:
                provider_dict["order"] = provider_order
            provider_sort = config.get("provider_sort", "")
            if provider_sort:
                provider_dict["sort"] = {"by": provider_sort}
            if provider_dict:
                extra_body["provider"] = provider_dict
            if enable_search and config.get("supports_search"):
                extra_body["tools"] = [{"type": "openrouter:web_search"}]
            if conversation_id:
                # session_id 用于 OpenRouter 可观测性（trace 分组），不影响 prompt cache
                # 哈希处理：避免用户标识明文传到境外服务，同时满足 256 字符上限
                import hashlib
                extra_body["session_id"] = hashlib.sha256(conversation_id.encode()).hexdigest()[:32]
            kwargs["api_base"] = OPENROUTER_BASE_URL
            kwargs["api_key"] = OPENROUTER_API_KEY
            kwargs["custom_llm_provider"] = "openai"
            effort = EFFORT_MAPPING.get(thinking_level)
            if config.get("supports_reasoning") and effort and effort != "none":
                extra_body["reasoning"] = {"effort": effort}
            if extra_body:
                kwargs["extra_body"] = extra_body
        elif config["model"].startswith("vertex_ai/"):
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
            # GPT-5/o1/o3/o4 系列只接受 temperature=1；去掉 provider/ 前缀再检测，兼容 supports_reasoning=false 的旧配置
            _model_name = model.split("/")[-1]
            _no_temp_model = config["supports_reasoning"] or any(
                _model_name.startswith(p) for p in ("gpt-5", "o1", "o3", "o4")
            )
            if _no_temp_model:
                kwargs.pop("temperature", None)
        else:
            # 无 api_base 的默认路径
            effort = EFFORT_MAPPING.get(thinking_level)
            if config["supports_reasoning"] and effort is not None:
                kwargs["reasoning_effort"] = effort
            _model_name = model.split("/")[-1]
            _no_temp_model = config["supports_reasoning"] or any(
                _model_name.startswith(p) for p in ("gpt-5", "o1", "o3", "o4")
            )
            if _no_temp_model:
                kwargs.pop("temperature", None)

        if not config["supports_vision"]:
            kwargs["messages"] = _strip_images(messages)

        if OPENROUTER_API_KEY:
            kwargs["messages"] = _inject_cache_control(kwargs["messages"], model)

        response = await litellm.acompletion(**kwargs)

        thinking_sent = False
        actual_model = model  # 跟踪实际响应模型（fallback 时与请求模型不同）

        async for chunk in response:
            # 从流式 chunk 读取真实模型名（fallback 后 OpenRouter 会更新此字段）
            if hasattr(chunk, "model") and chunk.model:
                actual_model = chunk.model
            # usage 先采集，final chunk 可能 choices 为空
            if hasattr(chunk, "usage") and chunk.usage:
                input_tokens = getattr(chunk.usage, "prompt_tokens", 0) or 0
                output_tokens = getattr(chunk.usage, "completion_tokens", 0) or 0

            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta

            model_extra = getattr(delta, "model_extra", None) or {}
            reasoning = (
                getattr(delta, "reasoning_content", None)
                or getattr(delta, "thinking", None)
                or model_extra.get("reasoning_content")
                or model_extra.get("thinking")
            )
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

        latency_ms = int((time.time() - start_time) * 1000)
        print(f"✅ [LiteLLM] 响应结束 | 输入: {input_tokens}, 输出: {output_tokens}, 延迟: {latency_ms}ms")

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
        print(f"❌ [LiteLLM] 调用失败: {error_msg}")
        traceback.print_exc()
        yield {"error": f"LiteLLM API Error: {error_msg}"}


async def analyze_complexity_with_openrouter(
    content: str,
    has_images: bool = False,
    soul_text: str = "",
) -> dict:
    """
    用 OpenRouter Haiku 分析消息复杂度，替代 Gemini flash-lite 的路由职责。
    返回与 analyze_complexity_with_model 相同的 dict 格式。
    """
    import litellm
    import json
    import re

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
        response = await litellm.acompletion(
            model=OPENROUTER_ROUTER_MODEL,
            messages=[{"role": "user", "content": prompt}],
            api_base=OPENROUTER_BASE_URL,
            api_key=OPENROUTER_API_KEY,
            custom_llm_provider="openai",
            stream=False,
            timeout=8,
            max_tokens=200,
            num_retries=1,
        )
        raw = response.choices[0].message.content or ""
        json_match = re.search(r'\{.*?\}', raw, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group())
            # 有图片时强制升级到 fast 层，防止 prompt 判断失误
            if has_images and result.get("model") == "lite":
                result["model"] = "fast"
                result["reason"] = result.get("reason", "") + " [图片升级→fast]"
            print(f"🔄 [OR路由] Haiku 分析: {result.get('model')} / {result.get('thinking_level')} / reason={result.get('reason')}")
            return result
    except Exception as e:
        print(f"⚠️ [OR路由] Haiku 分析失败，降级关键词匹配: {e}")

    # 降级：关键词匹配
    from app.ai.router import analyze_complexity_unified
    return analyze_complexity_unified(content, has_images)
