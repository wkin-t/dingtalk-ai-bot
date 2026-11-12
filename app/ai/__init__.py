# -*- coding: utf-8 -*-
"""
共享 AI 工具及兼容导出。
Deprecated：AIHandler、MessageBuffer 仅保留旧接入兼容；router 等共享工具仍在使用。
"""

from .handler import AIHandler
from .router import analyze_complexity_unified
from .buffer import MessageBuffer

__all__ = ['AIHandler', 'analyze_complexity_unified', 'MessageBuffer']
