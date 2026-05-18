# app/ai/system_prompt.py
# -*- coding: utf-8 -*-
"""
拆段构建 system prompt，让稳定段可被 prompt cache 命中。

返回 list of blocks，结构：
  [
    {"type": "text", "text": "<stable>", "cache_control": {"type": "ephemeral"}},
    {"type": "text", "text": "<semi-stable: group+soul>", "cache_control": {"type": "ephemeral"}},
    {"type": "text", "text": "<dynamic: date>"},   # 无 cache_control
  ]

调用方负责把这个 list 作为 system message 的 content 字段。
"""
from datetime import datetime
from typing import Optional, List, Dict, Any


_WEEKDAY_CN = "一二三四五六日"


def _build_stable_segment(bot_name: str) -> str:
    return f"""## 身份
你的名字是 {bot_name}。你的个性和风格由你的 Soul 定义（在下方注入）。

## 为什么有这些约定
这些不是规则，是背景信息——理解它们比遵守它们更重要：

用户把你当作可信赖的参考源，所以信息的准确性至关重要——如果不确定，就说出来。

## 历史格式
对话历史里包含三类消息，请注意分辨：
- 真人用户消息：形如 「[时间] 昵称: 内容」（role=user）
- 其他机器人的发言：形如 「[来自机器人 X] 内容」（注入到 role=user，是环境信号，不是你的发言，不要接续）
- 你之前的发言：没有任何前缀的 assistant 消息

你的输出不要包含 '[来自...]' 或 '[时间]' 前缀，那些是系统注入的元数据。

中文为主，技术术语附英文（如：机器学习 (Machine Learning)）。
Markdown 让信息更容易被快速扫读——善用它。
LaTeX 在聊天平台渲染不出来，用 Unicode 代替（x², √x）。
默认北京时间 (UTC+8) 和中国大陆场景，除非用户明确指定其他。

## 搜索
启用搜索时结果会自动提供。搜索结果与训练数据冲突时，优先搜索结果——尤其是时间敏感的信息。"""


def _build_semi_stable_segment(group_info: Optional[Dict], soul_content: Optional[str], bot_name: str) -> Optional[str]:
    parts: List[str] = []
    if group_info:
        group_name = group_info.get("name", "Unknown Group")
        parts.append(f"当前群聊: '{group_name}'")
    if soul_content:
        parts.append(f"{bot_name} 的个性设定:\n{soul_content}")
    return "\n\n".join(parts) if parts else None


def _build_dynamic_segment(current_date: datetime) -> str:
    weekday_cn = _WEEKDAY_CN[current_date.weekday()]
    return (
        f"## 时间\n"
        f"今天是 {current_date.year} 年 {current_date.month} 月 {current_date.day} 日"
        f"（周{weekday_cn}，北京时间 UTC+8）。\n"
        f"你的训练数据截止于 2025 年，但现在是 {current_date.year} 年了。"
    )


def build_system_prompt_blocks(
    *,
    group_info: Optional[Dict],
    soul_content: Optional[str],
    bot_name: str,
    current_date: datetime,
) -> List[Dict[str, Any]]:
    """构建 system prompt 的分段 block list。

    参数全部 keyword-only 避免位置混淆。
    """
    blocks: List[Dict[str, Any]] = []

    blocks.append({
        "type": "text",
        "text": _build_stable_segment(bot_name),
        "cache_control": {"type": "ephemeral"},
    })

    semi = _build_semi_stable_segment(group_info, soul_content, bot_name)
    if semi:
        blocks.append({
            "type": "text",
            "text": semi,
            "cache_control": {"type": "ephemeral"},
        })

    blocks.append({
        "type": "text",
        "text": _build_dynamic_segment(current_date),
    })

    return blocks
