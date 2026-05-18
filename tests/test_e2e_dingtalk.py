# -*- coding: utf-8 -*-
"""端到端测试钉钉路径的消息处理 pipeline（不真实发请求）。"""
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_dingtalk_messages_pipeline_role_rewrite():
    """模拟钉钉消息链路，验证转换后满足 user/assistant 交替。"""
    from app.ai.messages_pipeline import prepare_messages_for_backend

    messages_raw = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "[2026-05-18 14:23] 张三: q1"},
        {"role": "assistant", "content": "[来自机器人 Gem] a1", "bot_id": "gemini"},
        {"role": "user", "content": "[2026-05-18 14:24] 张三: q2"},
        {"role": "assistant", "content": "a2", "bot_id": "openrouter"},
        {"role": "user", "content": "[2026-05-18 14:25] 张三: current"},
    ]

    with patch("app.ai.messages_pipeline.AI_BACKEND", "openrouter"), patch(
        "app.ai.messages_pipeline.ENABLE_ROLE_REWRITE", True
    ):
        result = prepare_messages_for_backend(messages_raw, current_bot_id="openrouter")

    non_system = [msg for msg in result if msg["role"] != "system"]
    for i in range(1, len(non_system)):
        assert non_system[i]["role"] != non_system[i - 1]["role"], (
            f"连续相同 role at index {i}: {non_system[i - 1]['role']} -> {non_system[i]['role']}"
        )


@pytest.mark.asyncio
async def test_dingtalk_messages_pipeline_openclaw_ws_skips_rewrite():
    from app.ai.messages_pipeline import prepare_messages_for_backend

    messages_raw = [
        {"role": "assistant", "content": "[来自机器人 Gem] a", "bot_id": "gemini"},
    ]

    with patch("app.ai.messages_pipeline.AI_BACKEND", "openclaw"), patch(
        "app.ai.messages_pipeline.OPENCLAW_GATEWAY_TRANSPORT", "ws"
    ):
        result = prepare_messages_for_backend(messages_raw, current_bot_id="openclaw")

    assert result[0]["role"] == "assistant"
    assert "bot_id" not in result[0]
