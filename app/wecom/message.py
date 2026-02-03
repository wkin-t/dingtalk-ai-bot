# -*- coding: utf-8 -*-
"""
企业微信消息发送
"""
import requests
import time
from typing import Optional
from app.config import WECOM_CORP_ID, WECOM_AGENT_ID, WECOM_SECRET


class WeComMessageSender:
    """企业微信消息发送器"""

    def __init__(self):
        self.corp_id = WECOM_CORP_ID
        self.agent_id = WECOM_AGENT_ID
        self.secret = WECOM_SECRET
        self.access_token = None
        self.token_expires_at = 0

    def _get_access_token(self) -> str:
        """获取 access_token (带缓存)"""
        # 如果 token 未过期,直接返回
        if self.access_token and time.time() < self.token_expires_at:
            return self.access_token

        # 请求新 token
        url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
        params = {
            "corpid": self.corp_id,
            "corpsecret": self.secret
        }

        try:
            response = requests.get(url, params=params, timeout=10)
            data = response.json()

            if data.get("errcode") == 0:
                self.access_token = data["access_token"]
                # 提前 5 分钟过期,避免边界问题
                self.token_expires_at = time.time() + data.get("expires_in", 7200) - 300
                print(f"✅ 获取企业微信 access_token 成功,有效期: {data.get('expires_in', 7200)}秒")
                return self.access_token
            else:
                print(f"❌ 获取 access_token 失败: {data}")
                raise Exception(f"获取 access_token 失败: {data}")

        except Exception as e:
            print(f"❌ 获取 access_token 异常: {e}")
            raise

    def send_text(self, user_id: str, content: str) -> bool:
        """
        发送文本消息

        Args:
            user_id: 用户 ID (企业微信 UserID)
            content: 文本内容

        Returns:
            是否发送成功
        """
        access_token = self._get_access_token()
        url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={access_token}"

        data = {
            "touser": user_id,
            "msgtype": "text",
            "agentid": self.agent_id,
            "text": {
                "content": content
            },
            "safe": 0,
            "enable_duplicate_check": 0
        }

        try:
            response = requests.post(url, json=data, timeout=10)
            result = response.json()

            if result.get("errcode") == 0:
                print(f"✅ 文本消息发送成功: {user_id}")
                return True
            else:
                print(f"❌ 文本消息发送失败: {result}")
                return False

        except Exception as e:
            print(f"❌ 文本消息发送异常: {e}")
            return False

    def send_markdown(self, user_id: str, content: str) -> bool:
        """
        发送 Markdown 消息

        Args:
            user_id: 用户 ID (企业微信 UserID)
            content: Markdown 内容

        Returns:
            是否发送成功
        """
        access_token = self._get_access_token()
        url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={access_token}"

        data = {
            "touser": user_id,
            "msgtype": "markdown",
            "agentid": self.agent_id,
            "markdown": {
                "content": content
            },
            "enable_duplicate_check": 0
        }

        try:
            response = requests.post(url, json=data, timeout=10)
            result = response.json()

            if result.get("errcode") == 0:
                print(f"✅ Markdown 消息发送成功: {user_id}")
                return True
            else:
                print(f"❌ Markdown 消息发送失败: {result}")
                return False

        except Exception as e:
            print(f"❌ Markdown 消息发送异常: {e}")
            return False
