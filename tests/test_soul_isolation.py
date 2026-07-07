# -*- coding: utf-8 -*-
from app.ai.history_format import format_history_with_meta
from app.ai.messages_pipeline import prepare_messages_for_backend


def test_soul_evolve_receives_raw_messages_with_other_bot_marker():
    """Soul 进化应当能看到其他 bot 的原始来源标签。"""
    history = [
        {
            "role": "assistant",
            "content": "hi",
            "bot_id": "gemini",
        },
    ]
    raw_messages = format_history_with_meta(history, current_bot_id="openrouter")

    assert any("<other_bot" in msg.get("content", "") for msg in raw_messages)


def test_image_enrich_receives_raw_messages():
    """图片 prompt 增强同样应当看到原始来源标签。"""
    history = [
        {
            "role": "assistant",
            "content": "previous reply",
            "bot_id": "gemini",
        },
    ]
    raw_messages = format_history_with_meta(history, current_bot_id="openrouter")

    assert any('<other_bot name="Gem">' in msg.get("content", "") for msg in raw_messages)


def test_model_messages_are_rewritten_but_raw_marker_is_preserved():
    history = [
        {
            "role": "assistant",
            "content": "previous reply",
            "bot_id": "gemini",
        },
    ]
    raw_messages = format_history_with_meta(history, current_bot_id="openrouter")
    model_messages = prepare_messages_for_backend(raw_messages, current_bot_id="openrouter")

    assert raw_messages[0]["role"] == "assistant"
    assert '<other_bot name="Gem">' in raw_messages[0]["content"]
    assert model_messages[0]["role"] == "user"
    assert '<other_bot name="Gem">' in model_messages[0]["content"]
