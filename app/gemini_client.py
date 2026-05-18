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


async def analyze_complexity_with_model(content: str, has_images: bool = False, analysis_model: str = "gemini-3.1-flash-lite", soul_text: str = "") -> dict:
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
    import asyncio
    import json
    import re
    import traceback

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
   - "gemini-3-flash-preview": 日常问答、代码、一般分析 (默认)
   - "gemini-3.1-pro-preview": 仅用于复杂数学证明、学术研究、系统架构设计

2. thinking_level:
   - "minimal": 简单问候如"你好"、"谢谢"
   - "low": 普通问答、事实查询
   - "medium": 需要一定推理、代码问题
   - "high": 复杂分析、算法设计

3. need_search:
   - true: 需要实时信息（天气、新闻、股价、最新事件、当前日期、现在是几年、今年是哪年）
   - false: 不需要联网（默认）

4. thinking_text:
   - 一句简短的思考状态（10字以内，不用emoji），要和问题内容相关，风格符合你的性格
   - {soul_instruction}例如: 代码问题→"正在编译思路中", 数学问题→"开始推演计算", 闲聊→"让我想想"
   - 要有个性、不重复

5. need_image_gen:
   - true: 用户明确要求生成图片、画画、插图、绘制、画一张、生成图片
   - false: 不需要生图（默认）

6. image_gen_params (仅当 need_image_gen=true 时):
   - prompt: 提取用户描述的图片内容，转为英文描述（生图模型只支持英文）
   - aspect_ratio: 解析用户指定的比例 → "1:1" | "3:4" | "4:3" | "9:16" | "16:9"，默认 "1:1"
   - number_of_images: 解析数量 → 1-4，默认 1

7. need_image_edit:
   - true: 有图片(has_images=是) 且用户文字中明确包含修改指令（"帮我改"、"修改"、"换颜色"、"去掉背景"、"再生成类似的"等）
   - false: 默认 — 以下情况均为 false：无图片；只发图片没有文字；文字是提问/分析（"这是什么"、"帮我看看"、"分析一下"）；没有明确修改词

8. temperature:
   - "precise": 代码、数学、翻译、事实查询（需要准确性）
   - "balanced": 普通问答（默认）
   - "creative": 写作、诗歌、头脑风暴、创意任务

重要: 如果问题涉及"今年"、"现在"、"当前时间"等，设置 need_search=true

只返回JSON:
{{"model":"gemini-3-flash-preview","thinking_level":"low","need_search":false,"temperature":"balanced","need_image_gen":false,"need_image_edit":false,"reason":"简短原因","thinking_text":"正在思考"}}"""

    try:
        print(f"🔍 [预分析] 准备调用 {analysis_model}...")
        loop = asyncio.get_running_loop()

        def _analyze():
            print(f"🔍 [预分析] 进入线程执行器...")
            response = client.models.generate_content(
                model=analysis_model,
                contents=[types.Content(role="user", parts=[types.Part.from_text(text=analysis_prompt)])],
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    max_output_tokens=300
                )
            )
            print(f"🔍 [预分析] API 调用完成")
            return response.text

        result_text = await loop.run_in_executor(None, _analyze)
        print(f"📝 [预分析] 原始返回: {result_text[:200]}")

        # 解析 JSON（支持嵌套对象）
        json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group())
            # 验证和修正字段
            if result.get("model") not in ["gemini-3-flash-preview", "gemini-3.1-pro-preview"]:
                result["model"] = "gemini-3-flash-preview"
            if result.get("thinking_level") not in ["minimal", "low", "medium", "high"]:
                result["thinking_level"] = "low"
            if "need_search" not in result:
                result["need_search"] = False
            if "need_image_gen" not in result:
                result["need_image_gen"] = False
            if "need_image_edit" not in result:
                result["need_image_edit"] = False
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
    enable_search: bool = False,
    temperature: float = 0.7,
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
            temperature=temperature,
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

                # 检查 finish_reason - 如果因安全原因被阻止，报告给用户
                if hasattr(candidate, 'finish_reason') and candidate.finish_reason:
                    finish_reason = str(candidate.finish_reason)
                    if 'SAFETY' in finish_reason:
                        yield {"error": "⚠️ 内容被安全过滤器阻止。可能是图片包含敏感内容，或提示词触发了安全限制。请尝试其他图片或调整提问方式。"}
                        return
                    elif finish_reason not in ['STOP', 'MAX_TOKENS', '']:
                        print(f"⚠️ 异常的 finish_reason: {finish_reason}")

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

        # 检查是否返回了内容
        if output_tokens == 0:
            yield {"error": "⚠️ Gemini API 没有返回任何内容。可能原因：\n1. 图片内容触发了安全过滤器\n2. 图片格式不支持或损坏\n3. API 遇到内部错误\n\n请尝试：\n- 更换其他图片\n- 添加文字描述一起发送\n- 稍后重试"}
            return

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


async def google_search(query: str) -> Optional[str]:
    """
    用 Gemini Flash + Google Search grounding 做实时搜索。
    返回搜索摘要文本，供其他后端（如 OpenAI/LiteLLM）注入 prompt。

    Returns:
        搜索结果文本，搜索失败时返回 None
    """
    loop = asyncio.get_running_loop()

    def _search():
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite",
            contents=f"请搜索以下问题并给出简洁的事实性回答，包含关键信息来源：\n\n{query}",
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                max_output_tokens=2048,
            ),
        )
        return response

    try:
        response = await loop.run_in_executor(None, _search)
        if response.text:
            print(f"🔍 [Google Search] 搜索完成: {query[:50]}...")
            return response.text
        return None
    except Exception as e:
        print(f"⚠️ [Google Search] 搜索失败: {e}")
        return None
