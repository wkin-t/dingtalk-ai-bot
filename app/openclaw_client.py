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
import base64
import uuid
from typing import List, Dict, AsyncGenerator, Optional
import aiohttp
import websockets
from app.config import (
    OPENCLAW_HTTP_URL,
    OPENCLAW_GATEWAY_TOKEN,
    OPENCLAW_GATEWAY_TRANSPORT,
    OPENCLAW_GATEWAY_WS_URL,
    get_agent_for_conversation,
)


PROTOCOL_VERSION = 3


def _derive_ws_url(http_url: str) -> str:
    """
    Best-effort derive a Gateway WS URL from the OpenAI-compatible HTTP endpoint.
    The gateway accepts WS upgrades on any path, but "/ws" is commonly used behind reverse proxies.
    """
    raw = (http_url or "").strip()
    if not raw:
        return ""
    # http(s) -> ws(s)
    if raw.startswith("https://"):
        base = "wss://" + raw[len("https://"):]
    elif raw.startswith("http://"):
        base = "ws://" + raw[len("http://"):]
    else:
        base = raw

    # Strip OpenAI-compatible path if present.
    base = base.replace("/v1/chat/completions", "")
    if base.endswith("/"):
        base = base[:-1]
    return base + "/ws"


async def _ws_wait_for_response(ws, req_id: str, timeout_s: float = 30.0) -> dict:
    """Wait until we receive a response frame with matching id."""
    deadline = time.time() + timeout_s
    while True:
        remaining = max(0.1, deadline - time.time())
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        obj = json.loads(raw)
        if obj.get("type") == "res" and obj.get("id") == req_id:
            return obj


async def _ws_wait_for_challenge(ws, timeout_s: float = 10.0) -> str:
    """Wait for connect.challenge and return nonce (if present)."""
    deadline = time.time() + timeout_s
    while True:
        remaining = max(0.1, deadline - time.time())
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        obj = json.loads(raw)
        if obj.get("type") == "event" and obj.get("event") == "connect.challenge":
            payload = obj.get("payload") or {}
            nonce = payload.get("nonce")
            return nonce if isinstance(nonce, str) else ""


async def call_openclaw_ws_chat_stream(
    *,
    message: str,
    conversation_id: str,
    sender_id: str,
    sender_nick: str = "User",
    image_data_list: Optional[List[bytes]] = None,
    timeout_s: float = 300.0,
) -> AsyncGenerator[Dict, None]:
    """
    Call OpenClaw Gateway WebSocket protocol via chat.send.

    This is closer to official channel plugins:
    - Gateway manages session memory/transcript by sessionKey
    - Supports image attachments (base64) via chat.send params.attachments

    Notes:
    - chat.send attachments currently only accept images (audio/file should use tools-invoke first).
    - We embed the agent route into sessionKey as: agent:{agentId}:{rest}
    """
    agent_id = get_agent_for_conversation(conversation_id)
    if agent_id is None:
        error_msg = (
            f"❌ 群未绑定 AI Agent\n\n"
            f"当前 conversation_id: {conversation_id}\n\n"
            f"请在环境变量中配置 OPENCLAW_GROUP_AGENT_MAPPING\n\n"
            f"配置示例:\n"
            f'{{"cid_xxx":"agent-1","cid_yyy":"agent-2"}}\n\n'
            f"详见部署文档或联系管理员"
        )
        yield {"error": error_msg}
        return

    # Stable session key for gateway-managed transcripts.
    rest_key = f"dingtalk:{conversation_id}:{sender_id}"
    session_key = f"agent:{agent_id}:{rest_key}"

    ws_url = OPENCLAW_GATEWAY_WS_URL or _derive_ws_url(OPENCLAW_HTTP_URL)
    if not ws_url:
        yield {"error": "OpenClaw WS 未配置：请设置 OPENCLAW_GATEWAY_WS_URL 或 OPENCLAW_GATEWAY_URL"}
        return

    # Build chat.send attachments (images only)
    attachments = []
    if image_data_list:
        for idx, img in enumerate(image_data_list[:3], start=1):
            b64 = base64.b64encode(img).decode("utf-8")
            attachments.append({
                "type": "image",
                "mimeType": "image/jpeg",
                "fileName": f"image_{idx}.jpg",
                "content": b64,
            })

    # Compose user message. Keep it simple; gateway will stamp timestamp internally.
    # Note: callers typically already prefix speaker labels if needed.
    user_text = (message or "").strip()

    run_id = f"dingtalk-{uuid.uuid4().hex}"
    connect_req_id = f"connect-{uuid.uuid4().hex}"
    send_req_id = f"send-{uuid.uuid4().hex}"

    last_text = ""
    start_time = time.time()

    try:
        async with websockets.connect(
            ws_url,
            max_size=20 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
        ) as ws:
            await _ws_wait_for_challenge(ws, timeout_s=10.0)

            connect_frame = {
                "type": "req",
                "id": connect_req_id,
                "method": "connect",
                "params": {
                    "minProtocol": PROTOCOL_VERSION,
                    "maxProtocol": PROTOCOL_VERSION,
                    "client": {
                        "id": "gateway-client",
                        "version": "dingtalk-ai-bot",
                        "platform": "python",
                        "mode": "backend",
                    },
                    "role": "operator",
                    "scopes": [],
                    "auth": {"token": OPENCLAW_GATEWAY_TOKEN},
                },
            }
            await ws.send(json.dumps(connect_frame, ensure_ascii=False))
            connect_res = await _ws_wait_for_response(ws, connect_req_id, timeout_s=15.0)
            if not connect_res.get("ok"):
                err = (connect_res.get("error") or {}).get("message") or "unknown connect error"
                yield {"error": f"OpenClaw WS connect failed: {err}"}
                return

            send_frame = {
                "type": "req",
                "id": send_req_id,
                "method": "chat.send",
                "params": {
                    "sessionKey": session_key,
                    "message": user_text,
                    "attachments": attachments if attachments else None,
                    "timeoutMs": int(timeout_s * 1000),
                    "idempotencyKey": run_id,
                    "deliver": False,
                },
            }
            # Remove None fields for strict schema (additionalProperties=false)
            send_frame["params"] = {k: v for k, v in send_frame["params"].items() if v is not None}
            await ws.send(json.dumps(send_frame, ensure_ascii=False))

            send_res = await _ws_wait_for_response(ws, send_req_id, timeout_s=15.0)
            if not send_res.get("ok"):
                err = (send_res.get("error") or {}).get("message") or "unknown send error"
                yield {"error": f"OpenClaw WS chat.send failed: {err}"}
                return

            # Stream events until final/error/aborted for our run_id.
            deadline = time.time() + timeout_s
            while True:
                remaining = max(0.1, deadline - time.time())
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                obj = json.loads(raw)
                if obj.get("type") != "event" or obj.get("event") != "chat":
                    continue
                payload = obj.get("payload") or {}
                if payload.get("runId") != run_id:
                    continue

                state = payload.get("state")
                if state in {"delta", "final"}:
                    msg = payload.get("message") or {}
                    content = msg.get("content") or []
                    # Gateway sends full accumulated text in each delta; compute incremental diff.
                    text = ""
                    if isinstance(content, list) and content:
                        first = content[0] or {}
                        text = first.get("text") if isinstance(first, dict) else ""
                    if not isinstance(text, str):
                        text = ""

                    if text.startswith(last_text):
                        delta = text[len(last_text):]
                    else:
                        # Fallback: treat as full replacement (avoid dropping content).
                        delta = text
                        last_text = ""
                    last_text = text

                    if delta:
                        yield {"content": delta}

                    if state == "final":
                        break

                elif state == "error":
                    yield {"error": payload.get("errorMessage") or "OpenClaw WS run error"}
                    break
                elif state == "aborted":
                    yield {"error": "OpenClaw WS run aborted"}
                    break

            latency_ms = int((time.time() - start_time) * 1000)
            yield {"usage": {"latency_ms": latency_ms, "transport": "ws"}}

    except asyncio.TimeoutError:
        yield {"error": "OpenClaw WS 请求超时"}
    except websockets.WebSocketException as e:
        yield {"error": f"OpenClaw WS Error: {e}"}
    except Exception as e:
        yield {"error": f"OpenClaw WS Error: {e}"}


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
    model: str = "openclaw",
    image_data_list: Optional[List[bytes]] = None,
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
    # WS transport is closer to official channel plugins and supports image attachments.
    if OPENCLAW_GATEWAY_TRANSPORT == "ws":
        # Prefer the last user content as message body (gateway manages transcript).
        last_user = ""
        for msg in reversed(messages or []):
            if msg.get("role") == "user":
                last_user = msg.get("content") or ""
                break
        if not isinstance(last_user, str):
            last_user = ""
        async for chunk in call_openclaw_ws_chat_stream(
            message=last_user,
            conversation_id=conversation_id,
            sender_id=sender_id,
            sender_nick=sender_nick,
            image_data_list=image_data_list,
        ):
            yield chunk
        return

    # 根据 conversation_id 动态选择 agent (HTTP/OpenAI-compatible path)
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

    # 兼容逻辑：如果没有指定特定模型，使用 openclaw:{agent_id} 格式
    # 这有助于在不传递 header 的场景下也能路由（作为 fallback），
    # 但我们下面会显式传递 x-openclaw-agent-id header。
    if model and model not in {"openclaw", "default"}:
        request_model = model
    else:
        request_model = f"openclaw:{agent_id}"

    request_body = {
        "model": request_model,
        "messages": messages,
        "stream": True,
        # 给 Gateway 一个稳定的 user，有助于会话粘性（同一群/同一用户）。
        "user": f"dingtalk:{conversation_id}:{sender_id}",
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}",
        "x-openclaw-agent-id": agent_id,  # 显式 Header 路由
    }

    # 解析状态
    state = {
        "model": request_model,
        "input_tokens": 0,
        "output_tokens": 0,
        "content_len": 0,
        "thinking_len": 0,
    }

    try:
        # 不走代理 (OpenClaw 是内网服务)
        connector = aiohttp.TCPConnector(force_close=True, enable_cleanup_closed=True)
        timeout = aiohttp.ClientTimeout(total=300, sock_read=300, sock_connect=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.post(
                OPENCLAW_HTTP_URL,
                json=request_body,
                headers=headers,
                proxy=None,
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    print(f"❌ OpenClaw HTTP 错误 ({resp.status}): {error_text[:500]}")
                    yield {"error": f"OpenClaw HTTP Error ({resp.status}): {error_text[:200]}"}
                    return

                # 逐行读取 SSE 流 (readline 保证行完整性)
                # 说明：某些网络/代理/中间件可能导致连接提前断开，aiohttp 会抛 TransferEncodingError。
                try:
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
                except aiohttp.ClientPayloadError as e:
                    # 常见报错：Response payload is not completed / TransferEncodingError
                    print(f"⚠️ OpenClaw HTTP SSE payload 未完整（可能连接被中断）：{e}")
                    # 尽量把已生成的内容交给上游；不要在这里直接当作失败终止。
                    yield {
                        "error": "OpenClaw 流式连接中断（payload 未完整）。如频繁出现，请检查 WAF/反代/HTTP2 设置。"
                    }
                    return

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
