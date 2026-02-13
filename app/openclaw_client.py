# -*- coding: utf-8 -*-
"""
OpenClaw Gateway HTTP 客户端
使用 OpenAI 兼容的 /v1/chat/completions 端点 (SSE 流式)

端点: http://172.17.0.1:48789/v1/chat/completions (经过 Safeline WAF)
认证: Authorization: Bearer <gateway token>
格式: 标准 OpenAI SDK 格式

支持多 Agent 路由：
- 根据 conversation_id 动态选择 agent
- 配置在 OPENCLAW_GROUP_AGENT_MAPPING 环境变量中
"""
import asyncio
import json
import time
from typing import List, Dict, AsyncGenerator
import aiohttp
from app.config import OPENCLAW_HTTP_URL, OPENCLAW_GATEWAY_TOKEN, get_agent_for_conversation


def _parse_sse_delta(data: dict, state: dict) -> List[Dict]:
    """
    解析单个 SSE data JSON，提取增量内容

    Args:
        data: 解析后的 JSON 对象
        state: 可变状态字典 (model, input_tokens, output_tokens)

    Returns:
        要 yield 的 chunk 列表
    """
    chunks = []

    if "model" in data:
        state["model"] = data["model"]

    if "usage" in data and data["usage"]:
        usage = data["usage"]
        state["input_tokens"] = usage.get("prompt_tokens", 0)
        state["output_tokens"] = usage.get("completion_tokens", 0)

    choices = data.get("choices", [])
    if not choices:
        return chunks

    delta = choices[0].get("delta", {})

    # 思考内容 (reasoning_content 或 thinking)
    thinking_delta = delta.get("reasoning_content") or delta.get("thinking") or ""
    if thinking_delta:
        state["thinking_len"] += len(thinking_delta)
        chunks.append({"thinking": thinking_delta})

    # 正式回复内容
    content_delta = delta.get("content") or ""
    if content_delta:
        state["content_len"] += len(content_delta)
        chunks.append({"content": content_delta})

    return chunks


async def call_openclaw_stream(
    messages: List[Dict],
    conversation_id: str,
    sender_id: str,
    sender_nick: str = "User",
    model: str = "openclaw"
) -> AsyncGenerator[Dict, None]:
    """
    调用 OpenClaw Gateway HTTP API 进行流式对话

    使用 OpenAI 兼容的 /v1/chat/completions 端点，SSE 流式返回。

    Args:
        messages: OpenAI 格式的消息列表
        conversation_id: 会话 ID（用于路由到不同 agent）
        sender_id: 发送者 ID
        sender_nick: 发送者昵称
        model: 模型建议 (Gateway 可自行决定是否接受)

    Yields:
        {"content": "..."}   - 正式回复内容 (增量文本)
        {"thinking": "..."}  - 思考内容 (增量文本)
        {"error": "..."}     - 错误信息
        {"usage": {...}}     - 使用统计
    """
    # 根据 conversation_id 动态选择 agent
    agent_id = get_agent_for_conversation(conversation_id)

    # 严格路由模式：未配置的群返回错误提示
    if agent_id is None:
        error_msg = (
            f"❌ 群未绑定 AI Agent\n\n"
            f"当前 conversation_id: {conversation_id}\n\n"
            f"请在环境变量中配置 OPENCLAW_GROUP_AGENT_MAPPING\n\n"
            f"配置示例:\n"
            f'{{"cid_xxx":"agent-1","cid_yyy":"agent-2"}}\n\n'
            f"详见部署文档或联系管理员"
        )
        print(f"🚫 {error_msg}")
        yield {"error": error_msg}
        return

    print(f"📡 正在请求 OpenClaw HTTP API (conversation_id={conversation_id}, agent={agent_id})...")

    start_time = time.time()

    request_body = {
        "agent": agent_id,  # 动态 agent 路由
        "model": model,
        "messages": messages,
        "stream": True,
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}",
    }

    # 解析状态
    state = {
        "model": f"openclaw-{agent_id}",
        "input_tokens": 0,
        "output_tokens": 0,
        "content_len": 0,
        "thinking_len": 0,
    }

    try:
        # 不走代理 (OpenClaw 是内网服务)
        connector = aiohttp.TCPConnector(force_close=True)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(
                OPENCLAW_HTTP_URL,
                json=request_body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=180),
                proxy=None,
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    print(f"❌ OpenClaw HTTP 错误 ({resp.status}): {error_text[:500]}")
                    yield {"error": f"OpenClaw HTTP Error ({resp.status}): {error_text[:200]}"}
                    return

                # 逐行读取 SSE 流 (readline 保证行完整性)
                while True:
                    line_bytes = await resp.content.readline()
                    if not line_bytes:
                        break

                    line = line_bytes.decode("utf-8", errors="replace").strip()

                    if not line or line.startswith(":"):
                        continue

                    if not line.startswith("data: "):
                        continue

                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break

                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    for chunk in _parse_sse_delta(data, state):
                        yield chunk

        # 输出统计
        latency_ms = int((time.time() - start_time) * 1000)
        print(f"✅ OpenClaw HTTP 流式响应结束 | 延迟: {latency_ms}ms, 内容长度: {state['content_len']}")

        yield {
            "usage": {
                "model": state["model"],
                "input_tokens": state["input_tokens"],
                "output_tokens": state["output_tokens"],
                "latency_ms": latency_ms
            }
        }

    except aiohttp.ClientError as e:
        print(f"❌ OpenClaw HTTP 连接错误: {e}")
        yield {"error": f"OpenClaw HTTP Error: {e}"}

    except asyncio.TimeoutError:
        print("⚠️ OpenClaw HTTP 请求超时 (180s)")
        yield {"error": "OpenClaw HTTP 请求超时"}

    except Exception as e:
        print(f"❌ OpenClaw API 错误: {e}")
        yield {"error": f"OpenClaw API Error: {e}"}


async def close_openclaw_client():
    """关闭 OpenClaw 客户端连接 (兼容旧接口，HTTP 模式无需清理)"""
    pass
