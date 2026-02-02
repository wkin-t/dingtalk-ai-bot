# -*- coding: utf-8 -*-
"""
OpenClaw Gateway WebSocket 客户端
提供与 gemini_client.py 一致的流式接口
"""
import os
import json
import asyncio
import time
from typing import List, Dict, Any, Optional, AsyncGenerator
import websockets
from websockets.exceptions import WebSocketException
from app.config import OPENCLAW_GATEWAY_URL, OPENCLAW_GATEWAY_TOKEN, OPENCLAW_AGENT_ID


class OpenClawClient:
    """OpenClaw Gateway WebSocket 客户端 (单例模式)"""

    _instance = None
    _lock = asyncio.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.ws = None
        self.gateway_url = OPENCLAW_GATEWAY_URL
        self.token = OPENCLAW_GATEWAY_TOKEN
        self.agent_id = OPENCLAW_AGENT_ID
        self.request_id = 0
        self.pending_requests = {}  # {request_id: asyncio.Queue}
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 5
        self._initialized = True
        self._receive_task = None

    async def connect(self):
        """建立 WebSocket 连接并完成握手"""
        # 检查连接状态 (websockets 16.0 兼容)
        if self.ws:
            try:
                # 尝试 ping 来检查连接是否还活着
                await asyncio.wait_for(self.ws.ping(), timeout=1.0)
                return  # 连接正常,直接返回
            except Exception:
                # 连接已断开,继续重新连接
                self.ws = None

        try:
            print(f"🔗 正在连接 OpenClaw Gateway: {self.gateway_url}")
            self.ws = await websockets.connect(
                self.gateway_url,
                ping_interval=30,
                ping_timeout=10,
                proxy=None  # 禁用自动代理检测,OpenClaw Gateway 是内网服务
            )
            print(f"✅ WebSocket 已连接,正在执行握手...")

            # 发送 connect 握手请求 (OpenClaw Gateway 协议要求)
            self.request_id += 1
            connect_request = {
                "type": "req",
                "id": str(self.request_id),
                "method": "connect",
                "params": {
                    "minProtocol": 3,
                    "maxProtocol": 3,
                    "client": {
                        "id": "dingtalk-bot",
                        "version": "1.0.0",
                        "platform": "python",
                        "mode": "headless"
                    },
                    "role": "operator",
                    "scopes": []
                }
            }

            # 添加认证 token (如果配置了)
            if self.token:
                connect_request["params"]["auth"] = {"token": self.token}

            await self.ws.send(json.dumps(connect_request))

            # 等待 hello-ok 响应
            hello_response = await asyncio.wait_for(self.ws.recv(), timeout=5.0)
            response_data = json.loads(hello_response)

            if response_data.get("type") == "res" and response_data.get("ok"):
                protocol_version = response_data.get("payload", {}).get("protocol")
                print(f"✅ 握手成功 (协议版本: {protocol_version})")
            else:
                raise Exception(f"握手失败: {response_data}")

            # 启动接收任务
            if self._receive_task is None or self._receive_task.done():
                self._receive_task = asyncio.create_task(self._receive_messages())

            self.reconnect_attempts = 0
        except Exception as e:
            print(f"❌ 连接 OpenClaw Gateway 失败: {e}")
            raise

    async def _receive_messages(self):
        """后台接收消息任务"""
        try:
            async for message in self.ws:
                try:
                    data = json.loads(message)

                    # OpenClaw Gateway 响应格式: {"type": "res", "id": "...", "ok": true, "payload": {...}}
                    if "id" in data and str(data["id"]) in self.pending_requests:
                        request_id = str(data["id"])
                        queue = self.pending_requests[request_id]

                        # 转换 OpenClaw 格式到内部格式
                        if data.get("type") == "res":
                            if data.get("ok"):
                                # 成功响应
                                await queue.put({"result": data.get("payload", {})})
                            else:
                                # 错误响应
                                await queue.put({"error": data.get("error", {"message": "Unknown error"})})
                        else:
                            # 原始数据
                            await queue.put(data)

                    # 处理事件通知 (无 id 字段或流式事件)
                    elif data.get("type") == "event" or "method" in data:
                        # 流式事件分发到所有活跃请求
                        for queue in self.pending_requests.values():
                            await queue.put({"event": data})

                except json.JSONDecodeError as e:
                    print(f"⚠️ 解析 WebSocket 消息失败: {e}")
                except Exception as e:
                    print(f"⚠️ 处理 WebSocket 消息异常: {e}")

        except WebSocketException as e:
            print(f"⚠️ WebSocket 连接断开: {e}")
            await self._reconnect()
        except Exception as e:
            print(f"❌ 接收消息任务异常: {e}")

    async def _reconnect(self):
        """自动重连"""
        if self.reconnect_attempts >= self.max_reconnect_attempts:
            print(f"❌ 重连次数超过限制 ({self.max_reconnect_attempts}),放弃重连")
            return

        self.reconnect_attempts += 1
        wait_time = min(2 ** self.reconnect_attempts, 30)  # 指数退避,最多 30 秒
        print(f"🔄 {wait_time}秒后尝试第 {self.reconnect_attempts} 次重连...")
        await asyncio.sleep(wait_time)

        try:
            await self.connect()
        except Exception as e:
            print(f"⚠️ 重连失败: {e}")
            await self._reconnect()

    async def call_rpc(self, method: str, params: dict, stream: bool = False) -> AsyncGenerator[dict, None]:
        """
        调用 JSON-RPC 方法

        Args:
            method: RPC 方法名
            params: 参数
            stream: 是否流式返回

        Yields:
            RPC 响应或事件
        """
        await self.connect()

        self.request_id += 1
        request_id = str(self.request_id)  # 使用字符串ID

        # 创建响应队列
        response_queue = asyncio.Queue()
        self.pending_requests[request_id] = response_queue

        # 发送请求 (OpenClaw Gateway 协议格式)
        request = {
            "type": "req",
            "id": request_id,  # request_id 已经是字符串
            "method": method,
            "params": params
        }

        try:
            await self.ws.send(json.dumps(request))

            if stream:
                # 流式响应: 持续接收事件,直到收到结束标记
                while True:
                    try:
                        response = await asyncio.wait_for(response_queue.get(), timeout=60.0)

                        # 处理事件
                        if "event" in response:
                            yield response["event"]

                        # 处理最终响应
                        elif "result" in response:
                            yield response
                            break

                        # 处理错误
                        elif "error" in response:
                            yield response
                            break

                    except asyncio.TimeoutError:
                        print("⚠️ 等待响应超时")
                        yield {"error": {"code": -1, "message": "Response timeout"}}
                        break
            else:
                # 非流式: 等待单个响应
                response = await asyncio.wait_for(response_queue.get(), timeout=30.0)
                yield response

        finally:
            # 清理
            if request_id in self.pending_requests:
                del self.pending_requests[request_id]

    async def close(self):
        """关闭连接"""
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass

        if self.ws:
            await self.ws.close()
            self.ws = None
            print("✅ OpenClaw Gateway 连接已关闭")


# 全局客户端实例
_client = OpenClawClient()


async def call_openclaw_stream(
    messages: List[Dict],
    conversation_id: str,
    sender_id: str,
    sender_nick: str = "User"
) -> AsyncGenerator[Dict, None]:
    """
    调用 OpenClaw Gateway 进行流式对话

    Args:
        messages: OpenAI 格式的消息列表
        conversation_id: 会话 ID
        sender_id: 发送者 ID
        sender_nick: 发送者昵称

    Yields:
        {"content": "..."}  - 正常回复内容
        {"thinking": "..."}  - 思考内容 (如果启用)
        {"error": "..."}  - 错误信息
        {"usage": {...}}  - 使用统计
    """
    print(f"📡 正在请求 OpenClaw Gateway (conversation_id={conversation_id})...")

    start_time = time.time()
    input_tokens = 0
    output_tokens = 0
    full_content = ""

    try:
        # 提取最后一条用户消息
        user_message = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    user_message = content
                elif isinstance(content, list):
                    # 提取文本部分
                    for item in content:
                        if item.get("type") == "text":
                            user_message = item.get("text", "")
                            break
                break

        if not user_message:
            yield {"error": "未找到用户消息"}
            return

        # 调用 chat RPC
        params = {
            "agent_id": _client.agent_id,
            "session_id": conversation_id,
            "message": user_message,
            "sender_id": sender_id,
            "sender_name": sender_nick,
            "stream": True
        }

        async for response in _client.call_rpc("chat", params, stream=True):
            # 处理事件
            if "event" in response:
                event = response["event"]
                params = event.get("params", {})
                event_type = params.get("type")

                if event_type == "thinking":
                    # 思考内容
                    thinking_content = params.get("content", "")
                    if thinking_content:
                        yield {"thinking": thinking_content}

                elif event_type == "content":
                    # 正常回复内容
                    content = params.get("content", "")
                    if content:
                        full_content += content
                        yield {"content": content}

                elif event_type == "error":
                    # 错误事件
                    error_msg = params.get("message", "Unknown error")
                    yield {"error": error_msg}
                    return

            # 处理最终响应
            elif "result" in response:
                result = response["result"]
                # 提取 token 统计
                usage = result.get("usage", {})
                input_tokens = usage.get("input_tokens", 0)
                output_tokens = usage.get("output_tokens", 0)

            # 处理 RPC 错误
            elif "error" in response:
                error_info = response["error"]
                error_msg = error_info.get("message", "Unknown RPC error")
                yield {"error": f"OpenClaw RPC Error: {error_msg}"}
                return

        # 计算延迟
        latency_ms = int((time.time() - start_time) * 1000)
        print(f"✅ 流式响应结束 | 输入: {input_tokens} tokens, 输出: {output_tokens} tokens, 延迟: {latency_ms}ms")

        # 返回统计信息
        yield {
            "usage": {
                "model": f"openclaw-{_client.agent_id}",
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "latency_ms": latency_ms
            }
        }

    except Exception as e:
        error_msg = str(e)
        print(f"❌ OpenClaw API 错误: {error_msg}")
        yield {"error": f"OpenClaw API Error: {error_msg}"}


async def close_openclaw_client():
    """关闭 OpenClaw 客户端连接"""
    await _client.close()
