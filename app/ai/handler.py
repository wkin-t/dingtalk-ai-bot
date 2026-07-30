# -*- coding: utf-8 -*-
"""
统一 AI 处理层 - 抽象平台差异
"""
import asyncio
import time
from typing import Optional, Dict, List, Callable
from datetime import datetime, timezone, timedelta
from app.config import (
    MAX_HISTORY_LENGTH, DEFAULT_MODEL, GEMINI_MODEL_FAST, AI_BACKEND, BOT_ID, OPENCLAW_CONTEXT_MESSAGES,
    OPENCLAW_TOOLS_URL, OPENCLAW_TOOLS_TOKEN, OPENCLAW_VISION_TOOL_NAME,
    OPENCLAW_GATEWAY_TRANSPORT,
    get_model_pricing
)
from app.memory import get_history, update_history
from app.ai.router import analyze_complexity_unified
from app.ai.sampling_clamp import clamp_temperature
from app.error_safety import safe_error_summary

TEMPERATURE_MAP = {
    "precise": 0.1,   # 代码、数学、事实查询
    "balanced": 0.7,  # 默认
    "creative": 0.9,  # 写作、头脑风暴、诗歌
    "wild": 1.3,      # 高创意、探索性表达
    "chaotic": 1.8,   # 最大创意、实验性表达
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

            from app.clear_cutoff import get_cutoff
            _cutoff_at = get_cutoff(session_key)
            messages_raw = []
            for msg in self._format_history_with_meta(history_messages, BOT_ID, cutoff_at=_cutoff_at):
                role = msg.get("role")
                msg_content = msg.get("content", "")
                if role in {"user", "assistant"} and msg_content:
                    messages_raw.append(msg)

            if image_data_list:
                text_content = f"{sender_nick}: [图片x{len(image_data_list)}] {content}".strip()

                if OPENCLAW_GATEWAY_TRANSPORT != "ws":
                    # HTTP(OpenAI-compatible) 路径默认按"无多模态"处理：先用 tools-invoke 产出文字描述，
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
                messages_raw.append({"role": "user", "content": text_content})
            else:
                messages_raw.append({"role": "user", "content": f"{sender_nick}: {content}"})
        else:
            # 截取最近的 N 条发送给 AI
            if len(full_history) > MAX_HISTORY_LENGTH:
                history_messages = full_history[-MAX_HISTORY_LENGTH:]
            else:
                history_messages = full_history

            # 构造 System Prompt
            from app.ai.system_prompt import build_system_prompt_content
            system_prompt = build_system_prompt_content(group_info=group_info, soul_content=None)

            messages_raw = [{"role": "system", "content": system_prompt}]

            # 格式化历史消息，保留 bot_id 给转换层判断消息来源
            from app.clear_cutoff import get_cutoff
            _cutoff_at = get_cutoff(session_key)
            formatted_history = self._format_history_with_meta(history_messages, BOT_ID, cutoff_at=_cutoff_at)

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

                messages_raw.extend(formatted_history)
                messages_raw.append({"role": "user", "content": user_message_content})
            else:
                # 纯文本消息
                text_content = f"[{current_timestamp}] {sender_nick}: {content}"
                messages_raw.extend(formatted_history)
                messages_raw.append({"role": "user", "content": text_content})

        from app.ai.messages_pipeline import prepare_messages_for_backend

        messages = prepare_messages_for_backend(messages_raw, BOT_ID)

        # 智能路由
        has_images = bool(image_data_list)
        target_model, thinking_level, need_search, temperature, route_slot = await self._route_model(
            content, has_images
        )

        print(f"🎯 [AIHandler] 路由结果: model={target_model}, thinking={thinking_level}, search={need_search}, temp={temperature}")

        # 调用 AI 流式接口
        full_response = ""
        full_thinking = ""
        full_reasoning_details = None  # 收集 Anthropic thinking blocks（含 signature，用于多轮 thinking）
        usage_info = None
        search_info = None

        # 解析手动采样覆盖（D.3）
        from app.ai.sampling_pipeline import resolve_sampling
        final_temp, final_top_p, _override_rec = resolve_sampling(session_key, temperature)

        try:
            from app.ai.backend import create_backend_stream
            stream = create_backend_stream(
                messages,
                target_model=target_model,
                thinking_level=thinking_level,
                enable_search=need_search,
                temperature=final_temp,
                top_p=final_top_p,
                route_slot=route_slot,
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

                if "search" in chunk:
                    # 合并而非覆盖：executed 标志在流式中途补发，需累积到同一 dict
                    if search_info is None:
                        search_info = dict(chunk["search"])
                    else:
                        search_info.update(chunk["search"])
                    print(f"🔍 [AIHandler] 搜索状态: {search_info}")
                    continue

                if "error" in chunk:
                    error_msg = chunk["error"]
                    print(f"❌ AI 请求失败: {error_msg}")
                    return f"❌ **API 请求失败**\n\n{error_msg}"

                # 收集 reasoning_details（含 signature），不渲染到卡片
                if "reasoning_details" in chunk:
                    full_reasoning_details = chunk["reasoning_details"]
                    continue

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

            # 记录历史（含 reasoning_details，支持多轮 thinking）
            update_history(session_key, user_msg=None, assistant_msg=full_response,
                           reasoning_details=full_reasoning_details)

            # 调用完成回调
            if complete_callback:
                await complete_callback(full_response, full_thinking, usage_info)

            return full_response

        except asyncio.CancelledError:
            raise
        except Exception as error:
            error_msg = f"系统异常: {safe_error_summary(error, 'provider')}"
            print(f"💥 [AIHandler] {error_msg}")
            return f"💥 **系统异常**\n\n{error_msg}"
    def _format_history_with_meta(self, history_messages: List[Dict], current_bot_id: str, cutoff_at=None) -> List[Dict]:
        """格式化历史消息，保留 bot_id 给后续 transform 层。"""
        from app.ai.history_format import format_history_with_meta

        return format_history_with_meta(history_messages, current_bot_id, cutoff_at=cutoff_at)

    

    async def _route_model(self, content: str, has_images: bool) -> tuple:
        """
        智能路由：选择模型、thinking level 和是否联网

        Returns:
            (target_model, thinking_level, need_search)
        """
        if AI_BACKEND == "openclaw":
            # OpenClaw 模式: Gateway 自行决定模型和 thinking，客户端无法控制
            return ("openclaw", "default", False, 0.7, None)
        elif AI_BACKEND == "openrouter":
            # OpenRouter 模式: 用 Haiku 替代 Gemini flash-lite 做路由判断
            from app.openrouter_client import analyze_complexity_with_openrouter
            try:
                complexity = await analyze_complexity_with_openrouter(content, has_images)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print(f"❌ [OR路由] 异常: {safe_error_summary(error, 'analysis')}")
                complexity = {
                    "model": "fast",
                    "thinking_level": "low",
                    "need_search": False,
                    "reason": "路由异常，使用默认"
                }
        else:
            # Gemini / LiteLLM 模式: 用 Gemini flash-lite 做路由判断
            try:
                from app.gemini_client import analyze_complexity_with_model  # 延迟导入，避免循环依赖
                complexity = await analyze_complexity_with_model(content, has_images)
                print(f"🔄 [路由] 预分析返回: {complexity}")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print(f"❌ [路由] 预分析异常: {safe_error_summary(error, 'analysis')}")
                complexity = {
                    "model": GEMINI_MODEL_FAST,
                    "thinking_level": "low",
                    "need_search": False,
                    "reason": "路由异常，使用默认"
                }

        target_model = complexity.get("model", "fast")
        thinking_level = complexity.get("thinking_level", "low")
        route_slot = complexity.get("route_slot")
        if route_slot not in {"lite", "fast", "pro"}:
            route_slot = "lite" if thinking_level == "minimal" else "fast"
        from app.ai.router import should_force_search
        need_search = bool(complexity.get("need_search", False)) or should_force_search(content)
        temp_label = complexity.get("temperature", "balanced")
        temperature = TEMPERATURE_MAP.get(str(temp_label), 0.7)
        temperature = clamp_temperature(temperature, AI_BACKEND)

        return (target_model, thinking_level, need_search, temperature, route_slot)
