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
    get_model_pricing
)
from app.memory import get_history, update_history
from app.gemini_client import call_gemini_stream, analyze_complexity_with_model
from app.ai.router import analyze_complexity_unified


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
                import base64
                user_message_content = [{
                    "type": "text",
                    "text": f"{sender_nick}: [图片x{len(image_data_list)}] {content}"
                }]
                for i, img_data in enumerate(image_data_list):
                    b64_image = base64.b64encode(img_data).decode('utf-8')
                    print(f"🖼️ 处理第 {i+1} 张图片，大小: {len(img_data)} bytes")
                    user_message_content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"}
                    })
                messages.append({"role": "user", "content": user_message_content})
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
        target_model, thinking_level, need_search = await self._route_model(content, has_images)

        print(f"🎯 [AIHandler] 路由结果: model={target_model}, thinking={thinking_level}, search={need_search}")

        # 调用 AI 流式接口
        full_response = ""
        full_thinking = ""
        usage_info = None

        try:
            # 根据后端选择调用不同的 API
            if AI_BACKEND == "openclaw":
                from app.openclaw_client import call_openclaw_stream
                stream = call_openclaw_stream(
                    messages,
                    conversation_id=session_key,
                    sender_id=user_id,
                    sender_nick=sender_nick,
                    model=target_model
                )
            else:
                stream = call_gemini_stream(
                    messages,
                    target_model=target_model,
                    thinking_level=thinking_level,
                    enable_search=need_search
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
        bot_name = {"gemini": "Gem", "openclaw": "Claw"}.get(AI_BACKEND, "Gem")

        system_prompt = f"""你是 {bot_name}，一个有帮助的 AI 助手。你的回答应该准确，不要产生幻觉。

⏰ 重要时间信息（请务必记住）:
- 今天是: {year} 年 {month} 月 {day} 日
- 当前完整时间: {current_time} (北京时间, UTC+8)
- 你的训练数据可能截止于 2025 年，但现在已经是 {year} 年了
- 当回答涉及"今年"、"现在"、"当前"等时间相关问题时，请使用上述日期而非训练数据中的时间

格式规则:
1. 不要使用 LaTeX 语法（如 $x^2$ 或 $$...$$）。用纯文本或 Unicode 表示数学公式（如 x^2, sqrt(x)）。
2. 可以使用 Markdown：表格、加粗、斜体、列表、代码块。

上下文感知:
- 对话历史中包含用户昵称和时间戳，格式为 '[时间] 昵称: 消息'。
- 引用用户发言时，可以提及其昵称和时间（如 '正如张三在 14:30 所说...'）。
- 所有时间均为北京时间 (UTC+8)。
- AI 回复可能带有来源标签 [Gem] 或 [Claw]，表示由不同 AI 助手生成。
- 你是 {bot_name}，回复不需要添加来源标签。

重点:
- 直接回应最新用户的输入。
- 仅将之前的上下文作为参考。

输出要求:
- 直接输出答案。不要输出状态指示器或 '[AILoading]'。
- 使用中文回答。技术术语可在中文后加英文括号（如：机器学习 (Machine Learning)）。

搜索和实时信息:
- 如果启用了 Google Search，搜索结果会自动提供给你
- 当搜索结果与你的训练数据冲突时，优先相信搜索结果
- 特别是涉及时间、日期、最新事件时，搜索结果比训练数据更准确
- 如果用户质疑你对时间的认知，请再次确认：今天是 {year} 年 {month} 月 {day} 日

地理和时区规则:
- 默认按北京时间 (Asia/Shanghai, UTC+8) 回答时间相关问题。
- 用户未明确给出城市时，默认按中国大陆场景理解，并优先追问具体城市。
- 不要仅依据 IP/代理/VPN 推断用户在海外；若定位冲突，以用户明确地点为准。

思考语言:
- 请使用中文进行思考和推理。你的内部思考过程也应该用中文表达。"""

        # 注入群信息
        if group_info:
            group_name = group_info.get('name', 'Unknown Group')
            group_context = f"\n\nGROUP CONTEXT:\nYou are currently in a group chat named '{group_name}'.\n\nTASK:\nBased on the group name, briefly analyze what technical capabilities or domain knowledge you might need to assist this group effectively. Keep this analysis internal to guide your responses."
            system_prompt += group_context

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
                # assistant 消息有 bot_id 时，加来源标签
                msg_bot_id = msg["bot_id"]
                bot_label = {"gemini": "Gem", "openclaw": "Claw"}.get(msg_bot_id, msg_bot_id)
                formatted_msg["content"] = f"[{bot_label}] {msg_content}"
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
        else:
            # Gemini 模式: 智能路由分析
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

            target_model = complexity.get("model", "gemini-3-flash-preview")
            thinking_level = complexity.get("thinking_level", "low")
            need_search = complexity.get("need_search", False)

            return (target_model, thinking_level, need_search)
