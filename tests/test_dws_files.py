# -*- coding: utf-8 -*-
"""文档读取失败不得把临时链接或底层异常交给模型。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio
import os

import pytest

from app import dws_files


@pytest.mark.parametrize("extension", ["pdf", "docx", "xlsx"])
def test_parser_failure_redacts_uri_filename(monkeypatch, extension):
    monkeypatch.setattr(dws_files, f"parse_{extension}_bytes", lambda _: (_ for _ in ()).throw(ValueError("invalid")))
    _, note = dws_files.parse_document_bytes(b"invalid",
        filename="https://files.example/report?signature=secret-sentinel", extension=extension)
    assert "secret-sentinel" not in note and "report" in note


@pytest.mark.asyncio
async def test_cli_cancellation_is_not_converted_to_media_failure():
    quote = SimpleNamespace(conversation_id="review", message_id="file", text="", resources=[
        {"resourceType": "file", "resourceIdType": "fileId", "resourceId": "node"}])
    with pytest.raises(asyncio.CancelledError):
        await dws_files.fetch_quoted_media_and_docs(quotes=[quote], conversation_id="review",
            dws_ready=True, run_dws_fn=AsyncMock(side_effect=asyncio.CancelledError))


@pytest.mark.asyncio
async def test_document_header_redacts_uri_shaped_filename(monkeypatch):
    signed_url = "https://files.example/report.pdf?signature=secret-sentinel"
    runner = AsyncMock(side_effect=[{"name": signed_url, "extension": "pdf"},
        {"downloadUrl": signed_url, "fileName": signed_url}])
    quote = SimpleNamespace(conversation_id="review", message_id="doc", text="", resources=[
        {"resourceType": "file", "resourceIdType": "fileId", "resourceId": "node"}])
    monkeypatch.setattr(dws_files, "fetch_public_bytes", AsyncMock(side_effect=RuntimeError("failed")))
    result = await dws_files.fetch_quoted_media_and_docs(quotes=[quote], conversation_id="review", dws_ready=True, run_dws_fn=runner)
    assert result.documents[0].name == "report.pdf"
    assert "secret-sentinel" not in result.documents[0].note


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_command", ["info", "read", "download", "download-media"])
async def test_cli_exceptions_never_escape_media_boundary(monkeypatch, failed_command):
    async def runner(args):
        if failed_command in args:
            raise RuntimeError("provider-secret https://files.example/file?signature=secret-sentinel")
        return {"name": "report", "extension": "adoc" if failed_command == "read" else "pdf"}
    resource = {"resourceType": "image" if failed_command == "download-media" else "file",
        "resourceIdType": "mediaId" if failed_command == "download-media" else "fileId", "resourceId": "resource"}
    quote = SimpleNamespace(conversation_id="review", message_id="resource", text="", resources=[resource])
    result = await dws_files.fetch_quoted_media_and_docs(quotes=[quote], conversation_id="review", dws_ready=True, run_dws_fn=runner)
    assert all("secret-sentinel" not in doc.note for doc in result.documents)
    assert not result.images
    if failed_command != "download-media":
        assert result.documents and result.documents[0].note


@pytest.mark.asyncio
async def test_failed_image_attempts_are_bounded(monkeypatch):
    monkeypatch.setattr(dws_files.cfg, "DWS_MAX_QUOTE_IMAGES", 4)
    fetch = AsyncMock(return_value=None)
    monkeypatch.setattr(dws_files, "_fetch_single_quoted_image", fetch)
    quote = SimpleNamespace(conversation_id="review", message_id="images", text="", resources=[
        {"resourceType": "image", "resourceIdType": "mediaId", "resourceId": str(i)} for i in range(10)])
    result = await dws_files.fetch_quoted_media_and_docs(quotes=[quote], conversation_id="review",
        dws_ready=True, run_dws_fn=AsyncMock(), max_images=1, max_documents=0)
    assert result.images == [] and fetch.await_count == 1


@pytest.mark.parametrize("extension", ["pdf", "docx", "xlsx"])
def test_parser_failure_note_excludes_raw_exception_text(monkeypatch, extension):
    def fail(_):
        raise ValueError("provider-secret https://files.example/report?signature=secret-sentinel")
    monkeypatch.setattr(dws_files, f"parse_{extension}_bytes", fail)
    text, note = dws_files.parse_document_bytes(b"invalid", filename="report." + extension, extension=extension)
    assert text == "" and "失败" in note
    assert "provider-secret" not in note and "secret-sentinel" not in note


@pytest.mark.asyncio
async def test_document_failure_note_excludes_download_credentials(monkeypatch):
    signed_url = "https://files.example/private.pdf?signature=secret-sentinel"
    runner = AsyncMock(side_effect=[{"name": "report.pdf", "extension": "pdf"},
        {"downloadUrl": signed_url, "fileName": "report.pdf"}])
    quote = SimpleNamespace(conversation_id="review", message_id="doc", text="", resources=[
        {"resourceType": "file", "resourceIdType": "fileId", "resourceId": "node"}])
    monkeypatch.setattr(dws_files, "fetch_public_bytes", AsyncMock(side_effect=RuntimeError("provider-secret " + signed_url)))
    result = await dws_files.fetch_quoted_media_and_docs(quotes=[quote], conversation_id="review",
        dws_ready=True, run_dws_fn=runner)
    note = result.documents[0].note
    assert "report.pdf" in note and "失败" in note
    assert all(value not in note for value in (signed_url, "secret-sentinel", "provider-secret"))


@pytest.mark.asyncio
@pytest.mark.parametrize("images,documents", [(0, 1), (1, 0)])
async def test_media_budgets_disable_only_the_selected_resource_type(monkeypatch, images, documents):
    monkeypatch.setattr(dws_files.cfg, "DWS_QUOTE_FETCH_MEDIA", True)
    monkeypatch.setattr(dws_files.cfg, "DWS_MAX_QUOTE_IMAGES", 4)
    image = AsyncMock(return_value=b"image")
    monkeypatch.setattr(dws_files, "_fetch_single_quoted_image", image)
    runner = AsyncMock(side_effect=[{"name": "report", "extension": "adoc"}, {"markdown": "文档正文"}])
    quote = SimpleNamespace(conversation_id="review", message_id="mixed", text="", resources=[
        {"resourceType": "image", "resourceIdType": "mediaId", "resourceId": "image"},
        {"resourceType": "file", "resourceIdType": "fileId", "resourceId": "node"}])
    result = await dws_files.fetch_quoted_media_and_docs(quotes=[quote], conversation_id="review",
        dws_ready=True, run_dws_fn=runner, max_images=images, max_documents=documents)
    assert len(result.images) == images and len(result.documents) == documents
    assert image.await_count == images and runner.await_count == 2 * documents


@pytest.mark.asyncio
async def test_failed_image_candidates_have_a_bounded_attempt_budget(monkeypatch):
    monkeypatch.setattr(dws_files.cfg, "DWS_QUOTE_FETCH_MEDIA", True)
    monkeypatch.setattr(dws_files.cfg, "DWS_MAX_QUOTE_IMAGES", 1)
    image = AsyncMock(return_value=None)
    monkeypatch.setattr(dws_files, "_fetch_single_quoted_image", image)
    quote = SimpleNamespace(conversation_id="review", message_id="mixed", text="", resources=[
        {"resourceType": "image", "resourceIdType": "mediaId", "resourceId": str(i)} for i in range(100)])
    result = await dws_files.fetch_quoted_media_and_docs(quotes=[quote], conversation_id="review",
        dws_ready=True, run_dws_fn=AsyncMock(), max_images=1, max_documents=0)
    assert not result.images and image.await_count == 1


@pytest.mark.asyncio
async def test_media_deduplicates_same_bytes_from_alias_resource_ids(monkeypatch):
    monkeypatch.setattr(dws_files.cfg, "DWS_QUOTE_FETCH_MEDIA", True)
    monkeypatch.setattr(dws_files.cfg, "DWS_MAX_QUOTE_IMAGES", 4)
    image = AsyncMock(return_value=b"same-image")
    monkeypatch.setattr(dws_files, "_fetch_single_quoted_image", image)
    quote = SimpleNamespace(conversation_id="review", message_id="mixed", text="", resources=[
        {"resourceType": "image", "resourceIdType": "mediaId", "resourceId": "alias-a",
         "forwardedResource": True, "resourceRefs": {"download": {"arguments": {
             "message-id": "child", "open-conversation-id": "source", "resource-id": "alias-a"}}}},
        {"resourceType": "image", "resourceIdType": "mediaId", "resourceId": "alias-b",
         "forwardedResource": True, "resourceRefs": {"download": {"arguments": {
             "message-id": "child", "open-conversation-id": "source", "resource-id": "alias-b"}}}},
    ])

    result = await dws_files.fetch_quoted_media_and_docs(
        quotes=[quote], conversation_id="review", dws_ready=True,
        run_dws_fn=AsyncMock(), max_images=2, max_documents=0,
    )

    assert result.images == [b"same-image"]
    assert image.await_count == 2


@pytest.mark.asyncio
async def test_forwarded_alias_attempts_have_a_hard_bound(monkeypatch):
    monkeypatch.setattr(dws_files.cfg, "DWS_QUOTE_FETCH_MEDIA", True)
    monkeypatch.setattr(dws_files.cfg, "DWS_MAX_QUOTE_IMAGES", 4)
    image = AsyncMock(return_value=b"same-image")
    monkeypatch.setattr(dws_files, "_fetch_single_quoted_image", image)
    resources = [
        {
            "resourceType": "image",
            "resourceIdType": "mediaId",
            "resourceId": f"alias-{index}",
            "forwardedResource": True,
            "resourceRefs": {"download": {"arguments": {
                "message-id": "child",
                "open-conversation-id": "source",
                "resource-id": f"alias-{index}",
            }}},
        }
        for index in range(30)
    ]
    quote = SimpleNamespace(
        conversation_id="parent-conversation",
        message_id="parent-message",
        text="",
        resources=resources,
    )

    result = await dws_files.fetch_quoted_media_and_docs(
        quotes=[quote], conversation_id="parent-conversation", dws_ready=True,
        run_dws_fn=AsyncMock(), max_images=4, max_documents=0,
    )

    assert result.images == [b"same-image"]
    assert image.await_count == 8


@pytest.mark.asyncio
async def test_forwarded_bytes_are_not_deduplicated_across_parent_scopes(monkeypatch):
    monkeypatch.setattr(dws_files.cfg, "DWS_QUOTE_FETCH_MEDIA", True)
    monkeypatch.setattr(dws_files.cfg, "DWS_MAX_QUOTE_IMAGES", 4)
    image = AsyncMock(return_value=b"same-image")
    monkeypatch.setattr(dws_files, "_fetch_single_quoted_image", image)

    def resource(resource_id):
        return {
            "resourceType": "image",
            "resourceIdType": "mediaId",
            "resourceId": resource_id,
            "forwardedResource": True,
            "resourceRefs": {"download": {"arguments": {
                "message-id": "child",
                "open-conversation-id": "source",
                "resource-id": resource_id,
            }}},
        }

    quotes = [
        SimpleNamespace(conversation_id="parent-conversation", message_id=f"parent-{index}", text="", resources=[resource("same-resource")])
        for index in range(2)
    ]
    result = await dws_files.fetch_quoted_media_and_docs(
        quotes=quotes, conversation_id="parent-conversation", dws_ready=True,
        run_dws_fn=AsyncMock(), max_images=4, max_documents=0,
    )

    assert result.images == [b"same-image", b"same-image"]
    assert image.await_count == 2


@pytest.mark.asyncio
async def test_forwarded_same_resource_id_from_different_children_is_not_deduplicated(monkeypatch):
    monkeypatch.setattr(dws_files.cfg, "DWS_QUOTE_FETCH_MEDIA", True)
    monkeypatch.setattr(dws_files.cfg, "DWS_MAX_QUOTE_IMAGES", 4)
    image = AsyncMock(return_value=b"same-image")
    monkeypatch.setattr(dws_files, "_fetch_single_quoted_image", image)

    def resource(child_id):
        return {
            "resourceType": "image",
            "resourceIdType": "mediaId",
            "resourceId": "same-resource",
            "forwardedResource": True,
            "resourceRefs": {"download": {"arguments": {
                "message-id": child_id,
                "open-conversation-id": "source-group",
                "resource-id": "same-resource",
            }}},
        }

    quote = SimpleNamespace(
        conversation_id="parent-conversation",
        message_id="parent-message",
        text="",
        resources=[resource("child-a"), resource("child-b")],
    )
    result = await dws_files.fetch_quoted_media_and_docs(
        quotes=[quote], conversation_id="parent-conversation", dws_ready=True,
        run_dws_fn=AsyncMock(), max_images=4, max_documents=0,
    )

    assert result.images == [b"same-image", b"same-image"]
    assert image.await_count == 2


@pytest.mark.asyncio
async def test_forwarded_image_without_identity_never_falls_back_to_outer_message(monkeypatch):
    runner = AsyncMock()
    result = await dws_files._fetch_single_quoted_image(
        {"resourceType": "image", "resourceIdType": "mediaId", "resourceId": "picture", "forwardedResource": True},
        quote_conv_id="outer-group", quote_msg_id="outer-message", dws_ready=True, run_dws_fn=runner)
    assert result is None
    runner.assert_not_awaited()


@pytest.mark.asyncio
async def test_forwarded_image_download_uses_open_conversation_id(monkeypatch):
    png_bytes = b"\x89PNG\r\n\x1a\nchild-image"
    calls = []
    fetch = AsyncMock()
    monkeypatch.setattr(dws_files, "fetch_public_bytes", fetch)

    async def runner(args):
        calls.append(list(args))
        assert "--open-conversation-id" in args
        assert "--conversation-id" not in args
        assert args[args.index("--open-conversation-id") + 1] == "container-conversation"
        assert args[args.index("--message-id") + 1] == "container-message"
        assert args[args.index("--resource-id") + 1] == "child-media"
        output_dir = args[args.index("--output") + 1]
        output_path = os.path.join(output_dir, "child.png")
        with open(output_path, "wb") as handle:
            handle.write(png_bytes)
        return {"path": output_path}

    resource = {
        "resourceType": "image",
        "resourceIdType": "mediaId",
        "resourceId": "child-media",
        "forwardedResource": True,
        "url": "https://files.example/forged-callback-url.png",
        "resourceRefs": {"download": {"arguments": {
            "open-conversation-id": "child-conversation",
            "message-id": "child-message",
            "resource-id": "child-media",
        }}},
    }
    result = await dws_files._fetch_single_quoted_image(
        resource,
        quote_conv_id="container-conversation",
        quote_msg_id="container-message",
        quote_auth_verified=True,
        quote_resource_scope=SimpleNamespace(
            parent_message_id="container-message",
            parent_conversation_id="container-conversation",
            resources=frozenset({(
                "child-message", "child-conversation", "child-media", "mediaId",
            )}),
        ),
        dws_ready=True,
        run_dws_fn=runner,
    )

    assert result == png_bytes
    assert len(calls) == 1
    fetch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resource,quote_auth_verified",
    [
        (
            {
                "resourceType": "image",
                "resourceIdType": "mediaId",
                "resourceId": "child-media",
                "forwardedResource": True,
                "url": "https://files.example/forwarded.png",
                "resourceRefs": {"download": {"arguments": {
                    "open-conversation-id": "child-conversation",
                    "message-id": "child-message",
                    "resource-id": "child-media",
                }}},
            },
            False,
        ),
        (
            {
                "resourceType": "image",
                "resourceIdType": "mediaId",
                "resourceId": "child-media",
                "forwardedResource": True,
                "url": "https://files.example/forwarded.png",
                "resourceRefs": {"download": {"arguments": {
                    "open-conversation-id": "child-conversation",
                    "resource-id": "child-media",
                }}},
            },
            True,
        ),
    ],
)
async def test_forwarded_image_rejects_untrusted_or_incomplete_provenance_before_url(
    monkeypatch, resource, quote_auth_verified
):
    fetch = AsyncMock()
    runner = AsyncMock()
    monkeypatch.setattr(dws_files, "fetch_public_bytes", fetch)

    result = await dws_files._fetch_single_quoted_image(
        resource,
        quote_conv_id="container-conversation",
        quote_msg_id="container-message",
        quote_auth_verified=quote_auth_verified,
        dws_ready=True,
        run_dws_fn=runner,
    )

    assert result is None
    fetch.assert_not_awaited()
    runner.assert_not_awaited()


@pytest.mark.asyncio
async def test_forwarded_image_rejects_resource_outside_verified_dws_tree(monkeypatch):
    fetch = AsyncMock()
    runner = AsyncMock()
    resource = {
        "resourceType": "image",
        "resourceIdType": "mediaId",
        "resourceId": "callback-only-media",
        "forwardedResource": True,
        "url": "https://files.example/callback-only.png",
        "resourceRefs": {"download": {"arguments": {
            "open-conversation-id": "child-conversation",
            "message-id": "child-message",
            "resource-id": "callback-only-media",
        }}},
    }

    monkeypatch.setattr(dws_files, "fetch_public_bytes", fetch)
    result = await dws_files._fetch_single_quoted_image(
        resource,
        quote_conv_id="container-conversation",
        quote_msg_id="container-message",
        quote_auth_verified=True,
        quote_resource_scope=SimpleNamespace(
            parent_message_id="container-message",
            parent_conversation_id="container-conversation",
            resources=frozenset({(
                "other-child-message", "child-conversation", "other-media", "mediaId",
            )}),
        ),
        dws_ready=True,
        run_dws_fn=runner,
    )

    assert result is None
    fetch.assert_not_awaited()
    runner.assert_not_awaited()


@pytest.mark.asyncio
async def test_forwarded_scope_cannot_be_reused_for_another_parent(monkeypatch):
    fetch = AsyncMock()
    runner = AsyncMock()
    monkeypatch.setattr(dws_files, "fetch_public_bytes", fetch)
    resource = {
        "resourceType": "image",
        "resourceIdType": "mediaId",
        "resourceId": "child-media",
        "forwardedResource": True,
        "url": "https://files.example/child.png",
        "resourceRefs": {"download": {"arguments": {
            "open-conversation-id": "child-conversation",
            "message-id": "child-message",
            "resource-id": "child-media",
        }}},
    }

    result = await dws_files._fetch_single_quoted_image(
        resource,
        quote_conv_id="container-b-conversation",
        quote_msg_id="container-b-message",
        quote_auth_verified=True,
        quote_resource_scope=SimpleNamespace(
            parent_message_id="container-a-message",
            parent_conversation_id="container-a-conversation",
            resources=frozenset({(
                "child-message", "child-conversation", "child-media", "mediaId",
            )}),
        ),
        dws_ready=True,
        run_dws_fn=runner,
    )

    assert result is None
    fetch.assert_not_awaited()
    runner.assert_not_awaited()
