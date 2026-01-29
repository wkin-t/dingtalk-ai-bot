# -*- coding: utf-8 -*-
"""
对话历史管理模块
使用 Redis + MySQL 数据层，支持降级到文件存储
"""
import os
import json
import time
import threading
from typing import List, Dict, Optional

from app.config import MAX_STORAGE_LENGTH, HISTORY_TTL

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


def get_session_key(conversation_id: str, sender_id: str = None) -> str:
    """获取会话键 (只用 conversation_id，实现群聊上下文共享)"""
    return conversation_id


def _get_file_path(session_key: str) -> str:
    """获取历史文件路径"""
    safe_key = "".join([c for c in session_key if c.isalnum() or c in ('-', '_')])
    return os.path.join(DATA_DIR, f"{safe_key}.json")


def get_history(session_key: str, limit: int = 50) -> List[Dict[str, str]]:
    """
    获取对话历史

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
    sender_nick: Optional[str] = None
):
    """
    更新对话历史

    Args:
        session_key: 会话键
        user_msg: 用户消息 (可选)
        assistant_msg: AI 回复 (可选)
        sender_nick: 发送者昵称 (可选)
    """
    if USE_DATABASE:
        try:
            if user_msg:
                content = f"{sender_nick}: {user_msg}" if sender_nick else user_msg
                history_storage.add_message(session_key, "user", content, sender_nick)
            if assistant_msg:
                history_storage.add_message(session_key, "assistant", assistant_msg)
            return
        except Exception as e:
            print(f"⚠️ 数据库写入失败，降级到文件: {e}")

    # 降级：文件存储
    file_path = _get_file_path(session_key)
    lock = _get_file_lock(session_key)

    with lock:
        # 读取现有记录
        history = []
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    history = data.get("messages", [])
            except:
                pass

        # 记录用户消息
        if user_msg:
            content = f"{sender_nick}: {user_msg}" if sender_nick else user_msg
            history.append({"role": "user", "content": content})

        # 记录 AI 回复
        if assistant_msg:
            history.append({"role": "assistant", "content": assistant_msg})

        # 保持存储长度限制
        if len(history) > MAX_STORAGE_LENGTH:
            history = history[-MAX_STORAGE_LENGTH:]

        # 写入文件
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump({
                    "messages": history,
                    "last_active": time.time()
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️ 写入历史记录失败: {e}")


def clear_history(session_key: str):
    """清空对话历史"""
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
