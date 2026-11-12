# app/agent_history.py
# -*- coding: utf-8 -*-
"""Deprecated（2026-11-12）：旧数据库 / 文件历史的当前 bot 视图，保留兼容。

钉钉主链路和历史相关命令改用 fetch_dws_context_bundle(dws_only=True)，
同样应用各 bot 的 /clear 起点；新增钉钉历史读取不应经过此模块。
"""
from typing import List, Dict, Any


def get_history_for_current_agent(session_key: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Deprecated：读取旧历史并按当前 BOT_ID 的 cutoff 过滤，仅供兼容调用。

    timestamp 缺失的消息保留（保守策略，避免误删）。
    timestamp <= cutoff 的消息被排除（cutoff 时刻自身也算"被清掉"）。
    """
    from app.memory import get_history
    from app.clear_cutoff import get_cutoff

    raw = get_history(session_key, limit)
    cutoff = get_cutoff(session_key)
    if not cutoff:
        return raw
    return [m for m in raw if not (m.get("timestamp") and m["timestamp"] <= cutoff)]
