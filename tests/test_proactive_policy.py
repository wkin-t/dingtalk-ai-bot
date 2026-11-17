"""工作日主动群聊纯策略的行为测试。"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

import pytest


BEIJING = timezone(timedelta(hours=8))


def test_in_working_hours_uses_beijing_boundaries_and_holiday_calendar():
    from app.proactive_policy import in_working_hours

    assert in_working_hours(datetime(2026, 11, 17, 9, 0, tzinfo=BEIJING))
    assert in_working_hours(datetime(2026, 11, 17, 11, 59, 59, tzinfo=BEIJING))
    assert not in_working_hours(datetime(2026, 11, 17, 12, 0, tzinfo=BEIJING))
    assert in_working_hours(datetime(2026, 11, 17, 14, 0, tzinfo=BEIJING))
    assert in_working_hours(datetime(2026, 11, 17, 17, 59, 59, tzinfo=BEIJING))
    assert not in_working_hours(datetime(2026, 11, 17, 18, 0, tzinfo=BEIJING))
    assert in_working_hours(datetime(2026, 11, 17, 1, 0, tzinfo=timezone.utc))
    assert not in_working_hours(datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc))
    assert not in_working_hours(datetime(2026, 10, 1, 10, 0, tzinfo=BEIJING))
    assert in_working_hours(datetime(2026, 10, 10, 10, 0, tzinfo=BEIJING))


def test_unknown_calendar_year_fails_closed():
    from app.proactive_policy import in_working_hours

    assert not in_working_hours(datetime(2035, 1, 2, 10, 0, tzinfo=BEIJING))


def test_calendar_dependency_unavailable_is_silent(monkeypatch):
    import app.proactive_policy as policy
    monkeypatch.setattr(policy, "is_workday", None)
    assert not policy.in_working_hours(datetime(2026, 11, 17, 10, 0, tzinfo=BEIJING))


def test_calendar_data_covers_next_year_holiday_and_workday():
    from app.proactive_policy import in_working_hours
    assert not in_working_hours(datetime(2027, 1, 1, 10, 0, tzinfo=BEIJING))
    assert in_working_hours(datetime(2027, 1, 4, 10, 0, tzinfo=BEIJING))


def test_build_daily_slots_are_workday_random_and_at_least_90_minutes_apart():
    from app.proactive_policy import build_daily_slots

    day = date(2026, 11, 17)
    first = build_daily_slots(day, random.Random(17))
    second = build_daily_slots(day, random.Random(17))

    assert first == second
    assert 2 <= len(first) <= 4
    assert all(isinstance(value, float) for value in first)
    assert all(later - earlier >= 90 * 60 for earlier, later in zip(first, first[1:]))
    local_times = [datetime.fromtimestamp(value, BEIJING) for value in first]
    assert all(
        (local.hour == 9 and 0 <= local.minute <= 59)
        or (local.hour == 10 and 0 <= local.minute <= 59)
        or (local.hour == 11 and 0 <= local.minute <= 59)
        or (local.hour in (14, 15, 16, 17))
        for local in local_times
    )
    assert all(local.date() == day for local in local_times)


def test_build_daily_slots_does_not_schedule_unknown_or_holiday_day():
    from app.proactive_policy import build_daily_slots

    assert build_daily_slots(date(2026, 10, 1), random.Random(1)) == []
    assert build_daily_slots(date(2035, 1, 2), random.Random(1)) == []


def test_last_human_activity_excludes_unknown_bots_recalled_future_and_wrong_cid():
    from app.proactive_policy import last_human_activity

    now = datetime(2026, 11, 17, 12, 0, tzinfo=BEIJING)
    expected = datetime(2026, 11, 17, 11, 55, tzinfo=BEIJING).timestamp()
    rows = [
        {
            "conversationId": "cid-1",
            "senderId": "human-1",
            "sendType": "user",
            "createTime": "2026-11-17 11:55:00",
        },
        {
            "conversationId": "cid-1",
            "senderId": "bot-2",
            "sendType": "user",
            "createTime": "2026-11-17 11:59:00",
        },
        {
            "conversationId": "cid-1",
            "senderId": "human-2",
            "sendType": "user",
            "createTime": "2026-11-17 11:58:00",
            "isRecalled": True,
        },
        {
            "conversationId": "cid-1",
            "senderId": "human-3",
            "sendType": "user",
            "createTime": "2026-11-17 12:01:00",
        },
        {
            "conversationId": "cid-2",
            "senderId": "human-4",
            "sendType": "user",
            "createTime": "2026-11-17 11:59:30",
        },
        {
            "conversationId": "cid-1",
            "senderId": "",
            "sendType": "unknown",
            "createTime": "2026-11-17 11:59:30",
        },
        {
            "conversationId": "cid-1",
            "senderId": "human-5",
            "sendType": "robot",
            "createTime": "2026-11-17 11:57:00",
        },
    ]

    assert last_human_activity(rows, "cid-1", now, {"bot-1", "bot-2"}) == expected


def test_last_human_activity_accepts_epoch_and_timezone_aware_timestamps():
    from app.proactive_policy import last_human_activity

    now = datetime(2026, 11, 17, 12, 0, tzinfo=BEIJING)
    timestamp = datetime(2026, 11, 17, 11, 45, tzinfo=BEIJING).timestamp()
    rows = [
        {"conversation_id": "cid", "sender_id": "u-1", "timestamp": timestamp},
        {
            "conversation_id": "cid",
            "sender_id": "u-2",
            "timestamp": "2026-11-17T03:40:00Z",
        },
        {"conversation_id": "cid", "sender_id": "u-3", "timestamp": "not-a-time"},
    ]

    assert last_human_activity(rows, "cid", now, set()) == timestamp


def test_select_speaker_is_stable_and_avoids_consecutive_bot_when_possible():
    from app.proactive_policy import select_speaker

    bots = ["bot-b", "bot-a", "bot-c"]
    selected = select_speaker("seed-1", 2, bots)

    assert selected == select_speaker("seed-1", 2, list(reversed(bots)))
    assert selected in bots
    assert select_speaker("seed-1", 2, bots, last_bot=selected) != selected
    assert select_speaker("seed-1", 2, [selected], last_bot=selected) == selected
    assert select_speaker("seed-1", 2, [], last_bot="") == ""


def test_pathological_rng_fallback_never_schedules_lunch():
    from app.proactive_policy import build_daily_slots, in_working_hours
    class BrokenRng:
        def randint(self, start, end):
            return 3
        def sample(self, values, count):
            return [0, 0, 0]
    slots = build_daily_slots(date(2026,11,17), BrokenRng())
    assert len(slots) == 3
    assert all(in_working_hours(datetime.fromtimestamp(slot, BEIJING)) for slot in slots)
    assert all(b-a >= 5400 for a,b in zip(slots, slots[1:]))
