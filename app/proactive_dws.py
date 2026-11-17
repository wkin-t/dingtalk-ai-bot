# -*- coding: utf-8 -*-
"""工作日主动群聊使用的 DWS 只读适配。

主动调度不接受 Stream 回调作为当前输入，也不从旧历史存储补数据；每次快照都
重新确认企业、群列表、完整机器人名单和当前会话消息，任何来源不完整时直接跳过。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from app import config as cfg
from app.card_body import _extract_verified_bot_rows, resolve_card_sender_id
from app.clear_cutoff import get_cutoff
from app.dws_context import (
    check_dws_corp_access,
    extract_clean_dws_item,
    extract_messages_from_payload,
    normalize_dws_timestamp,
    run_dws_readonly_json,
)
from app.proactive_policy import last_human_activity


_BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")
_GROUP_LIST_LIMIT = 100
_GROUP_PAGE_LIMIT = 10
_MESSAGE_LIMIT = 100
_BOT_MEMBER_PAGE_LIMIT = 5


@dataclass
class ProactiveGroupSnapshot:
    """一次主动发送前可使用的、同企业同群只读快照。"""

    corp_id: str
    conversation_id: str
    name: str
    bot_sender_id: str
    bot_ids: list[str] = field(default_factory=list)
    last_human_at: float | None = None
    latest_message_id: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)


def _clean_id(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _raw_conversation_id(row: Mapping[str, Any]) -> str:
    values = {
        value
        for key in ("conversationId", "openConversationId", "conversation_id", "cid")
        if (value := _clean_id(row.get(key)))
    }
    return values.pop() if len(values) == 1 else ""


def _timestamp_epoch(value: Any) -> float | None:
    normalized = normalize_dws_timestamp(value)
    if not normalized:
        return None
    try:
        return datetime.strptime(normalized, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_BEIJING).timestamp()
    except ValueError:
        return None


def _valid_now(now: Any) -> bool:
    return isinstance(now, datetime)


def _parse_groups_payload(payload: Any) -> list[dict[str, Any]] | None:
    """只接受 dws 1.0.67 ``+chat-list-all`` 的完整顶层回执。"""

    if not isinstance(payload, dict) or payload.get("ok") is False:
        return None
    if payload.get("complete") is not True or payload.get("hasMore") is not False:
        return None
    failures = payload.get("failures")
    if not isinstance(failures, list) or failures:
        return None
    if payload.get("stopReason") not in {"source_complete", "completed"}:
        return None
    raw_groups = payload.get("groups")
    if not isinstance(raw_groups, list):
        return None

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_groups:
        if not isinstance(raw, dict):
            return None
        group_ids = {
            value
            for key in ("openConversationId", "conversationId")
            if (value := _clean_id(raw.get(key)))
        }
        conversation_id = group_ids.pop() if len(group_ids) == 1 else ""
        name = _clean_id(raw.get("name"))
        if not conversation_id or not name or conversation_id in seen:
            return None
        seen.add(conversation_id)
        result.append({"cid": conversation_id, "name": name, "complete": True})
    return result


def _group_list_args() -> list[str]:
    return [
        "chat",
        "+chat-list-all",
        "--limit",
        str(_GROUP_LIST_LIMIT),
        "--page-all",
        "--page-limit",
        str(_GROUP_PAGE_LIMIT),
        "-f",
        "json",
    ]


async def _check_corp(corp: str):
    if not corp:
        return None
    try:
        result = await check_dws_corp_access(expected_corp_ids=[corp])
    except asyncio.CancelledError:
        raise
    except Exception:
        return None
    identity = getattr(result, "identity", None)
    if not getattr(result, "allowed", False) or identity is None:
        return None
    if _clean_id(getattr(identity, "corp_id", "")) != corp:
        return None
    return result


async def _read_groups() -> list[dict[str, Any]] | None:
    try:
        payload = await run_dws_readonly_json(
            _group_list_args(),
            timeout_seconds=float(cfg.DWS_TIMEOUT_SECONDS),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return None
    return _parse_groups_payload(payload)


async def discover_groups(corp: str) -> list[dict[str, Any]]:
    """在当前已登录企业中完整列出已加入群；任何不完整回执均返回空列表。"""

    clean_corp = _clean_id(corp)
    if await _check_corp(clean_corp) is None:
        return []
    groups = await _read_groups()
    return groups or []


def _parse_complete_bot_rows(payload: Any, conversation_id: str) -> list[dict[str, Any]] | None:
    rows = _extract_verified_bot_rows(payload, conversation_id=conversation_id)
    if rows is None or not rows:
        return None

    bot_ids: set[str] = set()
    for row in rows:
        sender_id = _clean_id(row.get("openDingtalkId"))
        if not sender_id or sender_id in bot_ids:
            return None
        # 同一其他应用可在名单中有多个 sender；本 bot 的 AppKey 唯一性由
        # fetch_group_snapshot 单独核验，不能因此误判整个完整名单不可用。
        bot_ids.add(sender_id)
    return rows


async def _read_bot_rows(conversation_id: str) -> list[dict[str, Any]] | None:
    try:
        payload = await run_dws_readonly_json(
            [
                "chat",
                "+chat-members-list",
                "--conversation-id",
                conversation_id,
                "--member-types",
                "bot",
                "--page-limit",
                str(_BOT_MEMBER_PAGE_LIMIT),
                "-f",
                "json",
            ],
            timeout_seconds=min(3.0, float(cfg.DWS_TIMEOUT_SECONDS)),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return None
    return _parse_complete_bot_rows(payload, conversation_id)


def _payload_has_more(payload: Any) -> bool | None:
    containers = [payload]
    if isinstance(payload, dict):
        for key in ("data", "result"):
            value = payload.get(key)
            if isinstance(value, dict):
                containers.append(value)
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in ("hasMore", "has_more"):
            if key in container:
                value = container[key]
                return value if isinstance(value, bool) else None
    return None


async def _read_messages(conversation_id: str) -> tuple[list[dict[str, Any]], bool | None] | None:
    try:
        payload = await run_dws_readonly_json(
            [
                "chat",
                "message",
                "list",
                "--conversation-id",
                conversation_id,
                "--limit",
                str(_MESSAGE_LIMIT),
                "-f",
                "json",
            ],
            timeout_seconds=float(cfg.DWS_TIMEOUT_SECONDS),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return None
    if payload is None:
        return None
    rows = [
        row
        for row in extract_messages_from_payload(payload)
        if isinstance(row, dict) and _raw_conversation_id(row) == conversation_id
    ]
    return rows, _payload_has_more(payload)


def _latest_message_id(rows: Sequence[dict[str, Any]], now: datetime) -> str:
    local_now = now.replace(tzinfo=_BEIJING) if now.tzinfo is None else now.astimezone(_BEIJING)
    now_epoch = local_now.timestamp()
    candidates: list[tuple[float, int, str]] = []
    for index, raw in enumerate(rows):
        cleaned = extract_clean_dws_item(raw)
        if cleaned is None:
            continue
        timestamp = _timestamp_epoch(cleaned.get("timestamp"))
        message_id = _clean_id(cleaned.get("message_id"))
        if timestamp is None or timestamp > now_epoch or not message_id:
            continue
        candidates.append((timestamp, index, message_id))
    return max(candidates)[2] if candidates else ""


def _strict_human_rows(
    rows: Sequence[dict[str, Any]],
    conversation_id: str,
    now: datetime,
    bot_ids: Sequence[str],
) -> list[dict[str, Any]]:
    """在调用共享策略前先收窄到 DWS 明确标记的当前真人消息。"""

    local_now = now.replace(tzinfo=_BEIJING) if now.tzinfo is None else now.astimezone(_BEIJING)
    cutoff = local_now.timestamp()
    known_bots = {sender_id.lower() for sender_id in bot_ids if sender_id}
    result: list[dict[str, Any]] = []
    for row in rows:
        if _raw_conversation_id(row) != conversation_id:
            continue
        if _clean_id(row.get("sendType")).lower() != "user":
            continue
        sender_id = _clean_id(
            row.get("senderId") or row.get("senderUserId") or row.get("senderStaffId")
        )
        if not sender_id or sender_id.lower() in known_bots:
            continue
        cleaned = extract_clean_dws_item(row)
        if cleaned is None:
            continue
        timestamp = _timestamp_epoch(cleaned.get("timestamp"))
        if timestamp is None or timestamp > cutoff:
            continue
        result.append(row)
    return result


def _history_from_rows(
    rows: Sequence[dict[str, Any]],
    bot_rows: Sequence[dict[str, Any]],
    own_sender_id: str,
    now: datetime,
    cutoff_at: str | None,
) -> list[dict[str, Any]]:
    """从当前 dws 投影构造主动上下文，不进入正文回读或媒体下载链路。"""

    local_now = now.replace(tzinfo=_BEIJING) if now.tzinfo is None else now.astimezone(_BEIJING)
    now_epoch = local_now.timestamp()
    sender_to_source = {
        sender_id: _clean_id(row.get("robotCode")) or "other_bot"
        for row in bot_rows
        if (sender_id := _clean_id(row.get("openDingtalkId")))
    }
    history: list[dict[str, Any]] = []
    for raw in rows:
        cleaned = extract_clean_dws_item(raw)
        if cleaned is None:
            continue
        timestamp = _clean_id(cleaned.get("timestamp"))
        timestamp_epoch = _timestamp_epoch(timestamp)
        if timestamp_epoch is not None and timestamp_epoch > now_epoch:
            continue
        if cutoff_at and timestamp and timestamp <= cutoff_at:
            continue
        sender_id = _clean_id(cleaned.get("sender_id"))
        text = _clean_id(cleaned.get("display_text"))
        if not text and cleaned.get("is_bot_like"):
            text = "[钉钉卡片正文不可读取]"
        if not text or cleaned.get("message_type") == "system":
            continue

        source = sender_to_source.get(sender_id)
        if sender_id == own_sender_id:
            role = "assistant"
            bot_id = cfg.BOT_ID
            bot_source = cfg.BOT_ID
        elif source is not None:
            role = "assistant"
            bot_id = source
            bot_source = source
        elif _clean_id(raw.get("sendType")).lower() == "user" and sender_id:
            role = "user"
            bot_id = None
            bot_source = ""
        else:
            continue

        item: dict[str, Any] = {
            "role": role,
            "content": text,
            "timestamp": timestamp,
            "sender_nick": _clean_id(cleaned.get("sender")) or "User",
            "message_id": _clean_id(cleaned.get("message_id")),
            "bot_source": bot_source,
        }
        if bot_id is not None:
            item["bot_id"] = bot_id
        history.append(item)
    limit = max(0, int(cfg.MAX_HISTORY_LENGTH))
    return history[-limit:] if limit else []


async def fetch_group_snapshot(
    corp: str,
    cid: str,
    robot_code: str,
    now: datetime,
    with_context: bool = False,
    known_group: dict[str, Any] | None = None,
) -> ProactiveGroupSnapshot | None:
    """读取主动发送前的同企业同群快照；不确定时返回 ``None``。

    ``known_group`` 只复用 discovery 已确认的 ``cid/name`` 元数据，不参与权限或
    bot 身份判断；未提供时保持重新读取完整群列表的原行为。
    """

    clean_corp = _clean_id(corp)
    conversation_id = _clean_id(cid)
    clean_robot_code = _clean_id(robot_code)
    if not clean_corp or not conversation_id or not clean_robot_code or not _valid_now(now):
        return None

    try:
        if await _check_corp(clean_corp) is None:
            return None
        if known_group is None:
            groups = await _read_groups()
            if not groups:
                return None
            group = next((item for item in groups if item["cid"] == conversation_id), None)
            if group is None:
                return None
        else:
            cached_cid = _clean_id(known_group.get("cid")) if isinstance(known_group, dict) else ""
            cached_name = _clean_id(known_group.get("name")) if isinstance(known_group, dict) else ""
            if cached_cid != conversation_id or not cached_name:
                return None
            group = {"cid": cached_cid, "name": cached_name, "complete": True}

        bot_rows = await _read_bot_rows(conversation_id)
        if bot_rows is None:
            return None
        bot_ids = [_clean_id(row["openDingtalkId"]) for row in bot_rows]
        matches = [row for row in bot_rows if _clean_id(row.get("robotCode")) == clean_robot_code]
        if len(matches) != 1:
            return None
        expected_sender_id = _clean_id(matches[0].get("openDingtalkId"))
        if not expected_sender_id:
            return None

        # 复用已有的完整 bot bucket + 企业门禁实现，保证 sender 身份不落回 Stream ID 或昵称。
        bot_sender_id = await resolve_card_sender_id(
            corp_id=clean_corp,
            conversation_id=conversation_id,
            robot_code=clean_robot_code,
        )
        if bot_sender_id != expected_sender_id:
            return None

        message_result = await _read_messages(conversation_id)
        if message_result is None:
            return None
        rows, has_more = message_result
        latest_message_id = _latest_message_id(rows, now)
        human_rows = _strict_human_rows(rows, conversation_id, now, bot_ids)
        if not human_rows and has_more is not False:
            return None
        last_human_at = last_human_activity(human_rows, conversation_id, now, bot_ids)
        history: list[dict[str, Any]] = []
        if with_context:
            history = _history_from_rows(
                rows,
                bot_rows,
                bot_sender_id,
                now,
                get_cutoff(f"dingtalk_{conversation_id}"),
            )

        return ProactiveGroupSnapshot(
            corp_id=clean_corp,
            conversation_id=conversation_id,
            name=group["name"],
            bot_sender_id=bot_sender_id,
            bot_ids=bot_ids,
            last_human_at=last_human_at,
            latest_message_id=latest_message_id,
            history=history,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return None


__all__ = ["ProactiveGroupSnapshot", "discover_groups", "fetch_group_snapshot"]
