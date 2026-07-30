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
    async def test_gemini_route_slot_reaches_vertex_fallback_model(self):
        """相同主模型下，backend route_slot 必须决定最终 Vertex fallback 模型。"""
        import app.config as cfg
        from app import gemini_circuit, gemini_client
        from unittest.mock import AsyncMock, MagicMock, patch

        def make_chunk(text):
            part = MagicMock(thought=False, text=text)
            candidate = MagicMock()
            candidate.content.parts = [part]
            candidate.finish_reason = None
            candidate.grounding_metadata = None
            chunk = MagicMock()
            chunk.candidates = [candidate]
            chunk.usage_metadata = MagicMock(
                prompt_token_count=1,
                candidates_token_count=1,
                cached_content_token_count=0,
            )
            chunk.model_version = None
            return chunk

        async def one_chunk(chunk):
            yield chunk

        primary_client = MagicMock()
        fallback_client = MagicMock()
        primary_client.aio.models.generate_content_stream = AsyncMock(
            side_effect=RuntimeError("primary unavailable")
        )
        fallback_generate = AsyncMock()
        fallback_client.aio.models.generate_content_stream = fallback_generate

        expected_models = {
            "lite": "vertex-lite",
            "fast": "vertex-fast",
            "pro": "vertex-pro",
        }
        with patch.object(cfg, "AI_BACKEND", "gemini"), \
             patch.object(gemini_client, "client", primary_client), \
             patch.object(gemini_client, "fallback_client", fallback_client), \
             patch.object(gemini_client, "MODEL_LITE_FALLBACK", "vertex-lite"), \
             patch.object(gemini_client, "MODEL_FAST_FALLBACK", "vertex-fast"), \
             patch.object(gemini_client, "MODEL_PRO_FALLBACK", "vertex-pro"), \
             patch.object(gemini_circuit, "is_circuit_open_async", AsyncMock(return_value=False)), \
             patch.object(gemini_circuit, "open_circuit_async", AsyncMock(return_value=True)):
            from app.ai.backend import create_backend_stream

            for route_slot, expected_model in expected_models.items():
                fallback_generate.reset_mock()
                fallback_generate.return_value = one_chunk(make_chunk(route_slot))
                chunks = [
                    chunk
                    async for chunk in create_backend_stream(
                        [{"role": "user", "content": "test"}],
                        target_model="same-primary-model",
                        thinking_level="low",
                        enable_search=False,
                        route_slot=route_slot,
                    )
                ]

                usage = next(item["usage"] for item in chunks if "usage" in item)
                assert usage["model"] == expected_model
                assert fallback_generate.await_args.kwargs["model"] == expected_model

    @pytest.mark.asyncio
    async def test_openai_backend_dispatches_to_openai_client(self):
        """openai 后端应调用 call_openai_stream"""
        import app.config as cfg
        from unittest.mock import AsyncMock, patch

        original = cfg.AI_BACKEND
        try:
            cfg.AI_BACKEND = "openai"
            from app.ai.backend import create_backend_stream

            async def fake_stream(*args, **kwargs):
                yield {"content": "openai response"}

            with patch("app.openai_client.call_openai_stream", side_effect=fake_stream) as mock:
                chunks = []
                async for chunk in create_backend_stream(
                    [{"role": "user", "content": "hi"}],
                    target_model="gemini-3-flash-preview",
                    thinking_level="low",
                    enable_search=False,
                ):
                    chunks.append(chunk)
                assert len(chunks) == 1
                assert chunks[0]["content"] == "openai response"
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

    @pytest.mark.asyncio
    async def test_openai_passes_conversation_id_to_openai_client(self):
        """openai 后端应将 conversation_id 透传给 call_openai_stream"""
        import app.config as cfg
        from unittest.mock import patch, call

        original = cfg.AI_BACKEND
        try:
            cfg.AI_BACKEND = "openai"
            from app.ai.backend import create_backend_stream

            async def fake_stream(*args, **kwargs):
                yield {"content": "ok"}

            with patch("app.openai_client.call_openai_stream", side_effect=fake_stream) as mock:
                async for _ in create_backend_stream(
                    [{"role": "user", "content": "hi"}],
                    target_model="gemini-3-flash-preview",
                    conversation_id="test-conv-openai",
                ):
                    pass
                mock.assert_called_once()
                assert mock.call_args[1]["conversation_id"] == "test-conv-openai"
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
    async def test_openrouter_backend_dispatches_to_openrouter_client(self):
        """openrouter 后端应调用 call_openrouter_stream"""
        import app.config as cfg
        from unittest.mock import patch

        original_backend = cfg.AI_BACKEND
        try:
            cfg.AI_BACKEND = "openrouter"
            from app.ai.backend import create_backend_stream

            async def fake_stream(*args, **kwargs):
                yield {"content": "openrouter response"}

            with patch("app.openrouter_client.call_openrouter_stream", side_effect=fake_stream):
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
            cfg.AI_BACKEND = original_backend

    @pytest.mark.asyncio
    async def test_openrouter_passes_conversation_id_to_openrouter_client(self):
        """openrouter 后端应将 conversation_id 透传给 call_openrouter_stream"""
        import app.config as cfg
        from unittest.mock import patch

        original_backend = cfg.AI_BACKEND
        try:
            cfg.AI_BACKEND = "openrouter"
            from app.ai.backend import create_backend_stream

            async def fake_stream(*args, **kwargs):
                yield {"content": "ok"}

            with patch("app.openrouter_client.call_openrouter_stream", side_effect=fake_stream) as mock:
                async for _ in create_backend_stream(
                    [{"role": "user", "content": "hi"}],
                    target_model="gemini-3-flash-preview",
                    conversation_id="test-conv-openrouter",
                ):
                    pass
                mock.assert_called_once()
                assert mock.call_args[1]["conversation_id"] == "test-conv-openrouter"
        finally:
            cfg.AI_BACKEND = original_backend
