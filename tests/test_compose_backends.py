# -*- coding: utf-8 -*-
"""部署 compose 的后端路径约束。"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _compose_text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_gemini_container_uses_gemini_backend():
    text = _compose_text("docker-compose.yml")
    assert "container_name: dingtalk-ai-bot-gemini" in text
    assert "- AI_BACKEND=gemini" in text
    assert "- BOT_ID=gemini" in text
    assert "- FLASK_PORT=35000" in text


def test_openai_container_uses_openai_backend():
    text = _compose_text("docker-compose.openai.yml")
    assert "container_name: dingtalk-ai-bot-openai" in text
    assert "- AI_BACKEND=openai" in text
    assert "- BOT_ID=openai" in text
    assert "- FLASK_PORT=35001" in text


def test_openrouter_container_uses_openrouter_backend():
    text = _compose_text("docker-compose.openrouter.yml")
    assert "container_name: dingtalk-ai-bot-openrouter" in text
    assert "- AI_BACKEND=openrouter" in text
    assert "- BOT_ID=openrouter" in text
    assert "- FLASK_PORT=35002" in text
