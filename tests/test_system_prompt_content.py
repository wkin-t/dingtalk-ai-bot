# tests/test_system_prompt_content.py
# -*- coding: utf-8 -*-
"""Tests for build_system_prompt_content —— dingtalk_bot.py 与 handler.py
唯一共用的 system prompt 组装入口，覆盖此前两处重复实现的分支逻辑。"""
from unittest.mock import patch

from app.ai.system_prompt import build_system_prompt_content


def test_returns_list_of_blocks_when_cache_enabled_and_backend_openai():
    with patch("app.config.ENABLE_CACHE_BLOCKS", True), \
         patch("app.config.AI_BACKEND", "openai"), \
         patch("app.config.get_bot_display_name", return_value="小克"):
        content = build_system_prompt_content(group_info=None, soul_content=None)
    assert isinstance(content, list)
    assert all("type" in b for b in content)


def test_returns_list_of_blocks_when_cache_enabled_and_backend_openrouter():
    with patch("app.config.ENABLE_CACHE_BLOCKS", True), \
         patch("app.config.AI_BACKEND", "openrouter"), \
         patch("app.config.get_bot_display_name", return_value="小克"):
        content = build_system_prompt_content(group_info=None, soul_content=None)
    assert isinstance(content, list)


def test_returns_joined_string_when_cache_disabled():
    with patch("app.config.ENABLE_CACHE_BLOCKS", False), \
         patch("app.config.AI_BACKEND", "openai"), \
         patch("app.config.get_bot_display_name", return_value="小克"):
        content = build_system_prompt_content(group_info=None, soul_content=None)
    assert isinstance(content, str)


def test_returns_joined_string_for_gemini_backend_even_when_cache_enabled():
    with patch("app.config.ENABLE_CACHE_BLOCKS", True), \
         patch("app.config.AI_BACKEND", "gemini"), \
         patch("app.config.get_bot_display_name", return_value="小克"):
        content = build_system_prompt_content(group_info=None, soul_content=None)
    assert isinstance(content, str)


def test_returns_joined_string_for_openclaw_backend():
    with patch("app.config.ENABLE_CACHE_BLOCKS", True), \
         patch("app.config.AI_BACKEND", "openclaw"), \
         patch("app.config.get_bot_display_name", return_value="小克"):
        content = build_system_prompt_content(group_info=None, soul_content=None)
    assert isinstance(content, str)


def test_soul_and_group_info_flow_through_to_string_output():
    with patch("app.config.ENABLE_CACHE_BLOCKS", False), \
         patch("app.config.AI_BACKEND", "gemini"), \
         patch("app.config.get_bot_display_name", return_value="小克"):
        content = build_system_prompt_content(
            group_info={"name": "测试群"},
            soul_content="活泼健谈",
        )
    assert "测试群" in content
    assert "活泼健谈" in content


def test_soul_and_group_info_flow_through_to_block_output():
    with patch("app.config.ENABLE_CACHE_BLOCKS", True), \
         patch("app.config.AI_BACKEND", "openai"), \
         patch("app.config.get_bot_display_name", return_value="小克"):
        content = build_system_prompt_content(
            group_info={"name": "测试群"},
            soul_content="活泼健谈",
        )
    semi_stable = content[1]["text"]
    assert "测试群" in semi_stable
    assert "活泼健谈" in semi_stable


def test_bot_name_derived_from_get_bot_display_name():
    with patch("app.config.ENABLE_CACHE_BLOCKS", False), \
         patch("app.config.AI_BACKEND", "gemini"), \
         patch("app.config.get_bot_display_name", return_value="独一无二的名字"):
        content = build_system_prompt_content(group_info=None, soul_content=None)
    assert "独一无二的名字" in content
