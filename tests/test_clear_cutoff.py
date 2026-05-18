# tests/test_clear_cutoff.py
# -*- coding: utf-8 -*-
"""验证 /clear 的 per-agent cutoff 行为：历史按时间戳过滤"""
import pytest


@pytest.fixture(autouse=True)
def _clean_storage(tmp_path, monkeypatch):
    # BOT_ID 在 module-load 时从 env 读，setenv 不够，必须直接 patch 模块属性
    monkeypatch.setattr("app.clear_cutoff.BOT_ID", "test_bot")
    monkeypatch.setattr("app.clear_cutoff.CUTOFF_DIR", str(tmp_path))
    yield


def test_set_get_cutoff_roundtrip():
    from app.clear_cutoff import set_cutoff, get_cutoff
    assert get_cutoff("s1") is None
    cutoff = set_cutoff("s1", set_by="u1", set_by_nick="张三")
    assert get_cutoff("s1") == cutoff
    # 北京时间字符串格式
    assert len(cutoff) == 19
    assert cutoff[4] == "-" and cutoff[10] == " "


def test_reset_cutoff():
    from app.clear_cutoff import set_cutoff, get_cutoff, reset_cutoff
    set_cutoff("s2", set_by="u", set_by_nick="n")
    assert get_cutoff("s2") is not None
    reset_cutoff("s2")
    assert get_cutoff("s2") is None


def test_history_filter_skips_messages_before_cutoff():
    from app.ai.history_format import format_history_with_meta

    msgs = [
        {"role": "user", "content": "old", "timestamp": "2026-05-18 13:00:00", "sender_nick": "张三", "bot_id": None},
        {"role": "assistant", "content": "old reply", "timestamp": "2026-05-18 13:01:00", "bot_id": "test_bot"},
        {"role": "user", "content": "new", "timestamp": "2026-05-18 15:00:00", "sender_nick": "张三", "bot_id": None},
        {"role": "assistant", "content": "new reply", "timestamp": "2026-05-18 15:01:00", "bot_id": "test_bot"},
    ]

    # cutoff = 14:00 → 只剩 15:00 之后的两条
    result = format_history_with_meta(msgs, current_bot_id="test_bot", cutoff_at="2026-05-18 14:00:00")
    assert len(result) == 2
    # 当前 bot 的回复无 [来自X] 前缀，user 消息被 format 加上时间戳前缀
    assert all("new" in m["content"] for m in result)
    assert not any("old" in m["content"] for m in result)


def test_history_filter_no_cutoff_returns_all():
    from app.ai.history_format import format_history_with_meta

    msgs = [
        {"role": "user", "content": "x", "timestamp": "2026-05-18 13:00:00"},
        {"role": "user", "content": "y", "timestamp": "2026-05-18 15:00:00"},
    ]
    result = format_history_with_meta(msgs, current_bot_id="test_bot", cutoff_at=None)
    assert len(result) == 2


def test_history_filter_keeps_msgs_without_timestamp():
    """timestamp 缺失的消息保留（保守策略，避免误删）"""
    from app.ai.history_format import format_history_with_meta

    msgs = [
        {"role": "user", "content": "no ts"},
        {"role": "user", "content": "has ts old", "timestamp": "2026-05-18 13:00:00"},
    ]
    result = format_history_with_meta(msgs, current_bot_id="test_bot", cutoff_at="2026-05-18 14:00:00")
    assert len(result) == 1
    assert result[0]["content"] == "no ts"


def test_history_filter_exact_cutoff_excluded():
    """timestamp == cutoff 的消息被排除（cutoff_at 本身是"清空时刻"，之前的都不算）"""
    from app.ai.history_format import format_history_with_meta

    msgs = [
        {"role": "user", "content": "at cutoff", "timestamp": "2026-05-18 14:00:00"},
        {"role": "user", "content": "after cutoff", "timestamp": "2026-05-18 14:00:01"},
    ]
    result = format_history_with_meta(msgs, current_bot_id="test_bot", cutoff_at="2026-05-18 14:00:00")
    assert len(result) == 1
    # user 消息带 timestamp 前缀: "[2026-05-18 14:00:01] after cutoff"
    assert "after cutoff" in result[0]["content"]


def test_get_history_for_current_agent_respects_cutoff(tmp_path, monkeypatch):
    """agent_history 包装器：cutoff 过滤覆盖 /soul 等读历史场景"""
    import os, json
    fake = [
        {"role": "user", "content": "old", "timestamp": "2026-05-18 13:00:00"},
        {"role": "user", "content": "new", "timestamp": "2026-05-18 15:00:00"},
    ]
    # mock get_history 的调用路径（agent_history 内 from app.memory import get_history）
    monkeypatch.setattr("app.memory.get_history", lambda sk, limit=50: fake)

    from app.agent_history import get_history_for_current_agent

    # 无 cutoff 文件：全部返回
    assert len(get_history_for_current_agent("s_cutoff")) == 2

    # 手动写 cutoff 文件（CUTOFF_DIR 已被 fixture monkeypatch 到 tmp_path）
    path = os.path.join(str(tmp_path), "test_bot__s_cutoff.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"cutoff_at": "2026-05-18 14:00:00", "set_by": "u", "set_by_nick": "n"}, f)

    filtered = get_history_for_current_agent("s_cutoff")
    assert len(filtered) == 1
    assert filtered[0]["content"] == "new"
