"""dingtalk_bot 辅助函数单元测试"""
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("GEMINI_API_KEY", "test-dummy-key")


async def _iter_consumer_chunks(chunks):
    for chunk in chunks:
        yield chunk


async def _run_dingtalk_consumer(chunks):
    """运行真实 DingTalk handler 收尾逻辑，返回最终卡片全量更新数据。"""
    from app.dingtalk_bot import GeminiBotHandler

    handler = object.__new__(GeminiBotHandler)
    handler.card_template_id = "test-template"
    handler.card_helper = MagicMock()
    handler.card_helper.create_and_deliver = AsyncMock(return_value="track-test")
    handler.card_helper.stream_update = AsyncMock()
    handler.card_helper.update_card = AsyncMock(return_value=True)
    incoming_message = SimpleNamespace(
        sender_id="sender-test",
        sender_nick="测试用户",
        conversation_id="conversation-test",
        conversation_type="1",
    )

    route_result = {
        "model": "fast",
        "thinking_level": "medium",
        "need_search": False,
        "temperature": "balanced",
        "thinking_text": "正在核对",
        "reason": "test",
    }

    with patch("app.dingtalk_bot.AI_BACKEND", "openai"), \
         patch("app.dingtalk_bot.DINGTALK_TYPING_ENABLED", False), \
         patch("app.dingtalk_bot.DINGTALK_REFERENCE_AUTO_ENABLED", False), \
         patch("app.dingtalk_bot.USE_STATS", False), \
         patch("app.dingtalk_bot.get_session_key", return_value="session-test"), \
         patch("app.dingtalk_bot.get_history", return_value=[]), \
         patch("app.dingtalk_bot._load_soul", return_value=""), \
         patch("app.dingtalk_bot._analyze_with_openai", new_callable=AsyncMock, return_value=route_result), \
         patch("app.ai.messages_pipeline.prepare_messages_for_backend", side_effect=lambda messages, _bot_id: messages), \
         patch("app.ai.sampling_pipeline.resolve_sampling", return_value=(0.7, None, {})), \
         patch("app.ai.backend.create_backend_stream", return_value=_iter_consumer_chunks(chunks)), \
         patch("app.dingtalk_bot.update_history"), \
         patch("app.dingtalk_bot._maybe_evolve_soul", new=MagicMock(return_value=None)), \
         patch("app.dingtalk_bot.asyncio.create_task"):
        await handler.handle_ai_stream(
            incoming_message,
            "测试消息",
            "conversation-test",
            [],
        )

    return handler.card_helper.update_card.await_args_list[-1].args[1]


@pytest.mark.asyncio
async def test_dingtalk_consumer_promotes_reasoning_summary_to_final_status():
    """Responses summary chunk 应进入 DingTalk full_thinking 和最终摘要。"""
    final_update = await _run_dingtalk_consumer([
        {"thinking_start": True},
        {"thinking": "核对证据"},
        {"thinking": "并形成结论"},
        {"thinking_end": True},
        {"content": "最终回答"},
        {"usage": {"model": "claude-opus-4-6-thinking", "input_tokens": 1, "output_tokens": 1}},
    ])

    assert "最终回答" in final_update["msgContent"]
    assert "<font color='#aaaaaa' size='2'>🧠 核对证据并形成结论</font>" in final_update["statusText"]


@pytest.mark.asyncio
async def test_dingtalk_consumer_without_reasoning_has_no_fake_summary():
    """没有 reasoning chunk 时，DingTalk statusText 不应凭空生成思考摘要。"""
    final_update = await _run_dingtalk_consumer([
        {"content": "普通回答"},
        {"usage": {"model": "claude-opus-4-6-thinking", "input_tokens": 1, "output_tokens": 1}},
    ])

    assert "普通回答" in final_update["msgContent"]
    assert "<font color='#aaaaaa' size='2'>🧠" not in final_update["statusText"]



class TestShortenModelName:
    """_shorten_model_name 模型名归一化测试"""

    def test_openrouter_anthropic_prefix_stripped(self):
        from app.dingtalk_bot import _shorten_model_name
        assert _shorten_model_name("anthropic/claude-haiku-4-5") == "claude-haiku-4-5"

    def test_openrouter_openai_prefix_stripped(self):
        from app.dingtalk_bot import _shorten_model_name
        assert _shorten_model_name("openai/gpt-4o") == "gpt-4o"

    def test_vertex_version_suffix_stripped(self):
        from app.dingtalk_bot import _shorten_model_name
        assert _shorten_model_name("anthropic/claude-sonnet-4-5@20250929") == "claude-sonnet-4-5"

    def test_beta_suffix_stripped(self):
        from app.dingtalk_bot import _shorten_model_name
        assert _shorten_model_name("anthropic/claude-haiku-4-5:beta") == "claude-haiku-4-5"

    def test_gemini_prefix_stripped(self):
        from app.dingtalk_bot import _shorten_model_name
        assert _shorten_model_name("gemini-3-flash-preview") == "3-flash"

    def test_gemini_pro_preview_stripped(self):
        from app.dingtalk_bot import _shorten_model_name
        assert _shorten_model_name("gemini-3.1-pro-preview") == "3.1-pro"

    def test_plain_model_name_unchanged(self):
        from app.dingtalk_bot import _shorten_model_name
        assert _shorten_model_name("gpt-4o") == "gpt-4o"

    def test_provider_prefix_with_version_and_suffix(self):
        from app.dingtalk_bot import _shorten_model_name
        result = _shorten_model_name("vertex_ai/claude-opus-4-5@20260101:extended")
        assert result == "claude-opus-4-5"

    def test_no_slash_no_at_no_colon(self):
        from app.dingtalk_bot import _shorten_model_name
        assert _shorten_model_name("claude-haiku-4-5") == "claude-haiku-4-5"


class TestModelStatus:
    """模型 footer 的显示顺序和安全编码。"""

    def test_fallback_shows_actual_then_primary_error(self):
        from app.dingtalk_bot import _build_model_status

        status = _build_model_status(
            {
                "model": "gemini-3.6-flash",
                "requested_model": "gemini-3.6-flash-tiered",
                "fallback": True,
                "fallback_error": "Gemini server error HTTP 503: upstream </font>\n\u202e details",
            },
            "unused-primary",
        )

        assert status.index("🤖 gemini-3.6-flash") < status.index("主模型 gemini-3.6-flash-tiered")
        assert "&lt;/font&gt;" in status
        assert "\u202e" not in status
        assert "Gemini server error HTTP 503" in status

    def test_open_circuit_hides_historical_error(self):
        from app.dingtalk_bot import _build_model_status

        status = _build_model_status(
            {
                "model": "gemini-3.6-flash",
                "requested_model": "gemini-3.6-flash-tiered",
                "fallback": True,
                "circuit_open": True,
                "fallback_error": "historical secret HTTP 503",
            },
            "unused-primary",
        )

        assert "🤖 gemini-3.6-flash" in status
        assert "主模型 gemini-3.6-flash-tiered: circuit open" in status
        assert "HTTP" not in status

    def test_primary_status_keeps_existing_shape(self):
        from app.dingtalk_bot import _build_model_status

        assert _build_model_status({"model": "gemini-3.6-flash"}, "unused") == "🤖 3.6-flash"

    def test_error_card_display_preserves_markdown_newlines(self):
        from app.dingtalk_bot import safe_display_text

        rendered = safe_display_text(
            "❌ fallback 模型异常\n\n主模型异常\n1. 图片触发安全过滤\n2. 请稍后重试",
            1000,
            keep_newlines=True,
        )

        assert "fallback 模型异常\n\n主模型异常" in rendered
        assert "1. 图片触发安全过滤\n2. 请稍后重试" in rendered
