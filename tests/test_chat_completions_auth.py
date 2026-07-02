# -*- coding: utf-8 -*-
"""/v1/chat/completions 鉴权测试（finding #2：零鉴权开放 API 代理）

该端点用服务端 GEMINI_API_KEY 代付转发，必须 fail-closed：
token 未配置拒绝服务，Bearer 不匹配 401，匹配才放行转发。
"""
from unittest.mock import patch

import pytest


@pytest.fixture()
def client():
    from app import app as flask_app
    flask_app.testing = True
    return flask_app.test_client()


def test_unconfigured_token_fails_closed(client):
    import app.routes as routes
    with patch.object(routes, "CHAT_COMPLETIONS_BEARER_TOKEN", ""):
        resp = client.post("/v1/chat/completions", json={"model": "x", "messages": []})
    assert resp.status_code == 500


def test_missing_auth_rejected(client):
    import app.routes as routes
    with patch.object(routes, "CHAT_COMPLETIONS_BEARER_TOKEN", "token123"):
        resp = client.post("/v1/chat/completions", json={"model": "x", "messages": []})
    assert resp.status_code == 401


def test_wrong_token_rejected(client):
    import app.routes as routes
    with patch.object(routes, "CHAT_COMPLETIONS_BEARER_TOKEN", "token123"):
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer wrong"},
            json={"model": "x", "messages": []},
        )
    assert resp.status_code == 401


def test_gemini_api_key_as_token_rejected(client):
    """服务端转发用的 GEMINI_API_KEY 不能当访问 token 用（防误配等价放行）"""
    import app.routes as routes
    with patch.object(routes, "CHAT_COMPLETIONS_BEARER_TOKEN", "token123"):
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {routes.GEMINI_API_KEY}"},
            json={"model": "x", "messages": []},
        )
    assert resp.status_code == 401


def test_correct_token_forwards(client):
    import app.routes as routes

    async def fake_forward():
        return {"ok": True}, 200

    with patch.object(routes, "CHAT_COMPLETIONS_BEARER_TOKEN", "token123"):
        with patch.object(routes, "async_chat_completions", fake_forward):
            resp = client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer token123"},
                json={"model": "x", "messages": []},
            )
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
