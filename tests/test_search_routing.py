# -*- coding: utf-8 -*-
"""搜索触发策略测试。"""

from app.ai.router import should_force_search


def test_force_search_keywords_enable_search():
    """显式搜索/实时类关键词应强制打开联网搜索。"""
    assert should_force_search("帮我查一下今天的新闻")
    assert should_force_search("现在 Gemini 最新模型是什么")
    assert should_force_search("北京天气怎么样")


def test_force_search_ordinary_chat_stays_false():
    """普通闲聊不应因为强制词策略误触发搜索。"""
    assert not should_force_search("你好，帮我写一段自我介绍")
