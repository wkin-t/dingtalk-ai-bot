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


def test_force_search_ignores_history_labels_and_current_user_marker():
    """路由辅助标签中的“当前/新闻/天气”不能替当前用户强制联网。"""
    router_context = (
        "[近期群聊上下文]\n"
        "历史消息提到当前天气和新闻\n"
        "[当前用户消息]\n"
        "请画一只红苹果"
    )
    assert not should_force_search(router_context)


def test_force_search_respects_explicit_search_negation():
    """用户明确要求不要联网时，否定意图覆盖搜索关键词。"""
    assert not should_force_search("不要联网搜索，也不用查今天的新闻")
    assert not should_force_search("无需联网，别搜索最新消息")
