# -*- coding: utf-8 -*-
"""合并记录遵循 dws chat 实际 forwardMessages/quotedMessage 结构。"""
from unittest.mock import AsyncMock, patch

import pytest

from app.dws_context import DwsCorpCheckResult, _parse_quote_dict, extract_clean_dws_item, fetch_dws_context_bundle
from app.dws_files import QuotedMediaAndDocsResult


def child(index, **extra):
    return {"openMessageId": f"child-{index}", "openConversationId": "source-group",
        "sender": f"成员{index}", "senderOpenDingTalkId": f"user-{index}",
        "createTime": "2026-11-12 10:00:00", "content": f"子消息{index}", **extra}


def record(**extra):
    return {"messageId": "merged", "conversationId": "current-group", "sender": "转发人",
        "createTime": "2026-11-12 10:01:00", "text": "聊天记录", **extra}


@pytest.mark.parametrize("key", ["forwardMessages", "forwarded", "forward_messages"])
def test_forwarded_history_keeps_full_text_and_more_than_six_children(key):
    children = [child(i, content="长正文" * 100 + f"结尾{i}") for i in range(8)]
    item = extract_clean_dws_item(record(**{key: children}))
    assert "结尾7" in item["display_text"]
    assert "长正文" * 100 in item["display_text"]
    assert "成员7" in item["display_text"] and "2026-11-12 10:00:00" in item["display_text"]


def test_raw_forward_list_wins_over_projected_duplicate():
    item = extract_clean_dws_item(record(forwardMessages=[child(1)], forwarded=[child(1, text="重复摘要")]))
    assert item["display_text"].count("子消息1") == 1
    assert "重复摘要" not in item["display_text"]


def test_empty_raw_list_is_not_replaced_with_projected_messages():
    item = extract_clean_dws_item(record(forwardMessages=[], forwarded=[child(1, text="过期投影")]))
    assert "过期投影" not in item["display_text"]
    assert "不可读取" in item["display_text"]


def test_hydrated_forwarded_quote_replaces_stream_summary():
    from app.dws_context import DwsQuoteInfo, _merge_same_quote_info
    source = _parse_quote_dict({"messageId": "merged", "forwardMessages": [child(1, content="完整正文" * 500)]})
    merged = _merge_same_quote_info(DwsQuoteInfo(message_id="merged", text="聊天记录摘要"), source)
    assert merged.has_forwarded and "完整正文" * 500 in merged.text


def test_picture_placeholder_is_explicitly_unreadable_when_no_resource_id():
    item = extract_clean_dws_item(record(forwardMessages=[child(1, content="[图片]")]))
    assert "原图不可读取" in item["display_text"] and not item["resources"]


def test_forwarded_nested_content_and_explicit_truncation_are_bounded():
    deep = child(4, content="不应展开的第四层")
    for i in (3, 2, 1):
        deep = child(i, forwardMessages=[deep])
    nested = extract_clean_dws_item(record(forwardMessages=[deep]))["display_text"]
    assert "不应展开的第四层" not in nested and "截断" in nested
    large = extract_clean_dws_item(record(forwardMessages=[child(i, content="文" * 3000) for i in range(100)]))["display_text"]
    assert len(large) < 15000 and "截断" in large


def test_quote_parser_keeps_forwarded_text_without_outer_text():
    quote = _parse_quote_dict({"openMessageId": "merged", "forwardMessages": [child(1, content="完整正文" * 500)]})
    assert quote and "完整正文" * 500 in quote.text


def test_forwarded_image_resources_keep_child_download_identity():
    item = extract_clean_dws_item(record(forwardMessages=[child(1,
        content="[图片消息](mediaId=picture)", resources=[{"resourceType": "image", "resourceIdType": "mediaId", "resourceId": "picture"}])]))
    resource = item["resources"][0]
    args = resource["resourceRefs"]["download"]["arguments"]
    assert args["message-id"] == "child-1" and args["open-conversation-id"] == "source-group"
    assert "不可读取" not in item["display_text"]


def test_forwarded_media_description_is_normalized_without_extra_queries():
    item = extract_clean_dws_item(record(forwardMessages=[child(1, content="[图片消息](mediaId=picture)")]))
    assert item["resources"][0]["resourceId"] == "picture"


@pytest.mark.parametrize("body", ["图片 mediaId: @image", "图片 media-id=@image", '{"mediaId":"@image"}', "![图]($iwIMAGE)"])
def test_pinned_cli_media_descriptions_and_structured_content(body):
    item = extract_clean_dws_item(record(forwardMessages=[child(1, content=body)]))
    assert item["resources"]
    assert item["resources"][0]["resourceIdType"] == "mediaId"


def test_projected_resource_refs_preserve_child_identity_and_inherited_conversation():
    ref = {"type": "mediaId", "resourceId": "@image", "download": {"arguments": {
        "message-id": "child-1", "open-conversation-id": "current-group", "resource-id": "@image", "type": "mediaId"}}}
    item = extract_clean_dws_item(record(forwarded=[{"messageId": "child-1", "sender": "甲", "text": "[图片]", "resourceRefs": [ref]}]))
    assert item["resources"][0]["resourceRefs"]["download"]["arguments"]["message-id"] == "child-1"
    assert item["resources"][0]["resourceRefs"]["download"]["arguments"]["open-conversation-id"] == "current-group"


def test_forwarded_node_budget_is_shared_across_siblings():
    item = extract_clean_dws_item(record(forwardMessages=[child(i) for i in range(40)]))
    assert "子消息29" in item["display_text"] and "子消息30" not in item["display_text"]
    assert "截断" in item["display_text"]


@pytest.mark.asyncio
async def test_real_shaped_quote_reaches_prompt_without_800_character_cutoff():
    full = "正文" * 1450 + "实际结尾"
    rows = [record(forwardMessages=[child(1, content=full)]),
        {"messageId": "trigger", "text": "总结引用", "sender": "提问者", "createTime": "2026-11-12 10:02:00",
         "quotedMessage": {"openMessageId": "merged", "content": "聊天记录", "forwardMessages": [child(1, content=full)]}}]
    with patch("app.dws_context.check_dws_corp_access", AsyncMock(return_value=DwsCorpCheckResult(allowed=True))), \
         patch("app.dws_context.run_dws_readonly_json", AsyncMock(return_value={"messages": rows})), \
         patch("app.dws_context.fetch_quoted_media_and_docs", AsyncMock(return_value=QuotedMediaAndDocsResult())):
        bundle = await fetch_dws_context_bundle(conversation_id="current-group", is_group=False,
            incoming_messages=[], current_message_ids=["trigger"], raw_user_content="总结引用", history_messages=[], dws_only=True)
    assert full in bundle.quote_prompt_block
    assert len(bundle.merged_history) == 1


@pytest.mark.asyncio
async def test_clear_filters_whole_forwarded_record_without_expanding_old_history():
    rows = [record(forwardMessages=[child(1)])]
    with patch("app.dws_context.check_dws_corp_access", AsyncMock(return_value=DwsCorpCheckResult(allowed=True))), \
         patch("app.dws_context.run_dws_readonly_json", AsyncMock(return_value={"messages": rows})), \
         patch("app.dws_context.fetch_quoted_media_and_docs", AsyncMock(return_value=QuotedMediaAndDocsResult())) as media:
        bundle = await fetch_dws_context_bundle(conversation_id="current-group", is_group=False, incoming_messages=[],
            current_message_ids=[], raw_user_content="?", history_messages=[], cutoff_at="2026-11-12 10:01:01", dws_only=True)
    assert bundle.merged_history == []
    media.assert_not_awaited()
