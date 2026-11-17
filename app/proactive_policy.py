"""工作日主动群聊的纯策略函数。

本模块不读取配置、不访问网络或状态存储；调度层把当前 DWS 快照和随机源
传入这里，避免把不确定的外部状态误当成策略事实。
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Iterable, Mapping
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

try:
    from chinese_calendar import is_workday
except ImportError:
    # 可选主动功能缺少日历时保持静默，不能连带阻止正常 Stream 服务启动。
    is_workday = None


BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")
MIN_SLOT_GAP_SECONDS = 90 * 60
_WORKDAY_MINUTES = tuple(range(0, 180)) + tuple(range(300, 540))
_CID_KEYS = ("conversationId", "conversation_id", "cid")
_SENDER_ID_KEYS = (
    "senderId",
    "sender_id",
    "senderOpenDingTalkId",
    "sender_open_dingtalk_id",
    "openDingtalkId",
    "open_dingtalk_id",
)
_TYPE_KEYS = ("senderType", "sender_type", "sendType", "send_type")
_TIMESTAMP_KEYS = (
    "timestamp",
    "createTime",
    "create_time",
    "createdAt",
    "created_at",
    "time",
)
_RECALL_KEYS = (
    "isRecalled",
    "is_recalled",
    "isRecall",
    "is_recall",
    "recalled",
    "recall",
    "isRevoked",
    "is_revoked",
    "revoked",
    "withdrawn",
    "isWithdrawn",
    "is_withdrawn",
)
_RECALL_STATUS_KEYS = (
    "recallStatus",
    "recall_status",
    "messageStatus",
    "message_status",
    "status",
    "state",
    "eventType",
    "event_type",
    "event",
)
_ROBOT_TYPES = frozenset({"bot", "robot", "assistant", "model", "system", "service"})
_HUMAN_TYPES = frozenset({"user", "human", "member", "person"})
_UNKNOWN_TYPES = frozenset({"", "unknown", "unk", "null", "none", "n/a"})
_UNKNOWN_SENDERS = frozenset({"", "unknown", "unk", "null", "none", "?", "-"})


def _as_beijing(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=BEIJING)
    return value.astimezone(BEIJING)


def _calendar_workday(day: date) -> bool:
    try:
        return bool(is_workday(day))
    except Exception:
        # 日历库对未覆盖年份和损坏的输入都会抛异常；未知日历不能安全地发消息。
        return False


def in_working_hours(now: datetime) -> bool:
    """判断北京时间下当前时刻是否处于法定工作日的发送窗口。"""

    if not isinstance(now, datetime):
        return False
    local = _as_beijing(now)
    if not _calendar_workday(local.date()):
        return False
    current = local.time()
    return time(9, 0) <= current < time(12, 0) or time(14, 0) <= current < time(18, 0)


def _day_value(day: date | datetime) -> date | None:
    if isinstance(day, datetime):
        return _as_beijing(day).date()
    if isinstance(day, date):
        return day
    return None


def _slot_epoch(day: date, minute_offset: int) -> float:
    local = datetime.combine(day, time(9, 0), tzinfo=BEIJING) + timedelta(minutes=minute_offset)
    return local.timestamp()


def _valid_slot_offsets(offsets: Iterable[int]) -> bool:
    ordered = sorted(offsets)
    return all(
        later - earlier >= MIN_SLOT_GAP_SECONDS // 60
        for earlier, later in zip(ordered, ordered[1:])
    )


def build_daily_slots(day: date | datetime, rng: random.Random) -> list[float]:
    """为一个法定工作日生成 2–4 个随机发送时隙。

    时隙按分钟取整，午休和下班边界不包含在候选集合中；随机源由调用方注入，
    这样每日计划可以在 Redis 之外被独立复现和审计。
    """

    target_day = _day_value(day)
    if target_day is None or not _calendar_workday(target_day):
        return []
    if not hasattr(rng, "randint") or not hasattr(rng, "sample"):
        return []

    count = int(rng.randint(2, 4))
    candidates = list(_WORKDAY_MINUTES)
    for _ in range(5000):
        offsets = sorted(rng.sample(candidates, count))
        if _valid_slot_offsets(offsets):
            return [_slot_epoch(target_day, offset) for offset in offsets]

    # 极端自定义随机源可能反复给出不可行样本；仍以固定可行模板保持策略
    # 的安全边界，不能因随机源异常把时隙放到午休或相邻窗口。
    templates = {
        2: (30, 360),
        3: (15, 120, 360),
        4: (0, 120, 300, 420),
    }
    return [_slot_epoch(target_day, offset) for offset in templates[count]]


def _first_value(row: Mapping[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return value
    return None


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    return str(value).strip() if value is not None else ""


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "recalled", "revoked", "withdrawn"}
    return bool(value)


def _is_recalled(row: Mapping[str, Any]) -> bool:
    if any(_truthy(row.get(key)) for key in _RECALL_KEYS if key in row):
        return True
    statuses = {_text(row.get(key)).lower() for key in _RECALL_STATUS_KEYS if key in row}
    return bool(statuses & {"recalled", "recall", "revoked", "withdrawn", "deleted"})


def _timestamp(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        if not math.isfinite(result):
            return None
        if result > 100_000_000_000:
            result /= 1000
        return result
    if isinstance(value, datetime):
        return _as_beijing(value).timestamp()
    text = _text(value)
    if not text:
        return None
    try:
        numeric = float(text)
    except ValueError:
        numeric = None
    if numeric is not None:
        if not math.isfinite(numeric):
            return None
        return numeric / 1000 if numeric > 100_000_000_000 else numeric
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
    return _as_beijing(parsed).timestamp()


def _row_is_human(row: Mapping[str, Any], bot_ids: set[str]) -> bool:
    sender_id = _text(_first_value(row, _SENDER_ID_KEYS))
    if sender_id.lower() in {item.lower() for item in bot_ids}:
        return False
    if sender_id.lower() in _UNKNOWN_SENDERS:
        sender_id = ""

    types = {_text(row.get(key)).lower() for key in _TYPE_KEYS if key in row}
    if types & _ROBOT_TYPES:
        return False
    if types & _UNKNOWN_TYPES and not (types & _HUMAN_TYPES):
        return False
    if types & _HUMAN_TYPES:
        return True
    return bool(sender_id)


def last_human_activity(
    rows: Iterable[Mapping[str, Any]],
    cid: str,
    now: datetime,
    bot_ids: Iterable[str],
) -> float | None:
    """返回当前会话截至 ``now`` 的最新真人消息时间戳。"""

    if not isinstance(now, datetime) or not isinstance(cid, str) or not cid:
        return None
    cutoff = _as_beijing(now).timestamp()
    known_bots = {_text(bot) for bot in bot_ids if _text(bot)}
    latest: float | None = None
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if _text(_first_value(row, _CID_KEYS)) != cid:
            continue
        if _is_recalled(row) or not _row_is_human(row, known_bots):
            continue
        timestamp = _timestamp(_first_value(row, _TIMESTAMP_KEYS))
        if timestamp is None or timestamp > cutoff:
            continue
        if latest is None or timestamp > latest:
            latest = timestamp
    return latest


def select_speaker(seed: Any, slot_index: int, bot_ids: Iterable[str], last_bot: str = "") -> str:
    """按日计划种子稳定选择机器人，并尽量避免连续由同一机器人发言。"""

    candidates = sorted({_text(bot) for bot in bot_ids if _text(bot)})
    if not candidates:
        return ""
    ranked = sorted(
        candidates,
        key=lambda bot: hashlib.sha1(
            f"{seed!s}:{slot_index}:{bot}".encode("utf-8")
        ).hexdigest(),
    )
    previous = _text(last_bot)
    if len(ranked) > 1 and previous in ranked:
        ranked = [bot for bot in ranked if bot != previous]
    return ranked[0]


__all__ = [
    "BEIJING",
    "build_daily_slots",
    "in_working_hours",
    "last_human_activity",
    "select_speaker",
]
