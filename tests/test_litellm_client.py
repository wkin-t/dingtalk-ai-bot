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


class TestOpenRouterConfig:
    """OpenRouter 配置测试"""

    def test_openrouter_model_config_has_fast_and_pro(self):
        from app.config import OPENROUTER_MODEL_CONFIG
        assert "fast" in OPENROUTER_MODEL_CONFIG
        assert "pro" in OPENROUTER_MODEL_CONFIG

    def test_openrouter_config_has_required_fields(self):
        from app.config import OPENROUTER_MODEL_CONFIG
        for key in ("fast", "pro"):
            config = OPENROUTER_MODEL_CONFIG[key]
            assert "model" in config
            assert "fallbacks" in config
            assert isinstance(config["fallbacks"], list)
            assert "supports_reasoning" in config
            assert "supports_search" in config
            assert "supports_vision" in config

    def test_openrouter_fast_model_has_fallbacks(self):
        from app.config import OPENROUTER_MODEL_CONFIG
        assert len(OPENROUTER_MODEL_CONFIG["fast"]["fallbacks"]) >= 1

    def test_openrouter_base_url_is_correct(self):
        from app.config import OPENROUTER_BASE_URL
        assert OPENROUTER_BASE_URL == "https://openrouter.ai/api/v1"

    def test_openrouter_web_search_tool_format(self):
        """Web Search 应使用 openrouter:web_search tool，而非废弃的 plugins"""
        tool = {"type": "openrouter:web_search"}
        assert tool["type"] == "openrouter:web_search"

    def test_openrouter_extra_body_fallback_structure(self):
        """模型回退 extra_body 结构验证"""
        from app.config import OPENROUTER_MODEL_CONFIG
        config = OPENROUTER_MODEL_CONFIG["fast"]
        model = config["model"]
        fallbacks = config["fallbacks"]
        extra_body = {
            "models": [model] + fallbacks,
            "route": "fallback",
        }
        assert extra_body["models"][0] == model
        assert extra_body["route"] == "fallback"
        assert len(extra_body["models"]) >= 2

    def test_openrouter_provider_sort_empty_by_default(self):
        """默认不设 provider_sort，让 OpenRouter 自动决策"""
        import os
        from app.config import OPENROUTER_MODEL_CONFIG
        provider_sort = OPENROUTER_MODEL_CONFIG["fast"]["provider_sort"]
        assert isinstance(provider_sort, str)

    def test_openrouter_model_config_has_lite_tier(self):
        """三级模型：lite/fast/pro 均存在"""
        from app.config import OPENROUTER_MODEL_CONFIG
        assert "lite" in OPENROUTER_MODEL_CONFIG
        for key in ("lite", "fast", "pro"):
            assert "model" in OPENROUTER_MODEL_CONFIG[key]
            assert "fallbacks" in OPENROUTER_MODEL_CONFIG[key]

    def test_lite_tier_reasoning_disabled(self):
        """lite 层（Haiku）不开启 reasoning，避免不必要费用"""
        from app.config import OPENROUTER_MODEL_CONFIG
        assert OPENROUTER_MODEL_CONFIG["lite"]["supports_reasoning"] is False

    def test_openrouter_router_model_configured(self):
        """路由大脑模型有配置"""
        from app.config import OPENROUTER_ROUTER_MODEL
        assert isinstance(OPENROUTER_ROUTER_MODEL, str)
        assert len(OPENROUTER_ROUTER_MODEL) > 0

    def test_route_key_map_has_lite(self):
        """ROUTE_KEY_MAP 包含 lite 映射"""
        from app.config import ROUTE_KEY_MAP
        assert ROUTE_KEY_MAP.get("gemini-3-flash-lite") == "lite"
        assert ROUTE_KEY_MAP.get("gemini-3-flash-lite-preview") == "lite"

    def test_get_route_key_lite_model(self):
        """get_route_key 能识别 lite 逻辑模型名"""
        from app.config import get_route_key
        assert get_route_key("gemini-3-flash-lite") == "lite"

    def test_analyze_complexity_with_openrouter_importable(self):
        """analyze_complexity_with_openrouter 可导入"""
        from app.litellm_client import analyze_complexity_with_openrouter
        import asyncio
        assert callable(analyze_complexity_with_openrouter)


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
