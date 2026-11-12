# -*- coding: utf-8 -*-
"""受控公网 HTTP 字节流读取工具：禁用环境变量代理透传并逐跳拦截内网/元数据 SSRF。"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

import httpx

_CGNAT_METADATA_NET = ipaddress.ip_network("100.64.0.0/10")
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_BLOCKED_HOST_SUFFIXES = (
    ".local",
    ".localhost",
    ".internal",
    ".oss-internal.aliyun-inc.com",
)

_proxy_skip_until: float = 0.0


def _is_forbidden_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        ip_obj = ip_obj.ipv4_mapped
    if (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    ):
        return True
    if isinstance(ip_obj, ipaddress.IPv4Address) and ip_obj in _CGNAT_METADATA_NET:
        return True
    return False


def validate_safe_public_url(url: str) -> str:
    """校验 URL 协议与目标主机，拒绝内网 IP、云元数据网段及内部域名。"""
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"禁止非 HTTP(S) 协议 (SSRF 防护): {parsed.scheme or 'empty'}")

    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise ValueError("URL 缺少有效主机名")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("禁止 URL 包含用户凭据")

    if host in {"localhost", "metadata.google.internal"} or any(
        host.endswith(sfx) for sfx in _BLOCKED_HOST_SUFFIXES
    ):
        raise ValueError(f"禁止访问内网或内部域名 (SSRF 防护): {host}")

    try:
        ip_obj = ipaddress.ip_address(host)
    except ValueError:
        return url

    if _is_forbidden_ip(ip_obj):
        raise ValueError(f"禁止访问内网或私有地址 (SSRF 防护): {host}")
    return url


async def _verify_resolved_public_ips(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").strip().rstrip(".")
    if not host:
        raise ValueError("URL 缺少主机名")
    try:
        ip_obj = ipaddress.ip_address(host)
    except ValueError:
        ip_obj = None
    if ip_obj is not None:
        if _is_forbidden_ip(ip_obj):
            raise ValueError("禁止访问内网或私有地址")
        return host

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    infos = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    resolved = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        ip_str = sockaddr[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if _is_forbidden_ip(ip_obj):
            raise ValueError(f"域名解析命中内网或私有地址 (SSRF 防护): {host} -> {ip_str}")
        resolved.append(ip_str)
    if not resolved:
        raise ValueError("域名未解析到有效公网地址")
    return resolved[0]


def _url_origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    host = (parts.hostname or "").encode("idna").decode("ascii").lower()
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.scheme, host, port


def _download_error_summary(error: Optional[Exception]) -> str:
    # HTTPX 异常常含签名 URL 或响应正文，只允许固定类别进入调用方的错误说明。
    if isinstance(error, httpx.HTTPStatusError):
        return f"远端返回 HTTP {error.response.status_code}"
    if isinstance(error, (httpx.TimeoutException, asyncio.TimeoutError)):
        return "文件下载超时"
    if isinstance(error, ValueError):
        return "下载被安全策略禁止（URL、重定向或大小限制）"
    if isinstance(error, (httpx.RequestError, OSError)):
        return "文件下载网络连接失败"
    return "文件下载未完成"


async def fetch_public_bytes(
    url: str,
    *,
    max_bytes: int,
    timeout_seconds: float,
    headers: Optional[Dict[str, str]] = None,
    proxy_url: Optional[str] = None,
    allow_proxy_fallback: bool = True,
    max_redirects: int = 3,
) -> bytes:
    """安全拉取公网字节流：强制 trust_env=False，手动逐跳校验 30x 重定向目标与字节上限。"""
    global _proxy_skip_until

    validate_safe_public_url(url)

    attempts: List[Optional[str]] = []
    clean_proxy = (proxy_url or "").strip()
    # httpcore 的 HTTP CONNECT 隧道不接受独立 SNI，不能安全地在固定 IP 时验证原域名。
    if clean_proxy and urlparse(clean_proxy).scheme in {"http", "https"} and urlparse(url).scheme == "https":
        if not allow_proxy_fallback:
            raise ValueError("HTTPS 固定公网 IP 不支持 HTTP CONNECT 代理")
        clean_proxy = ""
    if clean_proxy and time.time() >= _proxy_skip_until:
        attempts.append(clean_proxy)
    if allow_proxy_fallback or not attempts:
        attempts.append(None)

    base_headers = {
        "User-Agent": "dingtalk-ai-bot/1.0",
        "Accept-Encoding": "gzip, deflate",
    }
    req_headers = dict(base_headers)
    if headers:
        req_headers.update({str(k): str(v) for k, v in headers.items() if k and v is not None})

    last_error: Optional[Exception] = None

    for chosen_proxy in attempts:
        connect_timeout = min(2.0 if chosen_proxy else 3.0, timeout_seconds)
        timeout_cfg = httpx.Timeout(timeout_seconds, connect=connect_timeout)
        client_kwargs: Dict[str, Any] = {
            "follow_redirects": False,
            "trust_env": False,
            "timeout": timeout_cfg,
        }
        if chosen_proxy:
            client_kwargs["proxy"] = chosen_proxy

        try:
            current_url = url
            current_headers = dict(req_headers)
            redirect_cookies = httpx.Cookies()
            for hop in range(max_redirects + 1):
                validate_safe_public_url(current_url)
                if chosen_proxy and urlparse(chosen_proxy).scheme in {"http", "https"} and urlparse(current_url).scheme == "https":
                    raise ValueError("HTTPS 固定公网 IP 不支持 HTTP CONNECT 代理")
                ip = await _verify_resolved_public_ips(current_url)
                parts = urlsplit(current_url)
                host = (parts.hostname or "").encode("idna").decode("ascii")
                ip_host = f"[{ip}]" if ":" in ip else ip
                netloc = f"{ip_host}:{parts.port}" if parts.port else ip_host
                pinned_url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
                host_header = f"[{host}]" if ":" in host else host
                if parts.port:
                    host_header += f":{parts.port}"
                # Cookie 按原域名匹配，避免固定 IP 把不同虚拟主机误当成同一站点。
                logical_request = httpx.Request("GET", current_url, headers=current_headers)
                redirect_cookies.set_cookie_header(logical_request)
                async with httpx.AsyncClient(headers=logical_request.headers, **client_kwargs) as client:
                    async with client.stream("GET", pinned_url, headers={"Host": host_header}, extensions={"sni_hostname": host}) as resp:
                        if resp.status_code in _REDIRECT_STATUSES:
                            if hop >= max_redirects:
                                raise ValueError(f"重定向次数超过上限 ({max_redirects})")
                            location = resp.headers.get("Location") or resp.headers.get("location") or ""
                            if not location:
                                raise ValueError("收到重定向响应但缺少 Location 头")
                            next_url = urljoin(current_url, location)
                            validate_safe_public_url(next_url)
                            if parts.scheme == "https" and urlsplit(next_url).scheme != "https":
                                raise ValueError("禁止 HTTPS 下载降级到 HTTP")
                            if _url_origin(current_url) != _url_origin(next_url):
                                current_headers = dict(base_headers)
                                redirect_cookies = httpx.Cookies()
                            else:
                                redirect_cookies.extract_cookies(httpx.Response(
                                    resp.status_code, headers=resp.headers, request=logical_request))
                            current_url = next_url
                            continue

                        resp.raise_for_status()
                        content_len = resp.headers.get("Content-Length")
                        if content_len and content_len.isdigit() and int(content_len) > max_bytes:
                            raise ValueError(f"远端文件超过上限 ({content_len} > {max_bytes})")

                        chunks: List[bytes] = []
                        total = 0
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            total += len(chunk)
                            if total > max_bytes:
                                raise ValueError(f"流式读取超过上限 ({total} > {max_bytes})")
                            chunks.append(chunk)
                        return b"".join(chunks)

            raise ValueError("重定向未到达有效终点")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = exc
            if chosen_proxy and "SSRF" not in str(exc) and "禁止" not in str(exc):
                _proxy_skip_until = time.time() + 60.0
                continue
            break

    raise RuntimeError(f"拉取失败：{_download_error_summary(last_error)}") from None
