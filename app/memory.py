# -*- coding: utf-8 -*-
"""
Deprecated（2026-11-12）：Redis / MySQL / 文件对话历史读写，仅保留旧路径兼容。
钉钉会话改用 app.dws_context.fetch_dws_context_bundle(dws_only=True)。
get_session_key 仍用于当前钉钉会话、统计及配置隔离，不属于弃用逻辑。
"""
import os
import json
import time
import threading
from typing import Any, List, Dict, Optional

from app.config import MAX_STORAGE_LENGTH, HISTORY_TTL, BOT_ID
from app.database import _normalize_dedup_text, _parse_beijing_epoch

# 尝试导入数据库模块
try:
    from app.database import history_storage, DistributedLock, init_database
    USE_DATABASE = True
    print("✅ 使用 Redis + MySQL 数据层")
except Exception as e:
    USE_DATABASE = False
    print(f"⚠️ 数据库不可用，降级到文件存储: {e}")

# 文件存储降级方案
DATA_DIR = "data/history"
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

# 文件锁管理 (降级方案)
_file_locks = {}
_global_lock = threading.Lock()


def _get_file_lock(session_key: str) -> threading.Lock:
    """获取或创建会话专用的文件锁"""
    with _global_lock:
        if session_key not in _file_locks:
            _file_locks[session_key] = threading.Lock()
        return _file_locks[session_key]


def get_session_key(conversation_id: str, sender_id: str = None, platform: str = "dingtalk") -> str:
    """
    获取会话键 (只用 conversation_id，实现群聊上下文共享)

    Args:
        conversation_id: 会话 ID
        sender_id: 发送者 ID (保留参数兼容性)
        platform: 平台标识 (dingtalk | wecom)

    Returns:
        带平台前缀的会话键，避免不同平台会话冲突
    """
    # 如果 conversation_id 已经带平台前缀，直接返回
    if conversation_id.startswith("dingtalk_") or conversation_id.startswith("wecom_"):
        return conversation_id

    # 否则添加平台前缀
    return f"{platform}_{conversation_id}"


def _get_file_path(session_key: str) -> str:
    """获取历史文件路径"""
    safe_key = "".join([c for c in session_key if c.isalnum() or c in ('-', '_')])
    return os.path.join(DATA_DIR, f"{safe_key}.json")


def get_history(session_key: str, limit: int = 50) -> List[Dict[str, str]]:
    """
    Deprecated：读取旧数据库 / 文件对话历史；钉钉会话改用 dws。

    Args:
        session_key: 会话键
        limit: 返回的最大消息条数

    Returns:
        消息列表 [{"role": "user/assistant", "content": "..."}]
    """
    if USE_DATABASE:
        try:
            return history_storage.get_history(session_key, limit)
        except Exception as e:
            print(f"⚠️ 数据库读取失败，降级到文件: {e}")

    # 降级：文件存储
    file_path = _get_file_path(session_key)
    if not os.path.exists(file_path):
        return []

    lock = _get_file_lock(session_key)
    with lock:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # 检查过期
            if time.time() - data.get("last_active", 0) > HISTORY_TTL:
                os.remove(file_path)
                return []

            messages = data.get("messages", [])
            return messages[-limit:] if len(messages) > limit else messages
        except Exception as e:
            print(f"⚠️ 读取历史记录失败: {e}")
            return []


def update_history(
    session_key: str,
    user_msg: Optional[str],
    assistant_msg: Optional[str] = None,
    sender_nick: Optional[str] = None,
    reasoning_details: Optional[list] = None,
    user_msg_id: Optional[str] = None,
    extra_user_msg_ids: Optional[List[str]] = None,
):
    """
    Deprecated：写入旧数据库 / 文件对话历史；钉钉主链路不再入库。

    Args:
        session_key: 会话键
        user_msg: 用户消息 (可选)
        assistant_msg: AI 回复 (可选)
        sender_nick: 发送者昵称 (可选)
        reasoning_details: Anthropic thinking blocks (可选，用于多轮推理)
        user_msg_id: 主触发消息的钉钉 message_id (可选，用于跨容器幂等去重)
        extra_user_msg_ids: 2s 缓冲窗口合并进本轮的其余 message_id 列表 (可选)
    """
    ext_id = (user_msg_id or "").strip() or None
    extra_ids = [str(m).strip() for m in (extra_user_msg_ids or []) if m and str(m).strip()]

    if USE_DATABASE:
        try:
            if extra_ids:
                history_storage.mark_consumed_message_ids(session_key, extra_ids)
            if user_msg:
                # 只保存纯文本内容，不拼接昵称（读取时会拼接）
                # 仅当 ext_id 非空时传递关键字参数，兼容既有单测精确断言签名
                if ext_id:
                    history_storage.add_message(
                        session_key,
                        "user",
                        user_msg,
                        sender_nick,
                        bot_id=BOT_ID,
                        external_msg_id=ext_id,
                    )
                else:
                    history_storage.add_message(session_key, "user", user_msg, sender_nick, bot_id=BOT_ID)
            if assistant_msg:
                history_storage.add_message(session_key, "assistant", assistant_msg, bot_id=BOT_ID,
                                             thinking_blocks=reasoning_details)
            return
        except Exception as e:
            print(f"⚠️ 数据库写入失败，降级到文件: {e}")

    # 降级：文件存储
    from datetime import datetime, timezone, timedelta
    beijing_tz = timezone(timedelta(hours=8))
    timestamp = datetime.now(beijing_tz).strftime("%Y-%m-%d %H:%M:%S")

    file_path = _get_file_path(session_key)
    lock = _get_file_lock(session_key)

    with lock:
        history = []
        consumed_msg_ids: List[str] = []
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    history = data.get("messages", [])
                    consumed_msg_ids = [str(x) for x in data.get("consumed_msg_ids", []) if x]
            except Exception:
                pass

        if extra_ids:
            seen_consumed = set(consumed_msg_ids)
            for mid in extra_ids:
                if mid not in seen_consumed:
                    consumed_msg_ids.append(mid)
                    seen_consumed.add(mid)
            if len(consumed_msg_ids) > 400:
                consumed_msg_ids = consumed_msg_ids[-400:]

        # 记录用户消息
        if user_msg:
            user_entry: Dict[str, Any] = {
                "role": "user",
                "content": user_msg,
                "timestamp": timestamp,
                "sender_nick": sender_nick,
            }
            if ext_id:
                user_entry["external_msg_id"] = ext_id
            history.append(user_entry)

        # 记录 AI 回复
        if assistant_msg:
            entry: Dict[str, Any] = {
                "role": "assistant",
                "content": assistant_msg,
                "timestamp": timestamp,
                "sender_nick": None,
                "bot_id": BOT_ID,
            }
            if reasoning_details:
                entry["reasoning_details"] = reasoning_details
            history.append(entry)

        # 保持存储长度限制
        if len(history) > MAX_STORAGE_LENGTH:
            history = history[-MAX_STORAGE_LENGTH:]

        payload: Dict[str, Any] = {
            "messages": history,
            "last_active": time.time(),
        }
        if consumed_msg_ids:
            payload["consumed_msg_ids"] = consumed_msg_ids

        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️ 写入历史记录失败: {e}")


def persist_external_messages(session_key: str, messages: List[Dict[str, Any]]) -> int:
    """Deprecated：旧 dws 消息入库兼容入口；钉钉现直接读取 dws，不再保存副本。"""
    if not session_key or not messages:
        return 0

    if USE_DATABASE:
        try:
            return history_storage.upsert_external_messages(session_key, messages)
        except Exception as e:
            print(f"⚠️ 数据库写入外部群聊消息失败，降级到文件: {e}")

    file_path = _get_file_path(session_key)
    lock = _get_file_lock(session_key)

    with lock:
        history: List[Dict[str, Any]] = []
        consumed_msg_ids: List[str] = []
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if time.time() - data.get("last_active", 0) <= HISTORY_TTL:
                    history = data.get("messages", [])
                    consumed_msg_ids = [str(x) for x in data.get("consumed_msg_ids", []) if x]
            except Exception:
                history = []
                consumed_msg_ids = []

        existing_ids = set(consumed_msg_ids)
        for msg in history:
            mid = str(msg.get("external_msg_id") or "").strip()
            if mid:
                existing_ids.add(mid)

        inserted = 0
        backfilled = False
        for item in messages:
            ext_id = str(item.get("external_msg_id") or "").strip()
            content = str(item.get("content") or "").strip()
            ts_str = str(item.get("timestamp") or "").strip()[:19]
            if not ext_id or not content or not ts_str or ext_id in existing_ids:
                continue

            sender_nick = str(item.get("sender_nick") or "").strip() or None
            cand_norm = _normalize_dedup_text(content)
            cand_epoch = _parse_beijing_epoch(ts_str)
            matched_legacy = False

            if cand_norm:
                for msg in history:
                    if msg.get("role") != "user":
                        continue
                    msg_sender = str(msg.get("sender_nick") or "").strip()
                    if sender_nick and msg_sender and sender_nick != msg_sender:
                        continue
                    msg_epoch = _parse_beijing_epoch(msg.get("timestamp"))
                    if cand_epoch is not None and msg_epoch is not None:
                        delta = msg_epoch - cand_epoch
                        if delta < -20.0 or delta > 300.0:
                            continue
                    raw_msg_content = str(msg.get("content") or "")
                    norm_lines = [
                        _normalize_dedup_text(line)
                        for line in raw_msg_content.splitlines()
                        if line.strip()
                    ]
                    if cand_norm == _normalize_dedup_text(raw_msg_content) or cand_norm in norm_lines:
                        matched_legacy = True
                        if not msg.get("external_msg_id"):
                            msg["external_msg_id"] = ext_id
                            backfilled = True
                        existing_ids.add(ext_id)
                        break

            if matched_legacy:
                continue

            history.append({
                "role": "user",
                "content": content,
                "timestamp": ts_str,
                "sender_nick": sender_nick,
                "bot_id": None,
                "external_msg_id": ext_id,
            })
            existing_ids.add(ext_id)
            inserted += 1

        if inserted == 0 and not backfilled:
            return 0

        # Python Timsort 稳定排序：相同 timestamp 的问答对严格保持原有先后顺序
        history.sort(key=lambda m: str(m.get("timestamp") or ""))
        if len(history) > MAX_STORAGE_LENGTH:
            history = history[-MAX_STORAGE_LENGTH:]

        payload: Dict[str, Any] = {
            "messages": history,
            "last_active": time.time(),
        }
        if consumed_msg_ids:
            payload["consumed_msg_ids"] = consumed_msg_ids

        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️ 写入外部群聊历史文件失败: {e}")

        return inserted


def clear_history(session_key: str):
    """Deprecated：删除旧存储历史；当前钉钉 /clear 使用 clear_cutoff.set_cutoff 软清空。"""
    if USE_DATABASE:
        try:
            history_storage.clear_history(session_key)
            return
        except Exception as e:
            print(f"⚠️ 数据库删除失败，降级到文件: {e}")

    # 降级：文件存储
    file_path = _get_file_path(session_key)
    lock = _get_file_lock(session_key)

    with lock:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                print(f"⚠️ 删除历史记录失败: {e}")
