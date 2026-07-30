# -*- coding: utf-8 -*-
"""Gemini antigravity 熔断状态的独立 Redis 存储。

该模块只保存一个无敏感内容的短期 marker，不复用业务数据层的文件降级。
同步 Redis 客户端只通过本模块的 async wrapper 从事件循环线程隔离出去。
"""

import asyncio
import os
import threading
import time
from typing import Optional

import redis

from app.config import BOT_ID, GEMINI_CIRCUIT_REDIS_TIMEOUT_SECONDS

# 直接读取环境变量，避免导入 app.database 时触发其 5 秒 Redis ping 和业务存储初始化。
REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
try:
    REDIS_PORT = int(os.getenv("REDIS_PORT", 36379))
except (TypeError, ValueError):
    REDIS_PORT = 36379
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")
try:
    REDIS_DB = int(os.getenv("REDIS_DB", 0))
except (TypeError, ValueError):
    REDIS_DB = 0


CIRCUIT_TTL_SECONDS = 600
CIRCUIT_MARKER = "open"
_redis_client: Optional[redis.Redis] = None
_redis_client_lock = threading.Lock()
_redis_next_retry_at = 0.0
_REDIS_FAILURE_BACKOFF_SECONDS = 0.5


def circuit_key(bot_id: str = BOT_ID) -> str:
    """返回按 bot 隔离的 antigravity 熔断 key。"""
    return f"gemini_circuit:{bot_id}:antigravity"


def _log_redis_failure(operation: str) -> None:
    """记录固定安全日志；不读取或格式化 Redis 异常文本。"""
    print(f"⚠️ [Gemini 熔断] Redis {operation} 失败，按 fail-open 处理")


def _build_redis_client() -> Optional[redis.Redis]:
    """懒构建并 ping 专用短超时 Redis client。"""
    try:
        candidate = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            password=REDIS_PASSWORD or None,
            db=REDIS_DB,
            decode_responses=True,
            socket_connect_timeout=GEMINI_CIRCUIT_REDIS_TIMEOUT_SECONDS,
            socket_timeout=GEMINI_CIRCUIT_REDIS_TIMEOUT_SECONDS,
        )
        candidate.ping()
        return candidate
    except Exception:
        _log_redis_failure("client/ping")
        return None


def _get_redis_client() -> Optional[redis.Redis]:
    global _redis_client, _redis_next_retry_at
    if _redis_client is None:
        now = time.monotonic()
        if now < _redis_next_retry_at:
            return None
        with _redis_client_lock:
            if _redis_client is not None:
                return _redis_client
            now = time.monotonic()
            if now < _redis_next_retry_at:
                return None
            _redis_client = _build_redis_client()
            if _redis_client is None:
                _redis_next_retry_at = time.monotonic() + _REDIS_FAILURE_BACKOFF_SECONDS
            else:
                _redis_next_retry_at = 0.0
    return _redis_client


def is_circuit_open(bot_id: str = BOT_ID) -> bool:
    """同步查询熔断 marker；Redis 任意异常均视为未熔断。"""
    global _redis_client, _redis_next_retry_at
    client = _get_redis_client()
    if client is None:
        return False
    try:
        return bool(client.exists(circuit_key(bot_id)))
    except Exception:
        _redis_client = None
        _redis_next_retry_at = time.monotonic() + _REDIS_FAILURE_BACKOFF_SECONDS
        _log_redis_failure("exists")
        return False


def open_circuit(bot_id: str = BOT_ID) -> bool:
    """同步写入固定 600 秒 marker；不把异常详情写入 Redis。"""
    global _redis_client, _redis_next_retry_at
    client = _get_redis_client()
    if client is None:
        return False
    try:
        client.setex(circuit_key(bot_id), CIRCUIT_TTL_SECONDS, CIRCUIT_MARKER)
        return True
    except Exception:
        _redis_client = None
        _redis_next_retry_at = time.monotonic() + _REDIS_FAILURE_BACKOFF_SECONDS
        _log_redis_failure("setex")
        return False


async def is_circuit_open_async(bot_id: str = BOT_ID) -> bool:
    """在线程中查询熔断，避免同步 Redis 阻塞 asyncio event loop。"""
    return await asyncio.to_thread(is_circuit_open, bot_id)


async def open_circuit_async(bot_id: str = BOT_ID) -> bool:
    """在线程中写入熔断，避免同步 Redis 阻塞 asyncio event loop。"""
    return await asyncio.to_thread(open_circuit, bot_id)


def _reset_client_for_tests() -> None:
    """仅供测试清理模块级 lazy client，不影响 Redis 中的 marker。"""
    global _redis_client, _redis_next_retry_at
    _redis_client = None
    _redis_next_retry_at = 0.0
