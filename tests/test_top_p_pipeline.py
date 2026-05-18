# -*- coding: utf-8 -*-
import inspect
from unittest.mock import MagicMock, patch

from app.ai.backend import create_backend_stream
from app.gemini_client import call_gemini_stream
from app.litellm_client import call_litellm_stream


def test_stream_signatures_include_top_p():
    assert "top_p" in inspect.signature(create_backend_stream).parameters
    assert "top_p" in inspect.signature(call_litellm_stream).parameters
    assert "top_p" in inspect.signature(call_gemini_stream).parameters


@patch("app.litellm_client.clamp_top_p")
@patch("app.litellm_client.litellm")
def test_litellm_stream_clamps_top_p_when_provided(mock_litellm, mock_clamp_top_p):
    mock_clamp_top_p.return_value = 0.9
    response = MagicMock()
    response.__aiter__.return_value = []
    mock_litellm.acompletion.return_value = response

    stream = call_litellm_stream(
        [{"role": "user", "content": "hi"}],
        target_model="fast",
        top_p=1.2,
    )
    try:
        stream.__anext__().send(None)
    except StopIteration:
        pass

    # provider 取决于 OPENROUTER_API_KEY 是否设置；测试不约束具体 provider 字符串
    mock_clamp_top_p.assert_called_once()
    args = mock_clamp_top_p.call_args
    assert args[0][0] == 1.2


@patch("app.gemini_client.clamp_top_p")
def test_gemini_stream_clamps_top_p_when_provided(mock_clamp_top_p):
    mock_clamp_top_p.return_value = 0.8

    stream = call_gemini_stream(
        [{"role": "user", "content": "hi"}],
        target_model="gemini-3-flash-preview",
        top_p=1.2,
    )
    try:
        stream.__anext__().send(None)
    except StopIteration:
        pass

    mock_clamp_top_p.assert_called_once_with(1.2, "gemini")
