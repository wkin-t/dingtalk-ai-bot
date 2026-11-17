"""终态卡片使用可取消的单次HTTP，最终门禁在token获取之后执行。"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest


def setup_helper(monkeypatch, status=200, body=None, transport_handler=None):
    import app.dingtalk_card as dc
    helper = object.__new__(dc.DingTalkCardHelper)
    helper.client_id = "robot-code"
    helper.get_access_token = AsyncMock(return_value="test-token")
    helper.card_client = MagicMock()
    posts = []
    async def send(request):
        posts.append(request)
        if transport_handler is not None:
            return await transport_handler(request)
        return httpx.Response(status, json=body if body is not None else {
            "success": True, "result": {"outTrackId": "stable-delivery", "deliverResults": [{"success": True}]}})
    real_client = httpx.AsyncClient
    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return real_client(transport=httpx.MockTransport(send), **kwargs)
    monkeypatch.setattr(dc.httpx, "AsyncClient", client)
    return helper, posts


@pytest.mark.asyncio
async def test_one_completed_card_uses_stable_identity_without_a_processing_card(monkeypatch):
    helper, posts = setup_helper(monkeypatch)
    assert await helper.deliver_proactive_card("cid", "template", "自然接话正文", "stable-delivery", AsyncMock(return_value=True))
    assert len(posts) == 1 and posts[0].method == "POST"
    assert posts[0].url.path == "/v1.0/card/instances/createAndDeliver"
    body = json.loads(posts[0].content)
    assert body["outTrackId"] == "stable-delivery"
    assert body["openSpaceId"] == "dtv1.card//im_group.cid"
    assert body["cardData"]["cardParamMap"]["flowStatus"] == "3"
    assert body["cardData"]["cardParamMap"]["msgContent"] == "自然接话正文"
    assert body["cardData"]["cardParamMap"]["isError"] == "false"
    assert not body["imGroupOpenDeliverModel"]["atUserIds"]
    helper.card_client.create_and_deliver_with_options.assert_not_called()


@pytest.mark.asyncio
async def test_final_async_guard_runs_after_token_fetch_and_can_block_http(monkeypatch):
    helper, posts = setup_helper(monkeypatch)
    stages = []
    async def token():
        stages.append("token")
        return "test-token"
    async def guard():
        stages.append("fresh-dws")
        return False
    helper.get_access_token.side_effect = token
    assert not await helper.deliver_proactive_card("cid", "template", "自然接话正文", "stable-delivery", guard)
    assert stages == ["token", "fresh-dws"] and posts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [(503, {}), (429, {}), (200, {"success": False}), (200, {"success": True, "result": {"outTrackId": "wrong"}})])
async def test_api_failure_or_unknown_ack_is_one_post_and_no_error_card(monkeypatch, status, body):
    helper, posts = setup_helper(monkeypatch, status, body)
    assert not await helper.deliver_proactive_card("cid", "template", "自然接话正文", "stable-delivery", AsyncMock(return_value=True))
    assert len(posts) == 1


@pytest.mark.asyncio
async def test_cancelled_async_transport_has_no_delayed_executor_post(monkeypatch):
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    async def transport(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    helper, posts = setup_helper(monkeypatch, transport_handler=transport)
    task = asyncio.create_task(helper.deliver_proactive_card("cid", "template", "自然接话正文", "stable-delivery", AsyncMock(return_value=True)))
    await asyncio.wait_for(entered.wait(), 0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set() and len(posts) == 1
