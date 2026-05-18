# -*- coding: utf-8 -*-
from unittest.mock import patch


def test_openclaw_ws_transport_marker_exists():
    """确认配置只允许已知 OpenClaw transport。"""
    from app.config import OPENCLAW_GATEWAY_TRANSPORT

    assert OPENCLAW_GATEWAY_TRANSPORT in ("http", "ws")


def test_openclaw_ws_skips_role_rewrite():
    from app.ai.messages_pipeline import prepare_messages_for_backend

    messages_raw = [
        {"role": "assistant", "content": "[来自机器人 Gem] hi", "bot_id": "gemini"},
    ]

    with patch("app.ai.messages_pipeline.AI_BACKEND", "openclaw"), patch(
        "app.ai.messages_pipeline.OPENCLAW_GATEWAY_TRANSPORT", "ws"
    ):
        result = prepare_messages_for_backend(messages_raw, current_bot_id="openclaw")

    assert result == [{"role": "assistant", "content": "[来自机器人 Gem] hi"}]


def test_openclaw_http_still_rewrites_roles():
    from app.ai.messages_pipeline import prepare_messages_for_backend

    messages_raw = [
        {"role": "assistant", "content": "[来自机器人 Gem] hi", "bot_id": "gemini"},
    ]

    with patch("app.ai.messages_pipeline.AI_BACKEND", "openclaw"), patch(
        "app.ai.messages_pipeline.OPENCLAW_GATEWAY_TRANSPORT", "http"
    ), patch("app.ai.messages_pipeline.ENABLE_ROLE_REWRITE", True):
        result = prepare_messages_for_backend(messages_raw, current_bot_id="openclaw")

    assert result == [{"role": "user", "content": "[来自机器人 Gem] hi"}]
