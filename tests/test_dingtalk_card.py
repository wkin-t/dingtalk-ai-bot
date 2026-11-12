# -*- coding: utf-8 -*-
"""钉钉原生消息 reaction 适配单元测试。"""

import json
import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest


def _make_helper(dc, token="access-token-test"):
    helper = object.__new__(dc.DingTalkCardHelper)
    helper.client_id = "robot-code-test"
    helper.get_access_token = AsyncMock(return_value=token)
    return helper


def _patch_httpx_transport(monkeypatch, responses):
    requests = []
    transport = httpx.MockTransport(
        lambda request: (
            requests.append(request) or responses.pop(0)
        )
    )
    client_options = {}
    real_async_client = httpx.AsyncClient

    def client_factory(**kwargs):
        client_options.update(kwargs)
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr("app.dingtalk_card.httpx.AsyncClient", client_factory)
    return requests, client_options


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("active", "path"),
    [(True, "/v1.0/robot/emotion/reply"), (False, "/v1.0/robot/emotion/recall")],
)
async def test_processing_reaction_uses_robot_payload_and_direct_http_transport(
    monkeypatch, active, path
):
    import app.dingtalk_card as dc

    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handle)
    client_options = {}
    real_async_client = httpx.AsyncClient

    def client_factory(**kwargs):
        client_options.update(kwargs)
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr(dc.httpx, "AsyncClient", client_factory)

    helper = _make_helper(dc)

    result = await helper.set_processing_reaction(
        "conversation-test", "message-test", active=active
    )

    assert result is True
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == f"https://api.dingtalk.com{path}"
    assert json.loads(request.content) == {
        "robotCode": "robot-code-test",
        "openMsgId": "message-test",
        "openConversationId": "conversation-test",
        "emotionType": 2,
        "emotionName": "🤔思考中",
        "textEmotion": {
            "emotionId": "2659900",
            "emotionName": "🤔思考中",
            "text": "🤔思考中",
            "backgroundId": "im_bg_1",
        },
    }
    assert request.headers["x-acs-dingtalk-access-token"] == "access-token-test"
    assert request.headers["content-type"] == "application/json"
    assert "authorization" not in request.headers
    assert client_options == {"trust_env": False, "timeout": 5}


@pytest.mark.asyncio
async def test_processing_reaction_reply_and_recall_share_exact_payload(monkeypatch):
    import app.dingtalk_card as dc

    requests, _ = _patch_httpx_transport(
        monkeypatch, [httpx.Response(200, json={}), httpx.Response(200, json={})]
    )
    helper = _make_helper(dc)

    assert await helper.set_processing_reaction(
        "conversation-test", "message-test", active=True
    )
    assert await helper.set_processing_reaction(
        "conversation-test", "message-test", active=False
    )

    assert json.loads(requests[0].content) == json.loads(requests[1].content)
    assert requests[0].url.path.endswith("/emotion/reply")
    assert requests[1].url.path.endswith("/emotion/recall")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("conversation_id", "message_id", "token"),
    [("", "message-test", "access-token-test"), ("conversation-test", "", "access-token-test"),
     ("conversation-test", "message-test", None)],
)
async def test_processing_reaction_returns_false_without_target_or_token(
    monkeypatch, conversation_id, message_id, token
):
    import app.dingtalk_card as dc

    helper = _make_helper(dc, token=token)
    client = AsyncMock()
    monkeypatch.setattr(dc.httpx, "AsyncClient", client)

    result = await helper.set_processing_reaction(
        conversation_id, message_id, active=True
    )

    assert result is False
    if not conversation_id or not message_id:
        helper.get_access_token.assert_not_awaited()
    else:
        helper.get_access_token.assert_awaited_once_with()
    client.assert_not_called()


@pytest.mark.asyncio
async def test_processing_reaction_refreshes_once_after_401_within_same_request_contract(monkeypatch):
    import app.dingtalk_card as dc

    requests, client_options = _patch_httpx_transport(
        monkeypatch,
        [httpx.Response(401), httpx.Response(200, json={"success": True})],
    )
    helper = _make_helper(dc)
    helper.get_access_token = AsyncMock(side_effect=["expired-token", "fresh-token"])

    result = await helper.set_processing_reaction(
        "conversation-test", "message-test", active=True
    )

    assert result is True
    assert helper.get_access_token.await_args_list[0].args == ()
    assert helper.get_access_token.await_args_list[1].kwargs == {"force_refresh": True}
    assert [request.headers["x-acs-dingtalk-access-token"] for request in requests] == [
        "expired-token", "fresh-token"
    ]
    assert client_options == {"trust_env": False, "timeout": 5}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"success": False}),
        httpx.Response(200, json={"code": "InvalidParameter"}),
        httpx.Response(500, json={"secret": "must-not-be-logged"}),
    ],
)
async def test_processing_reaction_rejects_http_or_business_errors_without_retry(
    monkeypatch, capsys, response
):
    import app.dingtalk_card as dc

    requests, _ = _patch_httpx_transport(monkeypatch, [response])
    helper = _make_helper(dc)

    result = await helper.set_processing_reaction(
        "conversation-test", "message-test", active=False
    )

    assert result is False
    assert len(requests) == 1
    helper.get_access_token.assert_awaited_once_with()
    output = capsys.readouterr().out
    assert "action=recall" in output
    assert "msg_id=message-test" in output
    assert "access-token-test" not in output
    assert "must-not-be-logged" not in output
    assert "x-acs-dingtalk-access-token" not in output


@pytest.mark.asyncio
async def test_processing_reaction_accepts_empty_success_body(monkeypatch):
    import app.dingtalk_card as dc

    requests, _ = _patch_httpx_transport(
        monkeypatch, [httpx.Response(200, content=b"")]
    )
    helper = _make_helper(dc)

    result = await helper.set_processing_reaction(
        "conversation-test", "message-test", active=True
    )

    assert result is True
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_processing_reaction_has_one_total_five_second_budget(monkeypatch):
    import app.dingtalk_card as dc

    real_wait_for = asyncio.wait_for
    observed = {}

    async def bounded_wait_for(awaitable, timeout):
        observed["timeout"] = timeout
        return await real_wait_for(awaitable, timeout=0.01)

    monkeypatch.setattr(dc.asyncio, "wait_for", bounded_wait_for)
    helper = _make_helper(dc)

    async def slow_token(*, force_refresh=False):
        await asyncio.sleep(0.1)
        return "access-token-test"

    helper.get_access_token = slow_token
    result = await helper.set_processing_reaction(
        "conversation-test", "message-test", active=True
    )

    assert result is False
    assert observed["timeout"] == 5
