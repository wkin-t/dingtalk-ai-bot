# -*- coding: utf-8 -*-
"""
OpenClaw Gateway WebSocket 客户端
兼容 OpenClaw Gateway Protocol v3 (challenge-response 握手 + chat.send 流式)

协议流程:
1. 连接 WebSocket
2. 服务端发送 connect.challenge (含 nonce)
3. 客户端发送 connect 请求 (含 token 认证)
4. 服务端响应 hello-ok
5. 客户端发送 chat.send 请求
6. 通过 event:chat 事件接收流式内容
"""
import os
import json
import asyncio
import uuid
import time
from typing import List, Dict, AsyncGenerator
import websockets
from app.config import OPENCLAW_GATEWAY_URL, OPENCLAW_GATEWAY_TOKEN, OPENCLAW_AGENT_ID


# 代理环境变量列表 (OpenClaw Gateway 是内网服务，需临时移除)
_PROXY_VARS = [
    "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
    "ALL_PROXY", "all_proxy", "SOCKS_PROXY", "socks_proxy",
]


async def _create_connection(gateway_url: str, token: str) -> "websockets.WebSocketClientProtocol":
    """
    创建 WebSocket 连接并完成 Protocol v3 challenge-response 握手

    流程:
    1. Server → connect.challenge {nonce, ts}
    2. Client → connect {auth.token, client metadata}
    3. Server → hello-ok {protocol, features, snapshot}
    """
    env_backup = {}
    try:
        # 临时移除代理，避免内网连接走代理
        for var in _PROXY_VARS:
            if var in os.environ:
                env_backup[var] = os.environ[var]
                del os.environ[var]

        # 从 ws:// URL 构造 Origin 头 (Gateway 要求 Origin 校验)
        origin = gateway_url.replace("ws://", "http://").replace("wss://", "https://")

        print(f"🔗 正在连接 OpenClaw Gateway: {gateway_url}")
        ws = await websockets.connect(
            gateway_url,
            ping_interval=30,
            ping_timeout=10,
            additional_headers={"Origin": origin},
            proxy=None,
        )

        # Step 1: 等待 connect.challenge
        challenge_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        challenge = json.loads(challenge_raw)
        if challenge.get("event") != "connect.challenge":
            raise Exception(f"期望 connect.challenge，收到: {challenge}")
        print("✅ 收到 connect.challenge")

        # Step 2: 发送 connect 请求
        connect_req = {
            "type": "req",
            "id": "0",
            "method": "connect",
            "params": {
                "minProtocol": 3,
                "maxProtocol": 3,
                "client": {
                    "id": "openclaw-control-ui",
                    "version": "1.0.0",
                    "platform": "linux",
                    "mode": "webchat"
                },
                "role": "operator",
                "scopes": [],
                "auth": {"token": token}
            }
        }
        await ws.send(json.dumps(connect_req))

        # Step 3: 等待 hello-ok
        hello_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        hello = json.loads(hello_raw)
        if not (hello.get("type") == "res" and hello.get("ok")):
            error_msg = hello.get("error", {}).get("message", str(hello))
            raise Exception(f"握手失败: {error_msg}")

        protocol = hello.get("payload", {}).get("protocol")
        print(f"✅ 握手成功 (协议版本: {protocol})")
        return ws

    except Exception as e:
        print(f"❌ 连接 OpenClaw Gateway 失败: {e}")
        raise
    finally:
        for var, value in env_backup.items():
            os.environ[var] = value


async def call_openclaw_stream(
    messages: List[Dict],
    conversation_id: str,
    sender_id: str,
    sender_nick: str = "User"
) -> AsyncGenerator[Dict, None]:
    """
    调用 OpenClaw Gateway 进行流式对话

    每次请求创建独立 WebSocket 连接，完成后关闭。
    避免持久连接的事件路由复杂性，对话级别的延迟开销可忽略。

    Args:
        messages: OpenAI 格式的消息列表
        conversation_id: 会话 ID
        sender_id: 发送者 ID
        sender_nick: 发送者昵称

    Yields:
        {"content": "..."}   - 正式回复内容 (增量文本)
        {"thinking": "..."}  - 思考内容 (增量文本)
        {"error": "..."}     - 错误信息
        {"usage": {...}}     - 使用统计
    """
    print(f"📡 正在请求 OpenClaw Gateway (conversation_id={conversation_id})...")

    start_time = time.time()
    ws = None

    try:
        # 提取最后一条用户消息
        user_message = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    user_message = content
                elif isinstance(content, list):
                    for item in content:
                        if item.get("type") == "text":
                            user_message = item.get("text", "")
                            break
                break

        if not user_message:
            yield {"error": "未找到用户消息"}
            return

        # 建立连接
        ws = await _create_connection(OPENCLAW_GATEWAY_URL, OPENCLAW_GATEWAY_TOKEN)

        # 构造 sessionKey: agent:<agentId>:<conversationId>
        agent_id = OPENCLAW_AGENT_ID or "main"
        session_key = f"agent:{agent_id}:{conversation_id}"

        # 发送 chat.send 请求
        chat_req = {
            "type": "req",
            "id": "1",
            "method": "chat.send",
            "params": {
                "sessionKey": session_key,
                "message": user_message,
                "idempotencyKey": str(uuid.uuid4())
            }
        }
        await ws.send(json.dumps(chat_req))
        print(f"🔄 已发送 chat.send (sessionKey={session_key})")

        # 读取流式响应
        # 策略: 锁定第一个产生文本内容的 runId，忽略其他 run 的事件
        active_run_id = None    # 正在追踪的 runId
        last_text = ""          # 已累积的文本 (用于计算增量)
        last_thinking = ""      # 已累积的思考文本
        got_content = False     # 是否已收到过文本内容

        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=120.0)
            except asyncio.TimeoutError:
                print("⚠️ 等待 OpenClaw 响应超时 (120s)")
                yield {"error": "响应超时"}
                return

            data = json.loads(raw)
            msg_type = data.get("type", "")

            # 处理 RPC 响应 (chat.send 的确认)
            if msg_type == "res" and data.get("id") == "1":
                if not data.get("ok"):
                    error = data.get("error", {})
                    error_msg = error.get("message", str(error))
                    print(f"❌ chat.send 失败: {error_msg}")
                    yield {"error": f"OpenClaw Error: {error_msg}"}
                    return
                status = data.get("payload", {}).get("status")
                print(f"✅ chat.send 已接受 (status={status})")
                continue

            # 处理 chat 事件
            if msg_type == "event" and data.get("event") == "chat":
                params = data.get("params", {})

                # 只处理匹配 sessionKey 的事件
                if params.get("sessionKey") != session_key:
                    continue

                state = params.get("state", "")
                run_id = params.get("runId", "")
                message_data = params.get("message", {})
                content_parts = message_data.get("content", [])

                # 跳过没有消息内容的事件 (如初始 run 的路由确认)
                if not content_parts:
                    continue

                # 锁定第一个产生内容的 run
                if active_run_id is None:
                    active_run_id = run_id
                    print(f"🎯 锁定内容 runId: {run_id}")

                # 只处理锁定的 run 的事件
                if run_id != active_run_id:
                    continue

                # 解析内容 (content_parts 是累积式的，需要计算增量)
                for part in content_parts:
                    part_type = part.get("type", "")
                    text = part.get("text", "")

                    if part_type == "text" and text:
                        # 计算增量: 累积文本 - 已发送文本
                        if len(text) > len(last_text):
                            delta = text[len(last_text):]
                            yield {"content": delta}
                            last_text = text
                            got_content = True

                    elif part_type == "thinking" and text:
                        # 思考内容也是累积式的
                        if len(text) > len(last_thinking):
                            delta = text[len(last_thinking):]
                            yield {"thinking": delta}
                            last_thinking = text

                # state=final 且已有内容 → 本轮对话结束
                if state == "final" and got_content:
                    break

            # 忽略其他事件类型 (health, presence, tick 等)

        # 输出统计
        latency_ms = int((time.time() - start_time) * 1000)
        print(f"✅ OpenClaw 流式响应结束 | 延迟: {latency_ms}ms, 内容长度: {len(last_text)}")

        yield {
            "usage": {
                "model": f"openclaw-{agent_id}",
                "input_tokens": 0,
                "output_tokens": 0,
                "latency_ms": latency_ms
            }
        }

    except Exception as e:
        error_msg = str(e)
        print(f"❌ OpenClaw API 错误: {error_msg}")
        yield {"error": f"OpenClaw API Error: {error_msg}"}

    finally:
        if ws:
            try:
                await ws.close()
            except Exception:
                pass


async def close_openclaw_client():
    """关闭 OpenClaw 客户端连接 (兼容旧接口，当前为空操作)"""
    pass
