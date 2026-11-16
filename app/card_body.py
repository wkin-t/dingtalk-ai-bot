# -*- coding: utf-8 -*-
"""钉钉 AI 卡片正文回读补全。

dws 负责决定消息是否可见；本模块只为已经由 dws 读到的本机器人卡片
提供发送时保存的完整 Markdown 正文。Redis 只作为短期、可丢失的补全
索引，任何连接或解析错误都保持 dws 原文，不阻断主回复。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import redis


REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
try:
    REDIS_PORT = int(os.getenv("REDIS_PORT", "36379"))
except (TypeError, ValueError):
    REDIS_PORT = 36379
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")
try:
    REDIS_DB = int(os.getenv("REDIS_DB", "0"))
except (TypeError, ValueError):
    REDIS_DB = 0

REDIS_TIMEOUT_SECONDS = 0.2
STORAGE_TTL_SECONDS = 72 * 60 * 60
MAX_INDEX_ENTRIES = 1024
MAX_BODY_BYTES = 64 * 1024
_CONFIRM_TOTAL_TIMEOUT_SECONDS = 5.0
_CONFIRM_MAX_LOOKUPS = 3
_CONFIRM_RETRY_DELAYS = (0.4, 0.8)
INDEX_KEY = "dingtalk_card_body:v1:index"
KEY_PREFIX = "dingtalk_card_body:v1:"
_VERSION = "cb1"
_REFERENCE_BYTES = 16
_REFERENCE_LENGTH = 22
_DIGEST_LENGTH = 12

_FOOTER_LINE_RE = re.compile(
    r"^[ \t]*(?:"
    r"ref:cb1-([A-Za-z0-9_-]{22})\.([0-9a-f]{12})"
    r"|<font color='#aaaaaa' size='2'>ref:cb1-([A-Za-z0-9_-]{22})\.([0-9a-f]{12})</font>"
    r")[ \t]*\r?$",
    re.MULTILINE,
)
_REFERENCE_RE = re.compile(r"[A-Za-z0-9_-]{22}\Z")

# SET、索引和淘汰必须在同一个 Redis Lua 调用中完成。Redis 脚本本身是
# 原子的；脚本内对可能失败的命令使用 pcall，并在索引失败时删除刚写入的
# 正文，避免留下无法由 dws 消费的孤儿正文。返回值：1=写入，0=NX 已存在，-1=失败。
_STORE_SCRIPT = """
local written = redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2], 'NX')
if not written then
  return 0
end

local function rollback()
  redis.pcall('DEL', KEYS[1])
  redis.pcall('ZREM', KEYS[2], ARGV[4])
  return -1
end

local added = redis.pcall('ZADD', KEYS[2], ARGV[3], ARGV[4])
if type(added) == 'table' and added.err then
  return rollback()
end

local count = redis.pcall('ZCARD', KEYS[2])
if type(count) == 'table' and count.err then
  return rollback()
end
local excess = count - tonumber(ARGV[5])
if excess > 0 then
  local old = redis.pcall('ZRANGE', KEYS[2], 0, excess - 1)
  if type(old) == 'table' and old.err then
    return rollback()
  end
  for _, reference in ipairs(old) do
    local deleted = redis.pcall('DEL', ARGV[6] .. reference)
    if type(deleted) == 'table' and deleted.err then
      return rollback()
    end
  end
  local removed = redis.pcall('ZREMRANGEBYRANK', KEYS[2], 0, excess - 1)
  if type(removed) == 'table' and removed.err then
    return rollback()
  end
end
return 1
"""

_redis_client: redis.Redis | None = None
_redis_client_lock = threading.Lock()
_redis_next_retry_at = 0.0
_REDIS_FAILURE_BACKOFF_SECONDS = 0.5


def _enabled() -> bool:
    value = os.getenv("ENABLE_CARD_BODY_RESTORE", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _body_size(body: str) -> int:
    return len(body.encode("utf-8"))


def _body_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _make_reference() -> str:
    # 16 个随机字节编码为 22 个 URL-safe 字符，提供 128 位熵，同时不在
    # Redis 键或 Markdown footer 中引入额外标点。
    return base64.urlsafe_b64encode(secrets.token_bytes(_REFERENCE_BYTES)).decode("ascii").rstrip("=")


def _storage_key(reference: str) -> str:
    return f"{KEY_PREFIX}{reference}"


@dataclass(frozen=True)
class CardBodyDraft:
    """待卡片发送确认的正文快照。"""

    corp_id: str
    conversation_id: str
    sender_id: str
    out_track_id: str
    body: str
    reference: str

    @property
    def footer(self) -> str:
        return f"ref:{_VERSION}-{self.reference}.{_body_sha256(self.body)[:_DIGEST_LENGTH]}"


def prepare_card_body(
    *,
    corp_id: str,
    conversation_id: str,
    sender_id: str,
    out_track_id: str,
    body: str,
) -> CardBodyDraft | None:
    """构造正文快照；不访问 Redis，也不产生写入副作用。"""

    if not _enabled():
        return None
    values = (corp_id, conversation_id, sender_id, out_track_id)
    if any(not isinstance(value, str) or not value.strip() for value in values):
        return None
    if not isinstance(body, str) or _body_size(body) > MAX_BODY_BYTES:
        return None
    reference = _make_reference()
    if len(reference) != _REFERENCE_LENGTH:
        return None
    return CardBodyDraft(
        corp_id=corp_id.strip(),
        conversation_id=conversation_id.strip(),
        sender_id=sender_id.strip(),
        out_track_id=out_track_id.strip(),
        body=body,
        reference=reference,
    )


def _log_redis_failure(operation: str) -> None:
    print(f"⚠️ [卡片正文回读] Redis {operation} 失败，保留 dws 原文")


def _build_redis_client() -> redis.Redis | None:
    try:
        candidate = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            password=REDIS_PASSWORD or None,
            db=REDIS_DB,
            decode_responses=True,
            socket_connect_timeout=REDIS_TIMEOUT_SECONDS,
            socket_timeout=REDIS_TIMEOUT_SECONDS,
        )
        candidate.ping()
        return candidate
    except Exception:
        _log_redis_failure("client/ping")
        return None


def _get_redis_client() -> redis.Redis | None:
    global _redis_client, _redis_next_retry_at
    if _redis_client is not None:
        return _redis_client
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


def _record_for(draft: CardBodyDraft, message_id: str) -> dict[str, str]:
    return {
        "version": _VERSION,
        "corp_id": draft.corp_id,
        "conversation_id": draft.conversation_id,
        "sender_id": draft.sender_id,
        "out_track_id": draft.out_track_id,
        "message_id": message_id,
        "reference": draft.reference,
        "body": draft.body,
        "body_sha256": _body_sha256(draft.body),
    }


def _payload_flag_is_true(value: Any) -> bool:
    """识别 dws 可能序列化出的 true；未知值不被当成完整结果。"""

    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() == "true"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 1
    return False


def _extract_verified_bot_rows(
    payload: Any,
    *,
    conversation_id: str,
) -> list[dict[str, Any]] | None:
    """只接受已完整覆盖目标会话的 bot bucket，不从昵称或其他字段推断身份。"""

    if not isinstance(payload, dict) or payload.get("ok") is False:
        return None
    container: Any = payload
    nested_data = payload.get("data")
    if "data" in payload:
        if not isinstance(nested_data, dict):
            return None
        container = nested_data

        # 若外层也带范围状态，必须与内层一致，避免从混合 envelope 中挑选安全片段。
        if "conversationId" in payload and payload.get("conversationId") != conversation_id:
            return None
        if "complete" in payload and payload.get("complete") is not True:
            return None
        if "partial" in payload and _payload_flag_is_true(payload.get("partial")):
            return None

    if container.get("conversationId") != conversation_id:
        return None
    if container.get("complete") is not True:
        return None
    if _payload_flag_is_true(container.get("partial")):
        return None

    buckets = container.get("buckets")
    bots = buckets.get("bots") if isinstance(buckets, dict) else None
    if not isinstance(bots, dict) or bots.get("complete") is not True:
        return None
    # 成员条目位于会话对象顶层 bots；buckets.bots 只承载完整性元数据。
    rows = container.get("bots")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return None
    return rows


async def resolve_card_sender_id(
    *,
    conversation_id: str,
    robot_code: str,
    corp_id: str,
) -> str:
    """从完整 dws bot 成员快照解析当前机器人实际 openDingtalkId。

    Stream 回调里的 chatbotUserId 与 dws 消息 senderId 不一定属于同一编号域，
    所以必须用受信任的 robotCode 在已校验企业和会话快照中做唯一精确匹配；
    任何缺失、重复、范围不一致或 dws 异常都按空结果拒绝。
    """

    values = (conversation_id, robot_code, corp_id)
    if any(not isinstance(value, str) or not value.strip() for value in values):
        return ""
    clean_conversation_id = conversation_id.strip()
    clean_robot_code = robot_code.strip()
    clean_corp_id = corp_id.strip()
    try:
        # 延迟导入避免 card_body 与 dws_context 的模块初始化环。
        from app.dws_context import check_dws_corp_access, run_dws_readonly_json

        corp_check = await check_dws_corp_access(expected_corp_ids=[clean_corp_id])
        if not getattr(corp_check, "allowed", False):
            return ""
        payload = await run_dws_readonly_json(
            [
                "chat",
                "+chat-members-list",
                "--conversation-id",
                clean_conversation_id,
                "--member-types",
                "bot",
                "--page-limit",
                "5",
                "-f",
                "json",
            ],
            timeout_seconds=3.0,
        )
        rows = _extract_verified_bot_rows(payload, conversation_id=clean_conversation_id)
        if rows is None:
            return ""
        matches = [row for row in rows if row.get("robotCode") == clean_robot_code]
        if len(matches) != 1:
            return ""
        sender_id = matches[0].get("openDingtalkId")
        if not isinstance(sender_id, str) or not sender_id.strip():
            return ""
        return sender_id.strip()
    except asyncio.CancelledError:
        raise
    except Exception:
        return ""


async def _lookup_delivered_message_id(draft: CardBodyDraft) -> str | None:
    """发送确认后从同一 dws 会话唯一定位这张卡片的真实消息 ID。"""

    try:
        # 延迟导入避免 card_body 与 dws_context 的模块初始化环。
        from app.dws_context import (
            check_dws_corp_access,
            extract_clean_dws_item,
            extract_messages_from_payload,
            run_dws_readonly_json,
        )

        corp_check = await check_dws_corp_access(expected_corp_ids=[draft.corp_id])
        if not getattr(corp_check, "allowed", False):
            return None
        payload = await run_dws_readonly_json(
            [
                "chat",
                "message",
                "list",
                "--conversation-id",
                draft.conversation_id,
                "--limit",
                "10",
                "-f",
                "json",
            ],
            timeout_seconds=3.0,
        )
        if payload is None:
            return None
        expected_digest = _body_sha256(draft.body)[:_DIGEST_LENGTH]
        matches: list[str] = []
        for raw_item in extract_messages_from_payload(payload):
            item = extract_clean_dws_item(raw_item)
            if item is None:
                continue
            if (
                item.get("conversation_id") != draft.conversation_id
                or item.get("sender_id") != draft.sender_id
                or not item.get("message_id")
            ):
                continue
            footer = _extract_footer(item.get("display_text"))
            if footer is None:
                footer = _extract_footer(item.get("raw_text"))
            if footer == (draft.reference, expected_digest):
                matches.append(str(item["message_id"]))
        return matches[0] if len(matches) == 1 else None
    except asyncio.CancelledError:
        raise
    except Exception:
        return None


def _confirm_sync(draft: CardBodyDraft | None, message_id: str) -> bool:
    if not _enabled() or draft is None or not isinstance(message_id, str) or not message_id.strip():
        return False
    if (
        not isinstance(draft, CardBodyDraft)
        or any(
            not isinstance(value, str) or not value.strip()
            for value in (
                draft.corp_id,
                draft.conversation_id,
                draft.sender_id,
                draft.out_track_id,
                draft.body,
            )
        )
        or not isinstance(draft.reference, str)
        or _REFERENCE_RE.fullmatch(draft.reference) is None
        or _body_size(draft.body) > MAX_BODY_BYTES
    ):
        return False
    client = _get_redis_client()
    if client is None:
        return False
    global _redis_client, _redis_next_retry_at
    try:
        payload = json.dumps(
            _record_for(draft, message_id.strip()),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        # NX 保证重试确认时不会覆盖已经生成的引用；Lua 调用同时保证索引
        # 写入和有界淘汰具备原子性。
        result = client.eval(
            _STORE_SCRIPT,
            2,
            _storage_key(draft.reference),
            INDEX_KEY,
            payload,
            STORAGE_TTL_SECONDS,
            time.time(),
            draft.reference,
            MAX_INDEX_ENTRIES,
            KEY_PREFIX,
        )
        return int(result) == 1
    except Exception:
        _redis_client = None
        _redis_next_retry_at = time.monotonic() + _REDIS_FAILURE_BACKOFF_SECONDS
        _log_redis_failure("write")
        return False


async def confirm_card_body(draft: CardBodyDraft | None) -> bool:
    """在卡片已得到成功回执后保存正文；Redis 故障按 fail-open 返回 False。"""

    if not _enabled() or draft is None:
        return False
    message_id = None
    try:
        # 卡片最终状态写入 dws 有短暂可见性延迟；重试受总预算和次数双重限制，
        # 超时或仍未唯一匹配时不写 Redis，避免把未经 dws 证实的正文落盘。
        async with asyncio.timeout(_CONFIRM_TOTAL_TIMEOUT_SECONDS):
            for attempt in range(_CONFIRM_MAX_LOOKUPS):
                message_id = await _lookup_delivered_message_id(draft)
                if message_id:
                    break
                if attempt < _CONFIRM_MAX_LOOKUPS - 1:
                    await asyncio.sleep(_CONFIRM_RETRY_DELAYS[attempt])
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        return False
    if not message_id:
        return False
    return await asyncio.to_thread(_confirm_sync, draft, message_id)


def _copy_item(item: Mapping[str, Any]) -> dict[str, Any]:
    return dict(item)


def _extract_footer(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    matches = list(_FOOTER_LINE_RE.finditer(value))
    # 正文中出现第二个候选标记时拒绝整条消息，不能猜哪个是本次卡片的
    # 版本；唯一候选还必须位于正文末尾（允许最后的换行和空格）。
    if len(matches) != 1:
        return None
    match = matches[0]
    if value[match.end() :].strip():
        return None
    reference = match.group(1) or match.group(3)
    digest = match.group(2) or match.group(4)
    if not reference or not digest:
        return None
    return reference, digest


def _valid_text_field(item: Mapping[str, Any], field: str) -> str:
    value = item.get(field)
    return value.strip() if isinstance(value, str) else ""


def _load_records(client: redis.Redis, references: Sequence[str]) -> dict[str, dict[str, Any]]:
    keys = [_storage_key(reference) for reference in references]
    values = client.mget(keys)
    output: dict[str, dict[str, Any]] = {}
    for reference, raw in zip(references, values):
        if not isinstance(raw, str):
            continue
        try:
            record = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(record, dict):
            output[reference] = record
    return output


def _restore_sync(
    items: Sequence[Mapping[str, Any]],
    *,
    corp_id: str,
    conversation_id: str,
) -> list[dict[str, Any]]:
    copies = [_copy_item(item) for item in items if isinstance(item, Mapping)]
    if not _enabled() or not isinstance(corp_id, str) or not corp_id.strip():
        return copies
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        return copies
    candidates: list[tuple[int, str, str]] = []
    references: list[str] = []
    for index, item in enumerate(copies):
        message_id = _valid_text_field(item, "message_id")
        sender_id = _valid_text_field(item, "sender_id")
        item_cid = _valid_text_field(item, "conversation_id")
        if not message_id or not sender_id or item_cid != conversation_id.strip():
            continue
        footer = _extract_footer(item.get("display_text"))
        if footer is None:
            footer = _extract_footer(item.get("raw_text"))
        if footer is None:
            continue
        reference, digest_prefix = footer
        candidates.append((index, reference, digest_prefix))
        if reference not in references:
            references.append(reference)
    if not references:
        return copies
    client = _get_redis_client()
    if client is None:
        return copies
    global _redis_client, _redis_next_retry_at
    try:
        records = _load_records(client, references)
        for index, reference, digest_prefix in candidates:
            item = copies[index]
            record = records.get(reference)
            if not isinstance(record, dict):
                continue
            body = record.get("body")
            body_hash = record.get("body_sha256")
            item_cid = _valid_text_field(item, "conversation_id")
            item_sender = _valid_text_field(item, "sender_id")
            item_message = _valid_text_field(item, "message_id")
            if (
                record.get("version") != _VERSION
                or record.get("reference") != reference
                or record.get("corp_id") != corp_id.strip()
                or record.get("conversation_id") != conversation_id.strip()
                or record.get("conversation_id") != item_cid
                or record.get("sender_id") != item_sender
                or record.get("message_id") != item_message
                or not isinstance(body, str)
                or not isinstance(body_hash, str)
                or len(body_hash) != 64
                or _body_size(body) > MAX_BODY_BYTES
                or _body_sha256(body) != body_hash
                or not body_hash.startswith(digest_prefix)
            ):
                continue
            item["display_text"] = body
    except Exception:
        _redis_client = None
        _redis_next_retry_at = time.monotonic() + _REDIS_FAILURE_BACKOFF_SECONDS
        _log_redis_failure("read")
        return copies
    return copies


async def restore_card_bodies(
    items: Sequence[Mapping[str, Any]],
    *,
    corp_id: str,
    conversation_id: str,
) -> list[dict[str, Any]]:
    """批量补全当前 dws 可见消息；失败时原样复制返回。"""

    return await asyncio.to_thread(
        _restore_sync,
        items,
        corp_id=corp_id,
        conversation_id=conversation_id,
    )
