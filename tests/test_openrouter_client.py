"""app/openrouter_client.py 单元测试"""
import os
import pytest

os.environ.setdefault("GEMINI_API_KEY", "test-dummy-key")
os.environ.setdefault("OPENROUTER_API_KEY", "test-dummy-openrouter-key")


class TestIsCompleteReasoning:
    """_is_complete_reasoning 完整性校验"""

    def test_empty_list_returns_false(self):
        from app.openrouter_client import _is_complete_reasoning
        assert not _is_complete_reasoning([])

    def test_non_list_returns_false(self):
        from app.openrouter_client import _is_complete_reasoning
        assert not _is_complete_reasoning(None)
        assert not _is_complete_reasoning("str")

    def test_thinking_block_with_signature_passes(self):
        from app.openrouter_client import _is_complete_reasoning
        rd = [{"type": "thinking", "text": "hello", "signature": "abc123"}]
        assert _is_complete_reasoning(rd)

    def test_thinking_block_with_data_passes(self):
        from app.openrouter_client import _is_complete_reasoning
        rd = [{"type": "thinking", "data": "encrypted_blob"}]
        assert _is_complete_reasoning(rd)

    def test_thinking_block_missing_signature_fails(self):
        from app.openrouter_client import _is_complete_reasoning
        rd = [{"type": "thinking", "text": "hello"}]
        assert not _is_complete_reasoning(rd)

    def test_non_thinking_blocks_skipped(self):
        from app.openrouter_client import _is_complete_reasoning
        # summary/text 类型不要求 signature
        rd = [{"type": "summary", "text": "a summary"}]
        assert _is_complete_reasoning(rd)

    def test_mixed_complete_passes(self):
        from app.openrouter_client import _is_complete_reasoning
        rd = [
            {"type": "thinking", "signature": "sig1"},
            {"type": "summary", "text": "summary"},
        ]
        assert _is_complete_reasoning(rd)

    def test_mixed_incomplete_fails(self):
        from app.openrouter_client import _is_complete_reasoning
        rd = [
            {"type": "thinking", "signature": "sig1"},
            {"type": "thinking"},  # missing signature/data
        ]
        assert not _is_complete_reasoning(rd)


class TestSerializeRd:
    """_serialize_rd 序列化"""

    def test_pydantic_model_serialized(self):
        from app.openrouter_client import _serialize_rd
        from unittest.mock import MagicMock

        obj = MagicMock()
        obj.model_dump = MagicMock(return_value={"type": "thinking", "signature": "s"})
        result = _serialize_rd([obj])
        assert result == [{"type": "thinking", "signature": "s"}]
        obj.model_dump.assert_called_once_with(by_alias=True, exclude_none=True)

    def test_non_pydantic_falls_back_to_dict(self):
        from app.openrouter_client import _serialize_rd

        # 没有 model_dump 的普通 dict 也能正确序列化
        plain = {"type": "summary", "text": "a summary"}
        # 用一个有 model_dump 的 mock 代表 Pydantic，其余不测（SDK 实际上总是 Pydantic）
        from unittest.mock import MagicMock
        obj = MagicMock()
        obj.model_dump = MagicMock(return_value=plain)
        result = _serialize_rd([obj])
        assert result == [plain]

    def test_empty_list(self):
        from app.openrouter_client import _serialize_rd
        assert _serialize_rd([]) == []


class TestOpenrouterClientImportable:
    """基础可导入测试"""

    def test_call_openrouter_stream_importable(self):
        from app.openrouter_client import call_openrouter_stream
        assert callable(call_openrouter_stream)

    def test_analyze_complexity_importable(self):
        from app.openrouter_client import analyze_complexity_with_openrouter
        assert callable(analyze_complexity_with_openrouter)

    def test_build_client_importable(self):
        from app.openrouter_client import _build_client
        assert callable(_build_client)


class TestOpenrouterClientStream:
    """call_openrouter_stream 流式调用 mock 测试"""

    @pytest.mark.asyncio
    async def test_streams_content_chunks(self):
        from app.openrouter_client import call_openrouter_stream
        from unittest.mock import AsyncMock, MagicMock, patch

        # 构造 mock SDK response
        mock_delta = MagicMock()
        mock_delta.reasoning = None
        mock_delta.reasoning_details = None
        mock_delta.content = "hello world"

        mock_choice = MagicMock()
        mock_choice.delta = mock_delta

        mock_chunk = MagicMock()
        mock_chunk.model = "anthropic/claude-haiku-4-5"
        mock_chunk.choices = [mock_choice]
        mock_chunk.usage = None

        # 最终 chunk：usage
        mock_usage_chunk = MagicMock()
        mock_usage_chunk.model = "anthropic/claude-haiku-4-5"
        mock_usage_chunk.choices = []
        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 10
        mock_usage.completion_tokens = 5
        mock_usage_chunk.usage = mock_usage

        async def mock_aiter(self):
            yield mock_chunk
            yield mock_usage_chunk

        mock_stream_ctx = MagicMock()
        mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_stream_ctx)
        mock_stream_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_stream_ctx.__aiter__ = mock_aiter

        mock_send = AsyncMock(return_value=mock_stream_ctx)

        with patch("app.openrouter_client._build_client") as mock_build:
            mock_client = MagicMock()
            mock_client.chat.send_async = mock_send
            mock_build.return_value = mock_client

            chunks = []
            async for chunk in call_openrouter_stream(
                [{"role": "user", "content": "hi"}],
                target_model="fast",
            ):
                chunks.append(chunk)

        content_chunks = [c for c in chunks if "content" in c]
        assert len(content_chunks) == 1
        assert content_chunks[0]["content"] == "hello world"

    @pytest.mark.asyncio
    async def test_reasoning_details_yielded_when_complete(self):
        """完整 reasoning_details（含 signature）应在流结束后 yield"""
        from app.openrouter_client import call_openrouter_stream
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_rd_obj = MagicMock()
        mock_rd_obj.model_dump = MagicMock(
            return_value={"type": "thinking", "text": "thinking text", "signature": "abc123"}
        )

        # 第一个 chunk：reasoning_details + content
        delta1 = MagicMock()
        delta1.reasoning = "thinking text"
        delta1.reasoning_details = [mock_rd_obj]
        delta1.content = None
        chunk1 = MagicMock()
        chunk1.model = "anthropic/claude-sonnet-4-5"
        chunk1.choices = [MagicMock(delta=delta1)]
        chunk1.usage = None

        # 第二个 chunk：正文
        delta2 = MagicMock()
        delta2.reasoning = None
        delta2.reasoning_details = None
        delta2.content = "Paris"
        chunk2 = MagicMock()
        chunk2.model = "anthropic/claude-sonnet-4-5"
        chunk2.choices = [MagicMock(delta=delta2)]
        chunk2.usage = None

        # 最终 usage chunk
        usage_chunk = MagicMock()
        usage_chunk.model = "anthropic/claude-sonnet-4-5"
        usage_chunk.choices = []
        usage_chunk.usage = MagicMock(prompt_tokens=20, completion_tokens=8)

        async def mock_aiter(self):
            yield chunk1
            yield chunk2
            yield usage_chunk

        mock_stream_ctx = MagicMock()
        mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_stream_ctx)
        mock_stream_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_stream_ctx.__aiter__ = mock_aiter

        with patch("app.openrouter_client._build_client") as mock_build:
            mock_client = MagicMock()
            mock_client.chat.send_async = AsyncMock(return_value=mock_stream_ctx)
            mock_build.return_value = mock_client

            chunks = []
            async for chunk in call_openrouter_stream(
                [{"role": "user", "content": "What is the capital of France?"}],
                target_model="pro",
                thinking_level="medium",
            ):
                chunks.append(chunk)

        rd_chunks = [c for c in chunks if "reasoning_details" in c]
        assert len(rd_chunks) == 1
        rd = rd_chunks[0]["reasoning_details"]
        assert rd[0]["signature"] == "abc123"

    @pytest.mark.asyncio
    async def test_incomplete_reasoning_not_yielded(self):
        """缺失 signature 的 reasoning_details 不应 yield（防残体）"""
        from app.openrouter_client import call_openrouter_stream
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_rd_obj = MagicMock()
        mock_rd_obj.model_dump = MagicMock(
            return_value={"type": "thinking", "text": "incomplete"}
            # 故意缺少 signature
        )

        delta = MagicMock()
        delta.reasoning = None
        delta.reasoning_details = [mock_rd_obj]
        delta.content = "answer"
        chunk = MagicMock()
        chunk.model = "anthropic/claude-sonnet-4-5"
        chunk.choices = [MagicMock(delta=delta)]
        chunk.usage = None

        usage_chunk = MagicMock()
        usage_chunk.model = "anthropic/claude-sonnet-4-5"
        usage_chunk.choices = []
        usage_chunk.usage = MagicMock(prompt_tokens=5, completion_tokens=3)

        async def mock_aiter(self):
            yield chunk
            yield usage_chunk

        mock_stream_ctx = MagicMock()
        mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_stream_ctx)
        mock_stream_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_stream_ctx.__aiter__ = mock_aiter

        with patch("app.openrouter_client._build_client") as mock_build:
            mock_client = MagicMock()
            mock_client.chat.send_async = AsyncMock(return_value=mock_stream_ctx)
            mock_build.return_value = mock_client

            chunks = []
            async for chunk in call_openrouter_stream(
                [{"role": "user", "content": "hi"}],
                target_model="pro",
                thinking_level="medium",
            ):
                chunks.append(chunk)

        rd_chunks = [c for c in chunks if "reasoning_details" in c]
        assert len(rd_chunks) == 0, "残体 reasoning_details 不应被 yield"


class TestAnalyzeComplexityOpenrouter:
    """analyze_complexity_with_openrouter 路由函数"""

    def test_importable_from_new_module(self):
        from app.openrouter_client import analyze_complexity_with_openrouter
        assert callable(analyze_complexity_with_openrouter)

    @pytest.mark.asyncio
    async def test_falls_back_on_error(self):
        """SDK 调用失败时应降级到 analyze_complexity_unified"""
        from app.openrouter_client import analyze_complexity_with_openrouter
        from unittest.mock import patch, AsyncMock, MagicMock

        with patch("app.openrouter_client._build_client") as mock_build:
            mock_client = MagicMock()
            mock_client.chat.send_async = AsyncMock(side_effect=Exception("network error"))
            mock_build.return_value = mock_client

            fallback_result = {"model": "fast", "thinking_level": "low", "need_search": False}
            with patch("app.ai.router.analyze_complexity_unified", return_value=fallback_result) as mock_fallback:
                result = await analyze_complexity_with_openrouter("hello", has_images=False)
                assert result["model"] == "fast"
                mock_fallback.assert_called_once()
