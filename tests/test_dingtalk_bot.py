"""dingtalk_bot 辅助函数单元测试"""
import os
import pytest

os.environ.setdefault("GEMINI_API_KEY", "test-dummy-key")



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
