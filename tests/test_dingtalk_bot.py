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
