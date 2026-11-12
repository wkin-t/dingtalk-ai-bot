"""dingtalk_bot 辅助函数单元测试"""
import os
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("GEMINI_API_KEY", "test-dummy-key")


@pytest.fixture(autouse=True)
def isolated_dws_context():
    from app.dws_context import DwsContextBundle
    with patch("app.dingtalk_bot.fetch_dws_context_bundle", new_callable=AsyncMock, return_value=DwsContextBundle(dws_available=True)), patch("app.dingtalk_bot.schedule_bqb_index_warmup"):
        yield


async def _iter_consumer_chunks(chunks):
    for chunk in chunks:
        yield chunk


def _reaction_handler(core_result=None, *, core_side_effect=None):
    from app.dingtalk_bot import GeminiBotHandler

    handler = object.__new__(GeminiBotHandler)
    handler.card_helper = MagicMock()
    handler.card_helper.set_processing_reaction = AsyncMock(
        return_value=True,
    )
    handler._handle_ai_stream_core = AsyncMock(
        return_value=core_result,
        side_effect=core_side_effect,
    )
    return handler


def _reaction_message(message_id):
    return SimpleNamespace(message_id=message_id, sender_id="sender")


@pytest.mark.asyncio
@pytest.mark.parametrize("same_conversation", [True, False])
@pytest.mark.parametrize("first_outcome", ["success", "error", "cancel"])
async def test_buffered_users_serialize_by_conversation_and_release_after_exit(same_conversation, first_outcome):
    import app.dingtalk_bot as bot

    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()
    real_sleep = asyncio.sleep
    events = []
    handler = object.__new__(bot.GeminiBotHandler)
    handler.card_helper = MagicMock()

    def turn(user, conversation):
        message = SimpleNamespace(message_id=user, sender_nick=user, conversation_id=conversation, conversation_type="2")
        return {"content": [user], "images": [], "incoming_message": message,
                "incoming_messages": [message], "message_ids": [user], "at_user_ids": []}

    async def handle(message, *_args, **kwargs):
        user = message.sender_nick
        events.append((user, "start"))
        if user == "first":
            first_started.set()
            try:
                await release_first.wait()
                if first_outcome == "error":
                    raise RuntimeError("first failed")
            finally:
                events.append((user, "end"))
        else:
            assert kwargs["buffered_context"].current_message_ids == ["second"]
            second_started.set()
            events.append((user, "end"))

    handler.handle_ai_stream = handle
    buffers = {"user-first": turn("first", "group-a"),
               "user-second": turn("second", "group-a" if same_conversation else "group-b")}
    with patch.object(bot, "message_buffer", buffers), \
         patch.object(bot, "session_locks", type(bot.session_locks)()), \
         patch.object(bot, "processing_sessions", set()), \
         patch.object(bot, "get_cached_group_info", new_callable=AsyncMock, return_value="测试群"), \
         patch.object(bot.asyncio, "sleep", new_callable=AsyncMock):
        first = asyncio.create_task(handler.process_buffered_messages("user-first"))
        await asyncio.wait_for(first_started.wait(), timeout=1)
        second = asyncio.create_task(handler.process_buffered_messages("user-second"))
        try:
            await real_sleep(0)
            assert second_started.is_set() is (not same_conversation)
        finally:
            if first_outcome == "cancel":
                first.cancel()
            else:
                release_first.set()
            outcomes = await asyncio.gather(first, second, return_exceptions=True)
        assert second_started.is_set()
        assert not bot.processing_sessions
        assert not buffers
        if first_outcome == "error":
            assert isinstance(outcomes[0], RuntimeError)
        elif first_outcome == "cancel":
            assert isinstance(outcomes[0], asyncio.CancelledError)
        else:
            assert outcomes == [None, None]
        if same_conversation:
            assert events.index(("first", "end")) < events.index(("second", "start"))


@pytest.mark.asyncio
async def test_waiting_user_timer_can_restart_without_losing_merged_messages():
    import app.dingtalk_bot as bot

    first_started = asyncio.Event()
    release_first = asyncio.Event()
    real_sleep = asyncio.sleep
    handler = object.__new__(bot.GeminiBotHandler)
    handler.card_helper = MagicMock()
    calls = []

    def message(mid):
        return SimpleNamespace(message_id=mid, sender_nick=mid, conversation_id="group", conversation_type="2")

    def turn(mid):
        incoming = message(mid)
        return {"content": [mid], "images": [], "incoming_message": incoming,
                "incoming_messages": [incoming], "message_ids": [mid], "at_user_ids": []}

    async def handle(incoming, content, *_args, **kwargs):
        calls.append((incoming.message_id, content, kwargs["buffered_context"].current_message_ids))
        if incoming.message_id == "first":
            first_started.set()
            await release_first.wait()

    handler.handle_ai_stream = handle
    buffers = {"user-first": turn("first"), "user-second": turn("second")}
    with patch.object(bot, "message_buffer", buffers), \
         patch.object(bot, "session_locks", type(bot.session_locks)()), \
         patch.object(bot, "processing_sessions", set()), \
         patch.object(bot, "get_cached_group_info", new_callable=AsyncMock, return_value="测试群"), \
         patch.object(bot.asyncio, "sleep", new_callable=AsyncMock):
        first = asyncio.create_task(handler.process_buffered_messages("user-first"))
        await asyncio.wait_for(first_started.wait(), timeout=1)
        old_second = asyncio.create_task(handler.process_buffered_messages("user-second"))
        await real_sleep(0)
        old_second.cancel()
        await asyncio.gather(old_second, return_exceptions=True)
        buffered = buffers["user-second"]
        buffered["content"].append("second-more")
        buffered["message_ids"].append("second-more")
        buffered["incoming_messages"].append(message("second-more"))
        buffered["incoming_message"] = buffered["incoming_messages"][-1]
        second = asyncio.create_task(handler.process_buffered_messages("user-second"))
        await real_sleep(0)
        assert len(calls) == 1
        release_first.set()
        await asyncio.gather(first, second)
        assert calls == [("first", "first", ["first"]),
                         ("second-more", "second\nsecond-more", ["second", "second-more"])]
        assert not buffers
        assert not bot.processing_sessions


@pytest.mark.asyncio
async def test_handle_ai_stream_reaction_uses_buffer_ids_and_cleans_on_early_return():
    from app.dingtalk_bot import BufferedTurnContext

    handler = _reaction_handler(core_result=None)
    incoming = _reaction_message("single")
    buffered = BufferedTurnContext(
        incoming_messages=[_reaction_message("buffer-1")],
        current_message_ids=["buffer-1", "", "buffer-1", "buffer-2"],
        at_user_ids=[],
        group_info=None,
    )

    await handler.handle_ai_stream(
        incoming,
        "内容",
        "conv",
        [],
        incoming_messages=[_reaction_message("fallback")],
        current_message_ids=["explicit"],
        buffered_context=buffered,
    )

    calls = handler.card_helper.set_processing_reaction.await_args_list
    assert [(call.args, call.kwargs["active"]) for call in calls] == [
        (("conv", "buffer-1"), True),
        (("conv", "buffer-2"), True),
        (("conv", "buffer-1"), False),
        (("conv", "buffer-2"), False),
    ]


@pytest.mark.asyncio
async def test_handle_ai_stream_reaction_preserves_core_exception():
    handler = _reaction_handler(core_side_effect=RuntimeError("core failed"))

    with pytest.raises(RuntimeError, match="core failed"):
        await handler.handle_ai_stream(
            _reaction_message("m1"), "内容", "conv", [], current_message_ids=["m1"]
        )

    assert [call.kwargs["active"] for call in handler.card_helper.set_processing_reaction.await_args_list] == [
        True,
        False,
    ]


@pytest.mark.asyncio
async def test_handle_ai_stream_reaction_waits_for_add_before_core():
    add_started = asyncio.Event()
    release_add = asyncio.Event()

    async def reaction(_conversation_id, _message_id, *, active):
        if active:
            add_started.set()
            await release_add.wait()
        return True

    handler = _reaction_handler(core_result="done")
    handler.card_helper.set_processing_reaction = AsyncMock(side_effect=reaction)
    task = asyncio.create_task(
        handler.handle_ai_stream(_reaction_message("m1"), "内容", "conv", [])
    )
    await add_started.wait()
    assert handler._handle_ai_stream_core.await_count == 0
    release_add.set()
    assert await task == "done"
    assert handler._handle_ai_stream_core.await_count == 1


@pytest.mark.asyncio
async def test_handle_ai_stream_reaction_cleans_when_add_is_cancelled():
    add_started = asyncio.Event()
    release_add = asyncio.Event()

    async def reaction(_conversation_id, _message_id, *, active):
        if active:
            add_started.set()
            await release_add.wait()
        return True

    handler = _reaction_handler(core_result="unreachable")
    handler.card_helper.set_processing_reaction = AsyncMock(side_effect=reaction)

    from app.dingtalk_reactions import ProcessingReactionScope

    def short_scope(card_helper, conversation_id, message_ids):
        return ProcessingReactionScope(
            card_helper,
            conversation_id,
            message_ids,
            timeout_seconds=0.05,
        )

    with patch("app.dingtalk_bot.ProcessingReactionScope", side_effect=short_scope):
        task = asyncio.create_task(
            handler.handle_ai_stream(_reaction_message("m1"), "内容", "conv", [])
        )
        await add_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert handler._handle_ai_stream_core.await_count == 0
    assert [call.kwargs["active"] for call in handler.card_helper.set_processing_reaction.await_args_list] == [
        True,
        False,
    ]


@pytest.mark.asyncio
async def test_handle_ai_stream_reaction_cleans_when_core_is_cancelled():
    import asyncio

    core_started = asyncio.Event()
    keep_core = asyncio.Event()

    async def core(*_args, **_kwargs):
        core_started.set()
        await keep_core.wait()

    handler = _reaction_handler()
    handler._handle_ai_stream_core = core
    task = asyncio.create_task(
        handler.handle_ai_stream(_reaction_message("m1"), "内容", "conv", [])
    )
    await core_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    active_values = [
        call.kwargs["active"] for call in handler.card_helper.set_processing_reaction.await_args_list
    ]
    assert active_values == [True, False]


@pytest.mark.asyncio
async def test_handle_ai_stream_reaction_cleanup_survives_outer_timeout():
    import asyncio

    core_started = asyncio.Event()
    keep_core = asyncio.Event()

    async def core(*_args, **_kwargs):
        core_started.set()
        await keep_core.wait()

    handler = _reaction_handler()
    handler._handle_ai_stream_core = core
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            handler.handle_ai_stream(_reaction_message("m1"), "内容", "conv", []),
            timeout=0.05,
        )

    assert [call.kwargs["active"] for call in handler.card_helper.set_processing_reaction.await_args_list] == [
        True,
        False,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "richText"])
async def test_bare_mention_enters_normal_buffered_context_flow(kind):
    from app.dingtalk_bot import GeminiBotHandler

    handler = object.__new__(GeminiBotHandler)
    incoming = SimpleNamespace(message_id=None, message_type=kind, text=SimpleNamespace(content=" "),
        rich_text_content=SimpleNamespace(rich_text_list=[{"text": " "}]),
        conversation_id="conv", conversation_type="2", sender_id="u", sender_staff_id="u",
        sender_nick="甲", at_users=[], is_in_at_list=True)
    buffers = {}
    def schedule(coroutine):
        coroutine.close()
        return MagicMock()
    with patch("app.dingtalk_bot.dingtalk_stream.ChatbotMessage.from_dict", return_value=incoming), \
         patch("app.dingtalk_bot.message_buffer", buffers), \
         patch("app.dingtalk_bot.asyncio.create_task", side_effect=schedule) as spawn:
        await handler.process(SimpleNamespace(data={}))
    spawn.assert_called_once()
    entry = next(iter(buffers.values()))
    assert "当前可见" in entry["content"][0]
    assert entry["incoming_messages"] == [incoming]


@pytest.mark.asyncio
async def test_empty_unmentioned_group_message_stays_silent():
    from app.dingtalk_bot import GeminiBotHandler

    incoming = SimpleNamespace(message_id=None, message_type="text", text=SimpleNamespace(content=" "),
                               conversation_type="2", is_in_at_list=False)
    with patch("app.dingtalk_bot.dingtalk_stream.ChatbotMessage.from_dict", return_value=incoming), \
         patch("app.dingtalk_bot.asyncio.create_task") as spawn:
        await object.__new__(GeminiBotHandler).process(SimpleNamespace(data={}))
    spawn.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("image_field", ["quoted_images", "context_images"])
async def test_dws_turn_integrates_history_mentions_and_quoted_images(image_field):
    from app.dingtalk_bot import GeminiBotHandler
    from app.dws_context import DwsContextBundle

    handler = object.__new__(GeminiBotHandler)
    handler.card_template_id = "test"
    handler.card_helper = MagicMock()
    handler.card_helper.set_processing_reaction = AsyncMock(return_value=True)
    handler.card_helper.create_and_deliver = AsyncMock(return_value="track")
    handler.card_helper.stream_update = AsyncMock()
    handler.card_helper.update_card = AsyncMock(return_value=True)
    incoming = SimpleNamespace(sender_id="u", sender_nick="甲", conversation_type="2", message_id="m1")
    png = b"\x89PNG\r\n\x1a\nimage"
    rows = [{"external_msg_id": "old", "content": "架构报错", "timestamp": "2026-11-11 10:00:00"}]
    bundle = DwsContextBundle(merged_history=[{"role": "user", "content": "架构报错", "sender_nick": "乙"}], persistable_messages=rows, restored_user_content="@小王 怎么看", quoted_images=[png])
    bundle.quoted_images = []
    setattr(bundle, image_field, [png])
    if image_field == "context_images":
        bundle.merged_history[0]["images"] = [png]
        bundle.merged_history[0]["timestamp"] = "2026-11-11 10:00:00"
    route = {"model": "fast", "thinking_level": "medium", "need_search": False, "temperature": "balanced", "thinking_text": "思考", "reason": "test"}
    with patch("app.dingtalk_bot.AI_BACKEND", "openai"), patch("app.dws_context.cfg.ENABLE_DWS_GROUP_CONTEXT", True), \
         patch("app.dws_context.cfg.DWS_PERSIST_GROUP_MESSAGES", True), patch("app.dingtalk_bot.ENABLE_BQB", False), \
         patch("app.dingtalk_bot.DINGTALK_TYPING_ENABLED", False), patch("app.dingtalk_bot.DINGTALK_REFERENCE_AUTO_ENABLED", False), \
         patch("app.dingtalk_bot.USE_STATS", False), patch("app.dingtalk_bot.get_session_key", return_value="session"), \
         patch("app.memory.get_history", side_effect=AssertionError("钉钉不得读取旧库")), patch("app.dingtalk_bot._load_soul", return_value=""), \
         patch("app.dingtalk_bot.fetch_dws_context_bundle", new_callable=AsyncMock, return_value=bundle), \
         patch("app.memory.persist_external_messages") as persist, \
         patch("app.dingtalk_bot._analyze_with_openai", new_callable=AsyncMock, return_value=route) as router, \
         patch("app.ai.messages_pipeline.prepare_messages_for_backend", side_effect=lambda messages, _: messages), \
         patch("app.ai.sampling_pipeline.resolve_sampling", return_value=(0.7, None, {})), \
         patch("app.ai.backend.create_backend_stream", return_value=_iter_consumer_chunks([{"content": "回答"}])) as stream, \
         patch("app.memory.update_history") as update, patch("app.dingtalk_bot._maybe_evolve_soul", new=MagicMock(return_value=None)), patch("app.dingtalk_bot.asyncio.create_task"):
        await handler.handle_ai_stream(incoming, "怎么看", "conv", [], current_message_ids=["m1", "m2"])

    persist.assert_not_called()
    assert "@小王" in router.await_args.args[0] and "架构报错" in router.await_args.args[0]
    assert router.await_args.args[1] is True
    assert stream.call_args.kwargs["image_data_list"] == ([png] if image_field == "quoted_images" else None)
    if image_field == "context_images":
        messages = stream.call_args.args[0]
        assert isinstance(messages[-1]["content"], str)
        assert "data:image/png" not in messages[-1]["content"]
        assert messages[1]["content"][1]["type"] == "image_url"
        assert "2026-11-11 10:00:00" in messages[1]["content"][0]["text"]
    assert "data:image/png;base64," in str(stream.call_args.args[0])
    assert "@小王" in str(stream.call_args.args[0])
    assert stream.call_args.kwargs["conversation_id"] == ""
    update.assert_not_called()


async def _capture_dws_media_messages(*, history_images=None, current_images=None, context_image_messages=None, route_need_image_edit=False):
    from app.dingtalk_bot import GeminiBotHandler
    from app.dws_context import DwsContextBundle

    handler = object.__new__(GeminiBotHandler)
    handler.card_template_id = "test"
    handler.card_helper = MagicMock()
    handler.card_helper.set_processing_reaction = AsyncMock(return_value=True)
    handler.card_helper.create_and_deliver = AsyncMock(return_value="track")
    handler.card_helper.stream_update = AsyncMock()
    handler.card_helper.update_card = AsyncMock(return_value=True)
    incoming = SimpleNamespace(
        sender_id="current-user",
        sender_nick="当前用户",
        sender_staff_id="current-user",
        conversation_type="2",
        message_id="current-message",
    )
    history_images = history_images or []
    current_images = current_images or []
    history = []
    for index, image in enumerate(history_images, start=1):
        history.append({
            "role": "user",
            "content": f"历史图片消息 {index}",
            "sender_nick": "历史成员",
            "timestamp": f"2026-11-12 10:0{index}:00",
            "images": [image],
        })
    bundle = DwsContextBundle(
        dws_available=True,
        context_images=list(history_images),
        context_image_messages=list(context_image_messages or []),
        merged_history=history,
        members_complete=True,
    )
    route = {
        "model": "fast",
        "thinking_level": "medium",
        "need_search": False,
        "temperature": "balanced",
        "thinking_text": "核对图片来源",
        "reason": "test",
        "need_image_edit": route_need_image_edit,
    }
    captured = {}

    async def backend_stream():
        yield {"content": "回答"}

    def capture_backend(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return backend_stream()

    with patch("app.dingtalk_bot.AI_BACKEND", "openai"), \
         patch("app.ai.messages_pipeline.AI_BACKEND", "openai"), \
         patch("app.ai.messages_pipeline.ENABLE_ROLE_REWRITE", True), \
         patch("app.dingtalk_bot.ENABLE_BQB", False), \
         patch("app.dingtalk_bot.DINGTALK_TYPING_ENABLED", False), \
         patch("app.dingtalk_bot.DINGTALK_REFERENCE_AUTO_ENABLED", False), \
         patch("app.dingtalk_bot.USE_STATS", False), \
         patch("app.dingtalk_bot.get_session_key", return_value="session"), \
         patch("app.dingtalk_bot._load_soul", return_value=""), \
         patch("app.dingtalk_bot.fetch_dws_context_bundle", new_callable=AsyncMock, return_value=bundle), \
         patch("app.dingtalk_bot._analyze_with_openai", new_callable=AsyncMock, return_value=route) as router, \
         patch("app.ai.sampling_pipeline.resolve_sampling", return_value=(0.7, None, {})), \
         patch("app.ai.backend.create_backend_stream", side_effect=capture_backend) as stream, \
         patch("app.dingtalk_bot.edit_image", new_callable=AsyncMock) as edit, \
         patch("app.memory.update_history"), \
         patch("app.dingtalk_bot._maybe_evolve_soul", new=MagicMock(return_value=None)), \
         patch("app.dingtalk_bot.asyncio.create_task"):
        await handler.handle_ai_stream(
            incoming,
            "请结合当前上下文回答",
            "conversation-test",
            [],
            image_data_list=current_images or None,
            current_message_ids=["current-message"],
        )

    assert stream.called
    return {
        "messages": captured["messages"],
        "kwargs": captured["kwargs"],
        "router": router,
        "edit": edit,
        "card": handler.card_helper,
    }


def _message_parts(message):
    content = message["content"]
    return content if isinstance(content, list) else [{"type": "text", "text": content}]


@pytest.mark.asyncio
async def test_handler_keeps_historical_image_before_current_text_with_real_message_pipeline():
    old_png = b"\x89PNG\r\n\x1a\nhistorical"
    result = await _capture_dws_media_messages(history_images=[old_png])

    messages = result["messages"]
    merged_user = messages[-1]
    parts = _message_parts(merged_user)
    assert [part.get("type") for part in parts] == ["text", "image_url", "text"]
    assert "历史图片" in parts[0]["text"]
    assert "当前用户" in parts[2]["text"]
    assert "本次上传图片" not in parts[2]["text"]


@pytest.mark.asyncio
async def test_handler_keeps_history_and_current_upload_image_ownership_and_counts():
    old_png = b"\x89PNG\r\n\x1a\nhistorical"
    current_jpeg = b"\xff\xd8\xffcurrent"
    result = await _capture_dws_media_messages(history_images=[old_png], current_images=[current_jpeg])

    parts = _message_parts(result["messages"][-1])
    assert [part.get("type") for part in parts] == ["text", "image_url", "text", "image_url"]
    assert "历史图片" in parts[0]["text"]
    assert "[本次上传图片x1]" in parts[2]["text"]
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert parts[3]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    # 普通历史图片已经在历史消息的多模态 block 中；后端的 current-image 参数只携带本次上传。
    assert result["kwargs"]["image_data_list"] == [current_jpeg]


@pytest.mark.asyncio
async def test_handler_places_quote_neighbor_image_before_current_text():
    quoted_png = b"\x89PNG\r\n\x1a\nquoted"
    neighbor = [{
        "role": "user",
        "content": "引用邻居图片",
        "sender_nick": "引用成员",
        "timestamp": "2026-11-12 10:20:00",
        "images": [quoted_png],
    }]
    result = await _capture_dws_media_messages(context_image_messages=neighbor)

    parts = _message_parts(result["messages"][-1])
    assert [part.get("type") for part in parts] == ["text", "image_url", "text"]
    assert "引用邻居图片" in parts[0]["text"]
    assert "当前用户" in parts[2]["text"]
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_handler_history_image_sets_visual_router_context_without_editing():
    old_png = b"\x89PNG\r\n\x1a\nhistorical"
    result = await _capture_dws_media_messages(history_images=[old_png], route_need_image_edit=True)

    assert result["router"].await_args.args[1] is True
    assert "历史上下文 1 张" in result["router"].await_args.args[0]
    assert "历史图片不是本次上传" in result["router"].await_args.args[0]
    result["edit"].assert_not_awaited()


@pytest.mark.asyncio
async def test_enrich_image_prompt_keeps_text_blocks_but_omits_image_base64():
    from app.dingtalk_bot import _enrich_image_prompt

    captured = {}

    async def capture_prompt(prompt):
        captured["prompt"] = prompt
        return "A cinematic portrait with warm natural light"

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "历史文字：用户想延续这个场景"},
                {
                    "type": "image_url",
                    "text": "不应出现在轻量提示词中的图片说明",
                    "image_url": {
                        "url": "data:image/png;base64,SECRET_IMAGE_BYTES",
                    },
                },
            ],
        },
        {"role": "user", "content": "当前文字：请把它改成电影海报"},
    ]

    with patch("app.dingtalk_bot._ask_lightweight_model", new=AsyncMock(side_effect=capture_prompt)):
        result = await _enrich_image_prompt(
            "a movie poster",
            "请把它改成电影海报",
            messages,
        )

    prompt = captured["prompt"]
    assert result == "A cinematic portrait with warm natural light"
    assert "历史文字：用户想延续这个场景" in prompt
    assert "当前文字：请把它改成电影海报" in prompt
    assert "data:image/png;base64,SECRET_IMAGE_BYTES" not in prompt
    assert "不应出现在轻量提示词中的图片说明" not in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/since", "/resume", "/soul evolve", "/clear", "/stats", "/temp"])
async def test_command_corp_mismatch_stops_before_execution(command):
    from app.dingtalk_bot import GeminiBotHandler
    from app.dws_context import DwsCorpCheckResult

    handler = object.__new__(GeminiBotHandler)
    handler.reply_markdown = MagicMock()
    incoming = SimpleNamespace(message_id=None, message_type="text", text=SimpleNamespace(content=command),
        conversation_id="conv", conversation_type="2", sender_id="u", sender_staff_id="u", sender_nick="甲", at_users=[], chatbot_corp_id="corp", is_in_at_list=True)
    with patch("app.dingtalk_bot.dingtalk_stream.ChatbotMessage.from_dict", return_value=incoming), \
         patch("app.dws_context.cfg.ENABLE_DWS_GROUP_CONTEXT", True), \
         patch("app.dingtalk_bot.check_dws_corp_access", new_callable=AsyncMock, return_value=DwsCorpCheckResult(allowed=False, corp_mismatch_error="企业不一致")), \
         patch("app.dws_context.run_dws_readonly_json", new_callable=AsyncMock) as fetch, \
         patch("app.clear_cutoff.reset_cutoff") as reset, patch("app.clear_cutoff.set_cutoff") as clear:
        await handler.process(SimpleNamespace(data={}))
    handler.reply_markdown.assert_called_once_with("dws 企业不一致错误", "企业不一致", incoming)
    fetch.assert_not_awaited()
    reset.assert_not_called()
    clear.assert_not_called()


@pytest.mark.asyncio
async def test_command_sync_persists_group_messages_before_command():
    from app.dingtalk_bot import GeminiBotHandler
    from app.dws_context import DwsCorpCheckResult

    handler = object.__new__(GeminiBotHandler)
    incoming = SimpleNamespace(conversation_type="2", chatbot_corp_id="corp", chatbot_user_id="bot")
    payload = {"messages": [{"messageId": "old", "sender": "乙", "text": "群消息", "createTime": "2026-11-11 14:00:00"}]}
    with patch("app.dws_context.cfg.ENABLE_DWS_GROUP_CONTEXT", True), patch("app.dws_context.cfg.DWS_PERSIST_GROUP_MESSAGES", True), \
         patch("app.dingtalk_bot.check_dws_corp_access", new_callable=AsyncMock, return_value=DwsCorpCheckResult(allowed=True)), \
         patch("app.dws_context.run_dws_readonly_json", new_callable=AsyncMock, return_value=payload), \
         patch("app.memory.persist_external_messages") as persist:
        assert await handler._sync_dws_for_command(incoming, "conv", "session", pull_group_messages=True)
    persist.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/since", "/resume", "/soul evolve"])
async def test_history_commands_sync_before_execution(command):
    from app.dingtalk_bot import GeminiBotHandler

    handler = object.__new__(GeminiBotHandler)
    handler.reply_markdown = MagicMock()
    incoming = SimpleNamespace(message_id=None, message_type="text", text=SimpleNamespace(content=command),
        conversation_id="conv", conversation_type="2", sender_id="u", sender_staff_id="u", sender_nick="甲", at_users=[], is_in_at_list=True)
    events = []
    async def sync(*args, **kwargs):
        assert kwargs["pull_group_messages"] is True
        events.append("sync")
        return True
    def command_action(*args, **kwargs):
        events.append("command")
        return ""
    with patch("app.dingtalk_bot.dingtalk_stream.ChatbotMessage.from_dict", return_value=incoming), \
         patch.object(handler, "_sync_dws_for_command", side_effect=sync), \
         patch.object(handler, "_read_dws_command_history", new_callable=AsyncMock, return_value=[]), \
         patch("app.context_inspector.render_since", side_effect=command_action), \
         patch("app.clear_cutoff.reset_cutoff", side_effect=command_action), \
         patch("app.agent_history.get_history_for_current_agent", return_value=[]), \
         patch("app.dingtalk_bot._is_soul_admin", return_value=True), \
         patch("app.dingtalk_bot._load_soul", return_value=""), \
         patch("app.dingtalk_bot._maybe_evolve_soul", new_callable=AsyncMock, side_effect=command_action):
        await handler.process(SimpleNamespace(data={}))
    assert events == ["sync", "command"]


async def _run_dingtalk_consumer(
    chunks,
    members=None,
    *,
    return_stream_updates=False,
    return_soul_hook=False,
    full_update_success=True,
    typing_task_factory=None,
    observed_stream_updates=None,
):
    """运行真实 DingTalk handler 收尾逻辑，返回最终卡片全量更新数据。"""
    from app.dingtalk_bot import GeminiBotHandler

    handler = object.__new__(GeminiBotHandler)
    handler.card_template_id = "test-template"
    handler.card_helper = MagicMock()
    handler.card_helper.set_processing_reaction = AsyncMock(return_value=True)
    handler.card_helper.create_and_deliver = AsyncMock(return_value="track-test")
    handler.card_helper.stream_update = AsyncMock()
    if observed_stream_updates is not None:
        handler.card_helper.stream_update.side_effect = lambda *args, **kwargs: observed_stream_updates.append((args, kwargs))
    handler.card_helper.update_card = AsyncMock(return_value=full_update_success)
    incoming_message = SimpleNamespace(
        sender_id="sender-test",
        sender_nick="测试用户",
        conversation_id="conversation-test",
        conversation_type="2" if members else "1",
    )

    route_result = {
        "model": "fast",
        "thinking_level": "medium",
        "need_search": False,
        "temperature": "balanced",
        "thinking_text": "正在核对",
        "reason": "test",
    }
    soul_hook = MagicMock(return_value=None)

    from app.dws_context import DwsContextBundle, DwsCorpCheckResult
    with patch("app.dingtalk_bot.fetch_dws_context_bundle", new_callable=AsyncMock, return_value=DwsContextBundle(dws_available=True, members=members or [], members_complete=True)), \
         patch("app.dingtalk_bot.check_dws_corp_access", new_callable=AsyncMock, return_value=DwsCorpCheckResult(allowed=True)), \
         patch("app.dws_members.send_mentions", new_callable=AsyncMock) as send, \
         patch("app.dingtalk_bot.AI_BACKEND", "openai"), \
         patch("app.dingtalk_bot.DINGTALK_TYPING_ENABLED", bool(typing_task_factory)), \
         patch("app.dingtalk_bot.DINGTALK_REFERENCE_AUTO_ENABLED", False), \
         patch("app.dingtalk_bot.USE_STATS", False), \
         patch("app.dingtalk_bot.get_session_key", return_value="session-test"), \
         patch("app.memory.get_history", return_value=[]), \
         patch("app.dingtalk_bot._load_soul", return_value=""), \
         patch("app.dingtalk_bot._analyze_with_openai", new_callable=AsyncMock, return_value=route_result), \
         patch("app.ai.messages_pipeline.prepare_messages_for_backend", side_effect=lambda messages, _bot_id: messages), \
         patch("app.ai.sampling_pipeline.resolve_sampling", return_value=(0.7, None, {})), \
         patch("app.ai.backend.create_backend_stream", return_value=_iter_consumer_chunks(chunks)), \
         patch("app.memory.update_history"), \
         patch("app.dingtalk_bot._maybe_evolve_soul", new=soul_hook), \
         patch("app.dingtalk_bot.asyncio.create_task", side_effect=typing_task_factory):
        await handler.handle_ai_stream(
            incoming_message,
            "测试消息",
            "conversation-test",
            [],
        )

    if members:
        send.assert_not_awaited()
    result = handler.card_helper.update_card.await_args_list[-1].args[1]
    if return_stream_updates and return_soul_hook:
        return result, handler.card_helper.stream_update.await_args_list, soul_hook
    if return_stream_updates:
        return result, handler.card_helper.stream_update.await_args_list
    if return_soul_hook:
        return result, soul_hook
    return result


@pytest.mark.asyncio
async def test_model_member_marker_renders_in_same_card_without_separate_notification():
    result = await _run_dingtalk_consumer([{"content": "请 [[at:1]] 帮忙核对。"}], [{"id": "verified-id", "name": "同事甲"}])
    assert "<a atId=verified-id>同事甲</a>" in result["msgContent"]
    assert "[[at:" not in result["msgContent"]


@pytest.mark.asyncio
async def test_split_member_marker_is_hidden_until_complete_and_card_keeps_anchor():
    result, updates = await _run_dingtalk_consumer(
        [{"content": "请 [[at:"}, {"content": "1]] 帮忙核对。"}],
        [{"id": "verified-id", "name": "同事甲"}],
        return_stream_updates=True,
    )

    displayed = [call.args[1] for call in updates if call.kwargs.get("content_key") == "msgContent"]
    assert displayed
    assert all("[[at:" not in content for content in displayed)
    assert any("<a atId=verified-id>同事甲</a>" in content for content in displayed)
    assert "<a atId=verified-id>同事甲</a>" in result["msgContent"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tail", ["[[", "[[a", "[[at", "[[at:"])
async def test_incomplete_member_marker_suffix_is_hidden_from_final_card(tail):
    result, updates = await _run_dingtalk_consumer(
        [{"content": "请稍候 " + tail}],
        [{"id": "verified-id", "name": "同事甲"}],
        return_stream_updates=True,
    )

    displayed = [call.args[1] for call in updates if call.kwargs.get("content_key") == "msgContent"]
    assert displayed
    assert all(tail not in content for content in displayed)
    assert tail not in result["msgContent"]


@pytest.mark.asyncio
async def test_history_keeps_plain_member_mention_while_card_uses_anchor():
    result, soul_hook = await _run_dingtalk_consumer(
        [{"content": "请 [[at:1]] 帮忙核对。"}],
        [{"id": "verified-id", "name": "同事甲"}],
        return_soul_hook=True,
    )

    assert "<a atId=verified-id>同事甲</a>" in result["msgContent"]
    history_response = soul_hook.call_args.args[2]
    assert "@同事甲" in history_response
    assert "<a atId=" not in history_response


@pytest.mark.asyncio
async def test_dingtalk_consumer_promotes_reasoning_summary_to_final_status():
    """Responses summary chunk 应进入 DingTalk full_thinking 和最终摘要。"""
    final_update = await _run_dingtalk_consumer([
        {"thinking_start": True},
        {"thinking": "核对证据"},
        {"thinking": "并形成结论"},
        {"thinking_end": True},
        {"content": "最终回答"},
        {"usage": {"model": "claude-opus-4-6-thinking", "input_tokens": 1, "output_tokens": 1}},
    ])

    assert "最终回答" in final_update["msgContent"]
    assert "<font color='#aaaaaa' size='2'>🧠 核对证据并形成结论</font>" in final_update["statusText"]


@pytest.mark.asyncio
async def test_dingtalk_consumer_without_reasoning_has_no_fake_summary():
    """没有 reasoning chunk 时，DingTalk statusText 不应凭空生成思考摘要。"""
    final_update = await _run_dingtalk_consumer([
        {"content": "普通回答"},
        {"usage": {"model": "claude-opus-4-6-thinking", "input_tokens": 1, "output_tokens": 1}},
    ])

    assert "普通回答" in final_update["msgContent"]
    assert "<font color='#aaaaaa' size='2'>🧠" not in final_update["statusText"]



class TestShortenModelName:
    """_shorten_model_name 模型名归一化测试"""

    def test_openrouter_anthropic_prefix_stripped(self):
        from app.dingtalk_bot import _shorten_model_name
        assert _shorten_model_name("anthropic/claude-haiku-4-5") == "claude-haiku-4-5"

    def test_openrouter_openai_prefix_stripped(self):
        from app.dingtalk_bot import _shorten_model_name
        assert _shorten_model_name("openai/gpt-4o") == "gpt-4o"

    def test_vertex_version_suffix_stripped(self):
        from app.dingtalk_bot import _shorten_model_name
        assert _shorten_model_name("anthropic/claude-sonnet-4-5@20250929") == "claude-sonnet-4-5"

    def test_beta_suffix_stripped(self):
        from app.dingtalk_bot import _shorten_model_name
        assert _shorten_model_name("anthropic/claude-haiku-4-5:beta") == "claude-haiku-4-5"

    def test_gemini_prefix_stripped(self):
        from app.dingtalk_bot import _shorten_model_name
        assert _shorten_model_name("gemini-3-flash-preview") == "3-flash"

    def test_gemini_pro_preview_stripped(self):
        from app.dingtalk_bot import _shorten_model_name
        assert _shorten_model_name("gemini-3.1-pro-preview") == "3.1-pro"

    def test_plain_model_name_unchanged(self):
        from app.dingtalk_bot import _shorten_model_name
        assert _shorten_model_name("gpt-4o") == "gpt-4o"

    def test_provider_prefix_with_version_and_suffix(self):
        from app.dingtalk_bot import _shorten_model_name
        result = _shorten_model_name("vertex_ai/claude-opus-4-5@20260101:extended")
        assert result == "claude-opus-4-5"

    def test_no_slash_no_at_no_colon(self):
        from app.dingtalk_bot import _shorten_model_name
        assert _shorten_model_name("claude-haiku-4-5") == "claude-haiku-4-5"


class TestModelStatus:
    """模型 footer 的显示顺序和安全编码。"""

    def test_fallback_shows_actual_then_primary_error(self):
        from app.dingtalk_bot import _build_model_status

        status = _build_model_status(
            {
                "model": "gemini-3.6-flash",
                "requested_model": "gemini-3.6-flash-tiered",
                "fallback": True,
                "fallback_error": "Gemini server error HTTP 503: upstream </font>\n\u202e details",
            },
            "unused-primary",
        )

        assert status.index("🤖 gemini-3.6-flash") < status.index("主模型 gemini-3.6-flash-tiered")
        assert "&lt;/font&gt;" in status
        assert "\u202e" not in status
        assert "Gemini server error HTTP 503" in status

    def test_open_circuit_hides_historical_error(self):
        from app.dingtalk_bot import _build_model_status

        status = _build_model_status(
            {
                "model": "gemini-3.6-flash",
                "requested_model": "gemini-3.6-flash-tiered",
                "fallback": True,
                "circuit_open": True,
                "fallback_error": "historical secret HTTP 503",
            },
            "unused-primary",
        )

        assert "🤖 gemini-3.6-flash" in status
        assert "主模型 gemini-3.6-flash-tiered: circuit open" in status
        assert "HTTP" not in status

    def test_primary_status_keeps_existing_shape(self):
        from app.dingtalk_bot import _build_model_status

        assert _build_model_status({"model": "gemini-3.6-flash"}, "unused") == "🤖 3.6-flash"

    def test_error_card_display_preserves_markdown_newlines(self):
        from app.dingtalk_bot import safe_display_text

        rendered = safe_display_text(
            "❌ fallback 模型异常\n\n主模型异常\n1. 图片触发安全过滤\n2. 请稍后重试",
            1000,
            keep_newlines=True,
        )

        assert "fallback 模型异常\n\n主模型异常" in rendered
        assert "1. 图片触发安全过滤\n2. 请稍后重试" in rendered


@pytest.mark.asyncio
async def test_dingtalk_consumer_embeds_bqb_in_card_and_sanitizes_history():
    """流式过程中隐藏 [[bqb:...]] 源码，收尾时卡片内嵌 @media_id 且历史不写入 @media_id。"""
    from app.bqb import BQBResolveResult
    from app.dingtalk_bot import GeminiBotHandler

    handler = object.__new__(GeminiBotHandler)
    handler.card_template_id = "test-template"
    handler.card_helper = MagicMock()
    handler.card_helper.set_processing_reaction = AsyncMock(return_value=True)
    handler.card_helper.create_and_deliver = AsyncMock(return_value="track-bqb")
    handler.card_helper.stream_update = AsyncMock()
    handler.card_helper.update_card = AsyncMock(return_value=True)

    incoming_message = SimpleNamespace(
        sender_id="sender-bqb",
        sender_nick="测试用户",
        conversation_id="conv-bqb",
        conversation_type="1",
    )
    route_result = {
        "model": "fast",
        "thinking_level": "low",
        "need_search": False,
        "temperature": "balanced",
        "thinking_text": "正在回梗",
        "reason": "test",
    }
    chunks = [
        {"content": "这也太真实了吧 [[bqb:熊猫"},
        {"content": "头 刺激]]"},
        {"usage": {"model": "claude-sonnet-4-6", "input_tokens": 10, "output_tokens": 8}},
    ]
    fake_resolved = BQBResolveResult(
        card_text="这也太真实了吧\n\n![这么刺激吗](@lALP_FAKE_BQB_123)",
        history_text="这也太真实了吧 [[bqb:金馆长熊猫 这么刺激吗]]",
        sent_count=1,
    )

    with patch("app.dingtalk_bot.AI_BACKEND", "openai"), \
         patch("app.dingtalk_bot.ENABLE_BQB", True), \
         patch("app.dingtalk_bot.schedule_bqb_index_warmup"), \
         patch("app.dingtalk_bot.resolve_bqb_tags", new_callable=AsyncMock, return_value=fake_resolved), \
         patch("app.dingtalk_bot.DINGTALK_TYPING_ENABLED", False), \
         patch("app.dingtalk_bot.DINGTALK_REFERENCE_AUTO_ENABLED", False), \
         patch("app.dingtalk_bot.USE_STATS", False), \
         patch("app.dingtalk_bot.get_session_key", return_value="session-bqb"), \
         patch("app.memory.get_history", return_value=[]), \
         patch("app.dingtalk_bot._load_soul", return_value=""), \
         patch("app.dingtalk_bot._analyze_with_openai", new_callable=AsyncMock, return_value=route_result), \
         patch("app.ai.messages_pipeline.prepare_messages_for_backend", side_effect=lambda messages, _bot_id: messages), \
         patch("app.ai.sampling_pipeline.resolve_sampling", return_value=(0.7, None, {})), \
         patch("app.ai.backend.create_backend_stream", return_value=_iter_consumer_chunks(chunks)), \
         patch("app.memory.update_history") as mock_update_history, \
         patch("app.dingtalk_bot._maybe_evolve_soul", new=MagicMock(return_value=None)), \
         patch("app.dingtalk_bot.asyncio.create_task"):
        await handler.handle_ai_stream(
            incoming_message,
            "今天开会差点睡着",
            "conv-bqb",
            [],
        )

    # 流式中间帧（is_finalize=False）绝不暴露未闭合或完整的 [[bqb:... 标记
    non_final_updates = [
        call.args[1]
        for call in handler.card_helper.stream_update.await_args_list
        if not call.kwargs.get("is_finalize") and call.kwargs.get("content_key") == "msgContent"
    ]
    assert non_final_updates
    assert all("[[bqb" not in text for text in non_final_updates)

    # 最终卡片渲染含 @media_id Markdown 图片
    final_update = handler.card_helper.update_card.await_args_list[-1].args[1]
    assert "![这么刺激吗](@lALP_FAKE_BQB_123)" in final_update["msgContent"]

    # 历史持久化仅保留语义标签，不写入短期 @media_id
    mock_update_history.assert_not_called()


@pytest.mark.asyncio
async def test_dingtalk_group_quote_injects_context_into_router_and_llm_prompt():
    """群聊 @机器人 引用回复问号 '?' 时，路由与主模型均收到被引用上下文与非@群聊历史。"""
    from app.dws_context import DwsContextBundle, DwsQuoteInfo
    from app.dingtalk_bot import GeminiBotHandler

    handler = object.__new__(GeminiBotHandler)
    handler.card_template_id = "test-template"
    handler.card_helper = MagicMock()
    handler.card_helper.set_processing_reaction = AsyncMock(return_value=True)
    handler.card_helper.create_and_deliver = AsyncMock(return_value="track-quote")
    handler.card_helper.stream_update = AsyncMock()
    handler.card_helper.update_card = AsyncMock(return_value=True)

    incoming_message = SimpleNamespace(
        message_id="msg_q_1",
        sender_id="sender-zeng",
        sender_nick="我司曾工",
        conversation_id="cid-group-quote",
        conversation_type="2",
    )

    fake_bundle = DwsContextBundle(
        dws_available=True,
        merged_history=[
            {
                "role": "user",
                "content": "越脱离群众越好",
                "timestamp": "2026-11-11 14:10:05",
                "sender_nick": "杰少",
            },
            {
                "role": "user",
                "content": "华少其实就是曾工本尊",
                "timestamp": "2026-11-11 14:12:00",
                "sender_nick": "杰少",
            },
        ],
        quote_info=DwsQuoteInfo(
            message_id="m_target",
            sender="杰少",
            text="越脱离群众越好",
            create_time="2026-11-11 14:10:05",
        ),
        quote_prompt_block=(
            "<quoted_context>\n"
            "[被引用原消息] [2026-11-11 14:10:05] 杰少: 越脱离群众越好\n"
            "[引用处前后上下文]\n"
            "- 👉 [2026-11-11 14:10:05] 杰少: 越脱离群众越好\n"
            "</quoted_context>"
        ),
        quote_router_text="[用户正在引用消息（杰少: 越脱离群众越好）]\n用户当前发言: ?",
        quote_history_prefix="[引用 杰少: 越脱离群众越好] ",
    )

    route_result = {
        "model": "fast",
        "thinking_level": "medium",
        "need_search": False,
        "temperature": "balanced",
        "thinking_text": "正在细看引用",
        "reason": "引用追问",
    }
    captured_stream_messages = []

    def fake_create_stream(messages, **_kwargs):
        captured_stream_messages.extend(messages)
        return _iter_consumer_chunks(
            [
                {"content": "原来曾工是在问杰少这句话！"},
                {"usage": {"model": "claude-sonnet-4-6", "input_tokens": 20, "output_tokens": 10}},
            ]
        )

    mock_router = AsyncMock(return_value=route_result)

    with patch("app.dingtalk_bot.AI_BACKEND", "openai"), \
         patch("app.dingtalk_bot.ENABLE_BQB", False), \
         patch("app.dingtalk_bot.DINGTALK_TYPING_ENABLED", False), \
         patch("app.dingtalk_bot.DINGTALK_REFERENCE_AUTO_ENABLED", True), \
         patch("app.dingtalk_bot.USE_STATS", False), \
         patch("app.dingtalk_bot.fetch_dws_context_bundle", new_callable=AsyncMock, return_value=fake_bundle), \
         patch("app.dingtalk_bot.get_session_key", return_value="dingtalk_cid-group-quote"), \
         patch("app.memory.get_history", return_value=[]), \
         patch("app.dingtalk_bot._load_soul", return_value=""), \
         patch("app.dingtalk_bot._analyze_with_openai", new=mock_router), \
         patch("app.ai.sampling_pipeline.resolve_sampling", return_value=(0.7, None, {})), \
         patch("app.ai.backend.create_backend_stream", side_effect=fake_create_stream), \
         patch("app.memory.update_history") as mock_update_history, \
         patch("app.dingtalk_bot._maybe_evolve_soul", new=MagicMock(return_value=None)), \
         patch("app.dingtalk_bot.asyncio.create_task"):
        await handler.handle_ai_stream(
            incoming_message,
            "?",
            "cid-group-quote",
            [],
            group_info={"name": "测试群"},
        )

    # 1. 路由预分析收到了带引用上下文的文本，而非裸 '?'
    router_arg = mock_router.await_args.args[0]
    assert "越脱离群众越好" in router_arg

    # 2. 主模型收到了群聊非@消息历史与当前轮 <quoted_context>
    joined_msgs = "\n".join(str(m.get("content", "")) for m in captured_stream_messages if m.get("role") == "user")
    assert "华少其实就是曾工本尊" in joined_msgs
    assert "<quoted_context>" in joined_msgs
    assert "越脱离群众越好" in joined_msgs

    # 3. 历史落盘保留紧凑单行引用摘要
    mock_update_history.assert_not_called()


@pytest.mark.asyncio
async def test_dingtalk_bot_reports_error_when_dws_corp_mismatches_bot_corp():
    """当 dws 登录企业与机器人所在企业不一致时，机器人必须直接向钉钉回复报错并中止后续调用。"""
    from app.dws_context import DwsContextBundle
    from app.dingtalk_bot import GeminiBotHandler

    handler = object.__new__(GeminiBotHandler)
    handler.card_template_id = "test-template"
    handler.card_helper = MagicMock()
    handler.card_helper.set_processing_reaction = AsyncMock(return_value=True)
    handler.card_helper.create_and_deliver = AsyncMock(return_value="track-never")
    handler.reply_markdown = MagicMock()

    incoming_message = SimpleNamespace(
        message_id="msg_corp_err_1",
        sender_id="sender-zeng",
        sender_nick="我司曾工",
        conversation_id="cid-group-1",
        conversation_type="2",
        chatbot_corp_id="ding42b8b39e5adac00a4ac5d6980864d335",
    )

    mismatch_bundle = DwsContextBundle(
        dws_available=False,
        corp_mismatch_error=(
            "❌ **dws 登录企业与当前机器人所在企业不一致**\n\n"
            "- **dws 当前登录企业**：其他外部公司 (`ding_wrong_company_888`)\n"
            "- **机器人所在企业**：`ding42b8b39e5adac00a4ac5d6980864d335`"
        ),
    )

    mock_router = AsyncMock()
    mock_stream = MagicMock()

    with patch("app.dingtalk_bot.AI_BACKEND", "gemini"), \
         patch("app.dingtalk_bot.fetch_dws_context_bundle", new_callable=AsyncMock, return_value=mismatch_bundle), \
         patch("app.dingtalk_bot.get_session_key", return_value="dingtalk_cid-group-1"), \
         patch("app.memory.get_history", return_value=[]), \
         patch("app.dingtalk_bot._analyze_with_gemini", new=mock_router), \
         patch("app.ai.backend.create_backend_stream", new=mock_stream):
        await handler.handle_ai_stream(
            incoming_message,
            "看看群里刚才聊了啥",
            "cid-group-1",
            [],
            group_info={"name": "测试群"},
        )

    handler.reply_markdown.assert_called_once()
    title_arg, text_arg, _msg_arg = handler.reply_markdown.call_args.args
    assert "企业不一致" in title_arg
    assert "ding_wrong_company_888" in text_arg
    assert "ding42b8b39e5adac00a4ac5d6980864d335" in text_arg
    mock_router.assert_not_awaited()
    handler.card_helper.create_and_deliver.assert_not_awaited()
    mock_stream.assert_not_called()




@pytest.mark.asyncio
async def test_final_card_does_not_prepend_an_extra_sender_mention():
    result = await _run_dingtalk_consumer(
        [{"content": "[[at:1]] @同事甲 请核对。"}],
        [{"id": "member-a", "name": "同事甲"}],
    )
    assert not result["msgContent"].startswith("👋")
    assert result["msgContent"].count("同事甲") == 1
    assert result["msgContent"].count("atId=member-a") == 1
    assert result["msgButtons"] == []


@pytest.mark.asyncio
async def test_completed_card_keeps_formal_status_after_cleanup():
    result, updates = await _run_dingtalk_consumer([{"content": "完成正文"}], return_stream_updates=True)
    assert "top_p=default" in result["statusText"]
    assert not any(c.kwargs.get("content_key") == "statusText" and not c.args[1] for c in updates)


@pytest.mark.asyncio
async def test_failed_full_card_update_keeps_status_in_body_without_buttons():
    result, updates = await _run_dingtalk_consumer(
        [{"content": "完成正文"}], return_stream_updates=True, full_update_success=False,
    )
    final_body = [c.args[1] for c in updates if c.kwargs.get("content_key") == "msgContent" and c.kwargs.get("is_finalize")][-1]
    assert "完成正文" in final_body and "top_p=default" in final_body
    assert "dtmd://" not in final_body
    assert result["msgButtons"] == []
    assert updates[-1].kwargs.get("content_key") == "statusText" and not updates[-1].args[1]


@pytest.mark.asyncio
async def test_cancelled_typing_task_still_cleans_card_status():
    import asyncio

    updates = []
    cancelled = asyncio.get_running_loop().create_future()
    cancelled.cancel()

    def cancelled_task(coro):
        if coro is not None:
            coro.close()
        return cancelled

    with pytest.raises(asyncio.CancelledError):
        await _run_dingtalk_consumer(
            [{"content": "完成正文"}], typing_task_factory=cancelled_task, observed_stream_updates=updates,
        )
    assert any(kwargs.get("content_key") == "statusText" and not args[1] for args, kwargs in updates)
