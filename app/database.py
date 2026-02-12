# -*- coding: utf-8 -*-
"""
数据库连接模块
- Redis: 缓存层 (对话历史缓存、会话锁、AccessToken)
- MySQL: 持久层 (对话历史持久化、用户配置)
"""
import os
import json
import time
import redis
import pymysql
from contextlib import contextmanager
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv

load_dotenv()

# Redis 配置
REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", 36379))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")
REDIS_DB = int(os.getenv("REDIS_DB", 0))

# MySQL 配置
MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", 33306))
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "dingtalk_gemini")

# Redis 键前缀
REDIS_PREFIX = "dingtalk_gemini:"
HISTORY_KEY_PREFIX = f"{REDIS_PREFIX}history:"
LOCK_KEY_PREFIX = f"{REDIS_PREFIX}lock:"
TOKEN_KEY = f"{REDIS_PREFIX}access_token"

# 配置常量
HISTORY_TTL = 3600 * 24 * 7  # 7 天过期
LOCK_TTL = 60  # 锁超时 60 秒


class RedisClient:
    """Redis 客户端单例"""
    _instance: Optional[redis.Redis] = None

    @classmethod
    def get_instance(cls) -> redis.Redis:
        if cls._instance is None:
            cls._instance = redis.Redis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                password=REDIS_PASSWORD if REDIS_PASSWORD else None,
                db=REDIS_DB,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5
            )
            # 测试连接
            try:
                cls._instance.ping()
                print(f"✅ Redis 连接成功: {REDIS_HOST}:{REDIS_PORT}")
            except redis.ConnectionError as e:
                print(f"⚠️ Redis 连接失败: {e}，将使用降级方案")
                cls._instance = None
        return cls._instance


class MySQLClient:
    """MySQL 连接管理"""

    @staticmethod
    @contextmanager
    def get_connection():
        """获取 MySQL 连接 (上下文管理器)"""
        conn = None
        try:
            conn = pymysql.connect(
                host=MYSQL_HOST,
                port=MYSQL_PORT,
                user=MYSQL_USER,
                password=MYSQL_PASSWORD,
                database=MYSQL_DATABASE,
                charset='utf8mb4',
                cursorclass=pymysql.cursors.DictCursor,
                connect_timeout=5,
                read_timeout=30,
                write_timeout=30
            )
            yield conn
        finally:
            if conn:
                conn.close()

    @staticmethod
    def init_database():
        """初始化数据库表结构"""
        try:
            # 先创建数据库 (如果不存在)
            conn = pymysql.connect(
                host=MYSQL_HOST,
                port=MYSQL_PORT,
                user=MYSQL_USER,
                password=MYSQL_PASSWORD,
                charset='utf8mb4'
            )
            with conn.cursor() as cursor:
                cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{MYSQL_DATABASE}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            conn.close()

            # 创建表
            with MySQLClient.get_connection() as conn:
                with conn.cursor() as cursor:
                    # 对话历史表
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS `conversation_history` (
                            `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
                            `session_key` VARCHAR(255) NOT NULL,
                            `role` ENUM('user', 'assistant', 'system') NOT NULL,
                            `content` MEDIUMTEXT NOT NULL,
                            `sender_nick` VARCHAR(255) DEFAULT NULL,
                            `bot_id` VARCHAR(50) DEFAULT NULL,
                            `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            INDEX `idx_session_key` (`session_key`),
                            INDEX `idx_created_at` (`created_at`)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                    """)

                    # 兼容旧表：自动添加 bot_id 列（如果不存在）
                    try:
                        cursor.execute("""
                            ALTER TABLE `conversation_history`
                            ADD COLUMN `bot_id` VARCHAR(50) DEFAULT NULL AFTER `sender_nick`
                        """)
                        print("✅ 已为 conversation_history 表添加 bot_id 列")
                    except Exception:
                        pass  # 列已存在，忽略

                    # 用户配置表
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS `user_config` (
                            `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
                            `user_id` VARCHAR(255) NOT NULL UNIQUE,
                            `config` JSON,
                            `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            `updated_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                    """)

                    # 使用统计表
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS `usage_stats` (
                            `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
                            `session_key` VARCHAR(255) NOT NULL,
                            `user_id` VARCHAR(255),
                            `model` VARCHAR(100),
                            `input_tokens` INT DEFAULT 0,
                            `output_tokens` INT DEFAULT 0,
                            `latency_ms` INT DEFAULT 0,
                            `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            INDEX `idx_session_key` (`session_key`),
                            INDEX `idx_created_at` (`created_at`)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                    """)

                conn.commit()
            print(f"✅ MySQL 数据库初始化成功: {MYSQL_DATABASE}")
            return True
        except Exception as e:
            print(f"⚠️ MySQL 初始化失败: {e}，将使用降级方案")
            return False


class DistributedLock:
    """基于 Redis 的分布式锁"""

    def __init__(self, lock_name: str, ttl: int = LOCK_TTL):
        self.lock_name = lock_name
        self.lock_key = f"{LOCK_KEY_PREFIX}{lock_name}"
        self.ttl = ttl
        self.redis = RedisClient.get_instance()

    def acquire(self, blocking: bool = True, timeout: int = 10) -> bool:
        """获取锁"""
        if not self.redis:
            return True  # 降级：无 Redis 时直接返回成功

        start_time = time.time()
        while True:
            if self.redis.set(self.lock_key, "1", nx=True, ex=self.ttl):
                return True

            if not blocking:
                return False

            if time.time() - start_time > timeout:
                return False

            time.sleep(0.1)

    def release(self):
        """释放锁"""
        if self.redis:
            self.redis.delete(self.lock_key)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


class HistoryStorage:
    """对话历史存储 (Redis 缓存 + MySQL 持久化)"""

    def __init__(self):
        self.redis = RedisClient.get_instance()

    def _get_cache_key(self, session_key: str) -> str:
        return f"{HISTORY_KEY_PREFIX}{session_key}"

    def _check_duplicate_user_message(
        self,
        session_key: str,
        content: str,
        bot_id: Optional[str]
    ) -> Optional[Dict]:
        """
        检查用户消息是否在最近 5 秒内已被保存（去重）

        Args:
            session_key: 会话键
            content: 消息内容
            bot_id: 当前机器人 ID

        Returns:
            如果找到重复消息，返回 {"id": int, "bot_id": str}，否则返回 None
        """
        try:
            with MySQLClient.get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        SELECT id, bot_id
                        FROM conversation_history
                        WHERE session_key = %s
                          AND role = 'user'
                          AND content = %s
                          AND created_at > NOW() - INTERVAL 5 SECOND
                        ORDER BY created_at DESC
                        LIMIT 1
                    """, (session_key, content))
                    row = cursor.fetchone()

                    if row:
                        existing_id = row["id"]
                        existing_bot_id = row["bot_id"]

                        # 如果当前 bot_id 已经在列表中，跳过
                        if existing_bot_id and bot_id:
                            existing_bots = set(existing_bot_id.split(','))
                            if bot_id in existing_bots:
                                return {"id": existing_id, "bot_id": existing_bot_id}

                            # 合并 bot_id
                            existing_bots.add(bot_id)
                            merged_bot_id = ','.join(sorted(existing_bots))

                            # 更新数据库中的 bot_id
                            cursor.execute("""
                                UPDATE conversation_history
                                SET bot_id = %s
                                WHERE id = %s
                            """, (merged_bot_id, existing_id))
                            conn.commit()

                            return {"id": existing_id, "bot_id": merged_bot_id}

                        return {"id": existing_id, "bot_id": existing_bot_id}

                    return None
        except Exception as e:
            print(f"⚠️ 检查重复消息失败: {e}")
            return None

    def get_history(self, session_key: str, limit: int = 50) -> List[Dict[str, str]]:
        """
        获取对话历史
        优先从 Redis 读取，缓存未命中时从 MySQL 读取并回填缓存
        """
        cache_key = self._get_cache_key(session_key)

        # 1. 尝试从 Redis 读取
        if self.redis:
            try:
                cached = self.redis.get(cache_key)
                if cached:
                    messages = json.loads(cached)
                    return messages[-limit:] if len(messages) > limit else messages
            except Exception as e:
                print(f"⚠️ Redis 读取失败: {e}")

        # 2. 从 MySQL 读取
        try:
            with MySQLClient.get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        SELECT role, content, sender_nick, bot_id, created_at
                        FROM conversation_history
                        WHERE session_key = %s
                        ORDER BY created_at DESC
                        LIMIT %s
                    """, (session_key, limit * 2))  # 多取一些，因为要倒序
                    rows = cursor.fetchall()

            if not rows:
                return []

            # 转换为消息格式 (需要反转顺序)
            messages = []
            for row in reversed(rows):
                msg = {
                    "role": row["role"],
                    "content": row["content"],
                    "timestamp": row["created_at"].strftime("%Y-%m-%d %H:%M:%S") if row.get("created_at") else None,
                    "sender_nick": row.get("sender_nick"),
                    "bot_id": row.get("bot_id")
                }
                messages.append(msg)

            # 3. 回填 Redis 缓存
            if self.redis and messages:
                try:
                    self.redis.set(cache_key, json.dumps(messages, ensure_ascii=False), ex=HISTORY_TTL)
                except Exception as e:
                    print(f"⚠️ Redis 回填失败: {e}")

            return messages[-limit:] if len(messages) > limit else messages

        except Exception as e:
            print(f"⚠️ MySQL 读取失败: {e}")
            return []

    def add_message(
        self,
        session_key: str,
        role: str,
        content: str,
        sender_nick: Optional[str] = None,
        bot_id: Optional[str] = None
    ):
        """添加消息到历史"""
        cache_key = self._get_cache_key(session_key)

        # 1. 用户消息去重逻辑（避免同时 @ 多个机器人时重复保存）
        if role == "user":
            duplicate = self._check_duplicate_user_message(session_key, content, bot_id)
            if duplicate:
                print(f"🔄 [去重] 用户消息已存在，合并 bot_id: {duplicate['bot_id']} + {bot_id}")
                return  # 消息已存在，跳过保存

        # 2. 写入 MySQL
        try:
            with MySQLClient.get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO conversation_history (session_key, role, content, sender_nick, bot_id)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (session_key, role, content, sender_nick, bot_id))
                conn.commit()
        except Exception as e:
            print(f"⚠️ MySQL 写入失败: {e}")

        # 2. 更新 Redis 缓存
        if self.redis:
            try:
                # 先获取现有缓存
                cached = self.redis.get(cache_key)
                messages = json.loads(cached) if cached else []

                # 追加新消息（包含时间戳）
                from datetime import datetime, timezone, timedelta
                # 使用北京时间 (UTC+8)
                beijing_tz = timezone(timedelta(hours=8))
                timestamp = datetime.now(beijing_tz).strftime("%Y-%m-%d %H:%M:%S")

                messages.append({
                    "role": role,
                    "content": content,
                    "timestamp": timestamp,
                    "sender_nick": sender_nick,
                    "bot_id": bot_id
                })

                # 限制缓存大小
                if len(messages) > 200:
                    messages = messages[-200:]

                self.redis.set(cache_key, json.dumps(messages, ensure_ascii=False), ex=HISTORY_TTL)
            except Exception as e:
                print(f"⚠️ Redis 更新失败: {e}")

    def clear_history(self, session_key: str):
        """清空对话历史"""
        cache_key = self._get_cache_key(session_key)

        # 1. 清空 Redis 缓存
        if self.redis:
            try:
                self.redis.delete(cache_key)
            except Exception as e:
                print(f"⚠️ Redis 删除失败: {e}")

        # 2. 删除 MySQL 记录 (软删除或标记)
        try:
            with MySQLClient.get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        DELETE FROM conversation_history WHERE session_key = %s
                    """, (session_key,))
                conn.commit()
        except Exception as e:
            print(f"⚠️ MySQL 删除失败: {e}")


class UsageStats:
    """使用统计"""

    @staticmethod
    def record(
        session_key: str,
        user_id: Optional[str],
        model: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int
    ):
        """记录使用统计"""
        try:
            with MySQLClient.get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO usage_stats
                        (session_key, user_id, model, input_tokens, output_tokens, latency_ms)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (session_key, user_id, model, input_tokens, output_tokens, latency_ms))
                conn.commit()
        except Exception as e:
            print(f"⚠️ 记录使用统计失败: {e}")

    @staticmethod
    def get_user_stats(user_id: str, days: int = 7) -> Dict[str, Any]:
        """获取用户统计 (最近 N 天)"""
        try:
            with MySQLClient.get_connection() as conn:
                with conn.cursor() as cursor:
                    # 基础统计
                    cursor.execute("""
                        SELECT
                            COUNT(*) as total_requests,
                            COALESCE(SUM(input_tokens), 0) as total_input_tokens,
                            COALESCE(SUM(output_tokens), 0) as total_output_tokens,
                            COALESCE(AVG(latency_ms), 0) as avg_latency_ms
                        FROM usage_stats
                        WHERE user_id = %s
                        AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
                    """, (user_id, days))
                    row = cursor.fetchone()

                    # 按模型分组统计 (用于计算费用)
                    cursor.execute("""
                        SELECT model,
                            COALESCE(SUM(input_tokens), 0) as input_tokens,
                            COALESCE(SUM(output_tokens), 0) as output_tokens
                        FROM usage_stats
                        WHERE user_id = %s
                        AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
                        GROUP BY model
                    """, (user_id, days))
                    model_usage = cursor.fetchall()

                    result = row if row else {}
                    result['model_usage'] = model_usage
                    return result
        except Exception as e:
            print(f"⚠️ 获取用户统计失败: {e}")
            return {}

    @staticmethod
    def get_session_stats(session_key: str, days: int = 7) -> Dict[str, Any]:
        """获取会话统计 (最近 N 天)"""
        try:
            with MySQLClient.get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        SELECT
                            COUNT(*) as total_requests,
                            COALESCE(SUM(input_tokens), 0) as total_input_tokens,
                            COALESCE(SUM(output_tokens), 0) as total_output_tokens,
                            COALESCE(AVG(latency_ms), 0) as avg_latency_ms,
                            COUNT(DISTINCT user_id) as unique_users
                        FROM usage_stats
                        WHERE session_key = %s
                        AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
                    """, (session_key, days))
                    row = cursor.fetchone()
                    return row if row else {}
        except Exception as e:
            print(f"⚠️ 获取会话统计失败: {e}")
            return {}

    @staticmethod
    def get_global_stats(days: int = 7) -> Dict[str, Any]:
        """获取全局统计 (最近 N 天)"""
        try:
            with MySQLClient.get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        SELECT
                            COUNT(*) as total_requests,
                            COALESCE(SUM(input_tokens), 0) as total_input_tokens,
                            COALESCE(SUM(output_tokens), 0) as total_output_tokens,
                            COALESCE(AVG(latency_ms), 0) as avg_latency_ms,
                            COUNT(DISTINCT user_id) as unique_users,
                            COUNT(DISTINCT session_key) as unique_sessions
                        FROM usage_stats
                        WHERE created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
                    """, (days,))
                    row = cursor.fetchone()

                    # 获取模型分布 (包含 token 用量用于计算费用)
                    cursor.execute("""
                        SELECT model,
                            COUNT(*) as count,
                            COALESCE(SUM(input_tokens), 0) as input_tokens,
                            COALESCE(SUM(output_tokens), 0) as output_tokens
                        FROM usage_stats
                        WHERE created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
                        GROUP BY model
                        ORDER BY count DESC
                    """, (days,))
                    models = cursor.fetchall()

                    result = row if row else {}
                    result['model_distribution'] = models
                    return result
        except Exception as e:
            print(f"⚠️ 获取全局统计失败: {e}")
            return {}


class TokenCache:

    def __init__(self):
        self.redis = RedisClient.get_instance()
        self._memory_cache = {}  # 降级方案：内存缓存

    def get(self, key: str = "default") -> Optional[str]:
        """获取 Token"""
        cache_key = f"{TOKEN_KEY}:{key}"

        if self.redis:
            try:
                return self.redis.get(cache_key)
            except Exception as e:
                print(f"⚠️ Redis 读取 Token 失败: {e}")

        # 降级：使用内存缓存
        item = self._memory_cache.get(cache_key)
        if item and item["expires_at"] > time.time():
            return item["token"]
        return None

    def set(self, token: str, ttl: int, key: str = "default"):
        """设置 Token"""
        cache_key = f"{TOKEN_KEY}:{key}"

        if self.redis:
            try:
                self.redis.set(cache_key, token, ex=ttl)
                return
            except Exception as e:
                print(f"⚠️ Redis 写入 Token 失败: {e}")

        # 降级：使用内存缓存
        self._memory_cache[cache_key] = {
            "token": token,
            "expires_at": time.time() + ttl
        }


# 全局实例
history_storage = HistoryStorage()
token_cache = TokenCache()
usage_stats = UsageStats()


def init_database():
    """初始化数据库"""
    # 测试 Redis 连接
    RedisClient.get_instance()

    # 初始化 MySQL 表结构
    MySQLClient.init_database()
