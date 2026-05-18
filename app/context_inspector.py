# app/context_inspector.py
# -*- coding: utf-8 -*-
"""/since 命令实现：报告当前 agent 在当前 session 可见的上下文起点。"""
from typing import Optional


def render_since(session_key: str) -> str:
    """返回 markdown，描述当前 agent 能看到的最早消息时间 + cutoff 状态 + 消息总数。"""
    from app.config import BOT_ID
    from app.agent_history import get_history_for_current_agent
    from app.clear_cutoff import get_cutoff_record

    history = get_history_for_current_agent(session_key, limit=500)
    cutoff_rec = get_cutoff_record(session_key)

    total = len(history)
    first_ts: Optional[str] = None
    for msg in history:
        ts = msg.get("timestamp")
        if ts:
            first_ts = ts
            break

    lines = [f"## 🔍 上下文起点（{BOT_ID}）", ""]

    if first_ts:
        lines.append(f"**最早可见消息时间**: `{first_ts}`")
    else:
        lines.append("**最早可见消息时间**: 无（暂无任何历史消息）")

    lines.append(f"**当前可见消息数**: {total} 条")

    if cutoff_rec:
        lines.append("")
        lines.append("### 🧹 /clear 状态")
        lines.append(f"- cutoff_at: `{cutoff_rec.get('cutoff_at', '?')}`")
        lines.append(f"- 设置人: {cutoff_rec.get('set_by_nick', '?')} ({cutoff_rec.get('set_by', '?')})")
        lines.append("")
        lines.append("> 此 cutoff 之前的历史对我不可见，但其他 agent 仍能看到。发 `/clear` 会更新 cutoff 到当前时刻。")
    else:
        lines.append("")
        lines.append("> 暂未发过 /clear，能看到此 session 全部历史。")

    return "\n".join(lines)
