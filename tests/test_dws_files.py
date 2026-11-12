# -*- coding: utf-8 -*-
"""文档读取失败不得把临时链接或底层异常交给模型。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio

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
async def test_forwarded_image_without_identity_never_falls_back_to_outer_message(monkeypatch):
    runner = AsyncMock()
    result = await dws_files._fetch_single_quoted_image(
        {"resourceType": "image", "resourceIdType": "mediaId", "resourceId": "picture", "forwardedResource": True},
        quote_conv_id="outer-group", quote_msg_id="outer-message", dws_ready=True, run_dws_fn=runner)
    assert result is None
    runner.assert_not_awaited()
