# -*- coding: utf-8 -*-
"""Responses 搜索证据归一化回归测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.openai_client import _responses_search_signal, call_openai_stream


def _make_async_stream(events):
    async def _gen():
        for event in events:
            yield event

    return _gen()


def _model_config():
    return {
        "model": "claude-opus-4-6-thinking",
        "region": "global",
        "supports_reasoning": True,
        "supports_search": True,
        "supports_vision": True,
        "reasoning_param": "openai_effort",
    }


@pytest.mark.parametrize("search_index, expected", [(63, True), (64, False)])
def test_search_candidate_scan_has_bounded_list_depth(search_index, expected):
    outputs = [{"type": "message"} for _ in range(search_index)]
    outputs.append({"type": "web_search_call"})
    outputs.extend({"type": "message"} for _ in range(128))

    signal = _responses_search_signal({
        "type": "response.completed",
        "response": {"output": outputs},
    })

    assert bool(signal) is expected


async def _collect(mock_openai_cls, mock_get_config, events, enable_search=True):
    mock_get_config.return_value = _model_config()
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.responses.create = AsyncMock(return_value=_make_async_stream(events))

    return [
        chunk
        async for chunk in call_openai_stream(
            [{"role": "user", "content": "搜索实时资料"}],
            target_model="fast",
            enable_search=enable_search,
        )
    ]


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_dict_delta_annotations_emit_search_once(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    chunks = await _collect(
        mock_openai_cls,
        mock_get_config,
        [
            {"type": "response.output_text.delta", "delta": "实时回答", "annotations": [
                {"type": "url_citation"},
            ]},
            {"type": "response.output_text.delta", "delta": "。"},
            {"type": "response.completed", "response": {}},
        ],
    )

    assert [chunk for chunk in chunks if chunk.get("search", {}).get("executed")] == [
        {"search": {"executed": True}},
    ]
    assert "实时回答" in "".join(chunk.get("content", "") for chunk in chunks)


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_nested_final_output_search_item_is_structured_evidence(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    chunks = await _collect(
        mock_openai_cls,
        mock_get_config,
        [
            {
                "type": "response.completed",
                "response": {"output": [{"type": "web_search_call"}]},
            },
        ],
    )

    assert [chunk for chunk in chunks if chunk.get("search", {}).get("executed")] == [
        {"search": {"executed": True}},
    ]


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_nested_final_output_annotation_is_structured_evidence(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    chunks = await _collect(
        mock_openai_cls,
        mock_get_config,
        [
            {
                "type": "response.completed",
                "response": {
                    "output": [{
                        "type": "message",
                        "content": [{"annotations": [{"type": "url_citation"}]}],
                    }],
                },
            },
        ],
    )

    assert [chunk for chunk in chunks if chunk.get("search", {}).get("executed")] == [
        {"search": {"executed": True}},
    ]


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_sdk_like_nested_search_item_is_structured_evidence(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    chunks = await _collect(
        mock_openai_cls,
        mock_get_config,
        [SimpleNamespace(
            type="response.output_item.done",
            item=SimpleNamespace(type="web_search_call"),
        )],
    )

    assert [chunk for chunk in chunks if chunk.get("search", {}).get("executed")] == [
        {"search": {"executed": True}},
    ]


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_structured_search_signal_without_native_search_stays_off(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    chunks = await _collect(
        mock_openai_cls,
        mock_get_config,
        [{
            "type": "response.output_text.delta",
            "delta": "普通回答",
            "annotations": [{"type": "url_citation"}],
        }],
        enable_search=False,
    )

    assert not any(chunk.get("search", {}).get("executed") for chunk in chunks)


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_unknown_event_type_is_collapsed_in_search_probe(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid, capsys,
):
    await _collect(
        mock_openai_cls,
        mock_get_config,
        [
            {"type": "BearerSecretTokenABC123"},
            {"type": "response.completed", "response": {}},
        ],
    )

    output = capsys.readouterr().out
    assert "BearerSecretTokenABC123" not in output
    assert "events=other=1,response.completed=1" in output


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_grounding_link_in_done_event_emits_search_without_duplicate_content(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    grounding_link = (
        "https://vertexaisearch.cloud.google.com/grounding-api-redirect/"
        "AbCdEf0123456789_-AbCdEf0123456789"
    )
    chunks = await _collect(
        mock_openai_cls,
        mock_get_config,
        [
            {"type": "response.output_text.delta", "delta": "回答"},
            {"type": "response.output_text.done", "text": grounding_link},
            {"type": "response.completed", "response": {}},
        ],
    )

    assert [chunk for chunk in chunks if chunk.get("search", {}).get("executed")] == [
        {"search": {"executed": True}},
    ]
    assert [chunk.get("content") for chunk in chunks if "content" in chunk] == ["回答"]


@pytest.mark.asyncio
@patch("app.responses_state.get_response_id", return_value=None)
@patch("app.responses_state.set_response_id")
@patch("app.openai_client.get_litellm_model_config")
@patch("app.openai_client.AsyncOpenAI")
async def test_output_text_done_is_not_emitted_as_duplicate_content(
    mock_openai_cls, mock_get_config, mock_set_rid, mock_get_rid,
):
    chunks = await _collect(
        mock_openai_cls,
        mock_get_config,
        [
            {"type": "response.output_text.delta", "delta": "回答"},
            {"type": "response.output_text.done", "text": "回答"},
            {"type": "response.completed", "response": {}},
        ],
    )

    assert [chunk.get("content") for chunk in chunks if "content" in chunk] == ["回答"]
    assert not any(chunk.get("search", {}).get("executed") for chunk in chunks)
