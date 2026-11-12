# -*- coding: utf-8 -*-
"""消息转换层：处理多 agent 角色重塑和连续相同 role 合并。"""
from typing import Any, Dict, List, Optional


def rewrite_roles_for_current_agent(
    messages: List[Dict[str, Any]],
    current_bot_id: Optional[str],
) -> List[Dict[str, Any]]:
    """把非当前 agent 的 assistant 消息重写为 user 角色。

    输出严格清理为 OpenAI SDK 兼容的 role/content 字段；assistant 消息的
    reasoning_details（thinking blocks）予以保留，user 消息不保留。
    """
    result: List[Dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        bot_id = msg.get("bot_id")

        if role == "assistant" and bot_id is not None and bot_id != current_bot_id:
            role = "user"

        new_msg: Dict[str, Any] = {"role": role, "content": content}
        # reasoning_details 仅对 assistant 消息有效（thinking blocks 不能放在 user 角色）
        if role == "assistant":
            rd = msg.get("reasoning_details")
            if rd:
                new_msg["reasoning_details"] = rd
        result.append(new_msg)
    return result


def merge_consecutive_same_role(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """合并连续相同 role 的消息，system 消息不合并。"""
    result: List[Dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        rd = msg.get("reasoning_details")

        if result and result[-1]["role"] == role and role in ("user", "assistant"):
            result[-1]["content"] = _merge_content(result[-1]["content"], content)
            # 若后续消息携带 reasoning_details，覆盖（取最新的）
            if rd:
                result[-1]["reasoning_details"] = rd
        else:
            new_msg: Dict[str, Any] = {"role": role, "content": content}
            if rd:
                new_msg["reasoning_details"] = rd
            result.append(new_msg)
    return result


def _merge_content(a: Any, b: Any) -> Any:
    """合并两个 message content，保留多模态 part。"""
    if isinstance(a, str) and isinstance(b, str):
        return f"{a}\n\n{b}"

    parts = _content_to_parts(a) + _content_to_parts(b)
    merged_parts = _merge_adjacent_text_parts(parts)

    if all(_is_text_part(part) for part in merged_parts):
        return "\n\n".join(part.get("text", "") for part in merged_parts)
    return merged_parts


def _content_to_parts(content: Any) -> List[Any]:
    """将字符串或多模态内容转成有序 part 列表。"""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]

    if isinstance(content, list):
        return list(content)

    return []


def _is_text_part(part: Any) -> bool:
    return isinstance(part, dict) and part.get("type") == "text"


def _merge_adjacent_text_parts(parts: List[Any]) -> List[Any]:
    """只合并相邻 text，保持图片和其他 part 的原始相对顺序。"""
    result: List[Any] = []
    for part in parts:
        if _is_text_part(part) and not part.get("text", ""):
            continue
        if _is_text_part(part) and result and _is_text_part(result[-1]):
            previous = result[-1]
            merged = dict(previous)
            merged["text"] = f"{previous.get('text', '')}\n\n{part.get('text', '')}"
            for key, value in part.items():
                if key not in {"type", "text"}:
                    merged.setdefault(key, value)
            result[-1] = merged
        else:
            result.append(part)
    return result
