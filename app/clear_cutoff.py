# app/clear_cutoff.py
# -*- coding: utf-8 -*-
"""按 (BOT_ID, session_key) 记录"上下文起始时间戳"。

当用户在群里发 /clear，记录当前北京时间为 cutoff。
该 bot 后续调用模型时，历史读取在 format_history_with_meta 层过滤掉
timestamp <= cutoff 的消息——其他 agent 不受影响。

不删 DB 行，只过滤。需要硬清可走 /clear hard（未实现）。

Schema: data/clear_cutoff/{BOT_ID}__{session_key}.json
  {"cutoff_at": "2026-05-18 14:30:15", "set_by": "stafffx_xxx", "set_by_nick": "张三"}
"""
import os
import json
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any

from app.config import BOT_ID


CUTOFF_DIR = "data/clear_cutoff"
os.makedirs(CUTOFF_DIR, exist_ok=True)

_BEIJING = timezone(timedelta(hours=8))


def _file_path(session_key: str) -> str:
    safe = session_key.replace("/", "_").replace(":", "_")
    return os.path.join(CUTOFF_DIR, f"{BOT_ID}__{safe}.json")


def get_cutoff(session_key: str) -> Optional[str]:
    """返回 cutoff 时间戳字符串（"%Y-%m-%d %H:%M:%S"），无则 None。"""
    path = _file_path(session_key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("cutoff_at")
    except Exception as e:
        print(f"⚠️ [clear_cutoff] 读失败 {path}: {e}")
        return None


def get_cutoff_record(session_key: str) -> Optional[Dict[str, Any]]:
    """返回完整记录（含 set_by 等）"""
    path = _file_path(session_key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def set_cutoff(session_key: str, *, set_by: str, set_by_nick: str) -> str:
    """记录当前北京时间为 cutoff，返回该时间戳字符串。

    同时清除 Responses API 的 previous_response_id：/clear 后服务端不应再
    续接旧 thinking 链，否则会把已软清空的历史重新带回上下文。
    """
    now = datetime.now(_BEIJING).strftime("%Y-%m-%d %H:%M:%S")
    data = {"cutoff_at": now, "set_by": set_by, "set_by_nick": set_by_nick}
    path = _file_path(session_key)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ [clear_cutoff] 写失败 {path}: {e}")

    try:
        from app.responses_state import clear_response_id
        clear_response_id(session_key)
    except Exception as e:
        print(f"⚠️ [clear_cutoff] clear_response_id 失败: {e}")

    return now


def reset_cutoff(session_key: str) -> None:
    """删除 cutoff（恢复看到全部历史）。

    同时清除 previous_response_id：/resume 后本地历史恢复到 /clear 之前，但服务端
    的 response_id 只记得 cutoff 后的子集——如果继续用旧 id，server 会丢失 resume
    带回来的旧消息。强制下一轮走全量历史，状态对齐。
    """
    path = _file_path(session_key)
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception as e:
            print(f"⚠️ [clear_cutoff] 删失败 {path}: {e}")

    try:
        from app.responses_state import clear_response_id
        clear_response_id(session_key)
    except Exception as e:
        print(f"⚠️ [clear_cutoff] clear_response_id 失败: {e}")
