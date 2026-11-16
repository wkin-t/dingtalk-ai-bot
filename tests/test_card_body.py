"""卡片正文回读补全的单元测试。"""

import asyncio
import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _item(text, *, message_id="msg-1", sender_id="bot-1", conversation_id="cid-1"):
    return {
        "message_id": message_id,
        "sender_id": sender_id,
        "conversation_id": conversation_id,
        "display_text": text,
        "raw_text": text,
        "resources": [{"resourceId": "keep-me"}],
    }


def test_prepare_card_body_has_immutable_reference_footer_and_no_storage_side_effect():
    from app.card_body import prepare_card_body

    with patch("app.card_body._get_redis_client") as get_client:
        draft = prepare_card_body(
            corp_id="corp-1",
            conversation_id="cid-1",
            sender_id="bot-1",
            out_track_id="track-1",
            body="> quote\n\n| A | B |",
        )

    assert draft is not None
    assert len(draft.reference) == 22
    assert draft.footer.startswith("ref:cb1-")
    assert draft.footer.count(".") == 1
    assert len(draft.footer.rsplit(".", 1)[1]) == 12
    assert draft.footer == draft.footer
    assert draft.body == "> quote\n\n| A | B |"
    get_client.assert_not_called()


def test_prepare_rejects_missing_scope_and_oversized_body():
    from app.card_body import MAX_BODY_BYTES, prepare_card_body

    fields = {
        "corp_id": "corp-1",
        "conversation_id": "cid-1",
        "sender_id": "bot-1",
        "out_track_id": "track-1",
        "body": "正文",
    }
    for name in ("corp_id", "conversation_id", "sender_id", "out_track_id"):
        candidate = dict(fields)
        candidate[name] = ""
        assert prepare_card_body(**candidate) is None

    candidate = dict(fields)
    candidate["body"] = "x" * (MAX_BODY_BYTES + 1)
    assert prepare_card_body(**candidate) is None


@pytest.mark.asyncio
async def test_lookup_requires_one_exact_dws_message_and_rechecks_corp():
    from app.card_body import _lookup_delivered_message_id, prepare_card_body

    draft = prepare_card_body(
        corp_id="corp-1",
        conversation_id="cid-1",
        sender_id="bot-1",
        out_track_id="track-1",
        body="正文",
    )
    row = {
        "messageId": "msg-1",
        "conversationId": "cid-1",
        "senderId": "bot-1",
        "sender": "GPT小助手",
        "text": f"正文\n\n{draft.footer}",
    }
    corp = AsyncMock(return_value=SimpleNamespace(allowed=True))
    query = AsyncMock(return_value={"messages": [row]})
    with patch("app.dws_context.check_dws_corp_access", corp), patch(
        "app.dws_context.run_dws_readonly_json", query
    ):
        assert await _lookup_delivered_message_id(draft) == "msg-1"
    corp.assert_awaited_once_with(expected_corp_ids=["corp-1"])
    assert query.await_args.kwargs["timeout_seconds"] == 3.0
    assert "cid-1" in query.await_args.args[0]

    duplicate = AsyncMock(return_value={"messages": [row, {**row, "messageId": "msg-2"}]})
    with patch("app.dws_context.check_dws_corp_access", AsyncMock(return_value=SimpleNamespace(allowed=True))), patch(
        "app.dws_context.run_dws_readonly_json", duplicate
    ):
        assert await _lookup_delivered_message_id(draft) is None


@pytest.mark.asyncio
async def test_resolve_card_sender_id_uses_verified_complete_bot_bucket():
    from app.card_body import resolve_card_sender_id

    payload = {
        "ok": True,
        "data": {
            "conversationId": "cid-1",
            "complete": True,
            "partial": False,
            "bots": [
                {"robotCode": "other-robot", "openDingtalkId": "other-id", "name": "同名机器人"},
                {"robotCode": "trusted-robot", "openDingtalkId": "dws-bot-id"},
            ],
            "buckets": {
                "bots": {"complete": True, "count": 2},
            },
        },
    }
    corp = AsyncMock(return_value=SimpleNamespace(allowed=True))
    query = AsyncMock(return_value=payload)
    with patch("app.dws_context.check_dws_corp_access", corp), patch(
        "app.dws_context.run_dws_readonly_json", query
    ):
        sender_id = await resolve_card_sender_id(
            corp_id="corp-1", conversation_id="cid-1", robot_code="trusted-robot"
        )

    assert sender_id == "dws-bot-id"
    corp.assert_awaited_once_with(expected_corp_ids=["corp-1"])
    query.assert_awaited_once()
    assert query.await_args.args[0] == [
        "chat",
        "+chat-members-list",
        "--conversation-id",
        "cid-1",
        "--member-types",
        "bot",
        "--page-limit",
        "5",
        "-f",
        "json",
    ]
    assert query.await_args.kwargs["timeout_seconds"] == 3.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload_change",
    [
        {"conversationId": "other-cid"},
        {"complete": False},
        {"partial": True},
        {"bots_complete": False},
    ],
)
async def test_resolve_card_sender_id_rejects_incomplete_or_wrong_scope(payload_change):
    from app.card_body import resolve_card_sender_id

    bots = [{"robotCode": "trusted-robot", "openDingtalkId": "dws-id"}]
    payload = {
        "ok": True,
        "data": {
            "conversationId": "cid-1",
            "complete": True,
            "partial": False,
            "bots": bots,
            "buckets": {"bots": {"complete": True, "count": len(bots)}},
        },
    }
    if payload_change.get("bots_complete") is False:
        payload["data"]["buckets"]["bots"]["complete"] = False
    else:
        payload["data"].update(
            {key: value for key, value in payload_change.items() if key != "bots_complete"}
        )
    with patch(
        "app.dws_context.check_dws_corp_access",
        AsyncMock(return_value=SimpleNamespace(allowed=True)),
    ), patch("app.dws_context.run_dws_readonly_json", AsyncMock(return_value=payload)):
        assert (
            await resolve_card_sender_id(
                corp_id="corp-1", conversation_id="cid-1", robot_code="trusted-robot"
            )
            == ""
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "items",
    [
        [
            {"robotCode": "trusted-robot", "openDingtalkId": "first"},
            {"robotCode": "trusted-robot", "openDingtalkId": "second"},
        ],
        [{"robotCode": "trusted-robot"}],
        [{"name": "GPT小助手", "openDingtalkId": "nickname-only"}],
    ],
)
async def test_resolve_card_sender_id_rejects_duplicate_missing_or_nickname_match(items):
    from app.card_body import resolve_card_sender_id

    payload = {
        "conversationId": "cid-1",
        "complete": True,
        "partial": False,
        "bots": items,
        "buckets": {"bots": {"complete": True, "count": len(items)}},
    }
    with patch(
        "app.dws_context.check_dws_corp_access",
        AsyncMock(return_value=SimpleNamespace(allowed=True)),
    ), patch("app.dws_context.run_dws_readonly_json", AsyncMock(return_value=payload)):
        assert (
            await resolve_card_sender_id(
                corp_id="corp-1", conversation_id="cid-1", robot_code="trusted-robot"
            )
            == ""
        )


@pytest.mark.asyncio
async def test_resolve_card_sender_id_fails_closed_on_corp_denial_and_cancellation():
    from app.card_body import resolve_card_sender_id

    with patch(
        "app.dws_context.check_dws_corp_access",
        AsyncMock(return_value=SimpleNamespace(allowed=False)),
    ) as corp, patch("app.dws_context.run_dws_readonly_json", AsyncMock()) as query:
        assert (
            await resolve_card_sender_id(
                corp_id="corp-1", conversation_id="cid-1", robot_code="trusted-robot"
            )
            == ""
        )
    corp.assert_awaited_once()
    query.assert_not_awaited()

    with patch(
        "app.dws_context.check_dws_corp_access",
        AsyncMock(return_value=SimpleNamespace(allowed=True)),
    ), patch(
        "app.dws_context.run_dws_readonly_json",
        AsyncMock(side_effect=asyncio.CancelledError),
    ):
        with pytest.raises(asyncio.CancelledError):
            await resolve_card_sender_id(
                corp_id="corp-1", conversation_id="cid-1", robot_code="trusted-robot"
            )


def test_footer_accepts_only_the_exact_dingtalk_status_font_wrapper():
    from app.card_body import _extract_footer, prepare_card_body

    draft = prepare_card_body(
        corp_id="corp-1", conversation_id="cid-1", sender_id="bot-1", out_track_id="track-1", body="正文"
    )
    wrapped = f"正文\n<font color='#aaaaaa' size='2'>{draft.footer}</font>"
    assert _extract_footer(wrapped) == (draft.reference, draft.footer.rsplit(".", 1)[1])
    assert _extract_footer(f"正文\n{draft.footer}") == (
        draft.reference,
        draft.footer.rsplit(".", 1)[1],
    )
    assert _extract_footer(f"正文\n<font color='#aaaaaa' size='2'>{draft.footer}</font>\n后文") is None
    assert _extract_footer(f"正文\n{draft.footer}\n{draft.footer}") is None
    assert _extract_footer(f"正文\n<font color='#808080' size='2'>{draft.footer}</font>") is None
    assert _extract_footer(f"正文\n<font color='#aaaaaa' size='2'><b>{draft.footer}</b></font>") is None


@pytest.mark.asyncio
async def test_exact_status_font_wrapper_round_trips_through_clean_dws_item():
    from app.card_body import confirm_card_body, prepare_card_body, restore_card_bodies
    from app.dws_context import extract_clean_dws_item

    body = "> 引用 Markdown\n\n**粗体**"
    draft = prepare_card_body(
        corp_id="corp-1", conversation_id="cid-1", sender_id="bot-1", out_track_id="track-font", body=body
    )
    raw = {
        "messageId": "msg-font",
        "conversationId": "cid-1",
        "senderId": "bot-1",
        "sender": "GPT小助手",
        "messageType": "interactiveCard",
        "text": f"卡片摘要\n<font color='#aaaaaa' size='2'>{draft.footer}</font>",
    }
    cleaned = extract_clean_dws_item(raw)
    assert cleaned is not None
    fake = MagicMock()
    fake.eval.return_value = 1
    with patch("app.card_body._lookup_delivered_message_id", return_value="msg-font"), patch(
        "app.card_body._redis_client", fake
    ):
        assert await confirm_card_body(draft) is True
    stored = json.loads(fake.eval.call_args.args[4])
    assert stored["message_id"] == "msg-font"
    fake.mget.return_value = [json.dumps(stored)]
    with patch("app.card_body._redis_client", fake):
        restored = await restore_card_bodies([cleaned], corp_id="corp-1", conversation_id="cid-1")
    assert restored[0]["display_text"] == body


@pytest.mark.asyncio
async def test_confirm_writes_nx_payload_and_bounded_index():
    from app.card_body import (
        INDEX_KEY,
        KEY_PREFIX,
        MAX_INDEX_ENTRIES,
        STORAGE_TTL_SECONDS,
        confirm_card_body,
        prepare_card_body,
    )

    fake = MagicMock()
    fake.eval.return_value = 1
    draft = prepare_card_body(
        corp_id="corp-1",
        conversation_id="cid-1",
        sender_id="bot-1",
        out_track_id="track-1",
        body="Markdown **正文**",
    )

    with patch("app.card_body._lookup_delivered_message_id", return_value="msg-1"), patch("app.card_body._redis_client", fake):
        assert await confirm_card_body(draft) is True

    key = f"dingtalk_card_body:v1:{draft.reference}"
    fake.eval.assert_called_once()
    args = fake.eval.call_args.args
    assert args[1] == 2
    assert args[2:4] == (key, INDEX_KEY)
    payload = json.loads(args[4])
    assert payload["version"] == "cb1"
    assert payload["corp_id"] == "corp-1"
    assert payload["conversation_id"] == "cid-1"
    assert payload["sender_id"] == "bot-1"
    assert payload["out_track_id"] == "track-1"
    assert payload["message_id"] == "msg-1"
    assert payload["reference"] == draft.reference
    assert payload["body"] == "Markdown **正文**"
    assert len(payload["body_sha256"]) == 64
    assert args[5] == STORAGE_TTL_SECONDS
    assert args[6] > 0
    assert args[7] == draft.reference
    assert args[8] == MAX_INDEX_ENTRIES
    assert args[9] == KEY_PREFIX
    script = args[0]
    assert "ZCARD" in script
    assert "ZREMRANGEBYRANK" in script
    assert "redis.pcall('ZADD'" in script


@pytest.mark.asyncio
async def test_confirm_nx_retry_does_not_overwrite_existing_body():
    from app.card_body import confirm_card_body, prepare_card_body

    fake = MagicMock()
    fake.eval.side_effect = [1, 0]
    draft = prepare_card_body(
        corp_id="corp-1",
        conversation_id="cid-1",
        sender_id="bot-1",
        out_track_id="track-1",
        body="immutable",
    )
    with patch("app.card_body._lookup_delivered_message_id", return_value="msg-1"), patch("app.card_body._redis_client", fake):
        assert await confirm_card_body(draft) is True
        assert await confirm_card_body(draft) is False
    assert fake.eval.call_count == 2


@pytest.mark.asyncio
async def test_confirm_retries_dws_visibility_and_succeeds_on_third_lookup():
    from app.card_body import confirm_card_body, prepare_card_body

    draft = prepare_card_body(
        corp_id="corp-1",
        conversation_id="cid-1",
        sender_id="bot-1",
        out_track_id="track-delay",
        body="延迟可见正文",
    )
    fake = MagicMock()
    fake.eval.return_value = 1
    lookup = AsyncMock(side_effect=[None, None, "msg-after-delay"])
    sleep = AsyncMock()
    with patch("app.card_body._lookup_delivered_message_id", lookup), patch(
        "app.card_body.asyncio.sleep", sleep
    ), patch("app.card_body._redis_client", fake):
        assert await confirm_card_body(draft) is True

    assert lookup.await_count == 3
    assert [call.args[0] for call in sleep.await_args_list] == [0.4, 0.8]
    payload = json.loads(fake.eval.call_args.args[4])
    assert payload["message_id"] == "msg-after-delay"


@pytest.mark.asyncio
async def test_confirm_stops_after_three_unmatched_dws_lookups_without_redis_write():
    from app.card_body import confirm_card_body, prepare_card_body

    draft = prepare_card_body(
        corp_id="corp-1",
        conversation_id="cid-1",
        sender_id="bot-1",
        out_track_id="track-no-match",
        body="始终未匹配",
    )
    lookup = AsyncMock(return_value=None)
    sleep = AsyncMock()
    fake = MagicMock()
    with patch("app.card_body._lookup_delivered_message_id", lookup), patch(
        "app.card_body.asyncio.sleep", sleep
    ), patch("app.card_body._redis_client", fake):
        assert await confirm_card_body(draft) is False

    assert lookup.await_count == 3
    assert [call.args[0] for call in sleep.await_args_list] == [0.4, 0.8]
    fake.eval.assert_not_called()


@pytest.mark.asyncio
async def test_confirm_total_timeout_returns_false_without_redis_write():
    from app.card_body import confirm_card_body, prepare_card_body

    draft = prepare_card_body(
        corp_id="corp-1",
        conversation_id="cid-1",
        sender_id="bot-1",
        out_track_id="track-timeout",
        body="总预算超时",
    )

    async def slow_lookup(_draft):
        await asyncio.sleep(0.05)
        return None

    fake = MagicMock()
    with patch("app.card_body._CONFIRM_TOTAL_TIMEOUT_SECONDS", 0.01), patch(
        "app.card_body._lookup_delivered_message_id", side_effect=slow_lookup
    ), patch("app.card_body._redis_client", fake):
        assert await confirm_card_body(draft) is False

    fake.eval.assert_not_called()


@pytest.mark.asyncio
async def test_confirm_retry_cancellation_propagates_without_redis_write():
    from app.card_body import confirm_card_body, prepare_card_body

    draft = prepare_card_body(
        corp_id="corp-1",
        conversation_id="cid-1",
        sender_id="bot-1",
        out_track_id="track-cancel",
        body="取消传播",
    )
    fake = MagicMock()
    with patch(
        "app.card_body._lookup_delivered_message_id", side_effect=asyncio.CancelledError
    ), patch("app.card_body._redis_client", fake):
        with pytest.raises(asyncio.CancelledError):
            await confirm_card_body(draft)

    fake.eval.assert_not_called()


@pytest.mark.asyncio
async def test_restore_requires_exact_scope_sender_message_and_footer_then_replaces_only_display_text():
    from app.card_body import confirm_card_body, prepare_card_body, restore_card_bodies

    body = "> 保留完整 Markdown 引用\n\n| A | B |\n|---|---|\n| 1 | 2 |"
    draft = prepare_card_body(
        corp_id="corp-1",
        conversation_id="cid-1",
        sender_id="bot-1",
        out_track_id="track-1",
        body=body,
    )
    fake = MagicMock()
    fake.eval.return_value = 1
    with patch("app.card_body._lookup_delivered_message_id", return_value="msg-1"), patch("app.card_body._redis_client", fake):
        assert await confirm_card_body(draft) is True

    fake.mget.side_effect = lambda keys: [fake._stored.get(key) for key in keys]
    payload = {
        "version": "cb1",
        "corp_id": draft.corp_id,
        "conversation_id": draft.conversation_id,
        "sender_id": draft.sender_id,
        "out_track_id": draft.out_track_id,
        "message_id": "msg-1",
        "reference": draft.reference,
        "body": draft.body,
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
    }
    fake._stored = {f"dingtalk_card_body:v1:{draft.reference}": json.dumps(payload)}
    original = _item(f"标题\n\n{draft.footer}")
    snapshot = deepcopy(original)
    with patch("app.card_body._redis_client", fake):
        restored = await restore_card_bodies([original], corp_id="corp-1", conversation_id="cid-1")

    assert restored[0]["display_text"] == body
    assert restored[0]["raw_text"] == snapshot["raw_text"]
    assert restored[0]["resources"] == snapshot["resources"]
    assert original == snapshot


@pytest.mark.asyncio
async def test_restore_does_not_guess_on_bad_marker_or_scope_or_missing_message_id():
    from app.card_body import prepare_card_body, restore_card_bodies

    draft = prepare_card_body(
        corp_id="corp-1",
        conversation_id="cid-1",
        sender_id="bot-1",
        out_track_id="track-1",
        body="完整正文",
    )
    fake = MagicMock()
    fake.mget.return_value = [None]
    bad_items = [
        _item(f"正文中间 ref:cb1-{draft.reference}.{draft.footer.rsplit('.', 1)[1]} 还有后文"),
        _item(f"正文\n\n{draft.footer}", sender_id="other-bot"),
        _item(f"正文\n\n{draft.footer}", conversation_id="other-cid"),
        _item(f"正文\n\n{draft.footer}", message_id=""),
    ]
    with patch("app.card_body._redis_client", fake):
        restored = await restore_card_bodies(bad_items, corp_id="corp-1", conversation_id="cid-1")
    assert restored == bad_items


@pytest.mark.asyncio
async def test_restore_rejects_wrong_scope_corrupt_hash_and_oversized_records():
    from app.card_body import MAX_BODY_BYTES, prepare_card_body, restore_card_bodies

    body = "完整 Markdown 正文"
    records_by_reference = {}
    items = []
    for index, (field, value) in enumerate(
        (("corp_id", "wrong-corp"), ("conversation_id", "wrong-cid"), ("sender_id", "wrong-sender"))
    ):
        draft = prepare_card_body(
            corp_id="corp-1",
            conversation_id="cid-1",
            sender_id="bot-1",
            out_track_id=f"track-{index}",
            body=body,
        )
        good = {
            "version": "cb1",
            "corp_id": "corp-1",
            "conversation_id": "cid-1",
            "sender_id": "bot-1",
            "out_track_id": f"track-{index}",
            "message_id": "msg-1",
            "reference": draft.reference,
            "body": body,
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        }
        record = dict(good)
        record[field] = value
        records_by_reference[draft.reference] = record
        items.append(_item(f"标题\n\n{draft.footer}"))

    hash_draft = prepare_card_body(
        corp_id="corp-1", conversation_id="cid-1", sender_id="bot-1", out_track_id="track-hash", body=body
    )
    wrong_hash = {
        "version": "cb1",
        "corp_id": "corp-1",
        "conversation_id": "cid-1",
        "sender_id": "bot-1",
        "out_track_id": "track-hash",
        "message_id": "msg-1",
        "reference": hash_draft.reference,
        "body": body,
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
    }
    wrong_hash["body_sha256"] = "0" * 64
    records_by_reference[hash_draft.reference] = wrong_hash
    items.append(_item(f"标题\n\n{hash_draft.footer}"))

    corrupt_draft = prepare_card_body(
        corp_id="corp-1", conversation_id="cid-1", sender_id="bot-1", out_track_id="track-corrupt", body=body
    )
    records_by_reference[corrupt_draft.reference] = {"version": "cb1", "body": "{"}
    items.append(_item(f"标题\n\n{corrupt_draft.footer}"))

    oversized_draft = prepare_card_body(
        corp_id="corp-1", conversation_id="cid-1", sender_id="bot-1", out_track_id="track-large", body=body
    )
    oversized = {
        "version": "cb1",
        "corp_id": "corp-1",
        "conversation_id": "cid-1",
        "sender_id": "bot-1",
        "out_track_id": "track-large",
        "message_id": "msg-1",
        "reference": oversized_draft.reference,
        "body": "x" * (MAX_BODY_BYTES + 1),
    }
    oversized["body_sha256"] = hashlib.sha256(oversized["body"].encode()).hexdigest()
    records_by_reference[oversized_draft.reference] = oversized
    oversized_digest = oversized["body_sha256"][:12]
    items.append(_item(f"标题\n\nref:cb1-{oversized_draft.reference}.{oversized_digest}"))

    fake = MagicMock()
    fake.mget.side_effect = lambda keys: [
        json.dumps(records_by_reference.get(key.rsplit(":", 1)[-1]))
        if key.rsplit(":", 1)[-1] in records_by_reference
        else None
        for key in keys
    ]
    with patch("app.card_body._redis_client", fake):
        restored = await restore_card_bodies(items, corp_id="corp-1", conversation_id="cid-1")
    assert all("完整 Markdown 正文" not in row["display_text"] for row in restored)


@pytest.mark.asyncio
async def test_reference_requires_the_exact_delivered_message_id():
    from app.card_body import prepare_card_body, restore_card_bodies

    body = "完整正文"
    draft = prepare_card_body(
        corp_id="corp-1", conversation_id="cid-1", sender_id="bot-1", out_track_id="track-1", body=body
    )
    record = {
        "version": "cb1",
        "corp_id": "corp-1",
        "conversation_id": "cid-1",
        "sender_id": "bot-1",
        "out_track_id": "track-1",
        "reference": draft.reference,
        "body": body,
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
    }
    record["message_id"] = "msg-first"
    fake = MagicMock()
    fake.mget.return_value = [json.dumps(record)]
    items = [
        _item(f"卡片\n\n{draft.footer}", message_id="msg-first"),
        _item(f"卡片\n\n{draft.footer}", message_id="msg-second"),
    ]
    with patch("app.card_body._redis_client", fake):
        restored = await restore_card_bodies(items, corp_id="corp-1", conversation_id="cid-1")
    assert restored[0]["display_text"] == body
    assert restored[1]["display_text"] == items[1]["display_text"]
    fake.mget.assert_called_once()


@pytest.mark.asyncio
async def test_restore_rejects_multiple_footer_markers_even_when_last_is_valid():
    from app.card_body import prepare_card_body, restore_card_bodies

    first = prepare_card_body(
        corp_id="corp-1", conversation_id="cid-1", sender_id="bot-1", out_track_id="track-1", body="一"
    )
    second = prepare_card_body(
        corp_id="corp-1", conversation_id="cid-1", sender_id="bot-1", out_track_id="track-2", body="二"
    )
    fake = MagicMock()
    with patch("app.card_body._redis_client", fake):
        restored = await restore_card_bodies(
            [_item(f"正文\n\n{first.footer}\n\n{second.footer}")],
            corp_id="corp-1",
            conversation_id="cid-1",
        )
    assert restored[0]["display_text"].count("ref:cb1-") == 2
    fake.mget.assert_not_called()


@pytest.mark.asyncio
async def test_redis_failure_is_fail_open_and_cancelled_error_propagates():
    from app.card_body import confirm_card_body, prepare_card_body, restore_card_bodies

    draft = prepare_card_body(
        corp_id="corp-1",
        conversation_id="cid-1",
        sender_id="bot-1",
        out_track_id="track-1",
        body="正文",
    )
    broken = MagicMock()
    broken.eval.side_effect = OSError("redis unavailable")
    broken.mget.side_effect = OSError("redis unavailable")
    with patch("app.card_body._lookup_delivered_message_id", return_value="msg-1"), patch("app.card_body._redis_client", broken):
        assert await confirm_card_body(draft) is False
        item = _item(f"正文\n\n{draft.footer}")
        assert await restore_card_bodies([item], corp_id="corp-1", conversation_id="cid-1") == [item]

    with patch("app.card_body._lookup_delivered_message_id", side_effect=asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await confirm_card_body(draft)
    with patch("app.card_body._lookup_delivered_message_id", return_value="msg-1"), patch(
        "app.card_body._confirm_sync", side_effect=asyncio.CancelledError
    ):
        with pytest.raises(asyncio.CancelledError):
            await confirm_card_body(draft)
    with patch("app.card_body._restore_sync", side_effect=asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await restore_card_bodies([], corp_id="corp-1", conversation_id="cid-1")


@pytest.mark.asyncio
async def test_feature_flag_disables_prepare_confirm_and_restore(monkeypatch):
    from app.card_body import confirm_card_body, prepare_card_body, restore_card_bodies

    monkeypatch.setenv("ENABLE_CARD_BODY_RESTORE", "false")
    assert prepare_card_body(
        corp_id="corp-1",
        conversation_id="cid-1",
        sender_id="bot-1",
        out_track_id="track-1",
        body="正文",
    ) is None
    assert await confirm_card_body(None) is False
    item = _item("原始")
    assert await restore_card_bodies([item], corp_id="corp-1", conversation_id="cid-1") == [item]
