"""主动发言只在后台完成生成且实时门禁成立后产生一张终态卡片。"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


BEIJING = timezone(timedelta(hours=8))


class Store:
    def __init__(self, now):
        self.plan = {"seed": "test-seed", "slots": [now.timestamp() - 10, now.timestamp() + 6000]}
        self.claimed = False
        self.claim_count = 0
        self.owned = True
        self.finished = []
        self.identity_matches = True

    def heartbeat(self, *args):
        pass

    def register_group(self, *args):
        pass

    def eligible_bots(self, *args):
        return ["gemini"]

    def get_or_create_plan(self, *args):
        return self.plan

    def last_speaker(self, *args):
        return ""

    def claim(self, *args):
        self.claim_count += 1
        if self.claimed:
            return None
        self.claimed = True
        return "owner-token"

    def matches_group_identity(self, *args):
        return self.identity_matches

    def owns(self, *args):
        return self.owned

    def finish(self, *args):
        self.finished.append(args[-1])


def setup_scheduler(monkeypatch, *, now=None):
    import app.proactive_chat as chat
    now = now or datetime(2026, 11, 17, 10, 0, tzinfo=BEIJING)
    clock = [now]
    snapshot = SimpleNamespace(corp_id="corp", conversation_id="cid", name="测试群",
        bot_sender_id="dws-bot", bot_ids={"dws-bot"}, last_human_at=now.timestamp() - 20,
        latest_message_id="human-message", history=[{"role": "user", "content": "最近在准备发布"}])
    fetch = AsyncMock(return_value=snapshot)
    monkeypatch.setattr(chat, "discover_groups", AsyncMock(return_value=[{"cid": "cid", "name": "测试群"}]))
    monkeypatch.setattr(chat, "fetch_group_snapshot", fetch)
    handler = SimpleNamespace(proactive_conversation_lock=lambda cid: asyncio.Lock(),
        proactive_has_pending=lambda cid: False,
        generate_proactive_text=AsyncMock(return_value="发布前最紧张的那一刻，通常也是最想喝杯茶的时候。"),
        send_proactive_text=AsyncMock(return_value=True))
    store = Store(now)
    scheduler = chat.ProactiveChatScheduler(handler, store, corp_id="corp", bot_id="gemini",
        robot_code="robot-code", clock=lambda: clock[0])
    return chat, scheduler, handler, store, snapshot, fetch, clock


@pytest.mark.asyncio
async def test_recent_human_message_can_trigger_without_a_quiet_period(monkeypatch):
    _, scheduler, handler, _, _, fetch, _ = setup_scheduler(monkeypatch)
    await scheduler.tick()
    handler.generate_proactive_text.assert_awaited_once()
    handler.send_proactive_text.assert_awaited_once()
    assert fetch.await_count >= 2


@pytest.mark.asyncio
async def test_second_tick_and_repeated_slot_cannot_send_again(monkeypatch):
    _, scheduler, handler, store, _, _, _ = setup_scheduler(monkeypatch)
    await scheduler.tick()
    await scheduler.tick()
    assert store.claim_count >= 2
    assert handler.send_proactive_text.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("activity", [None, -86401])
async def test_silent_or_bot_only_group_never_calls_model(monkeypatch, activity):
    _, scheduler, handler, _, snapshot, _, clock = setup_scheduler(monkeypatch)
    snapshot.last_human_at = None if activity is None else clock[0].timestamp() + activity
    await scheduler.tick()
    handler.generate_proactive_text.assert_not_awaited()
    handler.send_proactive_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [None, "", "OpenAI API Error: 503 No available accounts", "![图片](@fake)", "[[at:1]] 来看看"])
async def test_failed_empty_or_tool_output_never_creates_a_card(monkeypatch, reply):
    _, scheduler, handler, store, _, _, _ = setup_scheduler(monkeypatch)
    handler.generate_proactive_text.return_value = reply
    await scheduler.tick()
    handler.send_proactive_text.assert_not_awaited()
    assert store.finished == ["owner-token"]


@pytest.mark.asyncio
@pytest.mark.parametrize("hour,minute", [(11, 59), (17, 59)])
async def test_generation_crossing_lunch_or_end_of_day_is_discarded(monkeypatch, hour, minute):
    now = datetime(2026, 11, 17, hour, minute, 40, tzinfo=BEIJING)
    _, scheduler, handler, _, _, _, clock = setup_scheduler(monkeypatch, now=now)
    async def generate(snapshot):
        clock[0] += timedelta(seconds=30)
        return "准备发布时，也别忘了给自己留一点喘气的时间。"
    handler.generate_proactive_text.side_effect = generate
    await scheduler.tick()
    handler.send_proactive_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_lost_lease_after_generation_blocks_delivery(monkeypatch):
    _, scheduler, handler, store, _, _, _ = setup_scheduler(monkeypatch)
    store.owned = False
    await scheduler.tick()
    handler.send_proactive_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_human_activity_on_final_read_blocks_delivery(monkeypatch):
    _, scheduler, handler, _, snapshot, fetch, clock = setup_scheduler(monkeypatch)
    expired = SimpleNamespace(**vars(snapshot))
    expired.last_human_at = clock[0].timestamp() - 86401
    fetch.side_effect = [snapshot, snapshot, expired]
    await scheduler.tick()
    handler.send_proactive_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_user_response_wins_without_consuming_slot(monkeypatch):
    _, scheduler, handler, store, _, _, _ = setup_scheduler(monkeypatch)
    handler.proactive_has_pending = lambda cid: True
    await scheduler.tick()
    assert store.claim_count == 0
    handler.send_proactive_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_generation_releases_group_lock_and_never_sends(monkeypatch):
    _, scheduler, handler, store, _, _, _ = setup_scheduler(monkeypatch)
    lock = asyncio.Lock()
    handler.proactive_conversation_lock = lambda cid: lock
    handler.generate_proactive_text.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await scheduler.tick()
    assert not lock.locked()
    assert store.finished == ["owner-token"]
    handler.send_proactive_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_delivery_result_is_not_retried(monkeypatch):
    _, scheduler, handler, _, _, _, _ = setup_scheduler(monkeypatch)
    handler.send_proactive_text.side_effect = TimeoutError
    await scheduler.tick()
    await scheduler.tick()
    assert handler.send_proactive_text.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status,error,incomplete", [("incomplete", None, {"reason":"max_output_tokens"}), ("failed", {"code":"503"}, None), ("in_progress", None, None)])
async def test_only_real_completed_model_response_is_usable(monkeypatch, status, error, incomplete):
    import app.proactive_chat as chat
    import app.openai_client as oc
    monkeypatch.setattr(chat.cfg, "AI_BACKEND", "openai")
    client = MagicMock()
    client.with_options.return_value = client
    client.close = AsyncMock()
    client.responses.create = AsyncMock(return_value=SimpleNamespace(status=status, model="gpt-6.1-terra",
        error=error, incomplete_details=incomplete, output=[]))
    monkeypatch.setattr(oc, "_build_client", lambda: client)
    snapshot = SimpleNamespace(conversation_id="cid", name="测试群", history=[])
    assert await chat.generate_proactive_text(snapshot, soul_content="") is None
    options = client.responses.create.call_args.kwargs
    assert options["store"] is False and not options.get("tools")
    assert "previous_response_id" not in options and "conversation_id" not in options


@pytest.mark.asyncio
async def test_completed_openai_response_uses_real_text_without_tools_or_route_probe(monkeypatch):
    import app.proactive_chat as chat
    import app.openai_client as oc
    monkeypatch.setattr(chat.cfg, "AI_BACKEND", "openai")
    client = MagicMock()
    client.with_options.return_value = client
    client.close = AsyncMock()
    response = SimpleNamespace(status="completed", model="gpt-6.1-terra", error=None,
        incomplete_details=None, output=[SimpleNamespace(type="message", role="assistant", status="completed",
        content=[SimpleNamespace(type="output_text", text="忙归忙，也别忘了给自己留杯茶的时间。")])])
    client.responses.create = AsyncMock(return_value=response)
    monkeypatch.setattr(oc, "_build_client", lambda: client)
    snapshot = SimpleNamespace(conversation_id="cid", name="测试群", history=[])
    assert await chat.generate_proactive_text(snapshot, soul_content="") == "忙归忙，也别忘了给自己留杯茶的时间。"
    client.with_options.assert_called_once_with(max_retries=0, timeout=40)
    assert not client.responses.create.call_args.kwargs.get("tools")


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["STOP", "MAX_TOKENS", "SAFETY"])
async def test_gemini_requires_stop_and_discards_thoughts(monkeypatch, finish):
    from google.genai import types
    import app.proactive_chat as chat
    import app.gemini_client as gc
    monkeypatch.setattr(chat.cfg, "AI_BACKEND", "gemini")
    response = types.GenerateContentResponse(model_version="gemini-4-argon", candidates=[types.Candidate(
        finish_reason=finish, content=types.Content(role="model", parts=[types.Part(text="内部思考", thought=True),
        types.Part(text="话题聊到这儿，突然有点想去泡杯茶。")]))])
    call = AsyncMock(return_value=response)
    monkeypatch.setattr(gc, "client", SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=call))))
    snapshot = SimpleNamespace(conversation_id="cid", name="测试群", history=[{"role":"user","content":"聊聊吧"}])
    reply = await chat.generate_proactive_text(snapshot, soul_content="")
    assert reply == ("话题聊到这儿，突然有点想去泡杯茶。" if finish == "STOP" else None)
    assert not call.call_args.kwargs["config"].tools


@pytest.mark.asyncio
async def test_stream_session_cancels_worker_and_sdk_that_swallows_first_cancel():
    import app.proactive_chat as chat
    worker_started = asyncio.Event()
    worker_stopped = asyncio.Event()
    sdk_cancelled_once = asyncio.Event()
    async def worker():
        worker_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            worker_stopped.set()
    async def sdk():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            sdk_cancelled_once.set()
            await asyncio.sleep(10)
    client = SimpleNamespace(start=sdk)
    scheduler = SimpleNamespace(run=worker)
    session = asyncio.create_task(chat.run_stream_session(client, scheduler))
    await worker_started.wait()
    session.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(session, 1)
    assert worker_stopped.is_set() and sdk_cancelled_once.is_set()


@pytest.mark.asyncio
async def test_real_dingtalk_sdk_reconnect_keeps_one_worker_and_shutdown_awaits_children(monkeypatch):
    from contextlib import asynccontextmanager
    import dingtalk_stream
    import dingtalk_stream.stream as sdk
    import app.proactive_chat as chat
    entered_twice = asyncio.Event()
    closed = asyncio.Event()
    worker_calls = []
    worker_stopped = asyncio.Event()
    connects = []
    class Socket:
        def __init__(self, attempt):
            self.attempt = attempt
        def __aiter__(self):
            return self
        async def __anext__(self):
            if self.attempt == 1:
                raise RuntimeError("synthetic disconnect")
            entered_twice.set()
            await asyncio.Event().wait()
        async def ping(self):
            pass
    @asynccontextmanager
    async def connect(*args, **kwargs):
        connects.append(1)
        try:
            yield Socket(len(connects))
        finally:
            closed.set()
    async def sdk_sleep(seconds):
        await asyncio.sleep(0.001 if seconds == 3 else seconds)
    monkeypatch.setattr(sdk, "asyncio", SimpleNamespace(create_task=asyncio.create_task,
        sleep=sdk_sleep, exceptions=asyncio.exceptions))
    monkeypatch.setattr(sdk.websockets, "connect", connect)
    client = dingtalk_stream.DingTalkStreamClient(dingtalk_stream.Credential("test", "test-secret"))
    client.open_connection = MagicMock(return_value={"endpoint":"wss://unit.invalid", "ticket":"test-ticket"})
    client.logger = MagicMock()
    async def worker():
        worker_calls.append(1)
        try:
            await asyncio.Event().wait()
        finally:
            worker_stopped.set()
    task = asyncio.create_task(chat.run_stream_session(client, SimpleNamespace(run=worker)))
    await asyncio.wait_for(entered_twice.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert len(worker_calls) == 1 and len(connects) == 2
    assert worker_stopped.is_set() and closed.is_set()
    assert not [pending for pending in asyncio.all_tasks() if pending is not asyncio.current_task()
        and not pending.done() and "run_stream_session" in pending.get_coro().__qualname__]


@pytest.mark.parametrize("body", ["503 Service Unavailable", "HTTP 502 Bad Gateway", "Internal Server Error",
    "429 Too Many Requests", "request failed", "504 Gateway Timeout"])
def test_http_error_packaged_as_completed_text_is_silently_rejected(body):
    from app.proactive_chat import validate_proactive_text
    assert validate_proactive_text(body) is None


@pytest.mark.asyncio
async def test_sender_changed_since_discovery_cannot_claim_or_generate(monkeypatch):
    _, scheduler, handler, store, _, _, _ = setup_scheduler(monkeypatch)
    store.identity_matches = False
    await scheduler.tick()
    assert store.claim_count == 0
    handler.generate_proactive_text.assert_not_awaited()
    handler.send_proactive_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_sender_binding_lost_during_generation_cannot_reach_delivery(monkeypatch):
    _, scheduler, handler, store, _, _, _ = setup_scheduler(monkeypatch)
    async def generate(snapshot):
        store.identity_matches = False
        return "话题聊到这儿，突然有点想泡杯茶。"
    handler.generate_proactive_text.side_effect = generate
    await scheduler.tick()
    handler.send_proactive_text.assert_not_awaited()
