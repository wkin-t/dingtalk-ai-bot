# -*- coding: utf-8 -*-
"""消息转换层：处理多 agent 角色重塑和连续相同 role 合并。"""
from typing import Any, Dict, List, Optional, Tuple


def rewrite_roles_for_current_agent(
    messages: List[Dict[str, Any]],
    current_bot_id: Optional[str],
) -> List[Dict[str, Any]]:
    """把非当前 agent 的 assistant 消息重写为 user 角色。

    输出严格清理为 OpenAI SDK 兼容的 role/content 字段。
    """
    result: List[Dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        bot_id = msg.get("bot_id")

        if role == "assistant" and bot_id is not None and bot_id != current_bot_id:
            role = "user"

        result.append({"role": role, "content": content})
    return result


def merge_consecutive_same_role(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """合并连续相同 role 的消息，system 消息不合并。"""
    result: List[Dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if result and result[-1]["role"] == role and role in ("user", "assistant"):
            result[-1]["content"] = _merge_content(result[-1]["content"], content)
        else:
            result.append({"role": role, "content": content})
    return result


def _merge_content(a: Any, b: Any) -> Any:
    """合并两个 message content，保留多模态 part。"""
    if isinstance(a, str) and isinstance(b, str):
        return f"{a}\n\n{b}"

    a_text, a_other = _split_text_and_other(a)
    b_text, b_other = _split_text_and_other(b)
    merged_text = "\n\n".join(text for text in (a_text, b_text) if text)

    if not a_other and not b_other:
        return merged_text

    parts: List[Dict[str, Any]] = []
    if merged_text:
        parts.append({"type": "text", "text": merged_text})
    parts.extend(a_other)
    parts.extend(b_other)
    return parts


def _split_text_and_other(content: Any) -> Tuple[str, List[Dict[str, Any]]]:
    """从 content 中拆出 text 和非 text part。"""
    if isinstance(content, str):
        return content, []

    if isinstance(content, list):
        text_parts: List[str] = []
        other_parts: List[Dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                text_parts.append(part.get("text", ""))
            else:
                other_parts.append(part)
        return "\n\n".join(text for text in text_parts if text), other_parts

    return "", []
