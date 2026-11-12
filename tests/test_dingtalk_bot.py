"""dingtalk_bot 辅助函数单元测试"""
import os
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


@pytest.mark.asyncio
@pytest.mark.parametrize("image_field", ["quoted_images", "context_images"])
async def test_dws_turn_integrates_history_mentions_and_quoted_images(image_field):
    from app.dingtalk_bot import GeminiBotHandler
    from app.dws_context import DwsContextBundle

    handler = object.__new__(GeminiBotHandler)
    handler.card_template_id = "test"
    handler.card_helper = MagicMock()
    handler.card_helper.create_and_deliver = AsyncMock(return_value="track")
    handler.card_helper.stream_update = AsyncMock()
    handler.card_helper.update_card = AsyncMock(return_value=True)
    incoming = SimpleNamespace(sender_id="u", sender_nick="甲", conversation_type="2", message_id="m1")
    png = b"\x89PNG\r\n\x1a\nimage"
    rows = [{"external_msg_id": "old", "content": "架构报错", "timestamp": "2026-11-11 10:00:00"}]
    bundle = DwsContextBundle(merged_history=[{"role": "user", "content": "架构报错", "sender_nick": "乙"}], persistable_messages=rows, restored_user_content="@小王 怎么看", quoted_images=[png])
    bundle.quoted_images = []
    setattr(bundle, image_field, [png])
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
    assert stream.call_args.kwargs["image_data_list"] == [png]
    assert "data:image/png;base64," in str(stream.call_args.args[0])
    assert "@小王" in str(stream.call_args.args[0])
    assert stream.call_args.kwargs["conversation_id"] == ""
    update.assert_not_called()


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


async def _run_dingtalk_consumer(chunks, members=None):
    """运行真实 DingTalk handler 收尾逻辑，返回最终卡片全量更新数据。"""
    from app.dingtalk_bot import GeminiBotHandler

    handler = object.__new__(GeminiBotHandler)
    handler.card_template_id = "test-template"
    handler.card_helper = MagicMock()
    handler.card_helper.create_and_deliver = AsyncMock(return_value="track-test")
    handler.card_helper.stream_update = AsyncMock()
    handler.card_helper.update_card = AsyncMock(return_value=True)
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

    from app.dws_context import DwsContextBundle, DwsCorpCheckResult
    with patch("app.dingtalk_bot.fetch_dws_context_bundle", new_callable=AsyncMock, return_value=DwsContextBundle(dws_available=True, members=members or [], members_complete=True)), \
         patch("app.dingtalk_bot.check_dws_corp_access", new_callable=AsyncMock, return_value=DwsCorpCheckResult(allowed=True)), \
         patch("app.dingtalk_bot.send_mentions", new_callable=AsyncMock) as send, \
         patch("app.dingtalk_bot.AI_BACKEND", "openai"), \
         patch("app.dingtalk_bot.DINGTALK_TYPING_ENABLED", False), \
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
         patch("app.dingtalk_bot._maybe_evolve_soul", new=MagicMock(return_value=None)), \
         patch("app.dingtalk_bot.asyncio.create_task"):
        await handler.handle_ai_stream(
            incoming_message,
            "测试消息",
            "conversation-test",
            [],
        )

    if members:
        send.assert_awaited_once_with("conversation-test", [members[0]["id"]], members)
    return handler.card_helper.update_card.await_args_list[-1].args[1]


@pytest.mark.asyncio
async def test_model_member_marker_sends_actual_targeted_notification():
    result = await _run_dingtalk_consumer([{"content": "请 [[at:1]] 帮忙核对。"}], [{"id": "verified-id", "name": "同事甲"}])
    assert "@同事甲" in result["msgContent"]
    assert "[[at:" not in result["msgContent"]


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


