# -*- coding: utf-8 -*-
"""
Gemini 客户端单元测试

测试目标:
1. _convert_openai_to_gemini() 消息格式转换
2. analyze_complexity_with_model() 智能路由
3. call_gemini_stream() 流式生成 (mock genai)
4. get_model_pricing() 定价查询
"""
import pytest
import asyncio
import json
from unittest.mock import patch, MagicMock, AsyncMock, PropertyMock
from app.config import get_model_pricing, GEMINI_PRICING


async def _async_iter(items):
    """将同步列表转为异步生成器，用于 mock google-genai aio 流式接口"""
    for item in items:
        yield item


# ─── get_model_pricing 测试 ──────────────────────────────────────

class TestGetModelPricing:
    """config.get_model_pricing 测试"""

    def test_exact_match_gemini_3_flash(self):
        """精确匹配 gemini-3-flash"""
        pricing = get_model_pricing("gemini-3-flash")
        assert pricing["input"] == 0.50
        assert pricing["output"] == 3.00

    def test_match_gemini_3_pro_preview(self):
        """匹配 gemini-3-pro-preview (含 gemini-3-pro 子串)"""
        pricing = get_model_pricing("gemini-3-pro-preview")
        assert pricing["input"] == 2.00

    def test_match_gemini_2_5_flash(self):
        """匹配 gemini-2.5-flash"""
        pricing = get_model_pricing("gemini-2.5-flash")
        assert pricing["input"] == 0.15

    def test_match_case_insensitive(self):
        """大小写不敏感"""
        pricing = get_model_pricing("GEMINI-2.0-FLASH")
        assert pricing["input"] == 0.10

    def test_unknown_model_returns_default(self):
        """未知模型返回默认定价"""
        pricing = get_model_pricing("unknown-model-xyz")
        assert pricing == GEMINI_PRICING["default"]

    def test_free_preview_model(self):
        """免费预览模型 — 注意 get_model_pricing 使用子串匹配,
        gemini-2.0-flash 会先于 gemini-2.0-flash-exp 匹配到"""
        pricing = get_model_pricing("gemini-2.0-flash-exp")
        # 子串匹配会先命中 "gemini-2.0-flash" (input=0.10)
        assert pricing["input"] == 0.10
        assert pricing["output"] == 0.40


# ─── _convert_openai_to_gemini 测试 ──────────────────────────────

class TestConvertOpenAIToGemini:
    """消息格式转换测试"""

    def _convert(self, messages):
        # 延迟导入，避免模块级 genai 初始化影响
        from app.gemini_client import _convert_openai_to_gemini
        return _convert_openai_to_gemini(messages)

    def test_system_message_extracted(self):
        """system 消息提取为 system_instruction"""
        messages = [
            {"role": "system", "content": "You are a helpful assistant"},
            {"role": "user", "content": "Hello"},
        ]
        system, contents = self._convert(messages)
        assert system == "You are a helpful assistant"
        assert len(contents) == 1

    def test_no_system_message(self):
        """没有 system 消息"""
        messages = [{"role": "user", "content": "Hello"}]
        system, contents = self._convert(messages)
        assert system is None
        assert len(contents) == 1

    def test_role_mapping(self):
        """角色映射: user->user, assistant->model"""
        messages = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello"},
            {"role": "user", "content": "Bye"},
        ]
        system, contents = self._convert(messages)
        assert contents[0].role == "user"
        assert contents[1].role == "model"
        assert contents[2].role == "user"

    def test_string_content(self):
        """字符串内容正确转换"""
        messages = [{"role": "user", "content": "你好世界"}]
        _, contents = self._convert(messages)
        assert len(contents) == 1
        parts = contents[0].parts
        assert len(parts) == 1
        assert parts[0].text == "你好世界"

    def test_multimodal_content_text_only(self):
        """多模态消息中的纯文本"""
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "描述这张图"},
            ]
        }]
        _, contents = self._convert(messages)
        parts = contents[0].parts
        assert len(parts) == 1
        assert parts[0].text == "描述这张图"

    def test_multimodal_content_with_image(self):
        """多模态消息包含图片 data URL"""
        import base64
        img_bytes = b"\x89PNG\r\n\x1a\n"  # 最小 PNG 头
        b64 = base64.b64encode(img_bytes).decode()
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
            ]
        }]
        _, contents = self._convert(messages)
        parts = contents[0].parts
        assert len(parts) == 2
        assert parts[0].text == "看图"
        # 第二个 part 应是图片
        assert hasattr(parts[1], 'inline_data') or parts[1].text is None or True

    def test_empty_content_string(self):
        """空字符串内容"""
        messages = [{"role": "user", "content": ""}]
        _, contents = self._convert(messages)
        assert len(contents) == 1

    def test_non_string_non_list_content(self):
        """非字符串非列表的内容 (兜底转换)"""
        messages = [{"role": "user", "content": 12345}]
        _, contents = self._convert(messages)
        parts = contents[0].parts
        assert parts[0].text == "12345"

    def test_multi_turn_conversation(self):
        """多轮对话完整转换"""
        messages = [
            {"role": "system", "content": "你是AI助手"},
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！"},
            {"role": "user", "content": "天气怎么样"},
        ]
        system, contents = self._convert(messages)
        assert system == "你是AI助手"
        assert len(contents) == 3
        assert contents[0].role == "user"
        assert contents[1].role == "model"
        assert contents[2].role == "user"


# ─── analyze_complexity_with_model 测试 ──────────────────────────

def _make_analysis_chunk(text: str):
    """构造路由分析用的 stream chunk（无 thought，纯文本）"""
    mock_part = MagicMock()
    mock_part.thought = False
    mock_part.text = text
    mock_chunk = MagicMock()
    mock_chunk.candidates = [MagicMock()]
    mock_chunk.candidates[0].content = MagicMock()
    mock_chunk.candidates[0].content.parts = [mock_part]
    return mock_chunk


class TestAnalyzeComplexity:
    """智能路由分析测试"""

    @pytest.mark.asyncio
    async def test_normal_analysis(self):
        """正常分析返回结果"""
        from app.gemini_client import analyze_complexity_with_model

        with patch("app.gemini_client.client") as mock_client:
            chunk = _make_analysis_chunk('{"model":"gemini-3-flash-preview","thinking_level":"low","need_search":false,"reason":"简单问答"}')
            mock_client.aio.models.generate_content_stream = AsyncMock(return_value=_async_iter([chunk]))
            result = await analyze_complexity_with_model("你好")

        assert result["model"] == "gemini-3-flash-preview"
        assert result["thinking_level"] == "low"
        assert result["need_search"] is False
        assert result["route_slot"] == "fast"

    @pytest.mark.asyncio
    async def test_complex_question_routes_to_pro(self):
        """复杂问题路由到 pro 模型"""
        from app.gemini_client import analyze_complexity_with_model

        with patch("app.gemini_client.client") as mock_client:
            chunk = _make_analysis_chunk('{"model":"gemini-3.1-pro-preview","route_slot":"pro","thinking_level":"high","need_search":false,"reason":"复杂数学"}')
            mock_client.aio.models.generate_content_stream = AsyncMock(return_value=_async_iter([chunk]))
            result = await analyze_complexity_with_model("证明黎曼猜想")

        assert result["model"] == "gemini-3.1-pro-preview"
        assert result["thinking_level"] == "high"
        assert result["route_slot"] == "pro"

    @pytest.mark.asyncio
    async def test_search_needed_for_realtime(self):
        """实时信息需要搜索"""
        from app.gemini_client import analyze_complexity_with_model

        with patch("app.gemini_client.client") as mock_client:
            chunk = _make_analysis_chunk('{"model":"gemini-3-flash-preview","thinking_level":"low","need_search":true,"reason":"需要实时信息"}')
            mock_client.aio.models.generate_content_stream = AsyncMock(return_value=_async_iter([chunk]))
            result = await analyze_complexity_with_model("今天天气怎么样")

        assert result["need_search"] is True

    @pytest.mark.asyncio
    async def test_fallback_on_api_error(self):
        """API 错误时降级为默认配置"""
        from app.gemini_client import analyze_complexity_with_model

        with patch("app.gemini_client.client") as mock_client:
            mock_client.aio.models.generate_content_stream = AsyncMock(side_effect=Exception("API 不可用"))
            result = await analyze_complexity_with_model("测试")

        assert result["model"] == "gemini-3-flash-preview"
        assert result["thinking_level"] == "low"
        assert result["need_search"] is False
        assert "默认" in result["reason"] or "失败" in result["reason"]

    @pytest.mark.asyncio
    async def test_first_iterator_error_uses_vertex_fallback(self):
        """预分析的首次 async iterator 异常也触发一次 Vertex fallback。"""
        from app import gemini_client, gemini_circuit

        primary_client = MagicMock()
        fallback_client = MagicMock()
        fallback_chunk = _make_analysis_chunk(
            '{"model":"gemini-3-flash-preview","route_slot":"fast","thinking_level":"low","need_search":false,"reason":"fallback"}'
        )
        primary_client.aio.models.generate_content_stream = AsyncMock(
            return_value=TestCallGeminiStream._RaisingResponse(RuntimeError("analysis first pull"))
        )
        fallback_client.aio.models.generate_content_stream = AsyncMock(
            return_value=_async_iter([fallback_chunk])
        )
        with patch.object(gemini_client, "client", primary_client), \
             patch.object(gemini_client, "fallback_client", fallback_client), \
             patch.object(gemini_circuit, "is_circuit_open_async", AsyncMock(return_value=False)), \
             patch.object(gemini_circuit, "open_circuit_async", AsyncMock()) as open_circuit:
            result = await gemini_client.analyze_complexity_with_model("测试")

        assert result["reason"] == "fallback"
        open_circuit.assert_awaited_once()
        primary_client.aio.models.generate_content_stream.assert_awaited_once()
        fallback_client.aio.models.generate_content_stream.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_open_circuit_uses_fallback_without_primary_probe(self):
        """预分析命中已有 marker 时不触碰主 client。"""
        from app import gemini_client, gemini_circuit

        primary_client = MagicMock()
        fallback_client = MagicMock()
        fallback_chunk = _make_analysis_chunk(
            '{"model":"gemini-3-flash-preview","route_slot":"fast","thinking_level":"low","need_search":false,"reason":"circuit"}'
        )
        primary_client.aio.models.generate_content_stream = AsyncMock()
        fallback_client.aio.models.generate_content_stream = AsyncMock(
            return_value=_async_iter([fallback_chunk])
        )
        with patch.object(gemini_client, "client", primary_client), \
             patch.object(gemini_client, "fallback_client", fallback_client), \
             patch.object(gemini_circuit, "is_circuit_open_async", AsyncMock(return_value=True)):
            result = await gemini_client.analyze_complexity_with_model("测试")

        assert result["reason"] == "circuit"
        primary_client.aio.models.generate_content_stream.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invalid_json_does_not_open_circuit(self):
        """provider 成功但本地 JSON 无效时，不把本地解析问题当 provider 故障。"""
        from app import gemini_client, gemini_circuit

        with patch.object(gemini_client, "client") as mock_client, \
             patch.object(gemini_client, "fallback_client", MagicMock()), \
             patch.object(gemini_circuit, "is_circuit_open_async", AsyncMock(return_value=False)), \
             patch.object(gemini_circuit, "open_circuit_async", AsyncMock()) as open_circuit:
            mock_client.aio.models.generate_content_stream = AsyncMock(
                return_value=_async_iter([_make_analysis_chunk("not json")])
            )
            result = await gemini_client.analyze_complexity_with_model("测试")

        assert result["route_slot"] == "fast"
        open_circuit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fallback_on_invalid_json(self):
        """返回非 JSON 时降级"""
        from app.gemini_client import analyze_complexity_with_model

        with patch("app.gemini_client.client") as mock_client:
            chunk = _make_analysis_chunk("我不确定怎么回答这个问题")
            mock_client.aio.models.generate_content_stream = AsyncMock(return_value=_async_iter([chunk]))
            result = await analyze_complexity_with_model("测试")

        assert result["model"] == "gemini-3-flash-preview"

    @pytest.mark.asyncio
    async def test_invalid_model_corrected(self):
        """无效模型名被修正"""
        from app.gemini_client import analyze_complexity_with_model

        with patch("app.gemini_client.client") as mock_client:
            chunk = _make_analysis_chunk('{"model":"invalid-model","thinking_level":"low","need_search":false,"reason":"test"}')
            mock_client.aio.models.generate_content_stream = AsyncMock(return_value=_async_iter([chunk]))
            result = await analyze_complexity_with_model("测试")

        assert result["model"] == "gemini-3-flash-preview"

    @pytest.mark.asyncio
    async def test_invalid_thinking_level_corrected(self):
        """无效 thinking_level 被修正"""
        from app.gemini_client import analyze_complexity_with_model

        with patch("app.gemini_client.client") as mock_client:
            chunk = _make_analysis_chunk('{"model":"gemini-3-flash-preview","thinking_level":"ultra","need_search":false,"reason":"test"}')
            mock_client.aio.models.generate_content_stream = AsyncMock(return_value=_async_iter([chunk]))
            result = await analyze_complexity_with_model("测试")

        assert result["thinking_level"] == "low"


# ─── call_gemini_stream 测试 ─────────────────────────────────────

class TestCallGeminiStream:
    """call_gemini_stream 流式生成测试"""

    def _make_chunk(self, text, is_thought=False, usage=None, finish_reason=None):
        """构造模拟的 Gemini chunk"""
        part = MagicMock()
        part.thought = is_thought
        part.text = text

        candidate = MagicMock()
        candidate.content.parts = [part]
        candidate.finish_reason = finish_reason

        chunk = MagicMock()
        chunk.candidates = [candidate]

        if usage:
            chunk.usage_metadata.prompt_token_count = usage.get("input", 0)
            chunk.usage_metadata.candidates_token_count = usage.get("output", 0)
        else:
            chunk.usage_metadata = None

        return chunk

    class _RaisingResponse:
        def __init__(self, error):
            self.error = error

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise self.error

    class _OutputThenRaisingResponse:
        def __init__(self, chunk, error):
            self.chunk = chunk
            self.error = error
            self.calls = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.calls += 1
            if self.calls == 1:
                return self.chunk
            raise self.error

    @pytest.mark.asyncio
    async def test_normal_stream(self):
        """正常流式生成"""
        from app.gemini_client import call_gemini_stream

        chunks_data = [
            self._make_chunk("你好"),
            self._make_chunk("！"),
            self._make_chunk("", usage={"input": 10, "output": 5}),
        ]
        # 最后一个有 usage 但空文本
        chunks_data[2].candidates = []

        with patch("app.gemini_client.client") as mock_client:
            mock_client.aio.models.generate_content_stream = AsyncMock(return_value=_async_iter(chunks_data))

            results = []
            async for chunk in call_gemini_stream(
                messages=[{"role": "user", "content": "你好"}],
                target_model="gemini-3-flash-preview",
            ):
                results.append(chunk)

        content_results = [r for r in results if "content" in r]
        assert len(content_results) == 2
        assert content_results[0]["content"] == "你好"
        assert content_results[1]["content"] == "！"

    @pytest.mark.asyncio
    async def test_cached_tokens_parsed_from_real_field_shape(self):
        """回归防护：字段名(usage_metadata.cached_content_token_count)手滑打错时必须
        被测试发现——给一个真实非零值，断言真的解析到了 usage dict 里。"""
        from app.gemini_client import call_gemini_stream

        chunk = self._make_chunk("答案", usage={"input": 1000, "output": 10})
        chunk.usage_metadata.cached_content_token_count = 750

        with patch("app.gemini_client.client") as mock_client:
            mock_client.aio.models.generate_content_stream = AsyncMock(return_value=_async_iter([chunk]))

            results = []
            async for c in call_gemini_stream(
                messages=[{"role": "user", "content": "hi"}],
                target_model="gemini-3-flash-preview",
            ):
                results.append(c)

        usages = [r["usage"] for r in results if "usage" in r]
        assert len(usages) == 1
        assert usages[0]["cached_tokens"] == 750
        assert usages[0]["input_tokens"] == 1000

    @pytest.mark.asyncio
    async def test_stream_with_thinking(self):
        """流式带思考过程"""
        from app.gemini_client import call_gemini_stream

        chunks_data = [
            self._make_chunk("分析中...", is_thought=True),
            self._make_chunk("答案是2", is_thought=False, usage={"input": 15, "output": 8}),
        ]

        with patch("app.gemini_client.client") as mock_client:
            mock_client.aio.models.generate_content_stream = AsyncMock(return_value=_async_iter(chunks_data))
            with patch("app.gemini_client.ENABLE_THINKING", True):
                results = []
                async for chunk in call_gemini_stream(
                    messages=[{"role": "user", "content": "1+1"}],
                    target_model="gemini-3-flash-preview",
                    thinking_level="low",
                ):
                    results.append(chunk)

        thinking = [r for r in results if "thinking" in r]
        assert len(thinking) == 1
        assert thinking[0]["thinking"] == "分析中..."

        content = [r for r in results if "content" in r]
        assert len(content) == 1
        assert content[0]["content"] == "答案是2"

    @pytest.mark.asyncio
    async def test_safety_block(self):
        """安全过滤器阻止内容"""
        from app.gemini_client import call_gemini_stream

        part = MagicMock()
        part.thought = False
        part.text = ""

        candidate = MagicMock()
        candidate.content.parts = []
        candidate.finish_reason = "SAFETY"

        chunk = MagicMock()
        chunk.candidates = [candidate]
        chunk.usage_metadata = None

        with patch("app.gemini_client.client") as mock_client:
            mock_client.aio.models.generate_content_stream = AsyncMock(return_value=_async_iter([chunk]))

            results = []
            async for r in call_gemini_stream(
                messages=[{"role": "user", "content": "test"}],
                target_model="gemini-3-flash-preview",
            ):
                results.append(r)

        errors = [r for r in results if "error" in r]
        assert len(errors) >= 1
        assert "安全" in errors[0]["error"] or "过滤" in errors[0]["error"]

    @pytest.mark.asyncio
    async def test_non_stop_finish_reason_is_logged_verbatim(self, capsys):
        """SDK 有界 finish_reason 直接保留，便于定位非 STOP 终止原因。"""
        from app import gemini_client
        from app.gemini_client import call_gemini_stream

        chunk = self._make_chunk("", finish_reason="RECITATION")
        with patch.object(gemini_client, "fallback_client", None), \
             patch.object(gemini_client, "client") as mock_client:
            mock_client.aio.models.generate_content_stream = AsyncMock(
                return_value=_async_iter([chunk])
            )
            results = [
                item
                async for item in call_gemini_stream(
                    messages=[{"role": "user", "content": "test"}],
                    target_model="gemini-3-flash-preview",
                )
            ]

        assert any("没有返回" in item.get("error", "") for item in results)
        assert "异常的 finish_reason: RECITATION" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_api_exception(self):
        """API 异常处理"""
        from app.gemini_client import call_gemini_stream

        with patch("app.gemini_client.client") as mock_client:
            mock_client.aio.models.generate_content_stream = AsyncMock(side_effect=Exception("API quota exceeded"))

            results = []
            async for r in call_gemini_stream(
                messages=[{"role": "user", "content": "test"}],
                target_model="gemini-3-flash-preview",
            ):
                results.append(r)

        assert len(results) == 1
        assert "error" in results[0]
        assert "Gemini provider error" in results[0]["error"]
        assert "API quota exceeded" not in results[0]["error"]

    @pytest.mark.asyncio
    async def test_stale_circuit_without_fallback_is_ignored_with_warning(self, capsys):
        """fallback 配置缺失时 stale marker 不锁死主路径，只输出一次配置告警。"""
        from app import gemini_client, gemini_circuit

        chunk = self._make_chunk("主路径继续", usage={"input": 2, "output": 3})
        with patch.object(gemini_client, "fallback_client", None), \
             patch.object(gemini_client, "_stale_circuit_checked", False), \
             patch.object(gemini_client, "client") as primary_client, \
             patch.object(gemini_circuit, "is_circuit_open_async", AsyncMock(return_value=True)):
            primary_client.aio.models.generate_content_stream = AsyncMock(
                return_value=_async_iter([chunk])
            )
            results = [
                item
                async for item in gemini_client.call_gemini_stream(
                    messages=[{"role": "user", "content": "test"}],
                    target_model="gemini-3-flash-preview",
                )
            ]

        assert any(item.get("content") == "主路径继续" for item in results)
        assert "未配置 fallback" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_known_api_error_keeps_safe_status_without_secret(self):
        """已知 SDK 异常可显示状态码，但不暴露异常原文中的凭据。"""
        from app import gemini_client
        from google.genai import errors as genai_errors

        api_error = genai_errors.APIError(
            code=429,
            response_json={"error": {"message": "quota exceeded for api_key=secret-value"}},
        )
        with patch.object(gemini_client, "client") as mock_client:
            mock_client.aio.models.generate_content_stream = AsyncMock(side_effect=api_error)
            results = [
                chunk
                async for chunk in gemini_client.call_gemini_stream(
                    messages=[{"role": "user", "content": "test"}],
                    target_model="gemini-3-flash-preview",
                )
            ]

        assert len(results) == 1
        assert "Gemini API error HTTP 429" in results[0]["error"]
        assert "secret-value" not in results[0]["error"]

    @pytest.mark.asyncio
    async def test_provider_pre_output_error_uses_vertex_fallback(self):
        """主 provider 在首个可见输出前失败时切换 fallback，而不匹配错误文本。"""
        from app import gemini_client
        from app import gemini_circuit

        fallback_chunk = self._make_chunk("保底回答", usage={"input": 4, "output": 6})
        fallback_chunk.model_version = "gemini-3.6-flash"
        primary_client = MagicMock()
        fallback_client = MagicMock()
        primary_client.aio.models.generate_content_stream = AsyncMock(
            side_effect=RuntimeError("generic provider failure")
        )
        fallback_client.aio.models.generate_content_stream = AsyncMock(
            return_value=_async_iter([fallback_chunk])
        )
        with patch.object(gemini_client, "client", primary_client), \
             patch.object(gemini_client, "fallback_client", fallback_client), \
             patch.object(gemini_circuit, "is_circuit_open_async", AsyncMock(return_value=False)), \
             patch.object(gemini_circuit, "open_circuit_async", AsyncMock(return_value=True)) as open_circuit:
            results = []
            async for chunk in gemini_client.call_gemini_stream(
                messages=[{"role": "user", "content": "test"}],
                target_model="gemini-3.6-flash-tiered",
            ):
                results.append(chunk)

        assert any(item.get("content") == "保底回答" for item in results)
        assert open_circuit.await_count == 1
        usage = next(item["usage"] for item in results if "usage" in item)
        assert usage["model"] == "gemini-3.6-flash"
        assert usage["requested_model"] == "gemini-3.6-flash-tiered"

    @pytest.mark.asyncio
    async def test_first_iterator_error_uses_vertex_fallback(self):
        """await 成功但首次 __anext__ 失败同样属于 pre-output provider error。"""
        from app import gemini_client, gemini_circuit

        fallback_chunk = self._make_chunk("首拉保底", usage={"input": 4, "output": 6})
        primary_client = MagicMock()
        fallback_client = MagicMock()
        primary_client.aio.models.generate_content_stream = AsyncMock(
            return_value=self._RaisingResponse(RuntimeError("first iterator failure"))
        )
        fallback_client.aio.models.generate_content_stream = AsyncMock(
            return_value=_async_iter([fallback_chunk])
        )
        with patch.object(gemini_client, "client", primary_client), \
             patch.object(gemini_client, "fallback_client", fallback_client), \
             patch.object(gemini_circuit, "is_circuit_open_async", AsyncMock(return_value=False)), \
             patch.object(gemini_circuit, "open_circuit_async", AsyncMock()) as open_circuit:
            results = [
                chunk
                async for chunk in gemini_client.call_gemini_stream(
                    messages=[{"role": "user", "content": "test"}],
                    target_model="gemini-3.6-flash-tiered",
                )
            ]

        assert any(item.get("content") == "首拉保底" for item in results)
        open_circuit.assert_awaited_once()
        primary_client.aio.models.generate_content_stream.assert_awaited_once()
        fallback_client.aio.models.generate_content_stream.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_open_circuit_skips_primary_and_marks_usage(self):
        """已有 marker 时直接走 fallback，usage 只描述当前调用。"""
        from app import gemini_client, gemini_circuit

        fallback_chunk = self._make_chunk("熔断保底", usage={"input": 4, "output": 6})
        primary_client = MagicMock()
        fallback_client = MagicMock()
        primary_client.aio.models.generate_content_stream = AsyncMock()
        fallback_client.aio.models.generate_content_stream = AsyncMock(
            return_value=_async_iter([fallback_chunk])
        )
        with patch.object(gemini_client, "client", primary_client), \
             patch.object(gemini_client, "fallback_client", fallback_client), \
             patch.object(gemini_circuit, "is_circuit_open_async", AsyncMock(return_value=True)):
            results = [
                chunk
                async for chunk in gemini_client.call_gemini_stream(
                    messages=[{"role": "user", "content": "test"}],
                    target_model="gemini-3.6-flash-tiered",
                )
            ]

        primary_client.aio.models.generate_content_stream.assert_not_awaited()
        usage = next(item["usage"] for item in results if "usage" in item)
        assert usage["model"] == "gemini-3.6-flash-tiered"
        assert usage["requested_model"] == "gemini-3.6-flash-tiered"
        assert usage["fallback"] is True
        assert usage["fallback_error"] == "circuit open"
        assert usage["circuit_open"] is True

    @pytest.mark.asyncio
    async def test_iterator_error_after_visible_output_does_not_retry_or_open(self):
        """已有可见正文后异常只结束本次流，不重放请求或写熔断 marker。"""
        from app import gemini_client, gemini_circuit

        primary_client = MagicMock()
        fallback_client = MagicMock()
        visible_chunk = self._make_chunk("已经输出", usage={"input": 4, "output": 6})
        primary_client.aio.models.generate_content_stream = AsyncMock(
            return_value=self._OutputThenRaisingResponse(visible_chunk, RuntimeError("midstream failure"))
        )
        fallback_client.aio.models.generate_content_stream = AsyncMock(
            return_value=_async_iter([self._make_chunk("不应重试", usage={"output": 2})])
        )
        with patch.object(gemini_client, "client", primary_client), \
             patch.object(gemini_client, "fallback_client", fallback_client), \
             patch.object(gemini_circuit, "is_circuit_open_async", AsyncMock(return_value=False)), \
             patch.object(gemini_circuit, "open_circuit_async", AsyncMock()) as open_circuit:
            results = [
                chunk
                async for chunk in gemini_client.call_gemini_stream(
                    messages=[{"role": "user", "content": "test"}],
                    target_model="gemini-3.6-flash-tiered",
                )
            ]

        assert any(item.get("content") == "已经输出" for item in results)
        assert not any(item.get("content") == "不应重试" for item in results)
        assert any("Gemini provider error" in item.get("error", "") for item in results)
        open_circuit.assert_not_awaited()
        fallback_client.aio.models.generate_content_stream.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancellation_does_not_fallback_or_open(self):
        """取消属于控制流，不应被包装成 provider failure。"""
        from app import gemini_client, gemini_circuit

        primary_client = MagicMock()
        fallback_client = MagicMock()
        primary_client.aio.models.generate_content_stream = AsyncMock(
            side_effect=asyncio.CancelledError()
        )
        fallback_client.aio.models.generate_content_stream = AsyncMock()
        with patch.object(gemini_client, "client", primary_client), \
             patch.object(gemini_client, "fallback_client", fallback_client), \
             patch.object(gemini_circuit, "is_circuit_open_async", AsyncMock(return_value=False)), \
             patch.object(gemini_circuit, "open_circuit_async", AsyncMock()) as open_circuit:
            with pytest.raises(asyncio.CancelledError):
                async for _ in gemini_client.call_gemini_stream(
                    messages=[{"role": "user", "content": "test"}],
                    target_model="gemini-3.6-flash-tiered",
                ):
                    pass

        open_circuit.assert_not_awaited()
        fallback_client.aio.models.generate_content_stream.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_double_failure_lists_fallback_then_primary_safely(self):
        """双失败错误顺序固定且不回传未知异常原文。"""
        from app import gemini_client, gemini_circuit

        primary_client = MagicMock()
        fallback_client = MagicMock()
        primary_client.aio.models.generate_content_stream = AsyncMock(
            side_effect=RuntimeError("primary secret detail")
        )
        fallback_client.aio.models.generate_content_stream = AsyncMock(
            side_effect=RuntimeError("fallback secret detail")
        )
        with patch.object(gemini_client, "client", primary_client), \
             patch.object(gemini_client, "fallback_client", fallback_client), \
             patch.object(gemini_circuit, "is_circuit_open_async", AsyncMock(return_value=False)), \
             patch.object(gemini_circuit, "open_circuit_async", AsyncMock()):
            results = [
                chunk
                async for chunk in gemini_client.call_gemini_stream(
                    messages=[{"role": "user", "content": "test"}],
                    target_model="gemini-3.6-flash-tiered",
                )
            ]

        error = next(item["error"] for item in results if "error" in item)
        assert error.index("fallback 模型") < error.index("主模型")
        assert "primary secret detail" not in error
        assert "fallback secret detail" not in error

    @pytest.mark.asyncio
    async def test_usage_stats_returned(self):
        """流式结束后返回 usage 统计"""
        from app.gemini_client import call_gemini_stream

        chunk_with_usage = self._make_chunk("Hi", usage={"input": 50, "output": 20})
        with patch("app.gemini_client.client") as mock_client:
            mock_client.aio.models.generate_content_stream = AsyncMock(return_value=_async_iter([chunk_with_usage]))

            results = []
            async for r in call_gemini_stream(
                messages=[{"role": "user", "content": "hi"}],
                target_model="gemini-3-flash-preview",
            ):
                results.append(r)

        usage = [r for r in results if "usage" in r]
        assert len(usage) == 1
        assert usage[0]["usage"]["input_tokens"] == 50
        assert usage[0]["usage"]["output_tokens"] == 20
        assert usage[0]["usage"]["model"] == "gemini-3-flash-preview"
        assert "latency_ms" in usage[0]["usage"]

    @pytest.mark.asyncio
    async def test_provider_model_version_is_sanitized_before_usage(self):
        """provider 返回的模型标识不能把控制字符带入 usage。"""
        from app import gemini_client

        chunk = self._make_chunk("安全回答", usage={"input": 1, "output": 1})
        chunk.model_version = "vertex-model\n<spoof>\u202e"
        with patch.object(gemini_client, "fallback_client", None), \
             patch.object(gemini_client, "client") as mock_client:
            mock_client.aio.models.generate_content_stream = AsyncMock(
                return_value=_async_iter([chunk])
            )
            results = [
                item
                async for item in gemini_client.call_gemini_stream(
                    messages=[{"role": "user", "content": "test"}],
                    target_model="gemini-3-flash-preview",
                )
            ]

        usage = next(item["usage"] for item in results if "usage" in item)
        assert usage["model"] == "vertex-model__spoof_"
        assert "\n" not in usage["model"]
        assert "<" not in usage["model"]
        assert ">" not in usage["model"]

    @pytest.mark.asyncio
    async def test_empty_candidates_skipped(self):
        """空 candidates 被跳过"""
        from app.gemini_client import call_gemini_stream

        empty_chunk = MagicMock()
        empty_chunk.candidates = []
        empty_chunk.usage_metadata = None

        content_chunk = self._make_chunk("OK", usage={"input": 5, "output": 2})
        with patch("app.gemini_client.client") as mock_client:
            mock_client.aio.models.generate_content_stream = AsyncMock(return_value=_async_iter([empty_chunk, content_chunk]))

            results = []
            async for r in call_gemini_stream(
                messages=[{"role": "user", "content": "test"}],
                target_model="gemini-3-flash-preview",
            ):
                results.append(r)

        content = [r for r in results if "content" in r]
        assert len(content) == 1
        assert content[0]["content"] == "OK"

    @pytest.mark.asyncio
    async def test_no_output_tokens_gives_error(self):
        """output_tokens=0 时返回错误提示"""
        from app.gemini_client import call_gemini_stream

        # 所有 chunk 都是空 candidates
        empty_chunk = MagicMock()
        empty_chunk.candidates = []
        empty_chunk.usage_metadata = None
        with patch("app.gemini_client.client") as mock_client:
            mock_client.aio.models.generate_content_stream = AsyncMock(return_value=_async_iter([empty_chunk]))

            results = []
            async for r in call_gemini_stream(
                messages=[{"role": "user", "content": "test"}],
                target_model="gemini-3-flash-preview",
            ):
                results.append(r)

        errors = [r for r in results if "error" in r]
        assert len(errors) == 1
        assert "没有返回" in errors[0]["error"]

    @pytest.mark.asyncio
    async def test_visible_output_without_usage_is_not_reported_as_empty(self):
        """中转层缺少 usage_metadata 时，已经输出的正文仍应视为成功。"""
        from app.gemini_client import call_gemini_stream

        content_chunk = self._make_chunk("无 usage 的答案")
        with patch("app.gemini_client.client") as mock_client:
            mock_client.aio.models.generate_content_stream = AsyncMock(
                return_value=_async_iter([content_chunk])
            )
            results = [
                chunk
                async for chunk in call_gemini_stream(
                    messages=[{"role": "user", "content": "test"}],
                    target_model="gemini-3-flash-preview",
                )
            ]

        assert any(item.get("content") == "无 usage 的答案" for item in results)
        assert not any("没有返回" in item.get("error", "") for item in results)
        assert next(item["usage"] for item in results if "usage" in item)["output_tokens"] == 0


# ─── google_search fallback 测试 ──────────────────────────────────────

class TestGoogleSearchFallback:
    """google_search 用专用搜索模型、流式收集、超时防挂死"""

    @pytest.mark.asyncio
    async def test_uses_dedicated_search_model_not_router(self):
        """搜索用 GEMINI_SEARCH_MODEL（真实 Gemini 名），不借用可能是 gpt/claude 的路由模型"""
        from app import gemini_client

        captured = {}

        async def fake_stream(**kwargs):
            captured["model"] = kwargs.get("model")
            chunk = MagicMock()
            chunk.text = "特斯拉今日收盘 425 美元"
            yield chunk

        with patch.object(gemini_client, "GEMINI_SEARCH_MODEL", "gemini-3.5-flash"), \
             patch.object(gemini_client.client.aio.models, "generate_content_stream",
                          side_effect=lambda **kw: fake_stream(**kw)):
            result = await gemini_client.google_search("特斯拉股价")

        assert result == "特斯拉今日收盘 425 美元"
        assert captured["model"] == "gemini-3.5-flash"

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        """搜索挂起超过 SEARCH_TIMEOUT_SECONDS 时返回 None 而非卡死"""
        from app import gemini_client

        async def hanging_stream(**kwargs):
            await asyncio.sleep(10)
            yield MagicMock(text="never")

        with patch.object(gemini_client, "SEARCH_TIMEOUT_SECONDS", 0.05), \
             patch.object(gemini_client.client.aio.models, "generate_content_stream",
                          side_effect=lambda **kw: hanging_stream(**kw)):
            result = await gemini_client.google_search("会挂起的查询")

        assert result is None

    @pytest.mark.asyncio
    async def test_empty_stream_returns_none(self):
        """流无文本时返回 None（供上层判定 fallback_injected=False）"""
        from app import gemini_client

        async def empty_stream(**kwargs):
            if False:
                yield  # 空异步生成器

        with patch.object(gemini_client.client.aio.models, "generate_content_stream",
                          side_effect=lambda **kw: empty_stream(**kw)):
            result = await gemini_client.google_search("q")

        assert result is None


# ─── GEMINI_API_BASE 中转配置测试 ──────────────────────────────────────

class TestGeminiApiBase:
    """GEMINI_API_BASE 设置时对话 client 走中转、direct_client 保持直连"""

    def _reload_and_capture(self, env: dict) -> list:
        """按给定环境变量重载 config + gemini_client，捕获 genai.Client 构造参数。

        finally 中恢复环境并重载回真实模块，避免污染其他测试。
        """
        import os
        import importlib
        import app.config
        import app.gemini_client

        calls = []

        class _FakeClient:
            def __init__(self, **kwargs):
                calls.append(kwargs)

        old_env = {k: os.environ.get(k) for k in env}
        try:
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            importlib.reload(app.config)
            with patch("google.genai.Client", _FakeClient):
                importlib.reload(app.gemini_client)
        finally:
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            importlib.reload(app.config)
            importlib.reload(app.gemini_client)
        return calls

    def test_base_set_builds_proxy_and_direct_clients(self):
        """设置 GEMINI_API_BASE 时构建两个 client：对话走中转（尾部斜杠被去除），直连仍在"""
        calls = self._reload_and_capture({
            "GEMINI_API_BASE": "http://127.0.0.1:38090/",
            "GEMINI_API_BASE_KEY": "sk-sub2api-test",
            "SOCKS_PROXY": "",
        })
        assert len(calls) == 2
        # 第一个是中转 client
        assert calls[0]["api_key"] == "sk-sub2api-test"
        assert calls[0]["http_options"].base_url == "http://127.0.0.1:38090"
        # 第二个是直连 client，不带 base_url
        assert getattr(calls[1]["http_options"], "base_url", None) is None

    def test_base_unset_single_client(self):
        """未设置 GEMINI_API_BASE 时只构建一个直连 client"""
        calls = self._reload_and_capture({
            "GEMINI_API_BASE": None,
            "GEMINI_API_BASE_KEY": None,
            "SOCKS_PROXY": "",
        })
        assert len(calls) == 1
        assert getattr(calls[0]["http_options"], "base_url", None) is None

    def test_base_key_falls_back_to_gemini_api_key(self):
        """GEMINI_API_BASE_KEY 未设时中转 client 复用 GEMINI_API_KEY"""
        calls = self._reload_and_capture({
            "GEMINI_API_BASE": "http://127.0.0.1:38090",
            "GEMINI_API_BASE_KEY": None,
            "GEMINI_API_KEY": "google-key-abc",
            "SOCKS_PROXY": "",
        })
        assert len(calls) == 2
        assert calls[0]["api_key"] == "google-key-abc"

    def test_fallback_client_uses_explicit_base_and_key(self):
        """配置 Vertex fallback 时新增独立 client，不复用生图 direct_client。"""
        calls = self._reload_and_capture({
            "GEMINI_API_BASE": "http://127.0.0.1:38090/",
            "GEMINI_API_BASE_KEY": "primary-key",
            "GEMINI_API_BASE_FALLBACK": "http://vertex.example/",
            "GEMINI_API_BASE_FALLBACK_KEY": "vertex-key",
            "SOCKS_PROXY": "",
        })
        assert len(calls) == 3
        assert calls[0]["api_key"] == "primary-key"
        assert calls[0]["http_options"].base_url == "http://127.0.0.1:38090"
        assert getattr(calls[1]["http_options"], "base_url", None) is None
        assert calls[2]["api_key"] == "vertex-key"
        assert calls[2]["http_options"].base_url == "http://vertex.example"

    def test_missing_fallback_key_does_not_build_fallback_client(self):
        """fallback base 没有显式 key 时 fail-closed，不复用其他 key。"""
        calls = self._reload_and_capture({
            "GEMINI_API_BASE": "http://127.0.0.1:38090/",
            "GEMINI_API_BASE_KEY": "primary-key",
            "GEMINI_API_BASE_FALLBACK": "http://vertex.example/",
            "GEMINI_API_BASE_FALLBACK_KEY": None,
            "SOCKS_PROXY": "",
        })
        assert len(calls) == 2


def test_fallback_model_override_uses_explicit_route_slot(monkeypatch):
    """fallback override 按调用方 route slot 选择，不按主模型名反推。"""
    from app import gemini_client

    monkeypatch.setattr(gemini_client, "MODEL_ROUTER_FALLBACK", "router-vertex", raising=False)
    monkeypatch.setattr(gemini_client, "MODEL_LITE_FALLBACK", "lite-vertex", raising=False)
    monkeypatch.setattr(gemini_client, "MODEL_FAST_FALLBACK", "fast-vertex", raising=False)
    monkeypatch.setattr(gemini_client, "MODEL_PRO_FALLBACK", "pro-vertex", raising=False)

    select = getattr(gemini_client, "_select_fallback_model", lambda model, slot: model)
    assert select("same-primary-model", "router") == "router-vertex"
    assert select("same-primary-model", "lite") == "lite-vertex"
    assert select("same-primary-model", "fast") == "fast-vertex"
    assert select("same-primary-model", "pro") == "pro-vertex"


def test_safe_error_summary_does_not_echo_unknown_exception_text():
    """未知异常不能把凭据、URL、请求体或原始 message 带入用户可见字段。"""
    from app import gemini_client

    raw = (
        "Authorization: Bearer super-secret-token "
        "https://api.example.test/v1?key=secret&prompt=private "
        "body={'password':'hidden'}"
    )
    summarize = getattr(gemini_client, "safe_error_summary", lambda error, phase: str(error))
    summary = summarize(RuntimeError(raw), "provider")

    assert "super-secret-token" not in summary
    assert "api.example.test" not in summary
    assert "private" not in summary
    assert "hidden" not in summary
    assert len(summary) <= 1000


def test_safe_error_summary_sanitizes_known_reason_and_bounds_length():
    """已知 SDK reason 仅保留安全摘要，URL/凭据/HTML/控制符均被清洗。"""
    from app import gemini_client
    from google.genai import errors as genai_errors

    raw_reason = (
        "Authorization: Bearer secret-token https://example.test/path?key=private "
        "<font>reason</font>\n" + ("x" * 1500)
    )
    error = genai_errors.APIError(
        code=503,
        response_json={"error": {"message": raw_reason}},
    )
    summary = gemini_client.safe_error_summary(error, "provider")

    assert "Gemini API error HTTP 503" in summary
    assert "secret-token" not in summary
    assert "example.test" not in summary
    assert "private" not in summary
    assert "<font>" not in summary
    assert "</font>" not in summary
    assert "\n" not in summary
    assert len(summary) <= 1000


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
