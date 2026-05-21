# -*- coding: utf-8 -*-
"""OpenAI Responses API 的 previous_response_id 状态存储。

每个 conversation 保留最近一次 Responses API 调用返回的 response.id，
下一轮调用时作为 previous_response_id 传回，让服务端保留 thinking 上下文。

存储优先 Redis（TTL 7 天），不可用降级文件。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

STATE_DIR = os.path.join("data", "responses_state")
_DEFAULT_TTL_DAYS = 7

_redis_client: Any = None


def _current_bot_id() -> str:
    from app import config as cfg
    return getattr(cfg, "BOT_ID", "default")


def _get_redis():
    global _redis_client
    if _redis_client is None:
        try:
            import redis
            from app.database import REDIS_DB, REDIS_HOST, REDIS_PASSWORD, REDIS_PORT

            _redis_client = redis.StrictRedis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                password=REDIS_PASSWORD or None,
                db=REDIS_DB,
                decode_responses=True,
                socket_connect_timeout=2,
            )
            _redis_client.ping()
        except Exception as e:
            print(f"[responses_state] Redis 不可用，降级文件: {e}")
            _redis_client = False
    return _redis_client if _redis_client else None


def _redis_key(conversation_id: str) -> str:
    return f"responses_id:{_current_bot_id()}:{conversation_id}"


def _file_path(conversation_id: str) -> str:
    safe_key = conversation_id.replace("/", "_").replace("\\", "_").replace(":", "_")
    safe_bot = _current_bot_id().replace("/", "_").replace("\\", "_").replace(":", "_")
    return os.path.join(STATE_DIR, f"{safe_bot}__{safe_key}.json")


def _read_storage(conversation_id: str) -> Optional[Dict[str, Any]]:
    r = _get_redis()
    if r:
        try:
            raw = r.get(_redis_key(conversation_id))
            if raw:
                return json.loads(raw)
        except Exception as e:
            print(f"[responses_state] Redis 读失败: {e}")

    path = _file_path(conversation_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[responses_state] 文件读失败 {path}: {e}")
        return None


def _write_storage(conversation_id: str, data: Dict[str, Any]) -> None:
    r = _get_redis()
    if r:
        try:
            r.setex(
                _redis_key(conversation_id),
                _DEFAULT_TTL_DAYS * 86400,
                json.dumps(data, ensure_ascii=False),
            )
        except Exception as e:
            print(f"[responses_state] Redis 写失败: {e}")

    path = _file_path(conversation_id)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[responses_state] 文件写失败 {path}: {e}")


def _delete_storage(conversation_id: str) -> None:
    r = _get_redis()
    if r:
        try:
            r.delete(_redis_key(conversation_id))
        except Exception as e:
            print(f"[responses_state] Redis 删失败: {e}")

    path = _file_path(conversation_id)
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception as e:
            print(f"[responses_state] 文件删失败 {path}: {e}")


def get_response_id(conversation_id: str) -> Optional[str]:
    """读取最近一次 response.id。过期则清理并返回 None。"""
    if not conversation_id:
        return None
    rec = _read_storage(conversation_id)
    if rec is None:
        return None
    expires_at_str = rec.get("expires_at")
    if expires_at_str:
        try:
            expires_at = datetime.fromisoformat(expires_at_str)
            if datetime.now() > expires_at:
                _delete_storage(conversation_id)
                return None
        except Exception:
            pass
    return rec.get("response_id")


def set_response_id(conversation_id: str, response_id: str) -> None:
    """写入 response.id，TTL 7 天。空 conversation_id 不写。"""
    if not conversation_id or not response_id:
        return
    now = datetime.now()
    rec = {
        "response_id": response_id,
        "set_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "expires_at": (now + timedelta(days=_DEFAULT_TTL_DAYS)).isoformat(),
    }
    _write_storage(conversation_id, rec)


def clear_response_id(conversation_id: str) -> None:
    """清除存储的 response.id（用于 /clear 或上游报错）。"""
    if not conversation_id:
        return
    _delete_storage(conversation_id)
