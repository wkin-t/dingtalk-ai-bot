# -*- coding: utf-8 -*-
"""
OpenClaw HTTP 客户端单元测试

测试目标:
1. _parse_sse_delta() 纯函数测试
2. call_openclaw_stream() SSE 流式响应测试 (mock aiohttp)
3. 错误处理 (HTTP 错误、超时、JSON 解析失败)
4. close_openclaw_client() 兼容接口
"""
import pytest
import asyncio
import json
from unittest.mock import patch, AsyncMock, MagicMock
from app.openclaw_client import _parse_sse_delta, call_openclaw_stream, close_openclaw_client


# ─── _parse_sse_delta 纯函数测试 ─────────────────────────────────

class TestParseSSEDelta:
    """_parse_sse_delta 纯函数测试"""

    def _make_state(self):
        return {
            "model": "openclaw-main",
            "input_tokens": 0,
            "output_tokens": 0,
            "content_len": 0,
            "thinking_len": 0,
        }

    def test_content_delta(self):
        """解析 content delta"""
        state = self._make_state()
        data = {
            "choices": [{"delta": {"content": "Hello"}}]
        }
        chunks = _parse_sse_delta(data, state)
        assert chunks == [{"content": "Hello"}]
        assert state["content_len"] == 5

    def test_thinking_via_reasoning_content(self):
        """解析 reasoning_content (Claude 格式)"""
        state = self._make_state()
        data = {
            "choices": [{"delta": {"reasoning_content": "让我想想..."}}]
        }
        chunks = _parse_sse_delta(data, state)
        assert chunks == [{"thinking": "让我想想..."}]
        assert state["thinking_len"] == len("让我想想...")

    def test_thinking_via_thinking_field(self):
        """解析 thinking 字段 (备用格式)"""
        state = self._make_state()
        data = {
            "choices": [{"delta": {"thinking": "分析中..."}}]
        }
        chunks = _parse_sse_delta(data, state)
        assert chunks == [{"thinking": "分析中..."}]

    def test_reasoning_content_priority_over_thinking(self):
        """reasoning_content 优先于 thinking"""
        state = self._make_state()
        data = {
            "choices": [{"delta": {"reasoning_content": "RC", "thinking": "TK"}}]
        }
        chunks = _parse_sse_delta(data, state)
        # reasoning_content 非空时优先
        assert len(chunks) == 1
        assert chunks[0] == {"thinking": "RC"}

    def test_both_thinking_and_content(self):
        """同时有思考和内容"""
        state = self._make_state()
        data = {
            "choices": [{"delta": {"reasoning_content": "思考", "content": "回复"}}]
        }
        chunks = _parse_sse_delta(data, state)
        assert len(chunks) == 2
        assert {"thinking": "思考"} in chunks
        assert {"content": "回复"} in chunks

    def test_empty_choices(self):
        """choices 为空"""
        state = self._make_state()
        data = {"choices": []}
        chunks = _parse_sse_delta(data, state)
        assert chunks == []

    def test_no_choices(self):
        """没有 choices 字段"""
        state = self._make_state()
        data = {"id": "chatcmpl_xxx"}
        chunks = _parse_sse_delta(data, state)
        assert chunks == []

    def test_empty_delta(self):
        """delta 为空 (如 role-only chunk)"""
        state = self._make_state()
        data = {
            "choices": [{"delta": {"role": "assistant"}}]
        }
        chunks = _parse_sse_delta(data, state)
        assert chunks == []

    def test_model_extraction(self):
        """从 data 中提取 model 名"""
        state = self._make_state()
        data = {
            "model": "claude-opus-4-6",
            "choices": [{"delta": {"content": "hi"}}]
        }
        _parse_sse_delta(data, state)
        assert state["model"] == "claude-opus-4-6"

    def test_usage_extraction(self):
        """提取 usage 统计"""
        state = self._make_state()
        data = {
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            "choices": []
        }
        _parse_sse_delta(data, state)
        assert state["input_tokens"] == 100
        assert state["output_tokens"] == 50

    def test_empty_content_ignored(self):
        """空字符串 content 不产生 chunk"""
        state = self._make_state()
        data = {
            "choices": [{"delta": {"content": ""}}]
        }
        chunks = _parse_sse_delta(data, state)
        assert chunks == []

    def test_content_len_accumulates(self):
        """content_len 正确累加"""
        state = self._make_state()
        _parse_sse_delta({"choices": [{"delta": {"content": "AB"}}]}, state)
        _parse_sse_delta({"choices": [{"delta": {"content": "CDE"}}]}, state)
        assert state["content_len"] == 5


# ─── mock 辅助 ─────────────────────────────────────────────────

def _make_sse_lines(events: list[str]) -> list[bytes]:
    """将 SSE data 字符串列表转为 readline() 返回的 bytes 列表"""
    lines = []
    for event in events:
        lines.append(f"data: {event}\n\n".encode("utf-8"))
    return lines


def _make_sse_chunk(delta: dict, model: str = "openclaw", **extra) -> str:
    """构造单个 SSE data JSON"""
    obj = {
        "id": "chatcmpl_test",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": delta}],
        **extra
    }
    return json.dumps(obj)


class MockStreamReader:
    """模拟 aiohttp StreamReader 的 readline()"""

    def __init__(self, lines: list[bytes]):
        self._lines = list(lines)
        self._index = 0

    async def readline(self):
        if self._index >= len(self._lines):
            return b""
        line = self._lines[self._index]
        self._index += 1
        return line


class MockResponse:
    """模拟 aiohttp 响应"""

    def __init__(self, status: int, lines: list[bytes] = None, text: str = ""):
        self.status = status
        self.content = MockStreamReader(lines or [])
        self._text = text

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class MockSession:
    """模拟 aiohttp.ClientSession"""

    def __init__(self, response: MockResponse):
        self._response = response
        self.post_kwargs = None

    def post(self, url, **kwargs):
        self.post_kwargs = {"url": url, **kwargs}
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


# ─── call_openclaw_stream 集成测试 ──────────────────────────────

class TestCallOpenClawStream:
    """call_openclaw_stream SSE 流式测试"""

    @pytest.fixture(autouse=True)
    def disable_strict_routing_for_existing_tests(self, request):
        """为现有测试禁用严格路由（新增的 strict routing 测试除外）"""
        if 'strict_group_routing' not in request.node.name and 'fallback_group_routing' not in request.node.name:
            # Config name changed upstream: OPENCLAW_STRICT_GROUP_ROUTING -> OPENCLAW_STRICT_ROUTING
            try:
                cm = patch("app.config.OPENCLAW_STRICT_ROUTING", False)
            except AttributeError:
                cm = patch("app.config.OPENCLAW_STRICT_GROUP_ROUTING", False, create=True)
            with cm:
                yield
        else:
            yield

    @pytest.mark.asyncio
    async def test_normal_stream_content(self):
        """正常流式内容"""
        lines = _make_sse_lines([
            _make_sse_chunk({"role": "assistant"}),
            _make_sse_chunk({"content": "你"}),
            _make_sse_chunk({"content": "好"}),
            _make_sse_chunk({"content": "！"}),
            "[DONE]",
        ])
        mock_resp = MockResponse(200, lines)
        mock_session = MockSession(mock_resp)

        with patch("app.openclaw_client.aiohttp.TCPConnector"):
            with patch("app.openclaw_client.aiohttp.ClientSession", return_value=mock_session):
                chunks = []
                async for chunk in call_openclaw_stream(
                    messages=[{"role": "user", "content": "你好"}],
                    conversation_id="conv-1",
                    sender_id="user-1",
                ):
                    chunks.append(chunk)

        content_chunks = [c for c in chunks if "content" in c]
        assert len(content_chunks) == 3
        assert content_chunks[0]["content"] == "你"
        assert content_chunks[1]["content"] == "好"
        assert content_chunks[2]["content"] == "！"

        # 最后应有 usage
        usage_chunks = [c for c in chunks if "usage" in c]
        assert len(usage_chunks) == 1
        assert "latency_ms" in usage_chunks[0]["usage"]

    @pytest.mark.asyncio
    async def test_stream_with_thinking(self):
        """流式带思考内容"""
        lines = _make_sse_lines([
            _make_sse_chunk({"reasoning_content": "让我想想"}),
            _make_sse_chunk({"reasoning_content": "..."}),
            _make_sse_chunk({"content": "答案是2"}),
            "[DONE]",
        ])
        mock_resp = MockResponse(200, lines)
        mock_session = MockSession(mock_resp)

        with patch("app.openclaw_client.aiohttp.TCPConnector"):
            with patch("app.openclaw_client.aiohttp.ClientSession", return_value=mock_session):
                chunks = []
                async for chunk in call_openclaw_stream(
                    messages=[{"role": "user", "content": "1+1=?"}],
                    conversation_id="conv-2",
                    sender_id="user-1",
                ):
                    chunks.append(chunk)

        thinking_chunks = [c for c in chunks if "thinking" in c]
        assert len(thinking_chunks) == 2
        assert thinking_chunks[0]["thinking"] == "让我想想"

        content_chunks = [c for c in chunks if "content" in c]
        assert len(content_chunks) == 1
        assert content_chunks[0]["content"] == "答案是2"

    @pytest.mark.asyncio
    async def test_stream_with_usage_in_last_chunk(self):
        """最后一个 SSE chunk 包含 usage"""
        last_chunk = {
            "id": "chatcmpl_test",
            "object": "chat.completion.chunk",
            "model": "claude-opus-4-6",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50}
        }
        lines = _make_sse_lines([
            _make_sse_chunk({"content": "Hi"}),
            json.dumps(last_chunk),
            "[DONE]",
        ])
        mock_resp = MockResponse(200, lines)
        mock_session = MockSession(mock_resp)

        with patch("app.openclaw_client.aiohttp.TCPConnector"):
            with patch("app.openclaw_client.aiohttp.ClientSession", return_value=mock_session):
                chunks = []
                async for chunk in call_openclaw_stream(
                    messages=[{"role": "user", "content": "hi"}],
                    conversation_id="conv-3",
                    sender_id="user-1",
                ):
                    chunks.append(chunk)

        usage = [c for c in chunks if "usage" in c][0]["usage"]
        assert usage["model"] == "claude-opus-4-6"
        assert usage["input_tokens"] == 100
        assert usage["output_tokens"] == 50

    @pytest.mark.asyncio
    async def test_http_error_status(self):
        """HTTP 非 200 状态码"""
        mock_resp = MockResponse(500, text="Internal Server Error")
        mock_session = MockSession(mock_resp)

        with patch("app.openclaw_client.aiohttp.TCPConnector"):
            with patch("app.openclaw_client.aiohttp.ClientSession", return_value=mock_session):
                chunks = []
                async for chunk in call_openclaw_stream(
                    messages=[{"role": "user", "content": "test"}],
                    conversation_id="conv-err",
                    sender_id="user-1",
                ):
                    chunks.append(chunk)

        assert len(chunks) == 1
        assert "error" in chunks[0]
        assert "500" in chunks[0]["error"]

    @pytest.mark.asyncio
    async def test_http_401_unauthorized(self):
        """HTTP 401 认证失败"""
        mock_resp = MockResponse(401, text="Unauthorized: invalid token")
        mock_session = MockSession(mock_resp)

        with patch("app.openclaw_client.aiohttp.TCPConnector"):
            with patch("app.openclaw_client.aiohttp.ClientSession", return_value=mock_session):
                chunks = []
                async for chunk in call_openclaw_stream(
                    messages=[{"role": "user", "content": "test"}],
                    conversation_id="conv-auth",
                    sender_id="user-1",
                ):
                    chunks.append(chunk)

        assert "error" in chunks[0]
        assert "401" in chunks[0]["error"]

    @pytest.mark.asyncio
    async def test_malformed_json_in_sse(self):
        """SSE 包含无效 JSON (应跳过继续)"""
        lines = _make_sse_lines([
            "{invalid json}",
            _make_sse_chunk({"content": "OK"}),
            "[DONE]",
        ])
        mock_resp = MockResponse(200, lines)
        mock_session = MockSession(mock_resp)

        with patch("app.openclaw_client.aiohttp.TCPConnector"):
            with patch("app.openclaw_client.aiohttp.ClientSession", return_value=mock_session):
                chunks = []
                async for chunk in call_openclaw_stream(
                    messages=[{"role": "user", "content": "test"}],
                    conversation_id="conv-bad",
                    sender_id="user-1",
                ):
                    chunks.append(chunk)

        content = [c for c in chunks if "content" in c]
        assert len(content) == 1
        assert content[0]["content"] == "OK"

    @pytest.mark.asyncio
    async def test_connection_error(self):
        """aiohttp 连接错误"""
        import aiohttp

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("Connection refused"))
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("app.openclaw_client.aiohttp.TCPConnector"):
            with patch("app.openclaw_client.aiohttp.ClientSession", return_value=mock_session):
                chunks = []
                async for chunk in call_openclaw_stream(
                    messages=[{"role": "user", "content": "test"}],
                    conversation_id="conv-conn",
                    sender_id="user-1",
                ):
                    chunks.append(chunk)

        assert len(chunks) == 1
        assert "error" in chunks[0]
        assert "HTTP Error" in chunks[0]["error"] or "API Error" in chunks[0]["error"]

    @pytest.mark.asyncio
    async def test_request_body_format(self):
        """验证请求体格式正确 (OpenAI 兼容)"""
        lines = _make_sse_lines(["[DONE]"])
        mock_resp = MockResponse(200, lines)
        mock_session = MockSession(mock_resp)

        messages = [
            {"role": "system", "content": "You are a bot"},
            {"role": "user", "content": "Hi"},
        ]

        with patch("app.openclaw_client.aiohttp.TCPConnector"):
            with patch("app.openclaw_client.aiohttp.ClientSession", return_value=mock_session):
                async for _ in call_openclaw_stream(
                    messages=messages,
                    conversation_id="conv-fmt",
                    sender_id="user-1",
                ):
                    pass

        # 验证 POST 请求的参数
        assert mock_session.post_kwargs is not None
        body = mock_session.post_kwargs["json"]
        assert body["model"].startswith("openclaw:")
        assert body["stream"] is True
        assert body["messages"] == messages
        assert body["user"] == "dingtalk:conv-fmt:user-1"

        headers = mock_session.post_kwargs["headers"]
        assert "Bearer" in headers["Authorization"]

    @pytest.mark.asyncio
    async def test_proxy_none_passed(self):
        """验证请求不走代理 (proxy=None)"""
        lines = _make_sse_lines(["[DONE]"])
        mock_resp = MockResponse(200, lines)
        mock_session = MockSession(mock_resp)

        with patch("app.openclaw_client.aiohttp.TCPConnector"):
            with patch("app.openclaw_client.aiohttp.ClientSession", return_value=mock_session):
                async for _ in call_openclaw_stream(
                    messages=[{"role": "user", "content": "test"}],
                    conversation_id="conv-proxy",
                    sender_id="user-1",
                ):
                    pass

        assert mock_session.post_kwargs["proxy"] is None

    @pytest.mark.asyncio
    async def test_call_openclaw_stream_with_model_param(self):
        """验证自定义 model 参数传递到请求体"""
        lines = _make_sse_lines(["[DONE]"])
        mock_resp = MockResponse(200, lines)
        mock_session = MockSession(mock_resp)

        with patch("app.openclaw_client.aiohttp.TCPConnector"):
            with patch("app.openclaw_client.aiohttp.ClientSession", return_value=mock_session):
                async for _ in call_openclaw_stream(
                    messages=[{"role": "user", "content": "test"}],
                    conversation_id="conv-model",
                    sender_id="user-1",
                    model="custom-model-v2"
                ):
                    pass

        body = mock_session.post_kwargs["json"]
        assert body["model"] == "custom-model-v2"

    @pytest.mark.asyncio
    async def test_call_openclaw_stream_default_model(self):
        """默认 model 会根据路由 agent 组装为 openclaw:{agent}"""
        lines = _make_sse_lines(["[DONE]"])
        mock_resp = MockResponse(200, lines)
        mock_session = MockSession(mock_resp)

        with patch("app.openclaw_client.aiohttp.TCPConnector"):
            with patch("app.openclaw_client.aiohttp.ClientSession", return_value=mock_session):
                async for _ in call_openclaw_stream(
                    messages=[{"role": "user", "content": "test"}],
                    conversation_id="conv-default",
                    sender_id="user-1",
                ):
                    pass

        body = mock_session.post_kwargs["json"]
        assert body["model"].startswith("openclaw:")
        assert body["user"] == "dingtalk:conv-default:user-1"

    @pytest.mark.asyncio
    async def test_empty_stream(self):
        """空 SSE 流 (直接 [DONE])"""
        lines = _make_sse_lines(["[DONE]"])
        mock_resp = MockResponse(200, lines)
        mock_session = MockSession(mock_resp)

        with patch("app.openclaw_client.aiohttp.TCPConnector"):
            with patch("app.openclaw_client.aiohttp.ClientSession", return_value=mock_session):
                chunks = []
                async for chunk in call_openclaw_stream(
                    messages=[{"role": "user", "content": "test"}],
                    conversation_id="conv-empty",
                    sender_id="user-1",
                ):
                    chunks.append(chunk)

        # 只有 usage
        assert len(chunks) == 1
        assert "usage" in chunks[0]
        assert chunks[0]["usage"]["content_len"] if "content_len" in chunks[0]["usage"] else True


# ─── close_openclaw_client ──────────────────────────────────────

class TestCloseOpenClawClient:

    @pytest.mark.asyncio
    async def test_close_is_noop(self):
        """close 是空操作，不抛异常"""
        await close_openclaw_client()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
