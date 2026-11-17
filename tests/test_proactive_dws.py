# -*- coding: utf-8 -*-
"""主动群聊 DWS 只读适配的契约测试。

这些测试只模拟 dws 的 JSON 回执，不读取本机凭据、不启动 dws CLI，也不发送钉钉消息。
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.dws_context import DwsAuthIdentity, DwsCorpCheckResult


BEIJING = timezone(timedelta(hours=8))


def _allowed_corp(corp: str = "corp-1") -> DwsCorpCheckResult:
    return DwsCorpCheckResult(
        allowed=True,
        identity=DwsAuthIdentity(authenticated=True, corp_id=corp),
    )


def _groups_page(items, *, complete=True, has_more=False, failures=None):
    return {
        "complete": complete,
        "failures": list(failures or []),
        "groups": list(items),
        "hasMore": has_more,
        "stopReason": "source_complete" if complete and not has_more else "page_limit",
    }


def _bot_payload(conversation_id: str, rows, *, complete=True, bots_complete=True):
    return {
        "ok": True,
        "data": {
            "conversationId": conversation_id,
            "complete": complete,
            "partial": False,
            "buckets": {"bots": {"complete": bots_complete}},
            "bots": list(rows),
        },
    }


def _group_rows_payload(conversation_id: str, rows, *, has_more=False):
    return {
        "ok": True,
        "data": {"messages": list(rows), "conversationId": conversation_id, "hasMore": has_more},
    }


@pytest.mark.asyncio
async def test_discover_groups_requires_corp_gate_and_collects_finite_pages():
    from app import proactive_dws

    pages = [_groups_page([
        {"openConversationId": "cid-1", "name": "研发群"},
        {"openConversationId": "cid-2", "name": "产品群"},
    ])]
    calls = []

    async def read(args, **kwargs):
        calls.append((list(args), kwargs))
        return pages.pop(0)

    with patch.object(proactive_dws, "check_dws_corp_access", new_callable=AsyncMock, return_value=_allowed_corp()), \
         patch.object(proactive_dws, "run_dws_readonly_json", side_effect=read):
        groups = await proactive_dws.discover_groups("corp-1")

    assert [(item["cid"], item["name"]) for item in groups] == [
        ("cid-1", "研发群"),
        ("cid-2", "产品群"),
    ]
    assert all(item["complete"] is True for item in groups)
    assert len(calls) == 1
    assert all(args[:2] == ["chat", "+chat-list-all"] for args, _ in calls)
    assert calls[0][0][calls[0][0].index("--limit") + 1] == "100"
    assert "--page-all" in calls[0][0]
    assert calls[0][0][calls[0][0].index("--page-limit") + 1] == "10"
    assert all("-f" in args and args[args.index("-f") + 1] == "json" for args, _ in calls)


@pytest.mark.asyncio
async def test_discover_groups_fails_closed_on_corp_denial_or_mixed_source():
    from app import proactive_dws

    read = AsyncMock(
        return_value=_groups_page(
            [
                {"openConversationId": "cid-1", "name": "研发群"},
            ],
            failures=[{"reason": "group_source_inconsistent"}],
        )
    )
    with patch.object(
        proactive_dws,
        "check_dws_corp_access",
        new_callable=AsyncMock,
        return_value=DwsCorpCheckResult(allowed=False),
    ), patch.object(proactive_dws, "run_dws_readonly_json", read):
        assert await proactive_dws.discover_groups("corp-1") == []
    read.assert_not_awaited()

    with patch.object(
        proactive_dws,
        "check_dws_corp_access",
        new_callable=AsyncMock,
        return_value=_allowed_corp(),
    ), patch.object(proactive_dws, "run_dws_readonly_json", read):
        assert await proactive_dws.discover_groups("corp-1") == []


@pytest.mark.asyncio
async def test_discover_groups_stops_on_incomplete_page_instead_of_using_partial_groups():
    from app import proactive_dws

    read = AsyncMock(return_value=_groups_page([], complete=False, has_more=True))
    with patch.object(
        proactive_dws,
        "check_dws_corp_access",
        new_callable=AsyncMock,
        return_value=_allowed_corp(),
    ), patch.object(proactive_dws, "run_dws_readonly_json", read):
        assert await proactive_dws.discover_groups("corp-1") == []

    assert read.await_count == 1


@pytest.mark.asyncio
async def test_snapshot_requires_complete_unique_robot_membership_and_same_conversation():
    from app import proactive_dws

    now = datetime(2026, 11, 17, 10, 0, tzinfo=BEIJING)
    bot_rows = [
        {"robotCode": "robot-a", "openDingtalkId": "bot-a"},
        {"robotCode": "robot-b", "openDingtalkId": "bot-b"},
    ]
    messages = [
        {
            "messageId": "human-1",
            "conversationId": "cid-1",
            "senderId": "staff-1",
            "sendType": "user",
            "createTime": "2026-11-17 09:00:00",
            "text": "早上好",
        },
        {
            "messageId": "bot-1",
            "conversationId": "cid-1",
            "senderId": "bot-a",
            "sendType": "robot",
            "createTime": "2026-11-17 09:50:00",
            "text": "机器人消息",
        },
        {
            "messageId": "unknown-type",
            "conversationId": "cid-1",
            "senderId": "staff-9",
            "sendType": "event",
            "createTime": "2026-11-17 08:59:00",
            "text": "未知类型",
        },
        {
            "messageId": "future-human",
            "conversationId": "cid-1",
            "senderId": "staff-2",
            "sendType": "user",
            "createTime": "2026-11-17 10:01:00",
            "text": "未来消息",
        },
        {
            "messageId": "wrong-cid",
            "conversationId": "cid-other",
            "senderId": "staff-3",
            "sendType": "user",
            "createTime": "2026-11-17 09:59:00",
            "text": "其他会话",
        },
        {
            "messageId": "recalled",
            "conversationId": "cid-1",
            "senderId": "staff-4",
            "sendType": "user",
            "createTime": "2026-11-17 09:58:00",
            "isRecalled": True,
            "text": "已撤回",
        },
    ]

    async def read(args, **kwargs):
        if args[:2] == ["chat", "+chat-list-all"]:
            return _groups_page([{"openConversationId": "cid-1", "name": "研发群"}])
        if args[:2] == ["chat", "+chat-members-list"]:
            return _bot_payload("cid-1", bot_rows)
        if args[:3] == ["chat", "message", "list"]:
            return _group_rows_payload("cid-1", messages)
        raise AssertionError(args)

    activity = Mock(return_value=1731805200.0)
    with patch.object(
        proactive_dws,
        "check_dws_corp_access",
        new_callable=AsyncMock,
        return_value=_allowed_corp(),
    ), patch.object(proactive_dws, "run_dws_readonly_json", side_effect=read), patch.object(
        proactive_dws, "resolve_card_sender_id", new_callable=AsyncMock, return_value="bot-a"
    ) as resolve, patch.object(
        proactive_dws, "last_human_activity", activity, create=True
    ):
        snapshot = await proactive_dws.fetch_group_snapshot(
            "corp-1", "cid-1", "robot-a", now
        )

    assert snapshot is not None
    assert snapshot.corp_id == "corp-1"
    assert snapshot.conversation_id == "cid-1"
    assert snapshot.name == "研发群"
    assert snapshot.bot_sender_id == "bot-a"
    assert set(snapshot.bot_ids) == {"bot-a", "bot-b"}
    assert snapshot.last_human_at == 1731805200.0
    assert snapshot.latest_message_id == "bot-1"
    assert snapshot.history == []
    activity.assert_called_once()
    resolve.assert_awaited_once_with(corp_id="corp-1", conversation_id="cid-1", robot_code="robot-a")
    activity_args = activity.call_args.args
    assert [row["messageId"] for row in activity_args[0]] == ["human-1"]
    assert activity_args[1:] == ("cid-1", now, ["bot-a", "bot-b"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bot_payload",
    [
        _bot_payload("cid-1", [{"robotCode": "robot-a", "openDingtalkId": "bot-a"}], complete=False),
        _bot_payload(
            "cid-1",
            [
                {"robotCode": "robot-a", "openDingtalkId": "bot-a"},
                {"robotCode": "robot-a", "openDingtalkId": "bot-a-2"},
            ],
        ),
        _bot_payload("cid-other", [{"robotCode": "robot-a", "openDingtalkId": "bot-a"}]),
    ],
)
async def test_snapshot_skips_incomplete_duplicate_or_wrong_scope_bots(bot_payload):
    from app import proactive_dws

    async def read(args, **kwargs):
        if args[:2] == ["chat", "+chat-list-all"]:
            return _groups_page([{"openConversationId": "cid-1", "name": "研发群"}])
        if args[:2] == ["chat", "+chat-members-list"]:
            return bot_payload
        raise AssertionError(args)

    with patch.object(
        proactive_dws,
        "check_dws_corp_access",
        new_callable=AsyncMock,
        return_value=_allowed_corp(),
    ), patch.object(proactive_dws, "run_dws_readonly_json", side_effect=read) as run, patch.object(
        proactive_dws, "resolve_card_sender_id", new_callable=AsyncMock, return_value="bot-a"
    ):
        snapshot = await proactive_dws.fetch_group_snapshot(
            "corp-1", "cid-1", "robot-a", datetime.now(BEIJING)
        )

    assert snapshot is None
    assert not any(call.args[0][:3] == ["chat", "message", "list"] for call in run.await_args_list)


@pytest.mark.asyncio
async def test_snapshot_allows_complete_other_bot_without_robot_code():
    from app import proactive_dws

    async def read(args, **kwargs):
        if args[:2] == ["chat", "+chat-list-all"]:
            return _groups_page([{"openConversationId": "cid-1", "name": "研发群"}])
        if args[:2] == ["chat", "+chat-members-list"]:
            return _bot_payload(
                "cid-1",
                [
                    {"robotCode": "robot-a", "openDingtalkId": "bot-a"},
                    {"openDingtalkId": "bot-b"},
                ],
            )
        if args[:3] == ["chat", "message", "list"]:
            return _group_rows_payload("cid-1", [], has_more=False)
        raise AssertionError(args)

    with patch.object(
        proactive_dws,
        "check_dws_corp_access",
        new_callable=AsyncMock,
        return_value=_allowed_corp(),
    ), patch.object(proactive_dws, "run_dws_readonly_json", side_effect=read), patch.object(
        proactive_dws, "resolve_card_sender_id", new_callable=AsyncMock, return_value="bot-a"
    ):
        snapshot = await proactive_dws.fetch_group_snapshot(
            "corp-1", "cid-1", "robot-a", datetime.now(BEIJING)
        )

    assert snapshot is not None
    assert set(snapshot.bot_ids) == {"bot-a", "bot-b"}


@pytest.mark.asyncio
async def test_snapshot_context_uses_empty_current_input_and_existing_clear_cutoff():
    from app import proactive_dws

    now = datetime(2026, 11, 17, 10, 0, tzinfo=BEIJING)
    bot_payload = _bot_payload(
        "cid-1",
        [
            {"robotCode": "robot-a", "openDingtalkId": "bot-a"},
            {"robotCode": "robot-b", "openDingtalkId": "bot-b"},
        ],
    )
    messages = [
        {
            "messageId": "before-clear",
            "conversationId": "cid-1",
            "senderId": "staff-1",
            "sendType": "user",
            "sender": "甲",
            "createTime": "2026-11-16 17:00:00",
            "text": "清空前",
        },
        {
            "messageId": "after-clear",
            "conversationId": "cid-1",
            "senderId": "staff-1",
            "sendType": "user",
            "sender": "甲",
            "createTime": "2026-11-17 09:00:00",
            "text": "近期话题",
        },
        {
            "messageId": "other-bot",
            "conversationId": "cid-1",
            "senderId": "bot-b",
            "sendType": "robot",
            "sender": "其他助手",
            "createTime": "2026-11-17 09:30:00",
            "text": "机器人话题",
        },
    ]

    async def read(args, **kwargs):
        if args[:2] == ["chat", "+chat-list-all"]:
            return _groups_page([{"openConversationId": "cid-1", "name": "研发群"}])
        if args[:2] == ["chat", "+chat-members-list"]:
            return bot_payload
        if args[:3] == ["chat", "message", "list"]:
            return _group_rows_payload("cid-1", messages)
        raise AssertionError(args)

    with patch.object(
        proactive_dws,
        "check_dws_corp_access",
        new_callable=AsyncMock,
        return_value=_allowed_corp(),
    ), patch.object(proactive_dws, "run_dws_readonly_json", side_effect=read), patch.object(
        proactive_dws, "resolve_card_sender_id", new_callable=AsyncMock, return_value="bot-a"
    ), patch.object(proactive_dws, "get_cutoff", return_value="2026-11-16 18:00:00", create=True):
        snapshot = await proactive_dws.fetch_group_snapshot(
            "corp-1", "cid-1", "robot-a", now, with_context=True
        )

    assert snapshot is not None
    assert [item["message_id"] for item in snapshot.history] == ["after-clear", "other-bot"]
    assert snapshot.history[0]["role"] == "user"
    assert snapshot.history[0]["sender_nick"] == "甲"
    assert snapshot.history[0]["content"] == "近期话题"
    assert snapshot.history[1]["role"] == "assistant"
    assert snapshot.history[1]["bot_id"] == "robot-b"
    assert snapshot.history[1]["bot_source"] == "robot-b"


@pytest.mark.asyncio
async def test_snapshot_fails_closed_when_message_read_is_unavailable():
    from app import proactive_dws

    async def read(args, **kwargs):
        if args[:2] == ["chat", "+chat-list-all"]:
            return _groups_page([{"openConversationId": "cid-1", "name": "研发群"}])
        if args[:2] == ["chat", "+chat-members-list"]:
            return _bot_payload("cid-1", [{"robotCode": "robot-a", "openDingtalkId": "bot-a"}])
        if args[:3] == ["chat", "message", "list"]:
            return None
        raise AssertionError(args)

    with patch.object(
        proactive_dws,
        "check_dws_corp_access",
        new_callable=AsyncMock,
        return_value=_allowed_corp(),
    ), patch.object(proactive_dws, "run_dws_readonly_json", side_effect=read), patch.object(
        proactive_dws, "resolve_card_sender_id", new_callable=AsyncMock, return_value="bot-a"
    ):
        snapshot = await proactive_dws.fetch_group_snapshot(
            "corp-1", "cid-1", "robot-a", datetime.now(BEIJING)
        )

    assert snapshot is None


@pytest.mark.asyncio
async def test_snapshot_does_not_call_empty_inactive_when_message_page_has_more():
    from app import proactive_dws

    async def read(args, **kwargs):
        if args[:2] == ["chat", "+chat-list-all"]:
            return _groups_page([{"openConversationId": "cid-1", "name": "研发群"}])
        if args[:2] == ["chat", "+chat-members-list"]:
            return _bot_payload("cid-1", [{"robotCode": "robot-a", "openDingtalkId": "bot-a"}])
        if args[:3] == ["chat", "message", "list"]:
            return _group_rows_payload("cid-1", [], has_more=True)
        raise AssertionError(args)

    with patch.object(
        proactive_dws,
        "check_dws_corp_access",
        new_callable=AsyncMock,
        return_value=_allowed_corp(),
    ), patch.object(proactive_dws, "run_dws_readonly_json", side_effect=read), patch.object(
        proactive_dws, "resolve_card_sender_id", new_callable=AsyncMock, return_value="bot-a"
    ):
        snapshot = await proactive_dws.fetch_group_snapshot(
            "corp-1", "cid-1", "robot-a", datetime.now(BEIJING)
        )

    assert snapshot is None


@pytest.mark.asyncio
async def test_known_group_reuses_exact_discovery_metadata_without_group_list_read():
    from app import proactive_dws

    now = datetime(2026, 11, 17, 10, 0, tzinfo=BEIJING)
    bot_payload = _bot_payload("cid-1", [{"robotCode": "robot-a", "openDingtalkId": "bot-a"}])
    messages = [
        {
            "messageId": "human-1",
            "conversationId": "cid-1",
            "senderId": "staff-1",
            "sendType": "user",
            "createTime": "2026-11-17 09:00:00",
            "text": "早上好",
        }
    ]

    async def read(args, **kwargs):
        if args[:2] == ["chat", "+chat-list-all"]:
            raise AssertionError("known_group 不应再次读取群列表")
        if args[:2] == ["chat", "+chat-members-list"]:
            return bot_payload
        if args[:3] == ["chat", "message", "list"]:
            return _group_rows_payload("cid-1", messages)
        raise AssertionError(args)

    with patch.object(
        proactive_dws,
        "check_dws_corp_access",
        new_callable=AsyncMock,
        return_value=_allowed_corp(),
    ) as check, patch.object(proactive_dws, "run_dws_readonly_json", side_effect=read), patch.object(
        proactive_dws, "resolve_card_sender_id", new_callable=AsyncMock, return_value="bot-a"
    ):
        snapshot = await proactive_dws.fetch_group_snapshot(
            "corp-1",
            "cid-1",
            "robot-a",
            now,
            known_group={"cid": "cid-1", "name": "发现时的群名"},
        )

    assert snapshot is not None
    assert snapshot.name == "发现时的群名"
    check.assert_awaited_once_with(expected_corp_ids=["corp-1"])


@pytest.mark.asyncio
async def test_known_group_wrong_cid_fails_closed_before_any_read():
    from app import proactive_dws

    run = AsyncMock()
    with patch.object(
        proactive_dws,
        "check_dws_corp_access",
        new_callable=AsyncMock,
        return_value=_allowed_corp(),
    ), patch.object(proactive_dws, "run_dws_readonly_json", run):
        snapshot = await proactive_dws.fetch_group_snapshot(
            "corp-1",
            "cid-1",
            "robot-a",
            datetime(2026, 11, 17, 10, 0, tzinfo=BEIJING),
            known_group={"cid": "cid-other", "name": "错误群"},
        )

    assert snapshot is None
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_known_group_metadata_does_not_replace_current_bot_membership_gate():
    from app import proactive_dws

    async def read(args, **kwargs):
        if args[:2] == ["chat", "+chat-list-all"]:
            raise AssertionError("known_group 不应再次读取群列表")
        if args[:2] == ["chat", "+chat-members-list"]:
            return _bot_payload("cid-1", [{"robotCode": "robot-b", "openDingtalkId": "bot-b"}])
        if args[:3] == ["chat", "message", "list"]:
            raise AssertionError("bot 身份失败后不应读取消息")
        raise AssertionError(args)

    with patch.object(
        proactive_dws,
        "check_dws_corp_access",
        new_callable=AsyncMock,
        return_value=_allowed_corp(),
    ), patch.object(proactive_dws, "run_dws_readonly_json", side_effect=read), patch.object(
        proactive_dws, "resolve_card_sender_id", new_callable=AsyncMock, return_value=""
    ):
        snapshot = await proactive_dws.fetch_group_snapshot(
            "corp-1",
            "cid-1",
            "robot-a",
            datetime(2026, 11, 17, 10, 0, tzinfo=BEIJING),
            known_group={"cid": "cid-1", "name": "缓存群名"},
        )

    assert snapshot is None


def test_zero_history_budget_does_not_expose_the_entire_history(monkeypatch):
    from app import proactive_dws as dws
    monkeypatch.setattr(dws.cfg, "MAX_HISTORY_LENGTH", 0)
    rows = [{"conversationId":"cid", "messageId":"message", "senderId":"human",
        "sender":"用户", "sendType":"user", "createTime":"2026-11-17 10:00:00", "text":"可见正文"}]
    assert dws._history_from_rows(rows, [], "bot", datetime(2026,11,17,11,tzinfo=BEIJING), None) == []
