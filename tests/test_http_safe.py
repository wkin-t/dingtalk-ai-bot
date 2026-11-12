# -*- coding: utf-8 -*-
"""固定 IP 下载的凭据隔离与对外错误边界。"""
from unittest.mock import AsyncMock

import httpx
import pytest

from app import http_safe


@pytest.fixture
def install_transport(monkeypatch):
    original_client = httpx.AsyncClient
    monkeypatch.setattr(http_safe, "_verify_resolved_public_ips", AsyncMock(return_value="93.184.216.34"))

    def install(handler):
        monkeypatch.setattr(http_safe.httpx, "AsyncClient", lambda **kwargs: original_client(
            transport=httpx.MockTransport(handler), **kwargs))
    return install


@pytest.mark.asyncio
@pytest.mark.parametrize("destination", ["https://other.example/file", "https://source.example:8443/file"])
async def test_cross_origin_redirect_drops_custom_credentials(install_transport, destination):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": destination}) if len(seen) == 1 else httpx.Response(200, content=b"ok")

    install_transport(handle)
    await http_safe.fetch_public_bytes("https://source.example/start", max_bytes=100, timeout_seconds=1,
        headers={"Authorization": "Bearer sentinel", "Cookie": "session=sentinel", "X-Download-Signature": "sentinel"})
    assert seen[0].headers["authorization"] == "Bearer sentinel"
    assert all(key not in seen[1].headers for key in ("authorization", "cookie", "x-download-signature"))


@pytest.mark.asyncio
async def test_same_origin_redirect_preserves_explicit_credentials(install_transport):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "/file"}) if len(seen) == 1 else httpx.Response(200, content=b"ok")

    install_transport(handle)
    assert await http_safe.fetch_public_bytes("https://source.example/start", max_bytes=100, timeout_seconds=1,
        headers={"Authorization": "Bearer sentinel"}) == b"ok"
    assert seen[1].headers["authorization"] == "Bearer sentinel"


@pytest.mark.asyncio
async def test_same_origin_cookie_survives_redirect_and_dns_change(install_transport, monkeypatch):
    seen = []
    def handle(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(302, headers={"Location": "/file", "Set-Cookie": "gate=ok; Path=/"})
        return httpx.Response(200, content=b"ok") if request.headers.get("cookie") == "gate=ok" else httpx.Response(403)
    install_transport(handle)
    monkeypatch.setattr(http_safe, "_verify_resolved_public_ips", AsyncMock(side_effect=["93.184.216.34", "8.8.8.8"]))
    assert await http_safe.fetch_public_bytes("https://source.example/start", max_bytes=100, timeout_seconds=1) == b"ok"
    assert seen[0].url.host != seen[1].url.host


@pytest.mark.asyncio
async def test_same_origin_redirect_preserves_server_cookie(install_transport):
    seen = []

    def handle(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(302, headers={"Location": "/file", "Set-Cookie": "gate=ok; Path=/"})
        return httpx.Response(200, content=b"ok" if request.headers.get("cookie") == "gate=ok" else b"missing")

    install_transport(handle)
    assert await http_safe.fetch_public_bytes("https://source.example/start", max_bytes=100, timeout_seconds=1) == b"ok"


@pytest.mark.asyncio
async def test_fixed_ip_cookie_jar_does_not_cross_virtual_hosts(install_transport):
    seen = []

    def handle(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(302, headers={"Location": "https://other.example/file", "Set-Cookie": "session=sentinel; Path=/"})
        return httpx.Response(200, content=b"ok")

    install_transport(handle)
    await http_safe.fetch_public_bytes("https://source.example/start", max_bytes=100, timeout_seconds=1)
    assert "cookie" not in seen[1].headers


@pytest.mark.asyncio
async def test_redirect_chain_does_not_restore_dropped_credentials(install_transport):
    seen = []

    def handle(request):
        seen.append(request)
        targets = ["https://other.example/file", "https://source.example/end"]
        return httpx.Response(302, headers={"Location": targets[len(seen) - 1]}) if len(seen) < 3 else httpx.Response(200, content=b"ok")

    install_transport(handle)
    await http_safe.fetch_public_bytes("https://source.example/start", max_bytes=100, timeout_seconds=1,
        headers={"Authorization": "Bearer sentinel"})
    assert ["authorization" in request.headers for request in seen] == [True, False, False]


@pytest.mark.asyncio
async def test_https_redirect_cannot_downgrade_to_http(install_transport):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "http://source.example/file"}) if len(seen) == 1 else httpx.Response(200, content=b"ok")

    install_transport(handle)
    with pytest.raises(RuntimeError):
        await http_safe.fetch_public_bytes("https://source.example/start", max_bytes=100, timeout_seconds=1)
    assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["network", "timeout", "status", "oversize"])
async def test_download_errors_never_include_signed_urls_or_exception_text(install_transport, failure):
    signed_url = "https://source.example/private.pdf?signature=secret-sentinel#secret-fragment"

    def handle(request):
        if failure == "network":
            raise httpx.ConnectError("provider-secret " + signed_url, request=request)
        if failure == "timeout":
            raise httpx.ReadTimeout("provider-secret " + signed_url, request=request)
        return httpx.Response(403) if failure == "status" else httpx.Response(200, content=b"oversize")

    install_transport(handle)
    with pytest.raises(RuntimeError) as caught:
        await http_safe.fetch_public_bytes(signed_url, max_bytes=1, timeout_seconds=1)
    assert all(value not in str(caught.value) for value in (signed_url, "secret-sentinel", "secret-fragment", "provider-secret"))
    assert caught.value.__cause__ is None
