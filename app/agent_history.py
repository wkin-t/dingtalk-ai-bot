# app/agent_history.py
# -*- coding: utf-8 -*-
"""按当前 agent 视角读取历史，自动应用 /clear cutoff 过滤。

任何"读历史"的场景都应走这里，而不是直接调 app.memory.get_history：
- 主 AI 流（handler/dingtalk_bot 已通过 format_history_with_meta 走 cutoff）
- /soul evolve 读历史进化人格
- image_gen prompt 增强读上下文
- 其他未来场景
"""
from typing import List, Dict, Any


def get_history_for_current_agent(session_key: str, limit: int = 50) -> List[Dict[str, Any]]:
    """读历史并按当前 BOT_ID 的 cutoff 过滤。

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
