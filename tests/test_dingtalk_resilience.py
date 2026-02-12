# -*- coding: utf-8 -*-
"""DingTalk 抗抖动逻辑单元测试。"""

import importlib
from unittest.mock import AsyncMock, patch

import pytest


def _reload_config(monkeypatch, **env):
    keys = [
        "DINGTALK_FORCE_DIRECT",
        "DINGTALK_RETRY_ATTEMPTS",
        "DINGTALK_RETRY_BASE_DELAY",
        "DINGTALK_RETRY_MAX_DELAY",
        "DINGTALK_RETRY_JITTER",
        "DINGTALK_CONNECT_TIMEOUT_MS",
        "DINGTALK_READ_TIMEOUT_MS",
        "DINGTALK_RUNTIME_MAX_ATTEMPTS",
        "DINGTALK_FILE_DOWNLOAD_TIMEOUT",
        "DINGTALK_TOKEN_EARLY_REFRESH_SEC",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)

    for key, value in env.items():
        monkeypatch.setenv(key, str(value))

    import app.config as config

    return importlib.reload(config)


def test_config_parsing_and_clamp(monkeypatch):
    cfg = _reload_config(
        monkeypatch,
        DINGTALK_FORCE_DIRECT="false",
        DINGTALK_RETRY_ATTEMPTS="-3",
        DINGTALK_RETRY_BASE_DELAY="bad",
        DINGTALK_RETRY_MAX_DELAY="0.2",
        DINGTALK_RETRY_JITTER="-1",
        DINGTALK_CONNECT_TIMEOUT_MS="100",
        DINGTALK_READ_TIMEOUT_MS="abc",
        DINGTALK_RUNTIME_MAX_ATTEMPTS="0",
        DINGTALK_FILE_DOWNLOAD_TIMEOUT="1",
        DINGTALK_TOKEN_EARLY_REFRESH_SEC="10",
    )

    assert cfg.DINGTALK_FORCE_DIRECT is False
    assert cfg.DINGTALK_RETRY_ATTEMPTS == 1
    assert cfg.DINGTALK_RETRY_BASE_DELAY == 0.8
    assert cfg.DINGTALK_RETRY_MAX_DELAY == 0.8
    assert cfg.DINGTALK_RETRY_JITTER == 0.0
    assert cfg.DINGTALK_CONNECT_TIMEOUT_MS == 1000
    assert cfg.DINGTALK_READ_TIMEOUT_MS == 60000
    assert cfg.DINGTALK_RUNTIME_MAX_ATTEMPTS == 1
    assert cfg.DINGTALK_FILE_DOWNLOAD_TIMEOUT == 5
    assert cfg.DINGTALK_TOKEN_EARLY_REFRESH_SEC == 30


def test_retryable_and_auth_error_detection():
    import app.dingtalk_card as dc

    assert dc._is_auth_error(Exception("401 Unauthorized"))
    assert dc._is_auth_error(Exception("token expired"))
    assert not dc._is_retryable_exception(Exception("403 forbidden"))

    assert dc._is_retryable_exception(Exception("SSL: UNEXPECTED_EOF_WHILE_READING"))
    assert dc._is_retryable_exception(Exception("connection reset by peer"))
    assert not dc._is_retryable_exception(Exception("invalid request payload"))


def test_retry_wait_seconds_with_jitter(monkeypatch):
    import app.dingtalk_card as dc

    monkeypatch.setattr(dc.random, "uniform", lambda a, b: 0.2)

    wait1 = dc._retry_wait_seconds(1, base_delay=0.8, max_delay=8.0, jitter=0.35)
    wait4 = dc._retry_wait_seconds(4, base_delay=0.8, max_delay=8.0, jitter=0.35)

    assert wait1 == pytest.approx(1.0)
    assert wait4 == pytest.approx(6.6)


@pytest.mark.asyncio
async def test_async_retry_retries_on_none():
    import app.dingtalk_card as dc

    attempts = {"count": 0}

    @dc.async_retry(max_attempts=4, base_delay=0, max_delay=0, jitter=0)
    async def unstable_none():
        attempts["count"] += 1
        if attempts["count"] < 3:
            return None
        return "ok"

    with patch("app.dingtalk_card.asyncio.sleep", new=AsyncMock()) as sleep_mock:
        result = await unstable_none()

    assert result == "ok"
    assert attempts["count"] == 3
    assert sleep_mock.await_count == 2


@pytest.mark.asyncio
async def test_async_retry_respects_retry_if():
    import app.dingtalk_card as dc

    attempts = {"count": 0}

    @dc.async_retry(
        max_attempts=5,
        base_delay=0,
        max_delay=0,
        jitter=0,
        retry_if=lambda exc: False,
    )
    async def non_retryable_error():
        attempts["count"] += 1
        raise ValueError("bad input")

    with patch("app.dingtalk_card.asyncio.sleep", new=AsyncMock()) as sleep_mock:
        result = await non_retryable_error()

    assert result is None
    assert attempts["count"] == 1
    assert sleep_mock.await_count == 0


def test_build_requests_retry_total():
    import app.dingtalk_card as dc

    retry = dc._build_requests_retry(max_attempts=5, base_delay=0.8)

    assert retry.total == 4
