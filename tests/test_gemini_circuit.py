# -*- coding: utf-8 -*-
"""Gemini 熔断与 fallback 的回归测试。"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
import app.config as config
from importlib.util import find_spec


@pytest.fixture(autouse=True)
def reset_circuit_client():
    import app.gemini_circuit as circuit

    circuit._reset_client_for_tests()
    yield
    circuit._reset_client_for_tests()


def test_circuit_redis_timeout_has_a_short_dedicated_default():
    """熔断 Redis 使用独立的短超时，不继承历史数据层超时。"""
    assert getattr(config, "GEMINI_CIRCUIT_REDIS_TIMEOUT_SECONDS", None) == 0.2


def test_gemini_circuit_module_is_available():
    """Gemini 熔断状态必须有独立模块，避免复用业务数据降级逻辑。"""
    assert find_spec("app.gemini_circuit") is not None


def test_circuit_key_isolated_by_bot_id():
    """熔断 key 按 BOT_ID 隔离，不能让不同部署共享状态。"""
    import app.gemini_circuit as circuit

    assert getattr(circuit, "circuit_key", lambda _bot_id: None)("gemini-a") == (
        "gemini_circuit:gemini-a:antigravity"
    )
    assert circuit.circuit_key("gemini-b") != circuit.circuit_key("gemini-a")


def test_open_circuit_writes_fixed_marker_and_ttl():
    """写入只包含固定 marker，TTL 固定为 10 分钟。"""
    import app.gemini_circuit as circuit

    fake = MagicMock()
    with patch.object(circuit, "_redis_client", fake):
        assert circuit.open_circuit("gemini-a") is True

    fake.setex.assert_called_once_with(
        "gemini_circuit:gemini-a:antigravity", 600, "open"
    )


def test_is_open_uses_exists_and_fails_open_on_command_error():
    """Redis 查询异常不向 provider 路径抛出，也不会误判为已熔断。"""
    import app.gemini_circuit as circuit

    fake = MagicMock()
    fake.exists.return_value = 1
    with patch.object(circuit, "_redis_client", fake):
        assert circuit.is_circuit_open("gemini-a") is True
    fake.exists.assert_called_once_with("gemini_circuit:gemini-a:antigravity")

    fake.exists.side_effect = RuntimeError("redis socket secret")
    with patch.object(circuit, "_redis_client", fake):
        assert circuit.is_circuit_open("gemini-a") is False
        assert circuit._redis_client is None


def test_open_circuit_fails_open_on_command_error():
    """Redis 写入异常不阻断 fallback 或主请求。"""
    import app.gemini_circuit as circuit

    fake = MagicMock()
    fake.setex.side_effect = RuntimeError("redis outage details")
    with patch.object(circuit, "_redis_client", fake):
        assert circuit.open_circuit("gemini-a") is False
        assert circuit._redis_client is None


def test_client_build_failure_is_fail_open(monkeypatch):
    """首次 ping/建连失败时不向调用方暴露 Redis 异常。"""
    import app.gemini_circuit as circuit

    monkeypatch.setattr(circuit.redis, "Redis", MagicMock(side_effect=OSError("blackhole")))
    with patch.object(circuit, "_redis_client", None):
        assert circuit.is_circuit_open("gemini-a") is False


def test_async_wrappers_move_sync_redis_work_to_thread():
    """异步入口必须委托线程包装，避免同步 Redis 阻塞事件循环。"""
    import app.gemini_circuit as circuit

    async def run():
        with patch.object(circuit, "is_circuit_open", return_value=True) as sync_open, \
             patch.object(circuit, "open_circuit", return_value=True) as sync_write:
            assert await circuit.is_circuit_open_async("gemini-a") is True
            assert await circuit.open_circuit_async("gemini-a") is True
            sync_open.assert_called_once_with("gemini-a")
            sync_write.assert_called_once_with("gemini-a")

    asyncio.run(run())
