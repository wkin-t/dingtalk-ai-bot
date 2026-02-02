# -*- coding: utf-8 -*-
"""
OpenClaw 客户端单元测试

测试目标:
1. 验证 WebSocket 连接时正确传递 proxy=None 参数 (回归测试)
2. 验证单例模式正确性
3. 验证重连逻辑
4. 验证 RPC 调用格式
"""
import pytest
import asyncio
import json
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from app.openclaw_client import OpenClawClient, call_openclaw_stream


class TestOpenClawClient:
    """OpenClaw 客户端测试套件"""

    @pytest.fixture
    def client(self):
        """创建测试客户端实例"""
        # 重置单例状态以确保测试隔离
        OpenClawClient._instance = None
        client = OpenClawClient()
        yield client
        # 清理
        if client.ws:
            asyncio.run(client.close())

    @pytest.mark.asyncio
    async def test_websocket_connect_with_proxy_none(self, client):
        """
        回归测试: 验证 WebSocket 连接时正确传递 proxy=None 参数

        问题背景: websockets 15.0+ 在存在代理环境变量时会自动检测代理,
        导致 additional_headers 参数被错误传递给底层 asyncio API

        预期: websockets.connect() 调用时必须包含 proxy=None 参数
        """
        mock_websocket = AsyncMock()
        mock_websocket.open = True

        with patch("app.openclaw_client.websockets.connect", new_callable=AsyncMock) as mock_connect:
            mock_connect.return_value = mock_websocket

            # 执行连接
            await client.connect()

            # 验证: 必须调用了 websockets.connect
            assert mock_connect.called, "websockets.connect 未被调用"

            # 验证: 第一个参数是 gateway_url
            call_args = mock_connect.call_args
            assert call_args[0][0] == client.gateway_url, "gateway_url 参数不正确"

            # 验证: 关键参数 - proxy=None 必须存在
            assert "proxy" in call_args[1], "缺少 proxy 参数"
            assert call_args[1]["proxy"] is None, "proxy 参数必须为 None"

            # 验证: 其他必要参数也存在
            assert "additional_headers" in call_args[1], "缺少 additional_headers 参数"
            assert "ping_interval" in call_args[1], "缺少 ping_interval 参数"
            assert "ping_timeout" in call_args[1], "缺少 ping_timeout 参数"

    @pytest.mark.asyncio
    async def test_websocket_connect_with_authorization_header(self, client):
        """验证连接时正确设置 Authorization 头"""
        mock_websocket = AsyncMock()
        mock_websocket.open = True

        with patch("app.openclaw_client.websockets.connect", new_callable=AsyncMock) as mock_connect:
            mock_connect.return_value = mock_websocket

            # 设置 token
            client.token = "test-token-123"

            await client.connect()

            # 验证 Authorization 头
            call_args = mock_connect.call_args
            headers = call_args[1]["additional_headers"]
            assert "Authorization" in headers, "缺少 Authorization 头"
            assert headers["Authorization"] == "Bearer test-token-123", "Authorization 头格式不正确"

    @pytest.mark.asyncio
    async def test_singleton_pattern(self):
        """验证单例模式: 多次创建返回同一实例"""
        OpenClawClient._instance = None

        client1 = OpenClawClient()
        client2 = OpenClawClient()

        assert client1 is client2, "单例模式失效: 创建了多个实例"

    @pytest.mark.asyncio
    async def test_rpc_call_format(self, client):
        """验证 JSON-RPC 调用格式正确性"""
        mock_websocket = AsyncMock()
        mock_websocket.open = True
        mock_websocket.send = AsyncMock()

        # 模拟响应队列
        response_queue = asyncio.Queue()
        await response_queue.put({"jsonrpc": "2.0", "id": 1, "result": {"success": True}})

        with patch("app.openclaw_client.websockets.connect", new_callable=AsyncMock) as mock_connect:
            mock_connect.return_value = mock_websocket
            await client.connect()

            # 创建 RPC 调用
            client.pending_requests[1] = response_queue

            # 发送请求
            method = "chat"
            params = {"message": "test", "agent_id": "test-agent"}

            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": params
            }

            await client.ws.send(json.dumps(request))

            # 验证发送的数据
            sent_data = json.loads(mock_websocket.send.call_args[0][0])
            assert sent_data["jsonrpc"] == "2.0", "JSON-RPC 版本不正确"
            assert sent_data["id"] == 1, "请求 ID 不正确"
            assert sent_data["method"] == method, "方法名不正确"
            assert sent_data["params"] == params, "参数不正确"

    @pytest.mark.asyncio
    async def test_reconnect_logic(self, client):
        """验证重连逻辑: 连接失败时触发重连"""
        mock_websocket = AsyncMock()
        mock_websocket.open = True

        # 第一次连接失败,第二次成功
        connect_attempts = 0

        async def mock_connect_side_effect(*args, **kwargs):
            nonlocal connect_attempts
            connect_attempts += 1
            if connect_attempts == 1:
                raise ConnectionError("模拟连接失败")
            return mock_websocket

        with patch("app.openclaw_client.websockets.connect", side_effect=mock_connect_side_effect):
            with patch("asyncio.sleep", new_callable=AsyncMock):  # 跳过等待时间
                try:
                    await client.connect()
                except ConnectionError:
                    # 第一次连接应该失败
                    assert connect_attempts == 1, "第一次连接应该失败"

                    # 触发重连
                    await client._reconnect()

                    # 第二次应该成功
                    assert client.ws is not None, "重连后应该有 WebSocket 实例"
                    assert connect_attempts == 2, "应该尝试了两次连接"


class TestCallOpenClawStream:
    """call_openclaw_stream 函数测试套件"""

    @pytest.mark.asyncio
    async def test_extract_user_message_from_string(self):
        """验证从字符串格式的消息中提取用户输入"""
        messages = [
            {"role": "system", "content": "You are a helpful assistant"},
            {"role": "user", "content": "Hello, world!"}
        ]

        # 模拟客户端和流式响应
        mock_client = Mock()
        mock_client.agent_id = "test-agent"

        async def mock_rpc_call(*args, **kwargs):
            yield {"result": {"usage": {"input_tokens": 10, "output_tokens": 20}}}

        with patch("app.openclaw_client._client", mock_client):
            with patch.object(mock_client, "call_rpc", side_effect=mock_rpc_call):
                # 收集所有响应
                responses = []
                async for response in call_openclaw_stream(
                    messages=messages,
                    conversation_id="test-conv",
                    sender_id="test-sender",
                    sender_nick="TestUser"
                ):
                    responses.append(response)

                # 验证至少有一个响应
                assert len(responses) > 0, "应该有响应返回"

    @pytest.mark.asyncio
    async def test_extract_user_message_from_list(self):
        """验证从列表格式的消息中提取用户输入 (多模态消息)"""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "请描述这张图片"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/image.jpg"}}
                ]
            }
        ]

        mock_client = Mock()
        mock_client.agent_id = "test-agent"

        async def mock_rpc_call(method, params, stream=False):
            # 验证提取的消息正确
            assert params["message"] == "请描述这张图片", "应该提取文本部分"
            yield {"result": {"usage": {"input_tokens": 10, "output_tokens": 20}}}

        with patch("app.openclaw_client._client", mock_client):
            with patch.object(mock_client, "call_rpc", side_effect=mock_rpc_call):
                responses = []
                async for response in call_openclaw_stream(
                    messages=messages,
                    conversation_id="test-conv",
                    sender_id="test-sender"
                ):
                    responses.append(response)

    @pytest.mark.asyncio
    async def test_handle_error_event(self):
        """验证错误事件处理"""
        messages = [{"role": "user", "content": "test"}]

        mock_client = Mock()
        mock_client.agent_id = "test-agent"

        async def mock_rpc_call(*args, **kwargs):
            # 模拟错误事件
            yield {
                "event": {
                    "params": {
                        "type": "error",
                        "message": "测试错误消息"
                    }
                }
            }

        with patch("app.openclaw_client._client", mock_client):
            with patch.object(mock_client, "call_rpc", side_effect=mock_rpc_call):
                responses = []
                async for response in call_openclaw_stream(
                    messages=messages,
                    conversation_id="test-conv",
                    sender_id="test-sender"
                ):
                    responses.append(response)

                # 验证返回了错误
                assert any("error" in r for r in responses), "应该返回错误响应"
                error_response = next(r for r in responses if "error" in r)
                assert "测试错误消息" in error_response["error"], "错误消息不正确"

    @pytest.mark.asyncio
    async def test_stream_content_events(self):
        """验证流式内容事件处理"""
        messages = [{"role": "user", "content": "你好"}]

        mock_client = Mock()
        mock_client.agent_id = "test-agent"

        async def mock_rpc_call(*args, **kwargs):
            # 模拟多个内容块
            yield {"event": {"params": {"type": "content", "content": "你"}}}
            yield {"event": {"params": {"type": "content", "content": "好"}}}
            yield {"event": {"params": {"type": "content", "content": "！"}}}
            yield {"result": {"usage": {"input_tokens": 5, "output_tokens": 3}}}

        with patch("app.openclaw_client._client", mock_client):
            with patch.object(mock_client, "call_rpc", side_effect=mock_rpc_call):
                responses = []
                async for response in call_openclaw_stream(
                    messages=messages,
                    conversation_id="test-conv",
                    sender_id="test-sender"
                ):
                    responses.append(response)

                # 验证收到了内容块
                content_responses = [r for r in responses if "content" in r]
                assert len(content_responses) == 3, "应该收到 3 个内容块"
                assert content_responses[0]["content"] == "你"
                assert content_responses[1]["content"] == "好"
                assert content_responses[2]["content"] == "！"

                # 验证收到了使用统计
                usage_responses = [r for r in responses if "usage" in r]
                assert len(usage_responses) == 1, "应该收到 1 个使用统计"
                assert usage_responses[0]["usage"]["input_tokens"] == 5
                assert usage_responses[0]["usage"]["output_tokens"] == 3

    @pytest.mark.asyncio
    async def test_thinking_events(self):
        """验证思考内容事件处理"""
        messages = [{"role": "user", "content": "计算 1+1"}]

        mock_client = Mock()
        mock_client.agent_id = "test-agent"

        async def mock_rpc_call(*args, **kwargs):
            # 模拟思考过程
            yield {"event": {"params": {"type": "thinking", "content": "让我计算一下..."}}}
            yield {"event": {"params": {"type": "content", "content": "答案是 2"}}}
            yield {"result": {"usage": {"input_tokens": 10, "output_tokens": 5}}}

        with patch("app.openclaw_client._client", mock_client):
            with patch.object(mock_client, "call_rpc", side_effect=mock_rpc_call):
                responses = []
                async for response in call_openclaw_stream(
                    messages=messages,
                    conversation_id="test-conv",
                    sender_id="test-sender"
                ):
                    responses.append(response)

                # 验证收到了思考内容
                thinking_responses = [r for r in responses if "thinking" in r]
                assert len(thinking_responses) == 1, "应该收到思考内容"
                assert "让我计算一下" in thinking_responses[0]["thinking"]

    @pytest.mark.asyncio
    async def test_rpc_error_handling(self):
        """验证 RPC 错误处理"""
        messages = [{"role": "user", "content": "test"}]

        mock_client = Mock()
        mock_client.agent_id = "test-agent"

        async def mock_rpc_call(*args, **kwargs):
            # 模拟 RPC 错误
            yield {"error": {"code": -32600, "message": "Invalid request"}}

        with patch("app.openclaw_client._client", mock_client):
            with patch.object(mock_client, "call_rpc", side_effect=mock_rpc_call):
                responses = []
                async for response in call_openclaw_stream(
                    messages=messages,
                    conversation_id="test-conv",
                    sender_id="test-sender"
                ):
                    responses.append(response)

                # 验证返回了 RPC 错误
                assert any("error" in r for r in responses), "应该返回错误响应"
                error_response = next(r for r in responses if "error" in r)
                assert "OpenClaw RPC Error" in error_response["error"]
                assert "Invalid request" in error_response["error"]

    @pytest.mark.asyncio
    async def test_no_user_message_error(self):
        """验证缺少用户消息时的错误处理"""
        messages = [
            {"role": "system", "content": "You are a helpful assistant"},
            {"role": "assistant", "content": "Hello!"}
        ]

        mock_client = Mock()
        mock_client.agent_id = "test-agent"

        with patch("app.openclaw_client._client", mock_client):
            responses = []
            async for response in call_openclaw_stream(
                messages=messages,
                conversation_id="test-conv",
                sender_id="test-sender"
            ):
                responses.append(response)

            # 验证返回了错误
            assert len(responses) == 1, "应该只有一个错误响应"
            assert "error" in responses[0], "应该返回错误"
            assert "未找到用户消息" in responses[0]["error"]

    @pytest.mark.asyncio
    async def test_exception_handling_in_stream(self):
        """验证流式处理中的异常处理"""
        messages = [{"role": "user", "content": "test"}]

        mock_client = Mock()
        mock_client.agent_id = "test-agent"

        async def mock_rpc_call(*args, **kwargs):
            # 模拟异常
            raise RuntimeError("模拟的运行时错误")
            yield  # 永远不会执行

        with patch("app.openclaw_client._client", mock_client):
            with patch.object(mock_client, "call_rpc", side_effect=mock_rpc_call):
                responses = []
                async for response in call_openclaw_stream(
                    messages=messages,
                    conversation_id="test-conv",
                    sender_id="test-sender"
                ):
                    responses.append(response)

                # 验证返回了异常错误
                assert len(responses) == 1, "应该有一个错误响应"
                assert "error" in responses[0], "应该返回错误"
                assert "OpenClaw API Error" in responses[0]["error"]
                assert "模拟的运行时错误" in responses[0]["error"]


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
