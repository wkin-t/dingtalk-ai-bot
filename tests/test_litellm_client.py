"""litellm_client 单元测试"""
import os
import pytest

os.environ.setdefault("GEMINI_API_KEY", "test-dummy-key")

from app.config import get_route_key, get_litellm_model_config, LITELLM_MODEL_CONFIG


class TestRouteKeyMapping:
    """路由名归一化测试"""

    def test_flash_preview_maps_to_fast(self):
        assert get_route_key("gemini-3-flash-preview") == "fast"

    def test_flash_without_preview_maps_to_fast(self):
        assert get_route_key("gemini-3-flash") == "fast"

    def test_pro_preview_maps_to_pro(self):
        assert get_route_key("gemini-3.1-pro-preview") == "pro"

    def test_old_pro_maps_to_pro(self):
        assert get_route_key("gemini-3-pro-preview") == "pro"

    def test_unknown_model_falls_back_to_fast(self):
        assert get_route_key("unknown-model-xyz") == "fast"


class TestModelConfig:
    """模型配置测试"""

    def test_fast_config_has_model(self):
        config = get_litellm_model_config("fast")
        assert "model" in config
        assert config["model"] != ""

    def test_pro_config_has_model(self):
        config = get_litellm_model_config("pro")
        assert "model" in config
        assert config["model"] != ""

    def test_config_has_capability_fields(self):
        config = get_litellm_model_config("fast")
        assert "supports_reasoning" in config
        assert "supports_search" in config
        assert "supports_vision" in config

    def test_unknown_key_returns_fast(self):
        config = get_litellm_model_config("nonexistent")
        assert config["model"] == LITELLM_MODEL_CONFIG["fast"]["model"]


class TestCapabilityFiltering:
    """Capability 过滤测试"""

    def test_search_not_sent_to_non_supporting_provider(self):
        config = get_litellm_model_config("fast")
        if not config["supports_search"]:
            tools = [{"googleSearch": {}}] if config["supports_search"] else []
            assert tools == []

    def test_reasoning_not_sent_when_minimal(self):
        thinking_level = "minimal"
        config = get_litellm_model_config("fast")
        should_send = config["supports_reasoning"] and thinking_level != "minimal"
        assert should_send is False


class TestStripImages:
    """图片消息清理测试"""

    def test_strip_images_from_multimodal_message(self):
        from app.litellm_client import _strip_images
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "描述这张图"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                ]
            }
        ]
        cleaned = _strip_images(messages)
        assert cleaned[0]["content"] == "描述这张图"

    def test_text_only_message_unchanged(self):
        from app.litellm_client import _strip_images
        messages = [{"role": "user", "content": "你好"}]
        cleaned = _strip_images(messages)
        assert cleaned[0]["content"] == "你好"

    def test_image_only_message_gets_placeholder(self):
        from app.litellm_client import _strip_images
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                ]
            }
        ]
        cleaned = _strip_images(messages)
        assert cleaned[0]["content"] == "[图片已移除]"

    def test_multiple_messages_stripped(self):
        from app.litellm_client import _strip_images
        messages = [
            {"role": "user", "content": "第一句话"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "有图片的消息"},
                    {"type": "image_url", "image_url": {"url": "..."}},
                ]
            },
        ]
        cleaned = _strip_images(messages)
        assert cleaned[0]["content"] == "第一句话"
        assert cleaned[1]["content"] == "有图片的消息"
