# -*- coding: utf-8 -*-
"""
Gemini 官方 SDK 客户端 (使用新版 google-genai)
支持提取真实的 thinking 内容
支持 Google Search Grounding (实时搜索)
"""
import os
import time
import asyncio
from typing import List, Dict, Any, Optional, AsyncGenerator
from google import genai
from google.genai import types
from app.config import GEMINI_API_KEY, DEFAULT_MODEL, ENABLE_THINKING, SOCKS_PROXY, ENABLE_SEARCH

# 配置代理 (仅 Gemini API 使用代理，通过 httpx_client 单独配置)
# 将 socks5h:// 转换为 socks5:// (httpx 格式)
proxy_url = SOCKS_PROXY.replace("socks5h://", "socks5://") if SOCKS_PROXY else None

# 创建带代理的 httpx Client (仅用于 Gemini SDK)
if proxy_url:
    import httpx
    print(f"🔗 Gemini SDK 使用代理: {proxy_url}")
    _httpx_client = httpx.Client(proxy=proxy_url, timeout=60.0)
    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(
            api_version='v1beta',
            httpx_client=_httpx_client
        )
    )
else:
    print("🔗 Gemini SDK 直连 (无代理)")
    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(api_version='v1beta')
    )


async def analyze_complexity_with_model(content: str, has_images: bool = False) -> dict:
    """
    使用 Gemini Flash Lite 快速分析问题复杂度
    返回推荐的模型、thinking level 和是否需要联网搜索

    Returns:
        {
            "model": "gemini-3-flash-preview" or "gemini-3-pro-preview",
            "thinking_level": "minimal" | "low" | "medium" | "high",
            "need_search": true | false,
            "reason": "分析原因"
        }
    """
    import asyncio
    import json
    import re
    import traceback

    print(f"🔍 [预分析] 函数被调用，内容: {content[:50]}...")
    print(f"🔍 [预分析] has_images={has_images}")

    # 构造分析提示
    analysis_prompt = f"""分析用户问题，返回 JSON 路由建议。

问题: {content[:300]}
有图片: {"是" if has_images else "否"}

选择规则:
1. model:
   - "gemini-3-flash-preview": 日常问答、代码、一般分析 (默认)
   - "gemini-3-pro-preview": 仅用于复杂数学证明、学术研究、系统架构设计

2. thinking_level:
   - "minimal": 简单问候如"你好"、"谢谢"
   - "low": 普通问答、事实查询
   - "medium": 需要一定推理、代码问题
   - "high": 复杂分析、算法设计

3. need_search:
   - true: 需要实时信息（天气、新闻、股价、最新事件）
   - false: 不需要联网（默认）

只返回JSON:
{{"model":"gemini-3-flash-preview","thinking_level":"low","need_search":false,"reason":"简短原因"}}"""

    try:
        print(f"🔍 [预分析] 准备调用 gemini-flash-lite-latest...")
        loop = asyncio.get_running_loop()

        def _analyze():
            print(f"🔍 [预分析] 进入线程执行器...")
            response = client.models.generate_content(
                model="gemini-flash-lite-latest",
                contents=[types.Content(role="user", parts=[types.Part.from_text(text=analysis_prompt)])],
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    max_output_tokens=150
                )
            )
            print(f"🔍 [预分析] API 调用完成")
            return response.text

        result_text = await loop.run_in_executor(None, _analyze)
        print(f"📝 [预分析] 原始返回: {result_text[:200]}")

        # 解析 JSON
        json_match = re.search(r'\{[^}]+\}', result_text)
        if json_match:
            result = json.loads(json_match.group())
            # 验证和修正字段
            if result.get("model") not in ["gemini-3-flash-preview", "gemini-3-pro-preview"]:
                result["model"] = "gemini-3-flash-preview"
            if result.get("thinking_level") not in ["minimal", "low", "medium", "high"]:
                result["thinking_level"] = "low"
            if "need_search" not in result:
                result["need_search"] = False
            print(f"🤖 预分析结果: {result}")
            return result
        else:
            print(f"⚠️ 无法从返回中提取 JSON: {result_text}")

    except Exception as e:
        print(f"⚠️ 模型预分析失败: {e}")
        traceback.print_exc()

    # 降级：返回保守的默认值
    print("⚠️ 使用降级默认配置")
    return {
        "model": "gemini-3-flash-preview",
        "thinking_level": "low",
        "need_search": False,
        "reason": "预分析失败，使用默认配置"
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
                        except Exception as e:
                            print(f"⚠️ 解析图片 data URL 失败: {e}")
        else:
            parts.append(types.Part.from_text(text=str(content)))

        contents.append(types.Content(role=gemini_role, parts=parts))

    return system_instruction, contents


async def call_gemini_stream(
    messages: List[Dict[str, Any]],
    target_model: str = DEFAULT_MODEL,
    thinking_level: str = "low",
    enable_search: bool = False  # 由智能路由决定是否启用搜索
) -> AsyncGenerator[Dict[str, str], None]:
    """
    调用 Gemini API 进行流式生成

    Args:
        messages: OpenAI 格式的消息列表
        target_model: 模型名称
        thinking_level: 思考深度
        enable_search: 是否启用 Google Search

    Yields:
        {"content": "...", "thinking": "..."} 或 {"error": "..."}
    """
    print(f"📡 正在请求 Google Gemini API ({target_model})...")

    # 记录开始时间
    start_time = time.time()
    input_tokens = 0
    output_tokens = 0

    try:
        # 转换消息格式
        system_instruction, contents = _convert_openai_to_gemini(messages)

        # 配置工具 (Google Search 由智能路由决定)
        tools = []
        if enable_search:
            tools.append(types.Tool(google_search=types.GoogleSearch()))
            print("🔍 已启用 Google Search (实时搜索)")

        # 配置生成参数
        config = types.GenerateContentConfig(
            temperature=0.7,
            max_output_tokens=8192,
            system_instruction=system_instruction,
            tools=tools if tools else None,
            safety_settings=[
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
            ]
        )

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
                config.thinking_config = types.ThinkingConfig(
                    thinking_level="minimal",
                    include_thoughts=False
                )
                print("⚡ Thinking 模式 (level=minimal, 最快响应)")
                print("⚡ Thinking 模式 (level=low, 快速响应)")

        # 同步流式生成 (在线程池中运行)
        def _stream_generate():
            return client.models.generate_content_stream(
                model=target_model,
                contents=contents,
                config=config
            )

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, _stream_generate)

        # 标记是否已发送 thinking 内容
        thinking_sent = False

        # 迭代流式响应
        for chunk in response:
            try:
                # 提取 usage_metadata (token 统计)
                if hasattr(chunk, 'usage_metadata') and chunk.usage_metadata:
                    usage = chunk.usage_metadata
                    if hasattr(usage, 'prompt_token_count'):
                        input_tokens = usage.prompt_token_count or 0
                    if hasattr(usage, 'candidates_token_count'):
                        output_tokens = usage.candidates_token_count or 0

                # 检查是否有候选内容
                if not chunk.candidates:
                    continue

                candidate = chunk.candidates[0]
                if not candidate.content or not candidate.content.parts:
                    continue

                for part in candidate.content.parts:
                    # part.thought 是布尔值，表示这个 part 是否是思考内容
                    # 思考内容和正式回复都在 part.text 里
                    is_thought = getattr(part, 'thought', False)
                    text_content = getattr(part, 'text', '')

                    if not text_content:
                        continue

                    if is_thought:
                        # 这是思考内容
                        if ENABLE_THINKING and not thinking_sent:
                            yield {"thinking_start": True}
                            thinking_sent = True
                        yield {"thinking": text_content}
                    else:
                        # 这是正式回复
                        if thinking_sent:
                            yield {"thinking_end": True}
                            thinking_sent = False
                        yield {"content": text_content}

            except ValueError as e:
                print(f"⚠️ Chunk 处理警告: {e}")
                continue
            except Exception as e:
                print(f"⚠️ 处理 chunk 异常: {e}")
                continue

        # 计算延迟
        latency_ms = int((time.time() - start_time) * 1000)
        print(f"✅ 流式响应结束 | 输入: {input_tokens} tokens, 输出: {output_tokens} tokens, 延迟: {latency_ms}ms")

        # 返回统计信息
        yield {
            "usage": {
                "model": target_model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "latency_ms": latency_ms
            }
        }

    except Exception as e:
        error_msg = str(e)
        print(f"❌ Gemini API 错误: {error_msg}")
        yield {"error": f"Gemini API Error: {error_msg}"}
