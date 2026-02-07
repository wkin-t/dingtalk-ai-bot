# -*- coding: utf-8 -*-
"""
企业微信机器人消息处理器（机器人回调直返模式）
"""
import asyncio
import json
import random
import re
import string
from typing import Optional

from app.ai.handler import AIHandler
from app.memory import get_session_key, update_history, clear_history


class WeComBotHandler:
    """企业微信机器人消息处理器"""

    def __init__(self):
        self.ai_handler = AIHandler(platform="wecom")

    def handle_message(self, msg_dict: dict) -> Optional[str]:
        """
        处理企业微信回调消息并返回 stream 明文 JSON（由回调层加密）
        """
        msg_type = (msg_dict.get("msgtype") or msg_dict.get("MsgType") or "").lower()

        if msg_type == "event" or msg_dict.get("Event"):
            print(f"ℹ️ [企业微信] 忽略事件消息: {msg_dict}")
            return None

        if msg_type == "stream":
            # 当前实现为一次性回复（finish=true），不维护长任务拉取状态
            return None

        if msg_type != "text":
            print(f"⚠️ [企业微信] 暂不支持的消息类型: {msg_type}")
            return None

        from_user = self._extract_sender_id(msg_dict)
        conversation_id = self._extract_conversation_id(msg_dict, from_user)
        content = self._extract_text_content(msg_dict)
        content = self._normalize_content(content)

        if not content:
            print("⚠️ [企业微信] 文本内容为空，忽略")
            return None

        session_key = get_session_key(conversation_id, from_user)

        if content in ["/clear", "清空上下文", "🧹 清空记忆"]:
            clear_history(session_key)
            stream_id = self._new_stream_id()
            return self._build_text_stream(stream_id, "🧹 上下文已清空", True)

        update_history(session_key, content, assistant_msg=None, sender_nick=from_user)
        print(f"📩 [企业微信] 收到文本消息: {content} (From: {from_user})")

        response = self._call_ai(
            content=content,
            session_key=session_key,
            user_id=from_user,
            sender_nick=from_user,
        )

        stream_id = self._new_stream_id()
        return self._build_text_stream(stream_id, response, True)

    def _call_ai(self, content: str, session_key: str, user_id: str, sender_nick: str) -> str:
        try:
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                ai_response = loop.run_until_complete(
                    self.ai_handler.process_message(
                        content=content,
                        session_key=session_key,
                        user_id=user_id,
                        sender_nick=sender_nick,
                        image_data_list=None,
                        group_info=None,
                        stream_callback=None,
                        complete_callback=None,
                    )
                )
            finally:
                loop.close()

            cleaned = (ai_response or "").strip()
            return cleaned or "我暂时没有生成有效回复，请稍后重试。"
        except Exception as e:
            print(f"❌ [企业微信] AI 处理失败: {e}")
            import traceback

            traceback.print_exc()
            return f"系统异常：{e}"

    @staticmethod
    def _new_stream_id(length: int = 12) -> str:
        chars = string.ascii_letters + string.digits
        return "".join(random.choice(chars) for _ in range(length))

    @staticmethod
    def _extract_sender_id(msg_dict: dict) -> str:
        return (
            msg_dict.get("from")
            or msg_dict.get("FromUserName")
            or msg_dict.get("FromUserId")
            or msg_dict.get("SenderId")
            or msg_dict.get("UserId")
            or "unknown_user"
        )

    @staticmethod
    def _extract_conversation_id(msg_dict: dict, from_user: str) -> str:
        conv = (
            msg_dict.get("conversation_id")
            or msg_dict.get("chatid")
            or msg_dict.get("ChatId")
            or msg_dict.get("ConversationId")
            or msg_dict.get("SessionId")
            or msg_dict.get("ExternalChatId")
            or from_user
        )
        return f"wecom_{conv}"

    @staticmethod
    def _extract_text_content(msg_dict: dict) -> str:
        content = msg_dict.get("Content")
        if isinstance(content, str):
            return content.strip()

        text = msg_dict.get("text")
        if isinstance(text, dict):
            value = text.get("content") or text.get("Content") or ""
            if isinstance(value, str):
                return value.strip()

        text2 = msg_dict.get("Text")
        if isinstance(text2, dict):
            value = text2.get("content") or text2.get("Content") or ""
            if isinstance(value, str):
                return value.strip()

        return ""

    @staticmethod
    def _normalize_content(content: str) -> str:
        value = (content or "").strip()
        if not value:
            return ""

        # 去掉开头 @机器人 名称，避免干扰模型理解
        value = re.sub(r"^@\S+\s*", "", value)
        return value.strip()

    @staticmethod
    def _build_text_stream(stream_id: str, content: str, finish: bool) -> str:
        payload = {
            "msgtype": "stream",
            "stream": {
                "id": stream_id,
                "finish": bool(finish),
                "content": content,
            },
        }
        return json.dumps(payload, ensure_ascii=False)
