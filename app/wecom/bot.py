# -*- coding: utf-8 -*-
"""
企业微信机器人消息处理器
"""
import time
import asyncio
import xml.etree.ElementTree as ET
from typing import Optional
from app.wecom.message import WeComMessageSender
from app.memory import get_session_key, get_history, update_history, clear_history
from app.ai.handler import AIHandler


class WeComBotHandler:
    """企业微信机器人消息处理器"""

    def __init__(self):
        self.message_sender = WeComMessageSender()
        self.message_buffer = {}  # 消息缓冲: {session_key: {"content": [], "user_id": str, "timer": task}}
        self.ai_handler = AIHandler(platform="wecom")

    def handle_message(self, msg_dict: dict) -> Optional[str]:
        """
        处理企业微信消息

        Args:
            msg_dict: 解密后的消息字典

        Returns:
            回复消息 XML (可选)
        """
        msg_type = msg_dict.get('MsgType', '')

        # 只处理文本消息
        if msg_type != 'text':
            print(f"⚠️ 暂不支持的消息类型: {msg_type}")
            return None

        # 提取消息内容
        from_user = msg_dict.get('FromUserName', '')
        content = msg_dict.get('Content', '').strip()
        conversation_id = f"wecom_{from_user}"  # 企业微信会话 ID

        print(f"📩 [企业微信] 收到文本消息: {content} (From: {from_user})")

        # 获取会话 key (添加 wecom 前缀,避免与钉钉冲突)
        session_key = get_session_key(conversation_id, from_user)

        # 处理特殊命令
        if content in ["/clear", "清空上下文", "🧹 清空记忆"]:
            clear_history(session_key)
            self.message_sender.send_text(from_user, "🧹 你的上下文已清空")
            return None

        # 缓冲消息 (2秒合并)
        if session_key not in self.message_buffer:
            self.message_buffer[session_key] = {
                "content": [],
                "user_id": from_user,
                "timer": None
            }

        # 取消现有定时器
        if self.message_buffer[session_key]["timer"]:
            self.message_buffer[session_key]["timer"].cancel()

        # 添加消息到缓冲区
        self.message_buffer[session_key]["content"].append(content)

        # 启动 2 秒定时器
        import threading
        timer = threading.Timer(2.0, self._process_buffered_messages, args=[session_key])
        timer.start()
        self.message_buffer[session_key]["timer"] = timer

        # 不立即回复 (等待缓冲合并)
        return None

    def _process_buffered_messages(self, session_key: str):
        """处理缓冲的消息"""
        if session_key not in self.message_buffer:
            return

        data = self.message_buffer.pop(session_key)
        content_list = data["content"]
        user_id = data["user_id"]

        # 合并消息
        full_content = "\n".join(content_list)
        print(f"📥 [企业微信] 处理合并消息: {full_content} (User: {user_id})")

        # 记录用户消息
        update_history(session_key, full_content, assistant_msg=None, sender_nick=user_id)

        # 发送 "思考中" 提示
        self.message_sender.send_text(user_id, "🤔 AI 正在思考中...")

        # 调用统一 AI 处理层 (同步包装异步调用)
        try:
            # 创建事件循环运行异步函数
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def complete_callback(response: str, thinking: str, usage: dict):
                """完成回调 - 发送完整回复"""
                # 构建回复内容
                if thinking:
                    # 如果有思考过程，添加折叠块
                    thinking_brief = thinking[:100].replace("\n", " ").strip()
                    if len(thinking) > 100:
                        thinking_brief += "..."
                    reply_content = f"**🧠 思考过程:**\n{thinking_brief}\n\n---\n\n{response}"
                else:
                    reply_content = response

                # 发送 Markdown 消息
                self.message_sender.send_markdown(user_id, reply_content)

            # 运行 AI 处理
            ai_response = loop.run_until_complete(
                self.ai_handler.process_message(
                    content=full_content,
                    session_key=session_key,
                    user_id=user_id,
                    sender_nick=user_id,
                    image_data_list=None,
                    group_info=None,
                    stream_callback=None,  # 企业微信不支持流式更新
                    complete_callback=complete_callback
                )
            )

            loop.close()

            print(f"✅ [企业微信] AI 回复发送完成")

        except Exception as e:
            print(f"❌ [企业微信] AI 处理失败: {e}")
            import traceback
            traceback.print_exc()
            self.message_sender.send_text(user_id, f"❌ 系统异常: {str(e)}")

    def _build_text_reply(self, to_user: str, content: str) -> str:
        """
        构建文本回复 XML

        Args:
            to_user: 接收用户
            content: 文本内容

        Returns:
            XML 字符串
        """
        timestamp = int(time.time())
        xml_template = """<xml>
<ToUserName><![CDATA[{to_user}]]></ToUserName>
<FromUserName><![CDATA[{from_user}]]></FromUserName>
<CreateTime>{create_time}</CreateTime>
<MsgType><![CDATA[text]]></MsgType>
<Content><![CDATA[{content}]]></Content>
</xml>"""

        return xml_template.format(
            to_user=to_user,
            from_user=self.message_sender.corp_id,
            create_time=timestamp,
            content=content
        )
