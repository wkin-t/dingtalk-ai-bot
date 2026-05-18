# tests/test_system_prompt_blocks.py
# -*- coding: utf-8 -*-
"""Tests for build_system_prompt_blocks"""
from datetime import datetime, timezone, timedelta
import pytest

from app.ai.system_prompt import build_system_prompt_blocks


def _fixed_date():
    return datetime(2026, 5, 18, 14, 23, 45, tzinfo=timezone(timedelta(hours=8)))


def test_blocks_three_segments_when_group_and_soul_present():
    blocks = build_system_prompt_blocks(
        group_info={"name": "测试群"},
        soul_content="活泼健谈",
        bot_name="小克",
        current_date=_fixed_date(),
    )
    assert len(blocks) == 3
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}  # stable
    assert blocks[1]["cache_control"] == {"type": "ephemeral"}  # semi-stable (group + soul)
    assert "cache_control" not in blocks[2]                       # dynamic (date)


def test_no_group_no_soul_only_two_blocks():
    blocks = build_system_prompt_blocks(
        group_info=None,
        soul_content=None,
        bot_name="小克",
        current_date=_fixed_date(),
    )
    assert len(blocks) == 2
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in blocks[1]


def test_dynamic_segment_contains_date_no_seconds():
    blocks = build_system_prompt_blocks(
        group_info=None, soul_content=None, bot_name="小克", current_date=_fixed_date()
    )
    dynamic = blocks[-1]["text"]
    assert "2026" in dynamic
    assert "5" in dynamic and "18" in dynamic
    assert "14:23:45" not in dynamic
    assert "14:" not in dynamic
    assert ":45" not in dynamic


def test_dynamic_segment_includes_weekday_cn():
    blocks = build_system_prompt_blocks(
        group_info=None, soul_content=None, bot_name="小克", current_date=_fixed_date()
    )
    # 2026-05-18 是星期一
    assert "周一" in blocks[-1]["text"]


def test_stable_segment_contains_history_format_explanation():
    blocks = build_system_prompt_blocks(
        group_info=None, soul_content=None, bot_name="小克", current_date=_fixed_date()
    )
    stable = blocks[0]["text"]
    assert "真人用户消息" in stable
    assert "其他机器人的发言" in stable
    assert "没有任何前缀的 assistant" in stable


def test_semi_stable_combines_group_and_soul():
    blocks = build_system_prompt_blocks(
        group_info={"name": "AI 交流群"},
        soul_content="爱讲笑话",
        bot_name="小克",
        current_date=_fixed_date(),
    )
    semi = blocks[1]["text"]
    assert "AI 交流群" in semi
    assert "爱讲笑话" in semi


def test_only_group_no_soul():
    blocks = build_system_prompt_blocks(
        group_info={"name": "测试群"}, soul_content=None, bot_name="小克", current_date=_fixed_date()
    )
    assert len(blocks) == 3
    assert "测试群" in blocks[1]["text"]


def test_only_soul_no_group():
    blocks = build_system_prompt_blocks(
        group_info=None, soul_content="活泼", bot_name="小克", current_date=_fixed_date()
    )
    assert len(blocks) == 3
    assert "活泼" in blocks[1]["text"]


def test_bot_name_appears_in_stable():
    blocks = build_system_prompt_blocks(
        group_info=None, soul_content=None, bot_name="小克", current_date=_fixed_date()
    )
    assert "小克" in blocks[0]["text"]
