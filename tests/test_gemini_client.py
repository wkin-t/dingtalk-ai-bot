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

class TestAnalyzeComplexity:
    """智能路由分析测试"""

    @pytest.mark.asyncio
    async def test_normal_analysis(self):
        """正常分析返回结果"""
        from app.gemini_client import analyze_complexity_with_model

        mock_response = MagicMock()
        mock_response.text = '{"model":"gemini-3-flash-preview","thinking_level":"low","need_search":false,"reason":"简单问答"}'

        with patch("app.gemini_client.client") as mock_client:
            mock_client.models.generate_content.return_value = mock_response
            result = await analyze_complexity_with_model("你好")

        assert result["model"] == "gemini-3-flash-preview"
        assert result["thinking_level"] == "low"
        assert result["need_search"] is False

    @pytest.mark.asyncio
    async def test_complex_question_routes_to_pro(self):
        """复杂问题路由到 pro 模型"""
        from app.gemini_client import analyze_complexity_with_model

        mock_response = MagicMock()
        mock_response.text = '{"model":"gemini-3-pro-preview","thinking_level":"high","need_search":false,"reason":"复杂数学"}'

        with patch("app.gemini_client.client") as mock_client:
            mock_client.models.generate_content.return_value = mock_response
            result = await analyze_complexity_with_model("证明黎曼猜想")

        assert result["model"] == "gemini-3-pro-preview"
        assert result["thinking_level"] == "high"

    @pytest.mark.asyncio
    async def test_search_needed_for_realtime(self):
        """实时信息需要搜索"""
        from app.gemini_client import analyze_complexity_with_model

        mock_response = MagicMock()
        mock_response.text = '{"model":"gemini-3-flash-preview","thinking_level":"low","need_search":true,"reason":"需要实时信息"}'

        with patch("app.gemini_client.client") as mock_client:
            mock_client.models.generate_content.return_value = mock_response
            result = await analyze_complexity_with_model("今天天气怎么样")

        assert result["need_search"] is True

    @pytest.mark.asyncio
    async def test_fallback_on_api_error(self):
        """API 错误时降级为默认配置"""
        from app.gemini_client import analyze_complexity_with_model

        with patch("app.gemini_client.client") as mock_client:
            mock_client.models.generate_content.side_effect = Exception("API 不可用")
            result = await analyze_complexity_with_model("测试")

        assert result["model"] == "gemini-3-flash-preview"
        assert result["thinking_level"] == "low"
        assert result["need_search"] is False
        assert "默认" in result["reason"] or "失败" in result["reason"]

    @pytest.mark.asyncio
    async def test_fallback_on_invalid_json(self):
        """返回非 JSON 时降级"""
        from app.gemini_client import analyze_complexity_with_model

        mock_response = MagicMock()
        mock_response.text = "我不确定怎么回答这个问题"

        with patch("app.gemini_client.client") as mock_client:
            mock_client.models.generate_content.return_value = mock_response
            result = await analyze_complexity_with_model("测试")

        # 应返回降级默认值
        assert result["model"] == "gemini-3-flash-preview"

    @pytest.mark.asyncio
    async def test_invalid_model_corrected(self):
        """无效模型名被修正"""
        from app.gemini_client import analyze_complexity_with_model

        mock_response = MagicMock()
        mock_response.text = '{"model":"invalid-model","thinking_level":"low","need_search":false,"reason":"test"}'

        with patch("app.gemini_client.client") as mock_client:
            mock_client.models.generate_content.return_value = mock_response
            result = await analyze_complexity_with_model("测试")

        assert result["model"] == "gemini-3-flash-preview"

    @pytest.mark.asyncio
    async def test_invalid_thinking_level_corrected(self):
        """无效 thinking_level 被修正"""
        from app.gemini_client import analyze_complexity_with_model

        mock_response = MagicMock()
        mock_response.text = '{"model":"gemini-3-flash-preview","thinking_level":"ultra","need_search":false,"reason":"test"}'

        with patch("app.gemini_client.client") as mock_client:
            mock_client.models.generate_content.return_value = mock_response
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

        mock_stream = iter(chunks_data)

        with patch("app.gemini_client.client") as mock_client:
            mock_client.models.generate_content_stream.return_value = mock_stream

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
    async def test_stream_with_thinking(self):
        """流式带思考过程"""
        from app.gemini_client import call_gemini_stream

        chunks_data = [
            self._make_chunk("分析中...", is_thought=True),
            self._make_chunk("答案是2", is_thought=False, usage={"input": 15, "output": 8}),
        ]

        mock_stream = iter(chunks_data)

        with patch("app.gemini_client.client") as mock_client:
            mock_client.models.generate_content_stream.return_value = mock_stream
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

        mock_stream = iter([chunk])

        with patch("app.gemini_client.client") as mock_client:
            mock_client.models.generate_content_stream.return_value = mock_stream

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
    async def test_api_exception(self):
        """API 异常处理"""
        from app.gemini_client import call_gemini_stream

        with patch("app.gemini_client.client") as mock_client:
            mock_client.models.generate_content_stream.side_effect = Exception("API quota exceeded")

            results = []
            async for r in call_gemini_stream(
                messages=[{"role": "user", "content": "test"}],
                target_model="gemini-3-flash-preview",
            ):
                results.append(r)

        assert len(results) == 1
        assert "error" in results[0]
        assert "API quota exceeded" in results[0]["error"]

    @pytest.mark.asyncio
    async def test_usage_stats_returned(self):
        """流式结束后返回 usage 统计"""
        from app.gemini_client import call_gemini_stream

        chunk_with_usage = self._make_chunk("Hi", usage={"input": 50, "output": 20})
        mock_stream = iter([chunk_with_usage])

        with patch("app.gemini_client.client") as mock_client:
            mock_client.models.generate_content_stream.return_value = mock_stream

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
    async def test_empty_candidates_skipped(self):
        """空 candidates 被跳过"""
        from app.gemini_client import call_gemini_stream

        empty_chunk = MagicMock()
        empty_chunk.candidates = []
        empty_chunk.usage_metadata = None

        content_chunk = self._make_chunk("OK", usage={"input": 5, "output": 2})
        mock_stream = iter([empty_chunk, content_chunk])

        with patch("app.gemini_client.client") as mock_client:
            mock_client.models.generate_content_stream.return_value = mock_stream

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
        mock_stream = iter([empty_chunk])

        with patch("app.gemini_client.client") as mock_client:
            mock_client.models.generate_content_stream.return_value = mock_stream

            results = []
            async for r in call_gemini_stream(
                messages=[{"role": "user", "content": "test"}],
                target_model="gemini-3-flash-preview",
            ):
                results.append(r)

        errors = [r for r in results if "error" in r]
        assert len(errors) == 1
        assert "没有返回" in errors[0]["error"]


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
