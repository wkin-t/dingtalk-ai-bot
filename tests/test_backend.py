"""ai/backend.py 统一后端入口测试"""
import os
import pytest

os.environ.setdefault("GEMINI_API_KEY", "test-dummy-key")


class TestBackendSelection:
    """后端选择逻辑测试"""

    def test_create_backend_stream_importable(self):
        from app.ai.backend import create_backend_stream
        assert callable(create_backend_stream)

    def test_gemini_backend_selected(self):
        import app.config as cfg
        original = cfg.AI_BACKEND
        try:
            cfg.AI_BACKEND = "gemini"
            from app.ai.backend import _get_backend_name
            assert _get_backend_name() == "gemini"
        finally:
            cfg.AI_BACKEND = original

    def test_openai_backend_selectable(self):
        import app.config as cfg
        original = cfg.AI_BACKEND
        try:
            cfg.AI_BACKEND = "openai"
            from app.ai.backend import _get_backend_name
            assert _get_backend_name() == "openai"
        finally:
            cfg.AI_BACKEND = original

    def test_openclaw_backend_selectable(self):
        import app.config as cfg
        original = cfg.AI_BACKEND
        try:
            cfg.AI_BACKEND = "openclaw"
            from app.ai.backend import _get_backend_name
            assert _get_backend_name() == "openclaw"
        finally:
            cfg.AI_BACKEND = original

    @pytest.mark.asyncio
    async def test_gemini_backend_dispatches_to_gemini(self):
        """gemini 后端应调用 call_gemini_stream"""
        import app.config as cfg
        from unittest.mock import AsyncMock, patch, MagicMock

        original = cfg.AI_BACKEND
        try:
            cfg.AI_BACKEND = "gemini"
            from app.ai.backend import create_backend_stream

            async def fake_stream(*args, **kwargs):
                yield {"content": "test response"}

            with patch("app.gemini_client.call_gemini_stream", side_effect=fake_stream) as mock:
                chunks = []
                async for chunk in create_backend_stream(
                    [{"role": "user", "content": "hi"}],
                    target_model="gemini-3-flash-preview",
                    thinking_level="low",
                    enable_search=False,
                ):
                    chunks.append(chunk)
                assert len(chunks) == 1
                assert chunks[0]["content"] == "test response"
        finally:
            cfg.AI_BACKEND = original

    @pytest.mark.asyncio
    async def test_openai_backend_dispatches_to_litellm(self):
        """openai 后端应调用 call_litellm_stream"""
        import app.config as cfg
        from unittest.mock import AsyncMock, patch

        original = cfg.AI_BACKEND
        try:
            cfg.AI_BACKEND = "openai"
            from app.ai.backend import create_backend_stream

            async def fake_stream(*args, **kwargs):
                yield {"content": "litellm response"}

            with patch("app.litellm_client.call_litellm_stream", side_effect=fake_stream) as mock:
                chunks = []
                async for chunk in create_backend_stream(
                    [{"role": "user", "content": "hi"}],
                    target_model="gemini-3-flash-preview",
                    thinking_level="low",
                    enable_search=False,
                ):
                    chunks.append(chunk)
                assert len(chunks) == 1
                assert chunks[0]["content"] == "litellm response"
        finally:
            cfg.AI_BACKEND = original

    @pytest.mark.asyncio
    async def test_openclaw_backend_dispatches_to_openclaw(self):
        """openclaw 后端应调用 call_openclaw_stream"""
        import app.config as cfg
        from unittest.mock import AsyncMock, patch

        original = cfg.AI_BACKEND
        try:
            cfg.AI_BACKEND = "openclaw"
            from app.ai.backend import create_backend_stream

            async def fake_stream(*args, **kwargs):
                yield {"content": "openclaw response"}

            with patch("app.openclaw_client.call_openclaw_stream", side_effect=fake_stream) as mock:
                chunks = []
                async for chunk in create_backend_stream(
                    [{"role": "user", "content": "hi"}],
                    target_model="openclaw",
                    conversation_id="test-conv",
                    sender_id="user1",
                    sender_nick="TestUser",
                ):
                    chunks.append(chunk)
                assert len(chunks) == 1
                assert chunks[0]["content"] == "openclaw response"
                # 验证 openclaw 收到了正确的 kwargs
                mock.assert_called_once()
                call_kwargs = mock.call_args
                assert call_kwargs[1]["conversation_id"] == "test-conv"
                assert call_kwargs[1]["sender_id"] == "user1"
        finally:
            cfg.AI_BACKEND = original

    def test_openrouter_backend_selectable(self):
        import app.config as cfg
        original = cfg.AI_BACKEND
        try:
            cfg.AI_BACKEND = "openrouter"
            from app.ai.backend import _get_backend_name
            assert _get_backend_name() == "openrouter"
        finally:
            cfg.AI_BACKEND = original

    @pytest.mark.asyncio
    async def test_openrouter_backend_dispatches_to_litellm(self):
        """openrouter 后端应复用 call_litellm_stream"""
        import app.config as cfg
        from unittest.mock import patch

        original = cfg.AI_BACKEND
        try:
            cfg.AI_BACKEND = "openrouter"
            from app.ai.backend import create_backend_stream

            async def fake_stream(*args, **kwargs):
                yield {"content": "openrouter response"}

            with patch("app.litellm_client.call_litellm_stream", side_effect=fake_stream):
                chunks = []
                async for chunk in create_backend_stream(
                    [{"role": "user", "content": "hi"}],
                    target_model="gemini-3-flash-preview",
                    thinking_level="low",
                    enable_search=False,
                ):
                    chunks.append(chunk)
                assert len(chunks) == 1
                assert chunks[0]["content"] == "openrouter response"
        finally:
            cfg.AI_BACKEND = original
