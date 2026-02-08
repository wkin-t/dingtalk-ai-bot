# -*- coding: utf-8 -*-
"""
企业微信机器人消息处理器
"""
import asyncio
import json
import random
import re
import string
import threading
import time
from typing import Any, Dict, Optional

from app.ai.handler import AIHandler
from app.config import WECOM_BOT_REPLY_MODE, WECOM_BOT_STREAM_STYLE
from app.memory import get_session_key, update_history, clear_history


class WeComBotHandler:
    """企业微信机器人消息处理器"""

    def __init__(self):
        self.ai_handler = AIHandler(platform="wecom")
        self._lock = threading.Lock()
        self._processing_msgids = set()
        self._cached_replies = {}  # msgid -> {"ts": float, "reply": str}
        self._cache_ttl = 300.0
        self._stream_tasks: Dict[str, Dict[str, Any]] = {}
        self._stream_task_ttl = 3600.0

    def handle_message(self, msg_dict: dict) -> Optional[str]:
        """
        处理企业微信回调消息并返回明文 JSON（由回调层加密）
        """
        msg_id = str(msg_dict.get("msgid") or msg_dict.get("MsgId") or "").strip()
        self._gc_cache()
        self._gc_stream_tasks()

        # 企业微信可能重复回调同一 msgid（重试机制），这里做幂等控制
        if msg_id:
            with self._lock:
                cached = self._cached_replies.get(msg_id)
                if cached:
                    print(f"♻️ [企业微信] 命中重复消息缓存，直接复用结果: msgid={msg_id}")
                    return cached["reply"]
                if msg_id in self._processing_msgids:
                    print(f"⏳ [企业微信] 消息正在处理中，忽略重复回调: msgid={msg_id}")
                    return None
                self._processing_msgids.add(msg_id)

        msg_type = (msg_dict.get("msgtype") or msg_dict.get("MsgType") or "").lower()

        try:
            if msg_type == "event" or msg_dict.get("Event"):
                print(f"ℹ️ [企业微信] 忽略事件消息: {msg_dict}")
                return None

            if msg_type == "stream":
                # 被动流式模式：企业微信会携带 stream.id 轮询拉取最新内容
                if WECOM_BOT_REPLY_MODE != "passive_stream":
                    return None
                stream_id = self._extract_stream_id(msg_dict)
                if not stream_id:
                    reply = self._build_stream_payload(
                        stream_id=self._new_stream_id(),
                        content="无效的流式任务 ID。",
                        finish=True,
                        include_card=False,
                    )
                    self._cache_reply(msg_id, reply)
                    return reply
                reply = self._build_stream_poll_reply(stream_id)
                self._cache_reply(msg_id, reply)
                return reply

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
                reply = self._build_stream_payload(
                    stream_id=stream_id,
                    content="🧹 上下文已清空",
                    finish=True,
                    include_card=self._use_stream_with_card(),
                )
                self._cache_reply(msg_id, reply)
                return reply

            update_history(session_key, content, assistant_msg=None, sender_nick=from_user)
            print(f"📩 [企业微信] 收到文本消息: {content} (From: {from_user})")

            # 被动流式模式：首包快速返回 stream_id，后续由 stream 刷新回调拉取
            if WECOM_BOT_REPLY_MODE == "passive_stream":
                stream_id = self._new_stream_id()
                self._start_stream_task(
                    stream_id=stream_id,
                    content=content,
                    session_key=session_key,
                    user_id=from_user,
                    sender_nick=from_user,
                )
                reply = self._build_stream_payload(
                    stream_id=stream_id,
                    content="收到，正在思考中...",
                    finish=False,
                    include_card=self._use_stream_with_card(),
                )
                self._cache_reply(msg_id, reply)
                return reply

            # response_url 模式：一次性完整回复
            response = self._call_ai(
                content=content,
                session_key=session_key,
                user_id=from_user,
                sender_nick=from_user,
            )

            stream_id = self._new_stream_id()
            reply = self._build_stream_payload(
                stream_id=stream_id,
                content=response,
                finish=True,
                include_card=False,
            )
            self._cache_reply(msg_id, reply)
            return reply
        finally:
            if msg_id:
                with self._lock:
                    self._processing_msgids.discard(msg_id)

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

    def _start_stream_task(
        self,
        stream_id: str,
        content: str,
        session_key: str,
        user_id: str,
        sender_nick: str,
    ) -> None:
        with self._lock:
            self._stream_tasks[stream_id] = {
                "content": "",
                "finished": False,
                "error": "",
                "updated_at": time.time(),
            }

        t = threading.Thread(
            target=self._run_stream_task,
            args=(stream_id, content, session_key, user_id, sender_nick),
            daemon=True,
        )
        t.start()

    def _run_stream_task(
        self,
        stream_id: str,
        content: str,
        session_key: str,
        user_id: str,
        sender_nick: str,
    ) -> None:
        async def _stream_callback(thinking: str, content: str, is_thinking: bool) -> None:
            del thinking
            del is_thinking
            if content:
                self._update_stream_task(stream_id, content=content, finished=False)

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
                    stream_callback=_stream_callback,
                    complete_callback=None,
                )
            )
            final_text = (ai_response or "").strip() or "我暂时没有生成有效回复，请稍后重试。"
            self._update_stream_task(stream_id, content=final_text, finished=True)
        except Exception as e:
            print(f"❌ [企业微信] 流式任务失败: {e}")
            self._update_stream_task(stream_id, content=f"系统异常：{e}", finished=True, error=str(e))
        finally:
            loop.close()

    def _update_stream_task(
        self,
        stream_id: str,
        content: Optional[str] = None,
        finished: Optional[bool] = None,
        error: Optional[str] = None,
    ) -> None:
        with self._lock:
            task = self._stream_tasks.get(stream_id)
            if not task:
                return
            if content is not None:
                task["content"] = content
            if finished is not None:
                task["finished"] = bool(finished)
            if error is not None:
                task["error"] = error
            task["updated_at"] = time.time()

    def _build_stream_poll_reply(self, stream_id: str) -> str:
        with self._lock:
            task = self._stream_tasks.get(stream_id)
            if not task:
                return self._build_stream_payload(
                    stream_id=stream_id,
                    content="会话已过期，请重新 @Gemini 提问。",
                    finish=True,
                    include_card=False,
                )
            content = task.get("content") or ""
            finished = bool(task.get("finished"))

        if not content and not finished:
            content = "正在思考中..."
        if not content and finished:
            content = "处理完成。"

        return self._build_stream_payload(
            stream_id=stream_id,
            content=content,
            finish=finished,
            include_card=False,
        )

    @staticmethod
    def _new_stream_id(length: int = 12) -> str:
        chars = string.ascii_letters + string.digits
        return "".join(random.choice(chars) for _ in range(length))

    @staticmethod
    def _extract_sender_id(msg_dict: dict) -> str:
        from_field = msg_dict.get("from")
        if isinstance(from_field, dict):
            return (
                from_field.get("userid")
                or from_field.get("user_id")
                or from_field.get("open_userid")
                or from_field.get("name")
                or "unknown_user"
            )
        if isinstance(from_field, str) and from_field.strip():
            return from_field.strip()
        return (
            msg_dict.get("FromUserName")
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
    def _extract_stream_id(msg_dict: dict) -> str:
        stream = msg_dict.get("stream") or msg_dict.get("Stream")
        if isinstance(stream, dict):
            value = stream.get("id") or stream.get("Id") or ""
            return str(value).strip()
        return ""

    @staticmethod
    def _truncate_utf8(content: str, max_bytes: int = 20480) -> str:
        if not content:
            return ""
        raw = content.encode("utf-8")
        if len(raw) <= max_bytes:
            return content
        return raw[:max_bytes].decode("utf-8", errors="ignore")

    @staticmethod
    def _build_text_notice_card(content: str, finish: bool) -> dict:
        title = "Gemini 回复完成" if finish else "Gemini 正在回复"
        subtitle = content.replace("\n", " ").strip()
        if len(subtitle) > 112:
            subtitle = subtitle[:109] + "..."
        if not subtitle:
            subtitle = "处理中..." if not finish else "已完成"
        return {
            "card_type": "text_notice",
            "main_title": {
                "title": title,
                "desc": "企业微信机器人",
            },
            "sub_title_text": subtitle,
            "card_action": {
                "type": 1,
                "url": "https://work.weixin.qq.com",
            },
        }

    def _use_stream_with_card(self) -> bool:
        return (
            WECOM_BOT_REPLY_MODE == "passive_stream"
            and WECOM_BOT_STREAM_STYLE == "stream_with_template_card"
        )

    def _build_stream_payload(
        self,
        stream_id: str,
        content: str,
        finish: bool,
        include_card: bool = False,
    ) -> str:
        stream = {
            "id": stream_id,
            "finish": bool(finish),
            "content": self._truncate_utf8((content or "").strip()),
        }
        if self._use_stream_with_card():
            payload: Dict[str, Any] = {
                "msgtype": "stream_with_template_card",
                "stream": stream,
            }
            if include_card:
                payload["template_card"] = self._build_text_notice_card(stream["content"], bool(finish))
        else:
            payload = {
                "msgtype": "stream",
                "stream": stream,
            }
        return json.dumps(payload, ensure_ascii=False)

    def _cache_reply(self, msg_id: str, reply: str) -> None:
        if not msg_id:
            return
        with self._lock:
            self._cached_replies[msg_id] = {"ts": time.time(), "reply": reply}

    def _gc_cache(self) -> None:
        now = time.time()
        with self._lock:
            expired = [k for k, v in self._cached_replies.items() if now - v["ts"] > self._cache_ttl]
            for k in expired:
                self._cached_replies.pop(k, None)

    def _gc_stream_tasks(self) -> None:
        now = time.time()
        with self._lock:
            expired = [
                stream_id
                for stream_id, task in self._stream_tasks.items()
                if now - float(task.get("updated_at", now)) > self._stream_task_ttl
            ]
            for stream_id in expired:
                self._stream_tasks.pop(stream_id, None)
