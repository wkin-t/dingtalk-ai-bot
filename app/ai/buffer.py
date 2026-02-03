# -*- coding: utf-8 -*-
"""
消息缓冲器 - 支持多平台消息合并
"""
import asyncio
import threading
from typing import Dict, List, Any, Optional, Callable


class MessageBuffer:
    """消息缓冲器 - 支持异步和同步模式"""

    def __init__(self, buffer_time: float = 2.0):
        """
        初始化消息缓冲器

        Args:
            buffer_time: 缓冲时间 (秒)
        """
        self.buffer_time = buffer_time
        self.buffers: Dict[str, Dict[str, Any]] = {}
        self.locks: Dict[str, asyncio.Lock] = {}  # 异步锁
        self.thread_locks: Dict[str, threading.Lock] = {}  # 线程锁
        self.processing_sessions = set()

    async def add_message_async(
        self,
        session_key: str,
        content: str,
        metadata: dict,
        processor: Callable
    ):
        """
        添加消息到缓冲区 (异步模式)

        Args:
            session_key: 会话键
            content: 消息内容
            metadata: 元数据 (platform, user_id, etc.)
            processor: 消息处理函数
        """
        # 检查是否正在处理该会话 (防止并发竞态)
        if session_key in self.processing_sessions and session_key not in self.buffers:
            print(f"⏳ 会话正在处理中，将消息加入缓冲区排队: {session_key}")
            self.buffers[session_key] = {
                "content": [],
                "metadata": metadata,
                "timer": None
            }

        if session_key in self.buffers:
            # 取消已有的 timer
            existing_timer = self.buffers[session_key].get("timer")
            if existing_timer is not None:
                existing_timer.cancel()
        else:
            self.buffers[session_key] = {
                "content": [],
                "metadata": metadata
            }

        # 添加消息
        if content:
            self.buffers[session_key]["content"].append(content)

        # 更新元数据
        self.buffers[session_key]["metadata"] = metadata

        # 启动定时器
        task = asyncio.create_task(self._process_async(session_key, processor))
        self.buffers[session_key]["timer"] = task

    def add_message_sync(
        self,
        session_key: str,
        content: str,
        metadata: dict,
        processor: Callable
    ):
        """
        添加消息到缓冲区 (同步模式 - 用于企业微信)

        Args:
            session_key: 会话键
            content: 消息内容
            metadata: 元数据 (platform, user_id, etc.)
            processor: 消息处理函数 (线程安全)
        """
        if session_key not in self.buffers:
            self.buffers[session_key] = {
                "content": [],
                "metadata": metadata,
                "timer": None
            }

        # 取消已有的 timer
        if self.buffers[session_key]["timer"]:
            self.buffers[session_key]["timer"].cancel()

        # 添加消息
        if content:
            self.buffers[session_key]["content"].append(content)

        # 更新元数据
        self.buffers[session_key]["metadata"] = metadata

        # 启动 2 秒定时器
        timer = threading.Timer(
            self.buffer_time,
            self._process_sync,
            args=[session_key, processor]
        )
        timer.start()
        self.buffers[session_key]["timer"] = timer

    async def _process_async(self, session_key: str, processor: Callable):
        """异步处理缓冲消息"""
        await asyncio.sleep(self.buffer_time)

        if session_key not in self.buffers:
            return

        # 获取或创建会话锁
        if session_key not in self.locks:
            self.locks[session_key] = asyncio.Lock()

        async with self.locks[session_key]:
            # 再次检查
            if session_key not in self.buffers:
                return

            # 标记正在处理
            self.processing_sessions.add(session_key)

            try:
                data = self.buffers.pop(session_key)
                content_list = data["content"]
                metadata = data["metadata"]

                # 合并消息
                full_content = "\n".join(content_list)

                # 调用处理器
                await processor(session_key, full_content, metadata)

            finally:
                # 清除正在处理标记
                self.processing_sessions.discard(session_key)

    def _process_sync(self, session_key: str, processor: Callable):
        """同步处理缓冲消息"""
        if session_key not in self.buffers:
            return

        # 获取或创建线程锁
        if session_key not in self.thread_locks:
            self.thread_locks[session_key] = threading.Lock()

        with self.thread_locks[session_key]:
            if session_key not in self.buffers:
                return

            data = self.buffers.pop(session_key)
            content_list = data["content"]
            metadata = data["metadata"]

            # 合并消息
            full_content = "\n".join(content_list)

            # 调用处理器
            processor(session_key, full_content, metadata)
