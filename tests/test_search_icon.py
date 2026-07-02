# -*- coding: utf-8 -*-
"""🌐 搜索图标"只在真搜了才亮"逻辑测试

修复：全自主搜索后每条 fast/pro 回复常亮 🌐（挂了工具就亮，不代表本次真搜了）。
改为只在真实搜索信号（executed / fallback_injected）出现时点亮。
"""
import os

import pytest

os.environ.setdefault("GEMINI_API_KEY", "test-dummy-key")

from app.ai.backend import should_show_search_icon


class TestShouldShowSearchIcon:
    def test_none_off(self):
        assert should_show_search_icon(None) is False

    def test_empty_off(self):
        assert should_show_search_icon({}) is False

    def test_tool_mounted_but_not_searched_off(self):
        # 全自主核心场景：挂了工具（native_enabled）但模型没搜 → 不亮
        assert should_show_search_icon(
            {"requested": True, "native_enabled": True, "executed": False}
        ) is False

    def test_requested_alone_off(self):
        assert should_show_search_icon({"requested": True}) is False

    def test_executed_on(self):
        assert should_show_search_icon(
            {"requested": True, "native_enabled": True, "executed": True}
        ) is True

    def test_fallback_injected_on(self):
        assert should_show_search_icon(
            {"requested": True, "native_enabled": False, "fallback_injected": True}
        ) is True


class TestGeminiExecutedSignal:
    """gemini 流回流 grounding_metadata 时应补发 executed=True"""

    @pytest.mark.asyncio
    async def test_grounding_metadata_yields_executed(self, monkeypatch):
        from unittest.mock import MagicMock
        import app.gemini_client as gc

        # 构造带 grounding_metadata 的流式 chunk
        part = MagicMock()
        part.thought = False
        part.text = "根据搜索结果……"
        candidate = MagicMock()
        candidate.grounding_metadata = {"web_search_queries": ["天气"]}
        candidate.finish_reason = "STOP"
        candidate.content.parts = [part]
        chunk = MagicMock()
        chunk.usage_metadata.prompt_token_count = 5
        chunk.usage_metadata.candidates_token_count = 10
        chunk.candidates = [candidate]

        async def fake_gen(*args, **kwargs):
            async def _it():
                yield chunk
            return _it()

        fake_client = MagicMock()
        fake_client.aio.models.generate_content_stream = fake_gen
        monkeypatch.setattr(gc, "client", fake_client)

        search_chunks = []
        async for c in gc.call_gemini_stream(
            [{"role": "user", "content": "今天天气"}],
            target_model="gemini-3-flash-preview",
            enable_search=True,
        ):
            if "search" in c:
                search_chunks.append(c["search"])

        # 请求时发 native_enabled，grounding 回流后补发 executed
        assert any(s.get("executed") for s in search_chunks), search_chunks

    @pytest.mark.asyncio
    async def test_no_grounding_no_executed(self, monkeypatch):
        from unittest.mock import MagicMock
        import app.gemini_client as gc

        part = MagicMock()
        part.thought = False
        part.text = "你好"
        candidate = MagicMock()
        candidate.grounding_metadata = None  # 没搜
        candidate.finish_reason = "STOP"
        candidate.content.parts = [part]
        chunk = MagicMock()
        chunk.usage_metadata.prompt_token_count = 3
        chunk.usage_metadata.candidates_token_count = 4
        chunk.candidates = [candidate]

        async def fake_gen(*args, **kwargs):
            async def _it():
                yield chunk
            return _it()

        fake_client = MagicMock()
        fake_client.aio.models.generate_content_stream = fake_gen
        monkeypatch.setattr(gc, "client", fake_client)

        search_chunks = []
        async for c in gc.call_gemini_stream(
            [{"role": "user", "content": "你好"}],
            target_model="gemini-3-flash-preview",
            enable_search=True,
        ):
            if "search" in c:
                search_chunks.append(c["search"])

        # 挂了工具（native_enabled）但没搜 → 无 executed → 图标不亮
        assert not any(s.get("executed") for s in search_chunks), search_chunks
        assert not should_show_search_icon(
            {k: v for s in search_chunks for k, v in s.items()}
        )
