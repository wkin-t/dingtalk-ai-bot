# -*- coding: utf-8 -*-
"""
数据库模块单元测试

测试目标:
1. HistoryStorage.add_message() bot_id 参数 (MySQL + Redis 路径)
2. HistoryStorage.get_history() 返回 bot_id 字段 (Redis 缓存 + MySQL 路径)
3. HistoryStorage.clear_history()
4. DistributedLock 基础行为
5. TokenCache 基础行为
6. MySQLClient.init_database() bot_id 列迁移
7. UsageStats 基础行为
"""
import pytest
import json
import time
from unittest.mock import patch, MagicMock, PropertyMock, call
from contextlib import contextmanager
from datetime import datetime


# ─── HistoryStorage 测试 ──────────────────────────────────────────


class TestHistoryStorageAddMessage:
    """HistoryStorage.add_message() 测试"""

    @pytest.fixture(autouse=True)
    def setup(self):
        """创建 HistoryStorage 实例，mock Redis"""
        self.mock_redis = MagicMock()
        self.mock_redis.get.return_value = None  # 默认无缓存

        with patch("app.database.RedisClient.get_instance", return_value=self.mock_redis):
            from app.database import HistoryStorage
            self.storage = HistoryStorage()

    def test_add_user_message_without_bot_id(self):
        """用户消息不传 bot_id"""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cursor
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        @contextmanager
        def fake_conn():
            yield mock_conn

        with patch("app.database.MySQLClient.get_connection", side_effect=fake_conn):
            self.storage.add_message("dingtalk_s1", "user", "你好", sender_nick="张三")

        # 验证 MySQL INSERT 包含 bot_id=None
        mock_cursor.execute.assert_called_once()
        sql, params = mock_cursor.execute.call_args[0]
        assert "bot_id" in sql
        assert params == ("dingtalk_s1", "user", "你好", "张三", None)

    def test_add_assistant_message_with_bot_id(self):
        """assistant 消息应传入 bot_id"""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cursor
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        @contextmanager
        def fake_conn():
            yield mock_conn

        with patch("app.database.MySQLClient.get_connection", side_effect=fake_conn):
            self.storage.add_message("dingtalk_s1", "assistant", "AI 回复", bot_id="gemini")

        mock_cursor.execute.assert_called_once()
        _, params = mock_cursor.execute.call_args[0]
        assert params == ("dingtalk_s1", "assistant", "AI 回复", None, "gemini")

    def test_add_assistant_message_openclaw_bot_id(self):
        """OpenClaw bot_id 应正确传入"""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cursor
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        @contextmanager
        def fake_conn():
            yield mock_conn

        with patch("app.database.MySQLClient.get_connection", side_effect=fake_conn):
            self.storage.add_message("dingtalk_s1", "assistant", "Claw 回复", bot_id="openclaw")

        _, params = mock_cursor.execute.call_args[0]
        assert params[-1] == "openclaw"

    def test_add_message_updates_redis_cache_with_bot_id(self):
        """add_message 应更新 Redis 缓存，包含 bot_id"""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cursor
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        @contextmanager
        def fake_conn():
            yield mock_conn

        with patch("app.database.MySQLClient.get_connection", side_effect=fake_conn):
            self.storage.add_message("dingtalk_s1", "assistant", "回复", bot_id="gemini")

        # 验证 Redis set 被调用
        self.mock_redis.set.assert_called_once()
        cache_key, cache_value = self.mock_redis.set.call_args[0][:2]
        cached_messages = json.loads(cache_value)
        assert len(cached_messages) == 1
        assert cached_messages[0]["bot_id"] == "gemini"
        assert cached_messages[0]["role"] == "assistant"

    def test_add_message_appends_to_existing_redis_cache(self):
        """已有 Redis 缓存时应追加而非覆盖"""
        existing = json.dumps([
            {"role": "user", "content": "你好", "timestamp": "2026-02-01 10:00:00", "sender_nick": "用户", "bot_id": None}
        ])
        self.mock_redis.get.return_value = existing

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cursor
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        @contextmanager
        def fake_conn():
            yield mock_conn

        with patch("app.database.MySQLClient.get_connection", side_effect=fake_conn):
            self.storage.add_message("dingtalk_s1", "assistant", "回复", bot_id="gemini")

        cache_value = self.mock_redis.set.call_args[0][1]
        cached_messages = json.loads(cache_value)
        assert len(cached_messages) == 2
        assert cached_messages[1]["bot_id"] == "gemini"

    def test_add_message_redis_cache_size_limit(self):
        """Redis 缓存超过 200 条时应截断"""
        existing = json.dumps([
            {"role": "user", "content": f"msg{i}", "timestamp": "2026-02-01 10:00:00", "sender_nick": "用户", "bot_id": None}
            for i in range(200)
        ])
        self.mock_redis.get.return_value = existing

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cursor
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        @contextmanager
        def fake_conn():
            yield mock_conn

        with patch("app.database.MySQLClient.get_connection", side_effect=fake_conn):
            self.storage.add_message("dingtalk_s1", "assistant", "新回复", bot_id="gemini")

        cache_value = self.mock_redis.set.call_args[0][1]
        cached_messages = json.loads(cache_value)
        assert len(cached_messages) == 200  # 截断到 200

    def test_add_message_mysql_exception(self):
        """MySQL 写入异常不应抛出"""
        @contextmanager
        def fail_conn():
            raise Exception("MySQL 连接失败")
            yield  # pragma: no cover

        with patch("app.database.MySQLClient.get_connection", side_effect=fail_conn):
            # 不应抛出异常
            self.storage.add_message("dingtalk_s1", "user", "消息")

    def test_add_message_redis_exception(self):
        """Redis 更新异常不应抛出"""
        self.mock_redis.get.side_effect = Exception("Redis 异常")

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cursor
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        @contextmanager
        def fake_conn():
            yield mock_conn

        with patch("app.database.MySQLClient.get_connection", side_effect=fake_conn):
            # 不应抛出异常
            self.storage.add_message("dingtalk_s1", "user", "消息")


class TestHistoryStorageGetHistory:
    """HistoryStorage.get_history() 测试"""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.mock_redis = MagicMock()
        with patch("app.database.RedisClient.get_instance", return_value=self.mock_redis):
            from app.database import HistoryStorage
            self.storage = HistoryStorage()

    def test_get_history_from_redis_cache_with_bot_id(self):
        """Redis 缓存命中时应直接返回，包含 bot_id"""
        cached = json.dumps([
            {"role": "user", "content": "你好", "timestamp": "2026-02-01 10:00:00", "sender_nick": "用户", "bot_id": None},
            {"role": "assistant", "content": "回复", "timestamp": "2026-02-01 10:00:05", "sender_nick": None, "bot_id": "gemini"},
        ])
        self.mock_redis.get.return_value = cached

        result = self.storage.get_history("dingtalk_s1")
        assert len(result) == 2
        assert result[1]["bot_id"] == "gemini"

    def test_get_history_from_redis_with_limit(self):
        """Redis 缓存返回时应尊重 limit"""
        cached = json.dumps([
            {"role": "user", "content": f"msg{i}", "bot_id": None}
            for i in range(10)
        ])
        self.mock_redis.get.return_value = cached

        result = self.storage.get_history("dingtalk_s1", limit=3)
        assert len(result) == 3

    def test_get_history_from_mysql_with_bot_id(self):
        """Redis 未命中时从 MySQL 读取，包含 bot_id"""
        self.mock_redis.get.return_value = None

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            {"role": "assistant", "content": "回复", "sender_nick": None, "bot_id": "gemini",
             "created_at": datetime(2026, 2, 1, 10, 0, 5)},
            {"role": "user", "content": "你好", "sender_nick": "用户", "bot_id": None,
             "created_at": datetime(2026, 2, 1, 10, 0, 0)},
        ]
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cursor
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        @contextmanager
        def fake_conn():
            yield mock_conn

        with patch("app.database.MySQLClient.get_connection", side_effect=fake_conn):
            result = self.storage.get_history("dingtalk_s1")

        # 应反转顺序 (MySQL DESC -> reversed)
        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"
        assert result[1]["bot_id"] == "gemini"

    def test_get_history_mysql_backfill_redis(self):
        """MySQL 读取后应回填 Redis 缓存"""
        self.mock_redis.get.return_value = None

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            {"role": "user", "content": "你好", "sender_nick": "用户", "bot_id": None,
             "created_at": datetime(2026, 2, 1, 10, 0, 0)},
        ]
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cursor
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        @contextmanager
        def fake_conn():
            yield mock_conn

        with patch("app.database.MySQLClient.get_connection", side_effect=fake_conn):
            self.storage.get_history("dingtalk_s1")

        # 应调用 redis.set 回填
        self.mock_redis.set.assert_called_once()

    def test_get_history_mysql_empty(self):
        """MySQL 无数据时返回空列表"""
        self.mock_redis.get.return_value = None

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cursor
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        @contextmanager
        def fake_conn():
            yield mock_conn

        with patch("app.database.MySQLClient.get_connection", side_effect=fake_conn):
            result = self.storage.get_history("dingtalk_s1")

        assert result == []

    def test_get_history_mysql_exception(self):
        """MySQL 异常时返回空列表"""
        self.mock_redis.get.return_value = None

        @contextmanager
        def fail_conn():
            raise Exception("MySQL 连接失败")
            yield  # pragma: no cover

        with patch("app.database.MySQLClient.get_connection", side_effect=fail_conn):
            result = self.storage.get_history("dingtalk_s1")

        assert result == []

    def test_get_history_redis_exception_fallback_mysql(self):
        """Redis 异常时应降级到 MySQL"""
        self.mock_redis.get.side_effect = Exception("Redis 异常")

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            {"role": "user", "content": "你好", "sender_nick": "用户", "bot_id": None,
             "created_at": datetime(2026, 2, 1, 10, 0, 0)},
        ]
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cursor
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        @contextmanager
        def fake_conn():
            yield mock_conn

        with patch("app.database.MySQLClient.get_connection", side_effect=fake_conn):
            result = self.storage.get_history("dingtalk_s1")

        assert len(result) == 1

    def test_get_history_no_redis(self):
        """Redis 为 None 时应直接从 MySQL 读取"""
        self.storage.redis = None

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            {"role": "assistant", "content": "回复", "sender_nick": None, "bot_id": "openclaw",
             "created_at": datetime(2026, 2, 1, 10, 0, 5)},
        ]
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cursor
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        @contextmanager
        def fake_conn():
            yield mock_conn

        with patch("app.database.MySQLClient.get_connection", side_effect=fake_conn):
            result = self.storage.get_history("dingtalk_s1")

        assert len(result) == 1
        assert result[0]["bot_id"] == "openclaw"

    def test_get_history_redis_backfill_exception(self):
        """Redis 回填异常不应影响返回结果"""
        self.mock_redis.get.return_value = None
        self.mock_redis.set.side_effect = Exception("Redis 写入失败")

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            {"role": "user", "content": "你好", "sender_nick": "用户", "bot_id": None,
             "created_at": datetime(2026, 2, 1, 10, 0, 0)},
        ]
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cursor
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        @contextmanager
        def fake_conn():
            yield mock_conn

        with patch("app.database.MySQLClient.get_connection", side_effect=fake_conn):
            result = self.storage.get_history("dingtalk_s1")

        assert len(result) == 1  # 仍然返回数据


class TestHistoryStorageClearHistory:
    """HistoryStorage.clear_history() 测试"""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.mock_redis = MagicMock()
        with patch("app.database.RedisClient.get_instance", return_value=self.mock_redis):
            from app.database import HistoryStorage
            self.storage = HistoryStorage()

    def test_clear_history_redis_and_mysql(self):
        """clear_history 应同时清理 Redis 和 MySQL"""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cursor
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        @contextmanager
        def fake_conn():
            yield mock_conn

        with patch("app.database.MySQLClient.get_connection", side_effect=fake_conn):
            self.storage.clear_history("dingtalk_s1")

        self.mock_redis.delete.assert_called_once()
        mock_cursor.execute.assert_called_once()

    def test_clear_history_redis_exception(self):
        """Redis 异常不应影响 MySQL 清理"""
        self.mock_redis.delete.side_effect = Exception("Redis 异常")

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cursor
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        @contextmanager
        def fake_conn():
            yield mock_conn

        with patch("app.database.MySQLClient.get_connection", side_effect=fake_conn):
            self.storage.clear_history("dingtalk_s1")

        # MySQL 仍被调用
        mock_cursor.execute.assert_called_once()

    def test_clear_history_no_redis(self):
        """Redis 为 None 时只清理 MySQL"""
        self.storage.redis = None

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cursor
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        @contextmanager
        def fake_conn():
            yield mock_conn

        with patch("app.database.MySQLClient.get_connection", side_effect=fake_conn):
            self.storage.clear_history("dingtalk_s1")

        mock_cursor.execute.assert_called_once()


# ─── DistributedLock 测试 ──────────────────────────────────────────


class TestDistributedLock:
    """DistributedLock 测试"""

    def test_lock_without_redis(self):
        """无 Redis 时 acquire 应直接返回 True"""
        with patch("app.database.RedisClient.get_instance", return_value=None):
            from app.database import DistributedLock
            lock = DistributedLock("test_lock")
            assert lock.acquire() is True

    def test_lock_release_without_redis(self):
        """无 Redis 时 release 不应报错"""
        with patch("app.database.RedisClient.get_instance", return_value=None):
            from app.database import DistributedLock
            lock = DistributedLock("test_lock")
            lock.release()

    def test_lock_acquire_success(self):
        """Redis 可用时 acquire 成功"""
        mock_redis = MagicMock()
        mock_redis.set.return_value = True
        with patch("app.database.RedisClient.get_instance", return_value=mock_redis):
            from app.database import DistributedLock
            lock = DistributedLock("test_lock")
            assert lock.acquire() is True

    def test_lock_acquire_non_blocking_fail(self):
        """non-blocking acquire 失败应返回 False"""
        mock_redis = MagicMock()
        mock_redis.set.return_value = False
        with patch("app.database.RedisClient.get_instance", return_value=mock_redis):
            from app.database import DistributedLock
            lock = DistributedLock("test_lock")
            assert lock.acquire(blocking=False) is False

    def test_lock_context_manager(self):
        """with 语句应调用 acquire 和 release"""
        mock_redis = MagicMock()
        mock_redis.set.return_value = True
        with patch("app.database.RedisClient.get_instance", return_value=mock_redis):
            from app.database import DistributedLock
            lock = DistributedLock("test_lock")
            with lock:
                pass
            mock_redis.delete.assert_called_once()

    def test_lock_release_with_redis(self):
        """Redis 可用时 release 应调用 delete"""
        mock_redis = MagicMock()
        with patch("app.database.RedisClient.get_instance", return_value=mock_redis):
            from app.database import DistributedLock
            lock = DistributedLock("test_lock")
            lock.release()
            mock_redis.delete.assert_called_once()


# ─── TokenCache 测试 ──────────────────────────────────────────


class TestTokenCache:
    """TokenCache 测试"""

    def test_get_from_redis(self):
        """Redis 可用时应从 Redis 读取"""
        mock_redis = MagicMock()
        mock_redis.get.return_value = "test_token"
        with patch("app.database.RedisClient.get_instance", return_value=mock_redis):
            from app.database import TokenCache
            cache = TokenCache()
            assert cache.get("key1") == "test_token"

    def test_get_fallback_memory(self):
        """Redis 不可用时应使用内存缓存"""
        with patch("app.database.RedisClient.get_instance", return_value=None):
            from app.database import TokenCache
            cache = TokenCache()
            # 先设置
            cache.set("my_token", 3600, "key1")
            assert cache.get("key1") == "my_token"

    def test_get_expired_memory(self):
        """内存缓存过期时应返回 None"""
        with patch("app.database.RedisClient.get_instance", return_value=None):
            from app.database import TokenCache
            cache = TokenCache()
            # 手动注入过期数据
            cache._memory_cache["dingtalk_gemini:access_token:key1"] = {
                "token": "expired",
                "expires_at": time.time() - 10
            }
            assert cache.get("key1") is None

    def test_set_to_redis(self):
        """Redis 可用时应写入 Redis"""
        mock_redis = MagicMock()
        with patch("app.database.RedisClient.get_instance", return_value=mock_redis):
            from app.database import TokenCache
            cache = TokenCache()
            cache.set("token123", 3600, "key1")
            mock_redis.set.assert_called_once()

    def test_get_redis_exception(self):
        """Redis 异常时应降级到内存缓存"""
        mock_redis = MagicMock()
        mock_redis.get.side_effect = Exception("Redis 异常")
        with patch("app.database.RedisClient.get_instance", return_value=mock_redis):
            from app.database import TokenCache
            cache = TokenCache()
            # 没有内存缓存，应返回 None
            assert cache.get("key1") is None

    def test_set_redis_exception_fallback_memory(self):
        """Redis 写入异常时应降级到内存缓存"""
        mock_redis = MagicMock()
        mock_redis.set.side_effect = Exception("Redis 异常")
        with patch("app.database.RedisClient.get_instance", return_value=mock_redis):
            from app.database import TokenCache
            cache = TokenCache()
            cache.set("token123", 3600, "key1")
            # 应写入内存缓存
            assert "dingtalk_gemini:access_token:key1" in cache._memory_cache

    def test_get_no_cache_returns_none(self):
        """无缓存数据时应返回 None"""
        with patch("app.database.RedisClient.get_instance", return_value=None):
            from app.database import TokenCache
            cache = TokenCache()
            assert cache.get("nonexistent") is None


# ─── UsageStats 测试 ──────────────────────────────────────────


class TestUsageStats:
    """UsageStats 基础测试"""

    def test_record_success(self):
        """正常记录使用统计"""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cursor
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        @contextmanager
        def fake_conn():
            yield mock_conn

        with patch("app.database.MySQLClient.get_connection", side_effect=fake_conn):
            from app.database import UsageStats
            UsageStats.record("dingtalk_s1", "user1", "gemini-3-flash", 100, 200, 500)

        mock_cursor.execute.assert_called_once()
        _, params = mock_cursor.execute.call_args[0]
        assert params == ("dingtalk_s1", "user1", "gemini-3-flash", 100, 200, 500)

    def test_record_exception(self):
        """记录失败不应抛出异常"""
        @contextmanager
        def fail_conn():
            raise Exception("MySQL 异常")
            yield  # pragma: no cover

        with patch("app.database.MySQLClient.get_connection", side_effect=fail_conn):
            from app.database import UsageStats
            UsageStats.record("dingtalk_s1", "user1", "model", 0, 0, 0)

    def test_get_user_stats(self):
        """获取用户统计"""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {"total_requests": 5, "total_input_tokens": 100}
        mock_cursor.fetchall.return_value = [{"model": "gemini-3-flash", "input_tokens": 100, "output_tokens": 200}]
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cursor
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        @contextmanager
        def fake_conn():
            yield mock_conn

        with patch("app.database.MySQLClient.get_connection", side_effect=fake_conn):
            from app.database import UsageStats
            stats = UsageStats.get_user_stats("user1", days=7)

        assert stats["total_requests"] == 5

    def test_get_user_stats_exception(self):
        """获取统计失败应返回空 dict"""
        @contextmanager
        def fail_conn():
            raise Exception("MySQL 异常")
            yield  # pragma: no cover

        with patch("app.database.MySQLClient.get_connection", side_effect=fail_conn):
            from app.database import UsageStats
            stats = UsageStats.get_user_stats("user1")

        assert stats == {}

    def test_get_session_stats(self):
        """获取会话统计"""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {"total_requests": 10, "unique_users": 3}
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cursor
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        @contextmanager
        def fake_conn():
            yield mock_conn

        with patch("app.database.MySQLClient.get_connection", side_effect=fake_conn):
            from app.database import UsageStats
            stats = UsageStats.get_session_stats("dingtalk_s1")

        assert stats["unique_users"] == 3

    def test_get_session_stats_exception(self):
        """获取会话统计失败应返回空 dict"""
        @contextmanager
        def fail_conn():
            raise Exception("MySQL 异常")
            yield  # pragma: no cover

        with patch("app.database.MySQLClient.get_connection", side_effect=fail_conn):
            from app.database import UsageStats
            stats = UsageStats.get_session_stats("dingtalk_s1")

        assert stats == {}

    def test_get_global_stats(self):
        """获取全局统计"""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {"total_requests": 100, "unique_users": 10}
        mock_cursor.fetchall.return_value = [{"model": "gemini-3-flash", "count": 50, "input_tokens": 1000, "output_tokens": 2000}]
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cursor
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        @contextmanager
        def fake_conn():
            yield mock_conn

        with patch("app.database.MySQLClient.get_connection", side_effect=fake_conn):
            from app.database import UsageStats
            stats = UsageStats.get_global_stats(days=7)

        assert stats["total_requests"] == 100
        assert len(stats["model_distribution"]) == 1

    def test_get_global_stats_exception(self):
        """获取全局统计失败应返回空 dict"""
        @contextmanager
        def fail_conn():
            raise Exception("MySQL 异常")
            yield  # pragma: no cover

        with patch("app.database.MySQLClient.get_connection", side_effect=fail_conn):
            from app.database import UsageStats
            stats = UsageStats.get_global_stats()

        assert stats == {}


# ─── MySQLClient.init_database 测试 ──────────────────────────────


class TestInitDatabase:
    """MySQLClient.init_database() 测试"""

    def test_init_database_creates_tables_with_bot_id(self):
        """init_database 应创建包含 bot_id 列的表"""
        mock_init_conn = MagicMock()
        mock_init_cursor = MagicMock()
        mock_init_conn.cursor.return_value.__enter__ = lambda s: mock_init_cursor
        mock_init_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_table_conn = MagicMock()
        mock_table_cursor = MagicMock()
        mock_table_conn.cursor.return_value.__enter__ = lambda s: mock_table_cursor
        mock_table_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch("app.database.pymysql.connect", return_value=mock_init_conn):
            @contextmanager
            def fake_conn():
                yield mock_table_conn

            with patch("app.database.MySQLClient.get_connection", side_effect=fake_conn):
                from app.database import MySQLClient
                result = MySQLClient.init_database()

        assert result is True

        # 验证 CREATE TABLE 包含 bot_id
        create_calls = [c for c in mock_table_cursor.execute.call_args_list
                        if "CREATE TABLE" in str(c) and "conversation_history" in str(c)]
        assert len(create_calls) >= 1
        create_sql = create_calls[0][0][0]
        assert "bot_id" in create_sql

    def test_init_database_alter_table_existing_column(self):
        """ALTER TABLE 在列已存在时应静默忽略"""
        mock_init_conn = MagicMock()
        mock_init_cursor = MagicMock()
        mock_init_conn.cursor.return_value.__enter__ = lambda s: mock_init_cursor
        mock_init_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_table_conn = MagicMock()
        mock_table_cursor = MagicMock()
        # ALTER TABLE 抛出异常（列已存在）
        def execute_side_effect(sql, *args):
            if "ALTER TABLE" in sql:
                raise Exception("Duplicate column name 'bot_id'")
        mock_table_cursor.execute.side_effect = execute_side_effect
        mock_table_conn.cursor.return_value.__enter__ = lambda s: mock_table_cursor
        mock_table_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch("app.database.pymysql.connect", return_value=mock_init_conn):
            @contextmanager
            def fake_conn():
                yield mock_table_conn

            with patch("app.database.MySQLClient.get_connection", side_effect=fake_conn):
                from app.database import MySQLClient
                # 不应抛出异常（CREATE TABLE 也会因 side_effect 失败，所以会进入 except）
                # 这里主要测试 ALTER TABLE 异常被静默处理
                MySQLClient.init_database()

    def test_init_database_failure(self):
        """数据库初始化失败应返回 False"""
        with patch("app.database.pymysql.connect", side_effect=Exception("连接失败")):
            from app.database import MySQLClient
            result = MySQLClient.init_database()

        assert result is False
