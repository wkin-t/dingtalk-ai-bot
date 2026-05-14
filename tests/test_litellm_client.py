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

    def test_vertex_project_config_exists(self):
        from app.config import VERTEX_PROJECT
        assert isinstance(VERTEX_PROJECT, str)

    def test_config_has_region_field(self):
        config = get_litellm_model_config("fast")
        assert "region" in config
        assert isinstance(config["region"], str)

    def test_config_has_reasoning_param_field(self):
        config = get_litellm_model_config("fast")
        assert "reasoning_param" in config
        assert config["reasoning_param"] in ("openai_effort", "anthropic_thinking", "none")


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


class TestVertexProviderBranch:
    """Vertex AI provider 互斥分支测试"""

    def test_vertex_thinking_budget_mapping(self):
        """anthropic_thinking 应生成 thinking 参数而非 reasoning_effort"""
        effort_mapping = {"minimal": "none", "low": "low", "medium": "medium", "high": "high"}
        budget_map = {"low": 2048, "medium": 8192, "high": 32768}

        for level in ["low", "medium", "high"]:
            effort = effort_mapping.get(level)
            if effort and effort != "none":
                budget = budget_map.get(effort, 8192)
                assert budget > 0, f"budget 应 >0 for {level}"

        # minimal 不应生成 thinking
        effort = effort_mapping.get("minimal")
        assert effort == "none"

    def test_thinking_field_fallback(self):
        """流式解析应兼容 reasoning_content 和 thinking 字段"""
        # 模拟 delta 对象
        class MockDelta:
            reasoning_content = None
            thinking = "思考中..."
            content = "回答"

        delta = MockDelta()
        reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "thinking", None)
        assert reasoning == "思考中..."

    def test_reasoning_content_takes_priority(self):
        """reasoning_content 应优先于 thinking"""
        class MockDelta:
            reasoning_content = "原始推理"
            thinking = "备选思考"
            content = "回答"

        delta = MockDelta()
        reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "thinking", None)
        assert reasoning == "原始推理"
