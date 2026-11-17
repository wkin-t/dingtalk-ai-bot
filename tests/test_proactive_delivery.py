"""主动卡片预创建、终态更新、最终门禁与投递契约。"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

CID = "cid"
TEMPLATE = "template"
CONTENT = "自然接话正文"
DELIVERY = "stable-delivery"


def _success_for(request):
    if request.method == "POST" and request.url.path == "/v1.0/card/instances":
        return httpx.Response(200, json={"success": True, "result": "instance-id"})
    if request.method == "PUT" and request.url.path == "/v1.0/card/streaming":
        return httpx.Response(200, json={"success": True, "result": True})
    if request.method == "PUT" and request.url.path == "/v1.0/card/instances":
        return httpx.Response(200, json={"success": True, "result": True})
    if request.method == "POST" and request.url.path == "/v1.0/card/instances/deliver":
        return httpx.Response(200, json={"success": True, "result": [{"success": True}]})
    return httpx.Response(404, json={"success": False})


def setup_helper(monkeypatch, transport_handler=None, guard=None):
    import app.dingtalk_card as dc

    helper = object.__new__(dc.DingTalkCardHelper)
    helper.client_id = "robot-code"
    helper.get_access_token = AsyncMock(return_value="test-token")
    helper.card_client = MagicMock()
    requests = []
    stages = []

    async def send(request):
        requests.append(request)
        if transport_handler:
            return await transport_handler(request)
        if request.method == "POST" and request.url.path == "/v1.0/card/instances":
            return httpx.Response(200, json={"success": True, "result": "instance-id"})
        if request.method == "PUT" and request.url.path == "/v1.0/card/streaming":
            return httpx.Response(200, json={"success": True, "result": True})
        if request.method == "PUT" and request.url.path == "/v1.0/card/instances":
            return httpx.Response(200, json={"success": True, "result": True})
        if request.method == "POST" and request.url.path == "/v1.0/card/instances/deliver":
            return httpx.Response(200, json={"success": True, "result": [{"success": True}]})
        return httpx.Response(404, json={"success": False})

    real_client = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return real_client(transport=httpx.MockTransport(send), **kwargs)

    monkeypatch.setattr(dc.httpx, "AsyncClient", client)

    async def token():
        stages.append("token")
        return "test-token"

    helper.get_access_token.side_effect = token
    if guard is None:
        async def guard():
            stages.append("guard")
            return True
    return helper, requests, stages, guard


def body(request):
    return json.loads(request.content)


def expected_create(helper):
    from app.dingtalk_card import card_models
    return card_models.CreateCardRequest(
        callback_type="STREAM",
        card_data=card_models.CreateCardRequestCardData(card_param_map=helper._convert_card_data({
            "msgTitle": "AI", "thinkingText": "", "msgContent": CONTENT,
            "statusText": "", "flowStatus": "1", "isError": "false",
            "msgButtons": [], "config": {"autoLayout": True},
        })),
        card_template_id=TEMPLATE,
        im_group_open_space_model=card_models.CreateCardRequestImGroupOpenSpaceModel(
            support_forward=True),
        out_track_id=DELIVERY,
    ).to_map()


def expected_update(helper):
    from app.dingtalk_card import card_models
    return card_models.UpdateCardRequest(
        card_data=card_models.UpdateCardRequestCardData(card_param_map=helper._convert_card_data({
            "msgTitle": "AI", "thinkingText": "", "msgContent": CONTENT,
            "statusText": "", "flowStatus": "3", "isError": "false",
            "msgButtons": [], "config": {"autoLayout": True},
        })),
        card_update_options=card_models.UpdateCardRequestCardUpdateOptions(
            update_card_data_by_key=True),
        out_track_id=DELIVERY,
    ).to_map()


def expected_deliver():
    from app.dingtalk_card import card_models
    return card_models.DeliverCardRequest(
        open_space_id=f"dtv1.card//im_group.{CID}", out_track_id=DELIVERY,
        im_group_open_deliver_model=card_models.DeliverCardRequestImGroupOpenDeliverModel(
            robot_code="robot-code", at_user_ids={}),
    ).to_map()


@pytest.mark.asyncio
async def test_prepare_streaming_update_final_guard_then_deliver(monkeypatch):
    helper, requests, stages, guard = setup_helper(monkeypatch)
    assert await helper.deliver_proactive_card(CID, TEMPLATE, CONTENT, DELIVERY, guard)
    assert [(r.method, r.url.path) for r in requests] == [
        ("POST", "/v1.0/card/instances"),
        ("PUT", "/v1.0/card/streaming"),
        ("PUT", "/v1.0/card/instances"),
        ("POST", "/v1.0/card/instances/deliver"),
    ]
    assert stages == ["token", "guard", "guard"]
    assert body(requests[0]) == expected_create(helper)
    assert "openSpaceId" not in body(requests[0])
    stream = body(requests[1])
    assert stream["outTrackId"] == DELIVERY and stream["key"] == "msgContent"
    assert stream["content"] == CONTENT and stream["isFull"] is True
    assert stream["isFinalize"] is True and stream["isError"] is False and stream["guid"]
    assert body(requests[2]) == expected_update(helper)
    assert body(requests[3]) == expected_deliver()
    assert not body(requests[3])["imGroupOpenDeliverModel"].get("atUserIds")
    helper.card_client.create_and_deliver_with_options.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,reply", [
    (503, {}),
    (200, {"success": False, "result": True}),
    (200, {"success": True, "result": {}}),
])
async def test_prepare_or_finalize_failure_stops_without_retry_or_deliver(monkeypatch, status, reply):
    calls = {"count": 0}

    async def transport(request):
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(status, json=reply)
        return httpx.Response(200, json={"success": True, "result": True})

    helper, requests, _, guard = setup_helper(monkeypatch, transport_handler=transport)
    assert not await helper.deliver_proactive_card(CID, TEMPLATE, CONTENT, DELIVERY, guard)
    assert len(requests) == 1
    assert all(r.url.path != "/v1.0/card/instances/deliver" for r in requests)


@pytest.mark.asyncio
async def test_final_guard_false_after_three_prepare_requests_never_delivers(monkeypatch):
    calls = 0

    async def guard():
        nonlocal calls
        calls += 1
        return calls == 1

    helper, requests, _, _ = setup_helper(monkeypatch, guard=guard)
    assert not await helper.deliver_proactive_card(CID, TEMPLATE, CONTENT, DELIVERY, guard)
    assert calls == 2
    assert [r.url.path for r in requests] == [
        "/v1.0/card/instances", "/v1.0/card/streaming", "/v1.0/card/instances",
    ]


@pytest.mark.asyncio
async def test_precreate_guard_false_has_no_http(monkeypatch):
    guard = AsyncMock(return_value=False)
    helper, requests, _, _ = setup_helper(monkeypatch, guard=guard)
    assert not await helper.deliver_proactive_card(CID, TEMPLATE, CONTENT, DELIVERY, guard)
    guard.assert_awaited_once()
    assert requests == []


@pytest.mark.asyncio
async def test_cancelled_create_has_no_late_requests(monkeypatch):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def transport(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    helper, requests, _, guard = setup_helper(monkeypatch, transport_handler=transport)
    task = asyncio.create_task(helper.deliver_proactive_card(CID, TEMPLATE, CONTENT, DELIVERY, guard))
    await asyncio.wait_for(entered.wait(), 0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set() and len(requests) == 1
    await asyncio.sleep(0)
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,method,path,status,reply", [
    (1, "POST", "/v1.0/card/instances", 503, {}),
    (2, "PUT", "/v1.0/card/streaming", 200, {"success": False, "result": True}),
    (3, "PUT", "/v1.0/card/instances", 200, {"success": True, "result": {}}),
    (4, "POST", "/v1.0/card/instances/deliver", 503, {}),
])
async def test_each_stage_failure_stops_at_that_stage_without_retry(
    monkeypatch, stage, method, path, status, reply
):
    calls = 0

    async def transport(request):
        nonlocal calls
        calls += 1
        index = calls
        if index == stage:
            return httpx.Response(status, json=reply)
        return _success_for(request)

    helper, requests, _, guard = setup_helper(monkeypatch, transport_handler=transport)
    assert not await helper.deliver_proactive_card(CID, TEMPLATE, CONTENT, DELIVERY, guard)
    assert len(requests) == stage
    assert (requests[-1].method, requests[-1].url.path) == (method, path)
    assert not any(request.url.path == "/v1.0/card/instances/deliver" for request in requests[stage:])


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_method,cancel_path,expected_count", [
    ("POST", "/v1.0/card/instances", 1),
    ("PUT", "/v1.0/card/streaming", 2),
    ("PUT", "/v1.0/card/instances", 3),
    ("POST", "/v1.0/card/instances/deliver", 4),
])
async def test_cancel_at_each_http_stage_does_not_start_late_requests(
    monkeypatch, cancel_method, cancel_path, expected_count
):
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    requests = []

    async def transport(request):
        requests.append(request)
        if request.method == cancel_method and request.url.path == cancel_path and len(requests) == expected_count:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return _success_for(request)

    helper, recorded, _, guard = setup_helper(monkeypatch, transport_handler=transport)
    task = asyncio.create_task(helper.deliver_proactive_card(CID, TEMPLATE, CONTENT, DELIVERY, guard))
    await asyncio.wait_for(entered.wait(), 0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert len(recorded) == expected_count
    await asyncio.sleep(0)
    assert len(recorded) == expected_count
