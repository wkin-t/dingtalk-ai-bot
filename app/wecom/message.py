# -*- coding: utf-8 -*-
"""
企业微信机器人消息发送 (Webhook 模式)
"""
import base64
import hashlib
from typing import Optional

import requests

from app.config import WECOM_BOT_WEBHOOK_KEY, WECOM_BOT_WEBHOOK_URL


class WeComMessageSender:
    """企业微信机器人消息发送器 (Webhook)"""

    def __init__(self):
        self.webhook_url = self._build_webhook_url()

    @staticmethod
    def _build_webhook_url() -> str:
        if WECOM_BOT_WEBHOOK_URL:
            return WECOM_BOT_WEBHOOK_URL
        if WECOM_BOT_WEBHOOK_KEY:
            return f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WECOM_BOT_WEBHOOK_KEY}"
        return ""

    def _send(self, payload: dict) -> bool:
        if not self.webhook_url:
            print("❌ 企业微信机器人 Webhook 未配置")
            return False
        try:
            response = requests.post(self.webhook_url, json=payload, timeout=10)
            result = response.json()
            if result.get("errcode") == 0:
                return True
            print(f"❌ 企业微信机器人消息发送失败: {result}")
            return False
        except Exception as e:
            print(f"❌ 企业微信机器人消息发送异常: {e}")
            return False

    def send_text(
        self,
        user_id: str,
        content: str,
        mentioned_list: Optional[list[str]] = None,
        mentioned_mobile_list: Optional[list[str]] = None
    ) -> bool:
        payload = {
            "msgtype": "text",
            "text": {
                "content": content,
                "mentioned_list": mentioned_list or [],
                "mentioned_mobile_list": mentioned_mobile_list or [],
            },
        }
        ok = self._send(payload)
        if ok:
            print(f"✅ 企业微信机器人文本消息发送成功: {user_id}")
        return ok

    def send_markdown(self, user_id: str, content: str) -> bool:
        payload = {
            "msgtype": "markdown",
            "markdown": {"content": content},
        }
        ok = self._send(payload)
        if ok:
            print(f"✅ 企业微信机器人 Markdown 消息发送成功: {user_id}")
        return ok

    def send_image(self, image_bytes: bytes, filename: str = "image") -> bool:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        md5 = hashlib.md5(image_bytes).hexdigest()
        payload = {
            "msgtype": "image",
            "image": {"base64": b64, "md5": md5},
        }
        ok = self._send(payload)
        if ok:
            print(f"✅ 企业微信机器人图片消息发送成功: {filename}")
        return ok
