# -*- coding: utf-8 -*-
"""
统一 AI 处理层 - 抽象平台差异
"""
import asyncio
import time
from typing import Optional, Dict, List, Callable
from datetime import datetime, timezone, timedelta
from app.config import (
    MAX_HISTORY_LENGTH, DEFAULT_MODEL, AI_BACKEND, BOT_ID, OPENCLAW_CONTEXT_MESSAGES,
    OPENCLAW_TOOLS_URL, OPENCLAW_TOOLS_TOKEN, OPENCLAW_VISION_TOOL_NAME,
    OPENCLAW_GATEWAY_TRANSPORT,
    get_model_pricing
)
from app.memory import get_history, update_history
from app.gemini_client import analyze_complexity_with_model
from app.ai.router import analyze_complexity_unified

TEMPERATURE_MAP = {
    "precise": 0.1,   # 代码、数学、事实查询
    "balanced": 0.7,  # 默认
    "creative": 0.9,  # 写作、头脑风暴、诗歌
}


class AIHandler:
    """
    统一 AI 处理器 - 抽象平台差异

    支持:
    - 钉钉 (流式卡片更新)
    - 企业微信 (完整回复)
    """

    def __init__(self, platform: str = "dingtalk"):
        """
        初始化 AI 处理器

        Args:
            platform: 平台类型 (dingtalk | wecom)
        """
        self.platform = platform

    async def process_message(
        self,
        content: str,
        session_key: str,
        user_id: str,
        sender_nick: str = "User",
        image_data_list: Optional[List[bytes]] = None,
        group_info: Optional[Dict] = None,
        stream_callback: Optional[Callable] = None,
        complete_callback: Optional[Callable] = None
    ) -> str:
        """
        处理消息并调用 AI

        Args:
            content: 用户消息内容
            session_key: 会话键
            user_id: 用户 ID
            sender_nick: 发送者昵称
            image_data_list: 图片数据列表 (可选)
            group_info: 群信息 (可选)
            stream_callback: 流式更新回调 (thinking, content) -> None
            complete_callback: 完成回调 (response) -> None

        Returns:
            AI 完整回复
        """
        print(f"🚀 [AIHandler] 开始处理消息: {content} (User: {user_id}, Platform: {self.platform})")

        # 获取完整历史记录
        full_history = get_history(session_key)

        # OpenClaw 模式使用轻量上下文，避免覆盖 Gateway 侧 agent/system 策略
        if AI_BACKEND == "openclaw":
            if OPENCLAW_CONTEXT_MESSAGES > 0 and len(full_history) > OPENCLAW_CONTEXT_MESSAGES:
                history_messages = full_history[-OPENCLAW_CONTEXT_MESSAGES:]
            else:
                history_messages = full_history if OPENCLAW_CONTEXT_MESSAGES > 0 else []

            messages = []
            for msg in history_messages:
                role = msg.get("role")
                msg_content = msg.get("content", "")
                if role in {"user", "assistant"} and msg_content:
                    messages.append({"role": role, "content": msg_content})

            if image_data_list:
                text_content = f"{sender_nick}: [图片x{len(image_data_list)}] {content}".strip()

                if OPENCLAW_GATEWAY_TRANSPORT != "ws":
                    # HTTP(OpenAI-compatible) 路径默认按“无多模态”处理：先用 tools-invoke 产出文字描述，
                    # 再把纯文本送给 /v1/chat/completions，避免依赖 image_url 多模态能力。
                    from app.openclaw_tools_client import invoke_tool, build_vision_arguments

                    vision_sections = []
                    if OPENCLAW_TOOLS_URL and OPENCLAW_TOOLS_TOKEN and OPENCLAW_VISION_TOOL_NAME:
                        max_images = min(len(image_data_list), 3)
                        for idx, img in enumerate(image_data_list[:max_images], start=1):
                            try:
                                tool_res = await invoke_tool(
                                    tools_url=OPENCLAW_TOOLS_URL,
                                    token=OPENCLAW_TOOLS_TOKEN,
                                    tool_name=OPENCLAW_VISION_TOOL_NAME,
                                    arguments=build_vision_arguments(
                                        img,
                                        filename=f"image_{idx}.jpg",
                                        prompt=content or "",
                                    ),
                                    session_key=f"{self.platform}:{session_key}:{user_id}",
                                )
                                result_obj = tool_res.get("result") if isinstance(tool_res, dict) else None
                                vision_text = ""
                                if isinstance(result_obj, dict):
                                    vision_text = (result_obj.get("text") or result_obj.get("content") or "").strip()
                                elif isinstance(result_obj, str):
                                    vision_text = result_obj.strip()

                                if vision_text:
                                    vision_sections.append(f"[图片{idx}识别结果]\n{vision_text}")
                                else:
                                    vision_sections.append(f"[图片{idx}识别结果]\n(空结果)")
                            except Exception as e:
                                vision_sections.append(f"[图片{idx}识别失败]\n{e}")
                    else:
                        vision_sections.append(
                            "[系统]\n未配置 OPENCLAW_TOOLS_URL / OPENCLAW_TOOLS_TOKEN / OPENCLAW_VISION_TOOL_NAME，无法识别图片。"
                        )

                    vision_block = "\n\n".join(vision_sections).strip()
                    if vision_block:
                        text_content += f"\n\n{vision_block}"
                messages.append({"role": "user", "content": text_content})
            else:
                messages.append({"role": "user", "content": f"{sender_nick}: {content}"})
        else:
            # 截取最近的 N 条发送给 AI
            if len(full_history) > MAX_HISTORY_LENGTH:
                history_messages = full_history[-MAX_HISTORY_LENGTH:]
            else:
                history_messages = full_history

            # 构造 System Prompt
            system_prompt = self._build_system_prompt(group_info)

            messages = [{"role": "system", "content": system_prompt}]

            # 格式化历史消息
            formatted_history = self._format_history(history_messages)

            # 构造当前用户消息
            beijing_tz = timezone(timedelta(hours=8))
            current_timestamp = datetime.now(beijing_tz).strftime("%Y-%m-%d %H:%M:%S")

            if image_data_list:
                # 多模态消息
                import base64
                user_message_content = []
                user_message_content.append({
                    "type": "text",
                    "text": f"[{current_timestamp}] {sender_nick}: [图片x{len(image_data_list)}] {content}"
                })

                for i, img_data in enumerate(image_data_list):
                    b64_image = base64.b64encode(img_data).decode('utf-8')
                    print(f"🖼️ 处理第 {i+1} 张图片，大小: {len(img_data)} bytes")
                    user_message_content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"}
                    })

                messages.extend(formatted_history)
                messages.append({"role": "user", "content": user_message_content})
            else:
                # 纯文本消息
                text_content = f"[{current_timestamp}] {sender_nick}: {content}"
                messages.extend(formatted_history)
                messages.append({"role": "user", "content": text_content})

        # 智能路由
        has_images = bool(image_data_list)
        target_model, thinking_level, need_search, temperature = await self._route_model(content, has_images)

        print(f"🎯 [AIHandler] 路由结果: model={target_model}, thinking={thinking_level}, search={need_search}, temp={temperature}")

        # 调用 AI 流式接口
        full_response = ""
        full_thinking = ""
        usage_info = None

        try:
            from app.ai.backend import create_backend_stream
            stream = create_backend_stream(
                messages,
                target_model=target_model,
                thinking_level=thinking_level,
                enable_search=need_search,
                temperature=temperature,
                conversation_id=session_key,
                sender_id=user_id,
                sender_nick=sender_nick,
                image_data_list=image_data_list if image_data_list else None,
            )

            async for chunk in stream:
                # 处理使用统计
                if "usage" in chunk:
                    usage_info = chunk["usage"]
                    continue

                if "error" in chunk:
                    error_msg = chunk["error"]
                    print(f"❌ AI 请求失败: {error_msg}")
                    return f"❌ **API 请求失败**\n\n{error_msg}"

                # 处理 thinking
                thinking_delta = chunk.get("thinking", "")
                if thinking_delta:
                    full_thinking += thinking_delta
                    if stream_callback:
                        await stream_callback(thinking=full_thinking, content=full_response, is_thinking=True)
                    continue

                # 处理正式回复
                content_delta = chunk.get("content", "")
                if content_delta:
                    content_delta = content_delta.replace("[AILoading]", "")
                    full_response += content_delta
                    if stream_callback:
                        await stream_callback(thinking=full_thinking, content=full_response, is_thinking=False)

            print(f"✅ [AIHandler] 流式响应结束，总长度: {len(full_response)}, thinking: {len(full_thinking)}")

            # 清理回复
            full_response = full_response.replace("[AILoading]", "").strip()

            # 记录历史
            update_history(session_key, user_msg=None, assistant_msg=full_response)

            # 调用完成回调
            if complete_callback:
                await complete_callback(full_response, full_thinking, usage_info)

            return full_response

        except Exception as e:
            error_msg = f"系统异常: {str(e)}"
            print(f"💥 [AIHandler] {error_msg}")
            import traceback
            traceback.print_exc()
            return f"💥 **系统异常**\n\n{error_msg}"

    def _build_system_prompt(self, group_info: Optional[Dict] = None) -> str:
        """构建 System Prompt"""
        beijing_tz = timezone(timedelta(hours=8))
        current_time = datetime.now(beijing_tz).strftime("%Y-%m-%d %H:%M:%S")
        current_date = datetime.now(beijing_tz)
        year = current_date.year
        month = current_date.month
        day = current_date.day

        # 根据 AI_BACKEND 动态设置 bot 名称
        bot_name = {"gemini": "Gem", "openclaw": "Claw", "openai": "AI"}.get(AI_BACKEND, "Gem")

        system_prompt = f"""## 身份
你的名字是 {bot_name}。你的个性和风格由你的 Soul 定义（在下方注入）。

## 时间
今天是 {year} 年 {month} 月 {day} 日 {current_time} (北京时间 UTC+8)。
你的训练数据截止于 2025 年，但现在是 {year} 年了。

## 为什么有这些约定
这些不是规则，是背景信息——理解它们比遵守它们更重要：

用户把你当作可信赖的参考源，所以信息的准确性至关重要——如果不确定，就说出来。
对话历史以 '[时间] 昵称: 消息' 的格式展示，帮助你理解对话脉络和谁在说话。历史中 AI 回复前的 '[来自XXX]' 标签是系统注入的元数据，用于区分不同机器人——不是回复格式，你的输出不应包含此标签。
中文为主，技术术语附英文（如：机器学习 (Machine Learning)），因为大多数用户是中文母语。
Markdown 让信息更容易被快速扫读——善用它。
LaTeX 在聊天平台渲染不出来，用 Unicode 代替（x², √x）。
默认北京时间 (UTC+8) 和中国大陆场景，除非用户明确指定其他。

## 搜索
启用搜索时结果会自动提供。搜索结果与训练数据冲突时，优先搜索结果——尤其是时间敏感的信息。"""

        # 注入群信息
        if group_info:
            group_name = group_info.get('name', 'Unknown Group')
            system_prompt += f"\n\n当前群聊: '{group_name}'"

        return system_prompt

    def _format_history(self, history_messages: List[Dict]) -> List[Dict]:
        """格式化历史消息"""
        formatted_history = []
        for msg in history_messages:
            formatted_msg = {"role": msg["role"]}
            msg_content = msg.get("content", "")
            timestamp = msg.get("timestamp")

            # 如果有时间戳，添加到内容前面
            if timestamp and msg["role"] == "user":
                formatted_msg["content"] = f"[{timestamp}] {msg_content}"
            elif msg["role"] == "assistant" and msg.get("bot_id"):
                # 历史消息标注 AI 来源（仅用于上下文区分，不作为输出格式）
                msg_bot_id = msg["bot_id"]
                bot_source = {"gemini": "Gem", "openclaw": "Claw", "openai": "小G", "openrouter": "小克"}.get(msg_bot_id, msg_bot_id)
                tag = f"[来自{bot_source}]"
                if not msg_content.startswith(tag):
                    formatted_msg["content"] = f"{tag} {msg_content}"
                else:
                    formatted_msg["content"] = msg_content
            else:
                formatted_msg["content"] = msg_content

            formatted_history.append(formatted_msg)

        return formatted_history

    async def _route_model(self, content: str, has_images: bool) -> tuple:
        """
        智能路由：选择模型、thinking level 和是否联网

        Returns:
            (target_model, thinking_level, need_search)
        """
        if AI_BACKEND == "openclaw":
            # OpenClaw 模式: Gateway 自行决定模型和 thinking，客户端无法控制
            return ("openclaw", "default", False)
        elif AI_BACKEND == "openrouter":
            # OpenRouter 模式: 用 Haiku 替代 Gemini flash-lite 做路由判断
            from app.litellm_client import analyze_complexity_with_openrouter
            try:
                complexity = await analyze_complexity_with_openrouter(content, has_images)
            except Exception as e:
                print(f"❌ [OR路由] 异常: {e}")
                complexity = {
                    "model": "fast",
                    "thinking_level": "low",
                    "need_search": False,
                    "reason": "路由异常，使用默认"
                }
        else:
            # Gemini / LiteLLM 模式: 用 Gemini flash-lite 做路由判断
            try:
                complexity = await analyze_complexity_with_model(content, has_images)
                print(f"🔄 [路由] 预分析返回: {complexity}")
            except Exception as e:
                print(f"❌ [路由] 预分析异常: {e}")
                complexity = {
                    "model": "gemini-3-flash-preview",
                    "thinking_level": "low",
                    "need_search": False,
                    "reason": "路由异常，使用默认"
                }

        target_model = complexity.get("model", "fast")
        thinking_level = complexity.get("thinking_level", "low")
        need_search = complexity.get("need_search", False)
        temp_label = complexity.get("temperature", "balanced")
        temperature = TEMPERATURE_MAP.get(str(temp_label), 0.7)

        return (target_model, thinking_level, need_search, temperature)
