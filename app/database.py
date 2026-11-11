# -*- coding: utf-8 -*-
"""
数据库连接模块
- Redis: 缓存层 (对话历史缓存、会话锁、AccessToken)
- MySQL: 持久层 (对话历史持久化、用户配置)
"""
import os
import re
import json
import time
import redis
import pymysql
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any, Iterable
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
CONSUMED_MIDS_KEY_PREFIX = f"{REDIS_PREFIX}consumed_mids:"
LOCK_KEY_PREFIX = f"{REDIS_PREFIX}lock:"
TOKEN_KEY = f"{REDIS_PREFIX}access_token"

# 配置常量
HISTORY_TTL = 3600 * 24 * 7  # 7 天过期
LOCK_TTL = 60  # 锁超时 60 秒

_BEIJING_TZ = timezone(timedelta(hours=8))
_DEDUP_QUOTE_PREFIX_RE = re.compile(r"^\[引用[^\]]*\]\s*")
_DEDUP_MENTION_RE = re.compile(r"@[^\s@]+")
_DEDUP_IMAGE_TAG_RE = re.compile(r"\[(?:引用)?图片x\d+\]")
_DEDUP_WS_RE = re.compile(r"\s+")


def _normalize_dedup_text(text: str) -> str:
    """将消息内容标准化，用于比对 dws 原始文本与已剥除 @mention 的历史记录。"""
    s = (text or "").strip()
    if not s:
        return ""
    s = _DEDUP_QUOTE_PREFIX_RE.sub("", s)
    s = _DEDUP_MENTION_RE.sub("", s)
    s = _DEDUP_IMAGE_TAG_RE.sub("", s)
    s = _DEDUP_WS_RE.sub("", s)
    return s.lower()


def _parse_beijing_epoch(ts_val: Any) -> Optional[float]:
    """将 datetime 或 'YYYY-MM-DD HH:MM:SS' 解析为 UTC+8 epoch 秒。"""
    if isinstance(ts_val, datetime):
        if ts_val.tzinfo is None:
            return ts_val.replace(tzinfo=_BEIJING_TZ).timestamp()
        return ts_val.timestamp()
    if isinstance(ts_val, str):
        raw = ts_val.strip()
        if len(raw) >= 19:
            try:
                dt = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
                return dt.replace(tzinfo=_BEIJING_TZ).timestamp()
            except ValueError:
                return None
    return None


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
        """获取 MySQL 连接 (上下文管理器)。

        强制 session time_zone='+08:00' 保证 TIMESTAMP 列读出为北京时间，
        与 clear_cutoff / sender 端的时间戳字符串可直接比较。
        """
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
                write_timeout=30,
                init_command="SET time_zone = '+08:00'",
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
                            `external_msg_id` VARCHAR(128) DEFAULT NULL,
                            `thinking_blocks` MEDIUMTEXT DEFAULT NULL,
                            `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            INDEX `idx_session_key` (`session_key`),
                            INDEX `idx_created_at` (`created_at`),
                            UNIQUE INDEX `uk_session_ext_msg` (`session_key`, `external_msg_id`)
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

                    # 兼容旧表：自动添加 external_msg_id 列与跨容器幂等唯一索引
                    try:
                        cursor.execute("""
                            ALTER TABLE `conversation_history`
                            ADD COLUMN `external_msg_id` VARCHAR(128) DEFAULT NULL AFTER `bot_id`
                        """)
                        print("✅ 已为 conversation_history 表添加 external_msg_id 列")
                    except pymysql.err.OperationalError as e:
                        if not e.args or e.args[0] != 1060:
                            print(f"⚠️ 添加 external_msg_id 列异常: {e}")
                    except Exception:
                        pass

                    try:
                        cursor.execute("""
                            ALTER TABLE `conversation_history`
                            ADD UNIQUE INDEX `uk_session_ext_msg` (`session_key`, `external_msg_id`)
                        """)
                        print("✅ 已为 conversation_history 表添加 uk_session_ext_msg 唯一索引")
                    except pymysql.err.OperationalError as e:
                        if not e.args or e.args[0] != 1061:
                            print(f"⚠️ 添加 uk_session_ext_msg 索引异常: {e}")
                    except Exception:
                        pass

                    # 兼容旧表：自动添加 thinking_blocks 列（如果不存在）
                    try:
                        cursor.execute("""
                            ALTER TABLE `conversation_history`
                            ADD COLUMN `thinking_blocks` MEDIUMTEXT NULL
                        """)
                        print("✅ 已为 conversation_history 表添加 thinking_blocks 列")
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
        self._memory_consumed_mids: Dict[str, set] = {}

    def _get_cache_key(self, session_key: str) -> str:
        return f"{HISTORY_KEY_PREFIX}{session_key}"

    def _get_consumed_mids_key(self, session_key: str) -> str:
        return f"{CONSUMED_MIDS_KEY_PREFIX}{session_key}"

    def mark_consumed_message_ids(self, session_key: str, msg_ids: Iterable[str]) -> None:
        """标记已在 2s 缓冲合并消息中消费过的次要 message_id，防 dws 窗口重复插入。"""
        cleaned = [str(m).strip() for m in (msg_ids or []) if m and str(m).strip()]
        if not cleaned:
            return
        mem_map = getattr(self, "_memory_consumed_mids", None)
        if mem_map is None:
            mem_map = {}
            self._memory_consumed_mids = mem_map
        bucket = mem_map.setdefault(session_key, set())
        bucket.update(cleaned)
        if len(bucket) > 500:
            # 控制单会话内存集合上限
            mem_map[session_key] = set(list(bucket)[-400:])

        if self.redis:
            key = self._get_consumed_mids_key(session_key)
            try:
                self.redis.sadd(key, *cleaned)
                self.redis.expire(key, HISTORY_TTL)
            except Exception as e:
                print(f"⚠️ Redis 写入 consumed_mids 失败: {e}")

    def _get_consumed_message_ids(self, session_key: str) -> set:
        result = set()
        mem_map = getattr(self, "_memory_consumed_mids", None) or {}
        result.update(mem_map.get(session_key) or set())
        if self.redis:
            key = self._get_consumed_mids_key(session_key)
            try:
                members = self.redis.smembers(key)
                if members:
                    result.update(str(m) for m in members if m)
            except Exception as e:
                print(f"⚠️ Redis 读取 consumed_mids 失败: {e}")
        return result

    def _check_duplicate_user_message(
        self,
        session_key: str,
        content: str,
        bot_id: Optional[str],
        external_msg_id: Optional[str] = None,
    ) -> Optional[Dict]:
        """
        检查用户消息是否已被保存（优先按 external_msg_id 跨窗口/跨容器精确去重，无 ID 时降级 5 秒时间窗去重）
        """
        ext_id = (external_msg_id or "").strip() or None
        try:
            with MySQLClient.get_connection() as conn:
                with conn.cursor() as cursor:
                    if ext_id:
                        cursor.execute("""
                            SELECT id, bot_id
                            FROM conversation_history
                            WHERE session_key = %s
                              AND external_msg_id = %s
                            LIMIT 1
                        """, (session_key, ext_id))
                    else:
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
                        existing_bot_id = row.get("bot_id")

                        if bot_id:
                            existing_bots = {
                                b.strip()
                                for b in (existing_bot_id or "").split(",")
                                if b.strip()
                            }
                            if bot_id in existing_bots:
                                return {"id": existing_id, "bot_id": existing_bot_id}

                            existing_bots.add(bot_id)
                            merged_bot_id = ",".join(sorted(existing_bots))

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

    @staticmethod
    def _rows_to_messages(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """将倒序查出的 MySQL 行翻转为正序标准消息字典列表。"""
        messages: List[Dict[str, Any]] = []
        for row in reversed(rows or []):
            created_at = row.get("created_at")
            if isinstance(created_at, datetime):
                ts_str = created_at.strftime("%Y-%m-%d %H:%M:%S")
            elif created_at:
                ts_str = str(created_at)[:19]
            else:
                ts_str = None
            msg: Dict[str, Any] = {
                "role": row["role"],
                "content": row["content"],
                "timestamp": ts_str,
                "sender_nick": row.get("sender_nick"),
                "bot_id": row.get("bot_id"),
            }
            if row.get("external_msg_id"):
                msg["external_msg_id"] = row["external_msg_id"]
            tb = row.get("thinking_blocks")
            if tb:
                try:
                    msg["reasoning_details"] = json.loads(tb)
                except Exception:
                    pass
            messages.append(msg)
        return messages

    def _fetch_mysql_history(self, session_key: str, row_limit: int = 200) -> List[Dict[str, Any]]:
        with MySQLClient.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT role, content, sender_nick, bot_id, external_msg_id, created_at, thinking_blocks
                    FROM conversation_history
                    WHERE session_key = %s
                    ORDER BY created_at DESC, id DESC
                    LIMIT %s
                """, (session_key, row_limit))
                rows = cursor.fetchall()
        return self._rows_to_messages(rows)

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
            messages = self._fetch_mysql_history(session_key, max(limit * 2, 100))
            if not messages:
                return []

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
        bot_id: Optional[str] = None,
        thinking_blocks: Optional[list] = None,
        external_msg_id: Optional[str] = None,
        created_at: Optional[str] = None,
    ):
        """添加消息到历史"""
        cache_key = self._get_cache_key(session_key)
        ext_id = (external_msg_id or "").strip() or None

        # 1. 用户消息去重逻辑（避免同时 @ 多个机器人或 dws 先行落库后重复保存）
        if role == "user":
            duplicate = self._check_duplicate_user_message(
                session_key,
                content,
                bot_id,
                external_msg_id=ext_id,
            )
            if duplicate:
                print(f"🔄 [去重] 用户消息已存在，合并 bot_id: {duplicate['bot_id']} + {bot_id}")
                if self.redis:
                    try:
                        refreshed = self._fetch_mysql_history(session_key, 200)
                        if refreshed:
                            self.redis.set(cache_key, json.dumps(refreshed, ensure_ascii=False), ex=HISTORY_TTL)
                    except Exception:
                        pass
                return

        # 2. 写入 MySQL
        thinking_blocks_json = json.dumps(thinking_blocks, ensure_ascii=False) if thinking_blocks else None
        try:
            with MySQLClient.get_connection() as conn:
                with conn.cursor() as cursor:
                    if ext_id and created_at:
                        cursor.execute("""
                            INSERT INTO conversation_history
                            (session_key, role, content, sender_nick, bot_id, external_msg_id, thinking_blocks, created_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """, (session_key, role, content, sender_nick, bot_id, ext_id, thinking_blocks_json, created_at))
                    elif ext_id:
                        cursor.execute("""
                            INSERT INTO conversation_history
                            (session_key, role, content, sender_nick, bot_id, external_msg_id, thinking_blocks)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """, (session_key, role, content, sender_nick, bot_id, ext_id, thinking_blocks_json))
                    else:
                        cursor.execute("""
                            INSERT INTO conversation_history
                            (session_key, role, content, sender_nick, bot_id, thinking_blocks)
                            VALUES (%s, %s, %s, %s, %s, %s)
                        """, (session_key, role, content, sender_nick, bot_id, thinking_blocks_json))
                conn.commit()
        except Exception as e:
            print(f"⚠️ MySQL 写入失败: {e}")

        # 3. 更新 Redis 缓存
        if self.redis:
            try:
                cached = self.redis.get(cache_key)
                messages = json.loads(cached) if cached else []

                timestamp = (created_at or "").strip()[:19] or datetime.now(_BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")

                msg_entry: Dict[str, Any] = {
                    "role": role,
                    "content": content,
                    "timestamp": timestamp,
                    "sender_nick": sender_nick,
                    "bot_id": bot_id,
                }
                if ext_id:
                    msg_entry["external_msg_id"] = ext_id
                if thinking_blocks:
                    msg_entry["reasoning_details"] = thinking_blocks

                messages.append(msg_entry)

                if len(messages) > 200:
                    messages = messages[-200:]

                self.redis.set(cache_key, json.dumps(messages, ensure_ascii=False), ex=HISTORY_TTL)
            except Exception as e:
                print(f"⚠️ Redis 更新失败: {e}")

    def upsert_external_messages(self, session_key: str, messages: List[Dict[str, Any]]) -> int:
        """幂等落库来自 dws 的群聊人类消息，按原始时间戳正序刷新 Redis 缓存。"""
        if not session_key or not messages:
            return 0

        consumed_ids = self._get_consumed_message_ids(session_key)
        dedup_batch: Dict[str, Dict[str, str]] = {}
        for item in messages:
            ext_id = str(item.get("external_msg_id") or "").strip()
            content = str(item.get("content") or "").strip()
            ts_str = str(item.get("timestamp") or "").strip()[:19]
            if not ext_id or not content or not ts_str or ext_id in consumed_ids:
                continue
            dedup_batch[ext_id] = {
                "external_msg_id": ext_id,
                "content": content,
                "sender_nick": str(item.get("sender_nick") or "").strip() or None,
                "timestamp": ts_str,
            }

        if not dedup_batch:
            return 0

        ordered_candidates = sorted(
            dedup_batch.values(),
            key=lambda x: (x["timestamp"], x["external_msg_id"]),
        )

        inserted = 0
        backfilled = 0
        cache_key = self._get_cache_key(session_key)

        try:
            with MySQLClient.get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        SELECT id, content, sender_nick, external_msg_id, created_at
                        FROM conversation_history
                        WHERE session_key = %s
                          AND role = 'user'
                        ORDER BY created_at DESC, id DESC
                        LIMIT 300
                    """, (session_key,))
                    existing_rows = cursor.fetchall() or []

                    existing_ext_ids = {
                        str(r["external_msg_id"]).strip()
                        for r in existing_rows
                        if r.get("external_msg_id")
                    }
                    claimed_legacy_ids: set = set()

                    for cand in ordered_candidates:
                        ext_id = cand["external_msg_id"]
                        if ext_id in existing_ext_ids:
                            continue

                        cand_norm = _normalize_dedup_text(cand["content"])
                        cand_epoch = _parse_beijing_epoch(cand["timestamp"])
                        matched_existing = False

                        if cand_norm:
                            for row in existing_rows:
                                row_ext_id = row.get("external_msg_id")
                                row_id = row.get("id")
                                if row_ext_id:
                                    continue
                                # 比对发送者昵称（若两侧都有）
                                cand_sender = (cand.get("sender_nick") or "").strip()
                                row_sender = (row.get("sender_nick") or "").strip()
                                if cand_sender and row_sender and cand_sender != row_sender:
                                    continue

                                # 比对时间窗：update_history 在流式结束后落库，db_epoch 通常晚于 dws 0~300s
                                row_epoch = _parse_beijing_epoch(row.get("created_at"))
                                if cand_epoch is not None and row_epoch is not None:
                                    delta = row_epoch - cand_epoch
                                    if delta < -20.0 or delta > 300.0:
                                        continue

                                raw_row_content = str(row.get("content") or "")
                                row_norm_lines = [
                                    _normalize_dedup_text(line)
                                    for line in raw_row_content.splitlines()
                                    if line.strip()
                                ]
                                row_full_norm = _normalize_dedup_text(raw_row_content)

                                if cand_norm == row_full_norm or cand_norm in row_norm_lines:
                                    matched_existing = True
                                    if not row_ext_id and row_id is not None and row_id not in claimed_legacy_ids:
                                        claimed_legacy_ids.add(row_id)
                                        cursor.execute("""
                                            UPDATE conversation_history
                                            SET external_msg_id = %s
                                            WHERE id = %s AND external_msg_id IS NULL
                                        """, (ext_id, row_id))
                                        backfilled += 1
                                        existing_ext_ids.add(ext_id)
                                    break

                        if matched_existing:
                            continue

                        cursor.execute("""
                            INSERT IGNORE INTO conversation_history
                            (session_key, role, content, sender_nick, bot_id, external_msg_id, created_at)
                            VALUES (%s, 'user', %s, %s, NULL, %s, %s)
                        """, (
                            session_key,
                            cand["content"],
                            cand["sender_nick"],
                            ext_id,
                            cand["timestamp"],
                        ))
                        row_delta = max(0, int(getattr(cursor, "rowcount", 0) or 0))
                        inserted += row_delta
                        existing_ext_ids.add(ext_id)

                conn.commit()
        except Exception as e:
            print(f"⚠️ MySQL 批量写入外部群聊消息失败: {e}")
            raise

        if (inserted > 0 or backfilled > 0) and self.redis:
            try:
                refreshed = self._fetch_mysql_history(session_key, 200)
                self.redis.set(cache_key, json.dumps(refreshed, ensure_ascii=False), ex=HISTORY_TTL)
            except Exception as e:
                print(f"⚠️ Redis 刷新外部群聊历史缓存失败: {e}")

        return inserted

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
