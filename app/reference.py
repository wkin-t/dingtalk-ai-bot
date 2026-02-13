# -*- coding: utf-8 -*-
"""
Heuristic history reference injection for DingTalk.

DingTalk does not provide a universal "quote previous message" API for bots.
We simulate "history reference" by injecting a short quote block into the
current user input when we detect reference intent.
"""

from typing import Dict, List, Optional, Tuple


_TRIGGERS = (
    "你刚才",
    "刚刚",
    "上条",
    "上一条",
    "前面",
    "之前",
    "继续",
    "那个",
    "这张",
    "这个文件",
)


def maybe_inject_reference(
    *,
    user_content: str,
    history: List[Dict],
    max_quote_len: int = 160,
) -> Tuple[str, Optional[str]]:
    """
    Returns (new_content, injected_quote or None).
    """
    text = (user_content or "").strip()
    if not text:
        return text, None

    if not any(t in text for t in _TRIGGERS):
        return text, None

    last_user = None
    for msg in reversed(history or []):
        if msg.get("role") == "user" and (msg.get("content") or "").strip():
            last_user = msg
            break
    if not last_user:
        return text, None

    quote = (last_user.get("content") or "").strip().replace("\n", " ")
    if len(quote) > max_quote_len:
        quote = quote[: max(0, max_quote_len - 3)] + "..."

    injected = f"[引用] {quote}"
    new_text = f"{injected}\n\n{text}"
    return new_text, injected

