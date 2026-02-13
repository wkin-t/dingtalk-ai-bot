# -*- coding: utf-8 -*-
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture()
def client():
    from app import app as flask_app
    flask_app.testing = True
    return flask_app.test_client()


def test_push_unauthorized(client):
    import app.routes as routes
    with patch.object(routes, "DINGTALK_PUSH_BEARER_TOKEN", "token123"):
        resp = client.post("/api/dingtalk/push", json={"target_type": "group", "conversation_id": "cid"})
    assert resp.status_code == 401


def test_push_forbidden_ip(client):
    import app.routes as routes

    with patch.object(routes, "DINGTALK_PUSH_BEARER_TOKEN", "token123"):
        with patch.object(routes, "DINGTALK_PUSH_IP_ALLOWLIST_RAW", "10.0.0.0/8"):
            resp = client.post(
                "/api/dingtalk/push",
                headers={"Authorization": "Bearer token123"},
                environ_base={"REMOTE_ADDR": "203.0.113.10"},
                json={"target_type": "group", "conversation_id": "cid", "message_type": "text", "content": "hi"},
            )
    assert resp.status_code == 403


def test_push_group_text_ok(client):
    import app.routes as routes

    mock_sender = MagicMock()
    mock_sender.send_group_message = AsyncMock(return_value=True)

    with patch.object(routes, "DINGTALK_PUSH_BEARER_TOKEN", "token123"):
        with patch.object(routes, "DINGTALK_PUSH_IP_ALLOWLIST_RAW", ""):
            with patch.object(routes, "_get_sender", return_value=mock_sender):
                resp = client.post(
                    "/api/dingtalk/push",
                    headers={"Authorization": "Bearer token123"},
                    environ_base={"REMOTE_ADDR": "127.0.0.1"},
                    json={"target_type": "group", "conversation_id": "cid_1", "message_type": "text", "content": "hello"},
                )

    assert resp.status_code == 200
    body = json.loads(resp.data.decode("utf-8"))
    assert body["ok"] is True
    assert mock_sender.send_group_message.await_count == 1


def test_push_default_is_markdown(client):
    import app.routes as routes

    mock_sender = MagicMock()
    mock_sender.send_group_message = AsyncMock(return_value=True)

    with patch.object(routes, "DINGTALK_PUSH_BEARER_TOKEN", "token123"):
        with patch.object(routes, "DINGTALK_PUSH_IP_ALLOWLIST_RAW", ""):
            with patch.object(routes, "_get_sender", return_value=mock_sender):
                resp = client.post(
                    "/api/dingtalk/push",
                    headers={"Authorization": "Bearer token123"},
                    environ_base={"REMOTE_ADDR": "127.0.0.1"},
                    json={"target_type": "group", "conversation_id": "cid_1", "content": "## hi"},
                )

    assert resp.status_code == 200
    assert mock_sender.send_group_message.await_count == 1
