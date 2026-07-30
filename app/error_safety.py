# -*- coding: utf-8 -*-
"""跨后端共享的异常摘要与显示编码工具。

该模块不能依赖任何具体 AI client 或业务层，避免 `gemini_client -> app.ai ->
handler -> gemini_client` 的导入环。异常详情采用允许列表：未知响应正文永远不会
进入日志、usage、卡片或错误 chunk。
"""

import asyncio
import html
import unicodedata
from typing import Any

from google.genai import errors as genai_errors


_SAFE_ERROR_PHASES = {
    "provider": "provider",
    "stream": "stream",
    "analysis": "analysis",
    "soul": "soul",
    "fallback": "fallback",
}

# 这些短语是 provider 常见的稳定故障类别；只返回代码中的规范文本，
# 不返回 SDK message 中其余的任意响应正文。
_SAFE_REASON_PATTERNS = (
    ("no available gemini accounts", "no available Gemini accounts"),
    ("no available accounts", "no available accounts"),
    ("quota exceeded", "quota exceeded"),
    ("rate limit", "rate limit"),
    ("too many requests", "too many requests"),
    ("resource exhausted", "resource exhausted"),
    ("service unavailable", "service unavailable"),
    ("temporarily unavailable", "temporarily unavailable"),
    ("internal server error", "internal server error"),
    ("deadline exceeded", "deadline exceeded"),
    ("unauthenticated", "unauthenticated"),
    ("permission denied", "permission denied"),
    ("forbidden", "forbidden"),
    ("not found", "not found"),
    ("invalid argument", "invalid argument"),
    ("bad request", "bad request"),
    ("failed precondition", "failed precondition"),
    ("overloaded", "overloaded"),
    ("connection reset", "connection reset"),
    ("connection refused", "connection refused"),
    ("network", "network error"),
    ("timeout", "timeout"),
    ("safety", "safety filter"),
    ("blocked", "blocked"),
)


def _safe_reason(message: Any) -> str:
    """从已知 SDK message 中提取固定短语，不回传原始 message。"""
    if not isinstance(message, str):
        return ""
    lowered = message.casefold()
    for needle, display in _SAFE_REASON_PATTERNS:
        if needle in lowered:
            return display
    return ""


def safe_error_summary(
    error: BaseException,
    phase: str = "provider",
) -> str:
    """生成允许列表式摘要，未知异常原文不会进入任何输出 sink。

    摘要本身不依赖黑名单清洗来保证安全。运行时环境中的 secret 不会被拼入返回值。
    """
    phase_name = _SAFE_ERROR_PHASES.get(phase, "provider")

    if isinstance(error, asyncio.CancelledError):
        return f"Gemini {phase_name} canceled"

    category = "Gemini provider error"
    status_code = None
    reason = ""

    if isinstance(error, genai_errors.ServerError):
        category = "Gemini server error"
        status_code = getattr(error, "code", None)
        reason = _safe_reason(getattr(error, "message", None))
    elif isinstance(error, genai_errors.ClientError):
        category = "Gemini client error"
        status_code = getattr(error, "code", None)
        reason = _safe_reason(getattr(error, "message", None))
    elif isinstance(error, genai_errors.APIError):
        category = "Gemini API error"
        status_code = getattr(error, "code", None)
        reason = _safe_reason(getattr(error, "message", None))
    elif isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        category = "Gemini timeout"
    elif isinstance(error, (ConnectionError, OSError)):
        category = "Gemini network error"

    if isinstance(status_code, bool):
        status_code = None
    if isinstance(status_code, int) and 100 <= status_code <= 599:
        category = f"{category} HTTP {status_code}"

    summary = category
    if reason:
        summary = f"{summary}: {reason}"
    return summary[:1000]


def safe_display_text(value: Any, max_length: int = 240, keep_newlines: bool = False) -> str:
    """编码显示文本；Markdown 错误卡片可保留换行，状态栏默认单行。"""
    if not isinstance(value, str):
        return ""
    if keep_newlines:
        value = value.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(
        "\n" if keep_newlines and char == "\n" else (
            " " if (ord(char) < 32 or 127 <= ord(char) <= 159 or unicodedata.category(char) == "Cf") else char
        )
        for char in value
    )
    if keep_newlines:
        text = "\n".join(" ".join(line.split()) for line in text.split("\n"))
    else:
        text = " ".join(text.split())
    return html.escape(text, quote=False)[:max_length]


def safe_model_name(value: Any, max_length: int = 160) -> str:
    """清洗模型标识，供纯文本错误卡片和日志使用。"""
    if not isinstance(value, str):
        return "unknown"
    text = "".join(
        char if (char.isalnum() or char in "-_.:/") else "_"
        for char in value
        if unicodedata.category(char) != "Cf"
    )
    return text[:max_length] or "unknown"
