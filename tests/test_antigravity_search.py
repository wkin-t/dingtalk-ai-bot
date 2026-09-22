# -*- coding: utf-8 -*-
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.antigravity_search as search


class _Models:
    def __init__(self, stream=None, error=None):
        self.stream = stream
        self.error = error
        self.kwargs = None

    async def generate_content_stream(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return self.stream


class _Client:
    def __init__(self, stream=None, error=None):
        self.models = _Models(stream=stream, error=error)
        self.aio = SimpleNamespace(models=self.models)


def _stream(chunks):
    async def _gen():
        for chunk in chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk
    return _gen()


def _chunk(text="事实", url="https://example.com/news", title="来源"):
    web = SimpleNamespace(uri=url, title=title)
    grounding = SimpleNamespace(grounding_chunks=[SimpleNamespace(web=web)])
    candidate = SimpleNamespace(
        grounding_metadata=grounding,
        content=SimpleNamespace(parts=[]),
    )
    return SimpleNamespace(text=text, candidates=[candidate])


def _configure(monkeypatch):
    monkeypatch.setattr(search, "CLAUDE_SEARCH_BRIDGE_ENABLED", True)
    monkeypatch.setattr(search, "ANTIGRAVITY_GEMINI_API_BASE", "http://sub2api:38090/v1beta")
    monkeypatch.setattr(search, "ANTIGRAVITY_GEMINI_API_KEY", "dedicated-key")
    monkeypatch.setattr(search, "ANTIGRAVITY_GEMINI_SEARCH_MODEL", "gemini-2.5-flash")
    monkeypatch.setattr(search, "ANTIGRAVITY_SEARCH_TIMEOUT_SECONDS", 2)


def test_tool_name_is_neutral_and_payload_has_no_reserved_server_search_name():
    payload = json.dumps(search.build_search_tool(), ensure_ascii=False)
    assert search.BRIDGE_TOOL_NAME == "search_current_web"
    assert "web_search" not in payload
    assert "google_search" not in payload
    assert search.build_search_tool()["parameters"]["additionalProperties"] is False


@pytest.mark.parametrize(
    "value",
    [
        "",
        "https://generativelanguage.googleapis.com/v1beta",
        "http://user:pass@sub2api:38090/v1beta",
        "http://sub2api:38090/v1beta?key=secret",
        "http://sub2api:38090/v1beta#fragment",
        "http://sub2api:38090/v1",
        "http://sub2api:38090/v1beta\n",
        "file:///v1beta",
    ],
)
def test_search_base_is_fail_closed(value):
    assert not search.is_valid_search_base(value)


def test_search_base_accepts_explicit_sub2api_v1beta():
    assert search.is_valid_search_base("http://sub2api:38090/v1beta")


@pytest.mark.asyncio
async def test_grounding_requires_fact_text_and_public_source(monkeypatch):
    _configure(monkeypatch)
    client = _Client(stream=_stream([_chunk()]))

    evidence = await search.search_current_web("最新消息", client=client)

    assert evidence.success is True
    assert evidence.summary == "事实"
    assert evidence.sources == ("https://example.com/news",)
    assert client.aio.models.kwargs["model"] == "gemini-2.5-flash"
    assert json.loads(evidence.as_tool_output())["status"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chunk",
    [
        _chunk(text="", url="https://example.com/news"),
        _chunk(text="事实", url="javascript:alert(1)"),
        _chunk(text="事实", url="data:text/plain,secret"),
    ],
)
async def test_metadata_only_or_unsafe_source_does_not_count(monkeypatch, chunk):
    _configure(monkeypatch)
    evidence = await search.search_current_web("问题", client=_Client(stream=_stream([chunk])))
    assert evidence.success is False
    assert evidence.sources == ()
    assert json.loads(evidence.as_tool_output())["status"] == "unavailable"


@pytest.mark.asyncio
async def test_search_provider_error_is_neutral(monkeypatch):
    _configure(monkeypatch)
    evidence = await search.search_current_web(
        "问题", client=_Client(error=RuntimeError("secret provider response"))
    )
    assert evidence.success is False
    assert evidence.reason == "provider"
    assert "secret" not in evidence.as_tool_output()


@pytest.mark.asyncio
async def test_search_cancellation_propagates(monkeypatch):
    _configure(monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        await search.search_current_web(
            "问题", client=_Client(stream=_stream([asyncio.CancelledError()]))
        )


def test_build_client_uses_only_dedicated_key_and_base(monkeypatch):
    _configure(monkeypatch)
    fake_client = MagicMock()
    monkeypatch.setattr(search.genai, "Client", fake_client)

    search._build_client()

    kwargs = fake_client.call_args.kwargs
    assert kwargs["api_key"] == "dedicated-key"
    assert kwargs["http_options"].base_url == "http://sub2api:38090/v1beta"


def test_partial_config_disables_bridge(monkeypatch):
    monkeypatch.setattr(search, "ANTIGRAVITY_GEMINI_API_BASE", "http://sub2api:38090/v1beta")
    monkeypatch.setattr(search, "ANTIGRAVITY_GEMINI_API_KEY", "")
    monkeypatch.setattr(search, "ANTIGRAVITY_GEMINI_SEARCH_MODEL", "gemini-2.5-flash")
    assert search.bridge_ready() is False


def test_master_switch_disables_bridge_even_with_full_config(monkeypatch):
    """CLAUDE_SEARCH_BRIDGE_ENABLED=False 时，凭据齐全也必须拿不到桥接——
    这是暂时关闭 Claude 搜索工具（antigravity 会把 Claude 换成非 Claude 模型）的总开关。"""
    monkeypatch.setattr(search, "ANTIGRAVITY_GEMINI_API_BASE", "http://sub2api:38090/v1beta")
    monkeypatch.setattr(search, "ANTIGRAVITY_GEMINI_API_KEY", "dedicated-key")
    monkeypatch.setattr(search, "ANTIGRAVITY_GEMINI_SEARCH_MODEL", "gemini-2.5-flash")
    monkeypatch.setattr(search, "CLAUDE_SEARCH_BRIDGE_ENABLED", False)
    assert search.bridge_ready() is False
def test_oversized_evidence_remains_bounded_and_valid_json():
    evidence = search.SearchEvidence(
        True,
        summary="事实" * 6000,
        sources=tuple(f"https://example.com/news/{index}" for index in range(8)),
        source_titles=("来源" * 200,) * 8,
    )

    encoded = evidence.as_tool_output()
    payload = json.loads(encoded)

    assert len(encoded.encode("utf-8")) <= search.MAX_TOTAL_TOOL_CHARS
    assert payload["status"] == "ok"
    assert len(payload["summary"]) <= search.MAX_SUMMARY_CHARS
    assert len(payload["sources"]) <= search.MAX_SOURCES


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/news",
        "https://localhost/news",
        "https://example.com\n/news",
        "https://example.com/news with space",
    ],
)
def test_non_public_or_malformed_source_is_rejected(url):
    assert search._safe_public_url(url) == ""
