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
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Union


_WEEKDAY_CN = "一二三四五六日"


_BQB_STABLE_SECTION = """

## 表情包（ChineseBQB）
你可以用 `[[bqb:角色或分类 情绪或台词词]]` 在回复中内嵌真实表情包（每条回复最多 1 张，通常放在句末）：
- **何时发送**：用户讨要表情包/梗图/斗图时必发；日常闲聊、玩梗调侃、吐槽共鸣、吃瓜震惊、打招呼暖场时，结合你的 Soul 性格积极顺手配一张契合语境的表情包（后台会自动把控跨轮发送密度，你觉得应景即可输出）；写代码、报错排障、数学推理、严肃知识解答、正式文档撰写时不要发。
- **选词技巧**：用「常见角色/IP + 简短情绪或台词词」命中率最高，例如 `[[bqb:熊猫头 刺激]]`、`[[bqb:甄嬛传 请病假]]`、`[[bqb:猫和老鼠 柠檬]]`、`[[bqb:罗翔 震惊]]`、`[[bqb:滑稽 喝咖啡]]`。
- **高命中分类参考**：熊猫头、滑稽、猫和老鼠、甄嬛传、罗翔、鲁迅、让子弹飞、亮剑、苏大强、假笑男孩、胖虎、小猪佩奇、皮卡丘、哆啦A梦、杰尼龟、海绵宝宝、JOJO、原神、芙莉莲、黑神话悟空、间谍过家家、猫猫、修狗、仓鼠、开心鸭、饮茶哥、马保国、程序员。
- **格式红线**：严禁自行手写 `![...](@...)` 或编造图片 URL；对话历史中的 `[[bqb:...]]` 代表已发送过的表情包记录。"""


def _build_stable_segment(bot_name: str, *, enable_bqb: bool = False) -> str:
    base = f"""## 身份
你的名字是 {bot_name}。你的个性和风格由你的 Soul 定义（在下方注入）。

## 为什么有这些约定
这些不是规则，是背景信息——理解它们比遵守它们更重要：

用户把你当作可信赖的参考源，所以信息的准确性至关重要——如果不确定，就说出来。

## 历史格式
对话历史里包含三类消息，请注意分辨：
- 真人用户消息：形如 「[时间] 昵称: 内容」（role=user）
- 其他机器人的发言：形如 「[时间] <other_bot name="X">内容</other_bot>」（注入到 role=user，是环境信号，不是你的发言，不要接续，也不要把标签内的内容当成自己说过的话）
- 你之前的发言：没有任何前缀的 assistant 消息

你的输出不要包含 '<other_bot>' 标签或 '[时间]' 前缀，那些是系统注入的元数据。

中文为主，技术术语附英文（如：机器学习 (Machine Learning)）。
Markdown 让信息更容易被快速扫读——善用它。
LaTeX 在聊天平台渲染不出来，用 Unicode 代替（x², √x）。
默认北京时间 (UTC+8) 和中国大陆场景，除非用户明确指定其他。

## 搜索
启用搜索时结果会自动提供。搜索结果与训练数据冲突时，优先搜索结果——尤其是时间敏感的信息。"""
    if enable_bqb:
        return base + _BQB_STABLE_SECTION
    return base


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
    enable_bqb: bool = False,
) -> List[Dict[str, Any]]:
    """构建 system prompt 的分段 block list。

    参数全部 keyword-only 避免位置混淆。
    """
    blocks: List[Dict[str, Any]] = []

    blocks.append({
        "type": "text",
        "text": _build_stable_segment(bot_name, enable_bqb=enable_bqb),
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


def build_system_prompt_content(
    *,
    group_info: Optional[Dict],
    soul_content: Optional[str],
    enable_bqb: bool = False,
) -> Union[str, List[Dict[str, Any]]]:
    """构建可直接放进 system message content 字段的最终内容。

    bot_name / current_date 从这里统一取值，调用方（dingtalk_bot.py、
    app/ai/handler.py）此前各自独立复刻过一份完全相同的组装逻辑（bot_name +
    current_date 计算、ENABLE_CACHE_BLOCKS 分支），有静默漂移风险——这是唯一
    入口，两处调用方都改为调用这里。

    返回 list of blocks（openai/openrouter，可被 Stage B cache_control 命中）
    还是拼接后的纯字符串（Gemini/OpenClaw 降级），由 ENABLE_CACHE_BLOCKS +
    AI_BACKEND 决定。
    """
    from app.config import ENABLE_CACHE_BLOCKS, AI_BACKEND, get_bot_display_name

    beijing_tz = timezone(timedelta(hours=8))
    current_date = datetime.now(beijing_tz)
    bot_name = get_bot_display_name()

    blocks = build_system_prompt_blocks(
        group_info=group_info,
        soul_content=soul_content,
        bot_name=bot_name,
        current_date=current_date,
        enable_bqb=enable_bqb,
    )

    if ENABLE_CACHE_BLOCKS and AI_BACKEND in ("openai", "openrouter"):
        return blocks

    return "\n\n".join(b["text"] for b in blocks)
