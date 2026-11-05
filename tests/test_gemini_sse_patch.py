"""验证 Gemini SDK 对 SSE 心跳及数据帧的兼容处理。"""
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import httpx
import pytest
from google import genai
from google.genai import _api_client

from app import gemini_sse_patch


@pytest.fixture(autouse=True)
def apply_sse_patch():
    assert gemini_sse_patch.apply()


def test_patch_version_matches_installed_gemini_sdk():
    assert gemini_sse_patch.EXPECTED_VERSION == genai.__version__


def test_sync_httpx_stream_ignores_sse_keepalive():
    response = httpx.Response(200, content=b': keep-alive\n\ndata: {"value": 1}\n\n')
    stream = _api_client.HttpResponse({}, response_stream=response)
    assert list(stream._iter_response_stream()) == ['{"value": 1}']


@pytest.mark.asyncio
async def test_async_httpx_stream_ignores_sse_keepalive():
    response = httpx.Response(200, content=b': keep-alive\n\ndata: {"value": 1}\n\n')
    stream = _api_client.HttpResponse({}, response_stream=response)
    assert [event async for event in stream._aiter_response_stream()] == ['{"value": 1}']


@pytest.mark.asyncio
async def test_async_aiohttp_stream_ignores_sse_keepalive():
    response = MagicMock(spec=aiohttp.ClientResponse)
    response.content.readline = AsyncMock(side_effect=[
        b': keep-alive\n', b'\n', b'data: {"value": 1}\n', b'\n', b''
    ])
    stream = _api_client.HttpResponse({}, response_stream=response)
    assert [event async for event in stream._aiter_response_stream()] == ['{"value": 1}']
    response.release.assert_called_once()
