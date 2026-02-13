# -*- coding: utf-8 -*-
"""
OpenClaw Tools Invoke HTTP API client.

This project uses OpenClaw as the orchestration layer. For non-chat actions
like ASR or file summarization, prefer calling tools-invoke API instead of
encoding behavior in chat prompts.
"""

import base64
import json
from typing import Any, Dict

import asyncio
import aiohttp


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


async def invoke_tool(
    *,
    tools_url: str,
    token: str,
    tool_name: str,
    arguments: Dict[str, Any],
    timeout_s: int = 120,
) -> Dict[str, Any]:
    """
    Invoke an OpenClaw tool via HTTP API.

    The exact request/response schema can vary by OpenClaw version, so this
    client uses a conservative payload and returns the raw JSON response.
    """
    if not tools_url:
        return {"error": "OPENCLAW_TOOLS_URL 未配置"}
    if not token:
        return {"error": "OPENCLAW_TOOLS_TOKEN 未配置"}
    if not tool_name:
        return {"error": "tool_name 为空"}

    payload = {
        "tool_name": tool_name,
        "arguments": arguments or {},
    }

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    connector = aiohttp.TCPConnector(force_close=True)
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(
                tools_url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
                proxy=None,
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    return {"error": f"tools invoke HTTP {resp.status}", "detail": text[:1000]}
                try:
                    return json.loads(text) if text else {}
                except json.JSONDecodeError:
                    return {"error": "tools invoke 返回非 JSON", "detail": text[:1000]}
    except asyncio.TimeoutError:
        return {"error": f"tools invoke 超时({timeout_s}s)"}
    except Exception as e:
        return {"error": f"tools invoke 异常: {e}"}


def build_asr_arguments(audio_bytes: bytes, filename: str = "audio") -> Dict[str, Any]:
    # Keep arguments generic; actual tool decides how to parse.
    return {"filename": filename, "audio_base64": _b64(audio_bytes)}


def build_file_arguments(file_bytes: bytes, filename: str = "file") -> Dict[str, Any]:
    return {"filename": filename, "file_base64": _b64(file_bytes)}
