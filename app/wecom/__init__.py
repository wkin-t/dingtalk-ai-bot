# -*- coding: utf-8 -*-
"""
Deprecated（2026-07-08 起）：企业微信接入，保留兼容，不再维护。
当前生产使用钉钉 Stream 接入（app.dingtalk_bot）。
"""

from .crypto import WXBizMsgCrypt
from .bot import WeComBotHandler
from .message import WeComMessageSender

__all__ = ['WXBizMsgCrypt', 'WeComBotHandler', 'WeComMessageSender']
