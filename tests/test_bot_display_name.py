"""get_bot_display_name 单元测试 — 验证 s2a 透传场景按模型名推断显示名称"""
import os
import pytest
from unittest.mock import patch

os.environ.setdefault("GEMINI_API_KEY", "test-dummy-key")

import app.config as cfg


class TestGetBotDisplayName:

    def test_gemini_backend(self):
        with patch.object(cfg, "AI_BACKEND", "gemini"), patch.object(cfg, "BOT_NAME", ""):
            assert cfg.get_bot_display_name() == "Gem"

    def test_openclaw_backend(self):
        with patch.object(cfg, "AI_BACKEND", "openclaw"), patch.object(cfg, "BOT_NAME", ""):
            assert cfg.get_bot_display_name() == "Claw"

    def test_openrouter_backend(self):
        with patch.object(cfg, "AI_BACKEND", "openrouter"), patch.object(cfg, "BOT_NAME", ""):
            assert cfg.get_bot_display_name() == "小克"

    def test_openai_with_claude_model(self):
        with patch.object(cfg, "AI_BACKEND", "openai"), \
             patch.object(cfg, "MODEL_PRO", "anthropic/claude-opus-4-7"), \
             patch.object(cfg, "MODEL_FAST", ""), \
             patch.object(cfg, "BOT_NAME", ""):
            assert cfg.get_bot_display_name() == "小克"

    def test_openai_with_gpt_model(self):
        with patch.object(cfg, "AI_BACKEND", "openai"), \
             patch.object(cfg, "MODEL_PRO", "gpt-5.5"), \
             patch.object(cfg, "MODEL_FAST", ""), \
             patch.object(cfg, "BOT_NAME", ""):
            assert cfg.get_bot_display_name() == "小G"

    def test_openai_with_gemini_model(self):
        with patch.object(cfg, "AI_BACKEND", "openai"), \
             patch.object(cfg, "MODEL_PRO", "gemini-2.5-pro"), \
             patch.object(cfg, "MODEL_FAST", ""), \
             patch.object(cfg, "BOT_NAME", ""):
            assert cfg.get_bot_display_name() == "Gem"

    def test_openai_with_google_prefix(self):
        with patch.object(cfg, "AI_BACKEND", "openai"), \
             patch.object(cfg, "MODEL_PRO", "google/gemini-flash"), \
             patch.object(cfg, "MODEL_FAST", ""), \
             patch.object(cfg, "BOT_NAME", ""):
            assert cfg.get_bot_display_name() == "Gem"

    def test_openai_unknown_model_falls_to_xiaog(self):
        with patch.object(cfg, "AI_BACKEND", "openai"), \
             patch.object(cfg, "MODEL_PRO", "some-custom-model"), \
             patch.object(cfg, "MODEL_FAST", ""), \
             patch.object(cfg, "BOT_NAME", ""):
            assert cfg.get_bot_display_name() == "小G"

    def test_openai_model_fast_fallback_when_pro_empty(self):
        """MODEL_PRO 为空时从 MODEL_FAST 推断"""
        with patch.object(cfg, "AI_BACKEND", "openai"), \
             patch.object(cfg, "MODEL_PRO", ""), \
             patch.object(cfg, "MODEL_FAST", "anthropic/claude-haiku-4-5"), \
             patch.object(cfg, "BOT_NAME", ""):
            assert cfg.get_bot_display_name() == "小克"

    def test_bot_name_env_overrides_model_inference(self):
        with patch.object(cfg, "AI_BACKEND", "openai"), \
             patch.object(cfg, "MODEL_PRO", "anthropic/claude-opus-4-7"), \
             patch.object(cfg, "BOT_NAME", "小秘"):
            assert cfg.get_bot_display_name() == "小秘"

    def test_unknown_backend_falls_back_to_gem(self):
        with patch.object(cfg, "AI_BACKEND", "unknown-backend"), patch.object(cfg, "BOT_NAME", ""):
            assert cfg.get_bot_display_name() == "Gem"
