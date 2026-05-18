# -*- coding: utf-8 -*-
"""手动采样覆盖存储 (Redis 优先 + 文件 fallback)。

Schema:
  {
    "temperature": 1.5 | null,
    "top_p": 0.9 | null,
    "set_at": "2026-05-18 14:30:15",
    "set_by": "stafffx_xxxxx",
    "set_by_nick": "张三",
    "expires_at": "2026-05-19 14:30:15"
  }

TTL: 24 小时，群聊场景友好。
"""
import json
import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from app.config import BOT_ID


SAMPLE_DIR = "data/sample"
os.makedirs(SAMPLE_DIR, exist_ok=True)

_DEFAULT_TTL_HOURS = 24

# Redis client（lazy init）
_redis_client = None


def _current_bot_id() -> str:
    return os.getenv("BOT_ID", BOT_ID)


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
            print(f"[sample_override] Redis 不可用，降级文件: {e}")
            _redis_client = False
    return _redis_client if _redis_client else None


def _redis_key(session_key: str) -> str:
    return f"sample:{_current_bot_id()}:{session_key}"


def _file_path(session_key: str) -> str:
    safe_key = session_key.replace("/", "_").replace("\\", "_").replace(":", "_")
    safe_bot = _current_bot_id().replace("/", "_").replace("\\", "_").replace(":", "_")
    return os.path.join(SAMPLE_DIR, f"{safe_bot}__{safe_key}.json")


def _read_storage(session_key: str) -> Optional[Dict[str, Any]]:
    r = _get_redis()
    if r:
        try:
            raw = r.get(_redis_key(session_key))
            if raw:
                return json.loads(raw)
        except Exception as e:
            print(f"[sample_override] Redis 读失败: {e}")

    path = _file_path(session_key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[sample_override] 文件读失败 {path}: {e}")
        return None


def _write_storage(session_key: str, data: Dict[str, Any]) -> None:
    r = _get_redis()
    if r:
        try:
            r.setex(_redis_key(session_key), _DEFAULT_TTL_HOURS * 3600, json.dumps(data, ensure_ascii=False))
        except Exception as e:
            print(f"[sample_override] Redis 写失败: {e}")

    path = _file_path(session_key)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[sample_override] 文件写失败 {path}: {e}")


def _delete_storage(session_key: str) -> None:
    r = _get_redis()
    if r:
        try:
            r.delete(_redis_key(session_key))
        except Exception:
            pass

    path = _file_path(session_key)
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception as e:
            print(f"[sample_override] 文件删除失败: {e}")


def get_override(session_key: str) -> Optional[Dict[str, Any]]:
    """读取手动覆盖。过期则删除并返回 None。"""
    rec = _read_storage(session_key)
    if rec is None:
        return None
    expires_at_str = rec.get("expires_at")
    if expires_at_str:
        try:
            expires_at = datetime.fromisoformat(expires_at_str)
            if datetime.now() > expires_at:
                _delete_storage(session_key)
                return None
        except Exception:
            pass
    return rec


def set_override(
    session_key: str,
    *,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    set_by: str,
    set_by_nick: str,
) -> None:
    """设置手动覆盖。如果只设其中一个，另一个保留原值（不存在则为 None）。"""
    existing = _read_storage(session_key) or {}
    now = datetime.now()
    rec = {
        "temperature": temperature if temperature is not None else existing.get("temperature"),
        "top_p": top_p if top_p is not None else existing.get("top_p"),
        "set_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "set_by": set_by,
        "set_by_nick": set_by_nick,
        "expires_at": (now + timedelta(hours=_DEFAULT_TTL_HOURS)).isoformat(),
    }
    _write_storage(session_key, rec)


def reset_override(session_key: str, what: str = "all") -> None:
    """清除手动覆盖。what: all | temperature | top_p"""
    if what == "all":
        _delete_storage(session_key)
        return
    existing = _read_storage(session_key)
    if not existing:
        return
    if what == "temperature":
        existing["temperature"] = None
    elif what == "top_p":
        existing["top_p"] = None
    if existing.get("temperature") is None and existing.get("top_p") is None:
        _delete_storage(session_key)
    else:
        _write_storage(session_key, existing)


def validate_temperature(value: Any) -> Tuple[bool, Optional[str]]:
    try:
        t = float(value)
    except (TypeError, ValueError):
        return False, "温度必须是 0.0-2.0 之间的数字"
    if t < 0.0 or t > 2.0:
        return False, f"温度必须是 0.0-2.0 之间的数字（当前: {t} 超界）"
    return True, None


def validate_top_p(value: Any) -> Tuple[bool, Optional[str]]:
    try:
        p = float(value)
    except (TypeError, ValueError):
        return False, "top_p 必须是 (0.0, 1.0] 之间的数字"
    if p <= 0.0 or p > 1.0:
        return False, f"top_p 必须是 (0.0, 1.0] 之间的数字（当前: {p}）"
    return True, None
