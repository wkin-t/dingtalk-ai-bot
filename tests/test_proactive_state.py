"""主动群聊共享 Redis 状态的行为测试。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import fakeredis
import pytest


BEIJING = timezone(timedelta(hours=8))
DAY = date(2026, 11, 17)
SLOT_0 = datetime(2026, 11, 17, 10, 0, tzinfo=BEIJING).timestamp()
SLOTS = [SLOT_0, SLOT_0 + 90 * 60, SLOT_0 + 4 * 60 * 60]


@pytest.fixture
def redis_client():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def store(redis_client, tmp_path):
    from app.proactive_state import ProactiveStore

    return ProactiveStore(redis_client, state_dir=tmp_path)


def _register(store, corp="corp-1", cid="cid-1", *bots):
    for bot in bots:
        store.heartbeat(corp, bot, SLOT_0)
        store.register_group(corp, cid, bot, SLOT_0)


def test_heartbeat_and_group_registration_are_scoped_and_ttl_bounded(store, redis_client):
    _register(store, "corp-1", "cid-1", "bot-a", "bot-b")

    assert store.eligible_bots("corp-1", "cid-1", SLOT_0) == ["bot-a", "bot-b"]
    assert store.eligible_bots("corp-1", "cid-2", SLOT_0) == []
    assert store.eligible_bots("corp-2", "cid-1", SLOT_0) == []

    heartbeat_ttls = [
        redis_client.ttl(key)
        for key in redis_client.scan_iter(match="*heartbeat:*")
    ]
    member_ttls = [
        redis_client.ttl(key)
        for key in redis_client.scan_iter(match="*member*")
    ]
    assert heartbeat_ttls and all(170 <= ttl <= 180 for ttl in heartbeat_ttls)
    assert member_ttls and all(1790 <= ttl <= 1800 for ttl in member_ttls)


def test_plan_is_created_once_with_48_hour_ttl_and_survives_new_store(redis_client, tmp_path):
    from app.proactive_state import ProactiveStore

    first = ProactiveStore(redis_client, state_dir=tmp_path)
    plan = first.get_or_create_plan("corp-1", "cid-1", DAY, SLOTS)
    second = ProactiveStore(redis_client, state_dir=tmp_path)
    same_plan = second.get_or_create_plan(
        "corp-1", "cid-1", DAY, [SLOT_0, SLOT_0 + 5400, SLOT_0 + 4 * 60 * 60, SLOT_0 + 5.5 * 60 * 60]
    )

    assert set(plan) == {"seed", "slots", "day"}
    assert plan["day"] == DAY.isoformat()
    assert plan["slots"] == SLOTS
    assert same_plan == plan
    plan_ttls = [redis_client.ttl(key) for key in redis_client.scan_iter(match="*plan*")]
    assert plan_ttls and all(172700 <= ttl <= 172800 for ttl in plan_ttls)


def test_epoch_is_file_bound_and_redis_key_never_expires(redis_client, tmp_path):
    from app.proactive_state import ProactiveStore

    store = ProactiveStore(redis_client, state_dir=tmp_path)
    epoch = store.ensure_epoch("corp-1")
    epoch_files = list(tmp_path.glob("*.epoch"))
    epoch_keys = list(redis_client.scan_iter(match="*epoch*"))

    assert epoch and len(epoch_files) == 1
    assert epoch_files[0].read_text(encoding="utf-8") == epoch
    assert epoch_keys and all(redis_client.ttl(key) == -1 for key in epoch_keys)
    assert ProactiveStore(redis_client, state_dir=tmp_path).ensure_epoch("corp-1") == epoch


def test_flushdb_epoch_loss_rejects_new_plan_and_claim(redis_client, tmp_path):
    from app.proactive_state import ProactiveStateError, ProactiveStore

    store = ProactiveStore(redis_client, state_dir=tmp_path)
    _register(store, "corp-1", "cid-1", "bot-a")
    store.get_or_create_plan("corp-1", "cid-1", DAY, SLOTS)
    redis_client.flushdb()

    with pytest.raises(ProactiveStateError):
        store.get_or_create_plan("corp-1", "cid-1", DAY, SLOTS)
    with pytest.raises(ProactiveStateError):
        store.claim("corp-1", "cid-1", DAY, 0, "bot-a", SLOT_0)


def test_robot_pin_and_group_sender_identity_reject_mixed_metadata(redis_client, tmp_path):
    from app.proactive_state import ProactiveStateError, ProactiveStore

    trusted = ProactiveStore(
        redis_client,
        state_dir=tmp_path,
        bot_id="bot-a",
        robot_code="robot-a",
        instance_id="instance-a",
    )
    trusted.heartbeat("corp-1", "bot-a", SLOT_0)
    trusted.register_group("corp-1", "cid-1", "bot-a", SLOT_0, bot_sender_id="sender-a")
    trusted.register_group("corp-1", "cid-1", "bot-a", SLOT_0, bot_sender_id="sender-a")

    with pytest.raises(ProactiveStateError):
        trusted.register_group("corp-1", "cid-1", "bot-a", SLOT_0, bot_sender_id="sender-b")
    with pytest.raises(ProactiveStateError):
        ProactiveStore(
            redis_client,
            state_dir=tmp_path,
            bot_id="bot-a",
            robot_code="robot-b",
            instance_id="instance-b",
        ).heartbeat("corp-1", "bot-a", SLOT_0)


def test_matches_group_identity_is_read_only_and_fail_closed(redis_client, tmp_path):
    from app.proactive_state import ProactiveStore

    store = ProactiveStore(redis_client, state_dir=tmp_path)
    identity_key = store._group_identity_key("corp-1", "cid-1", "bot-a")

    assert not store.matches_group_identity("corp-1", "cid-1", "bot-a", "sender-a")
    assert not redis_client.exists(identity_key)
    store.register_group("corp-1", "cid-1", "bot-a", SLOT_0, bot_sender_id="sender-a")
    assert store.matches_group_identity("corp-1", "cid-1", "bot-a", "sender-a")
    assert not store.matches_group_identity("corp-1", "cid-1", "bot-a", "sender-b")
    assert redis_client.get(identity_key) == "sender-a"


def test_same_bot_with_two_live_instances_is_not_eligible(redis_client, tmp_path):
    from app.proactive_state import ProactiveStore

    first = ProactiveStore(
        redis_client,
        state_dir=tmp_path,
        bot_id="bot-a",
        robot_code="robot-a",
        instance_id="instance-a",
    )
    second = ProactiveStore(
        redis_client,
        state_dir=tmp_path,
        bot_id="bot-a",
        robot_code="robot-a",
        instance_id="instance-b",
    )
    for store in (first, second):
        store.heartbeat("corp-1", "bot-a", SLOT_0)
        store.register_group("corp-1", "cid-1", "bot-a", SLOT_0)

    assert first.eligible_bots("corp-1", "cid-1", SLOT_0) == []
    assert second.eligible_bots("corp-1", "cid-1", SLOT_0) == []


def test_three_instances_share_global_unique_roster_and_only_winner_claims(redis_client, tmp_path):
    from app.proactive_policy import select_speaker
    from app.proactive_state import ProactiveStore

    stores = [
        ProactiveStore(redis_client, state_dir=tmp_path, instance_id=f"instance-{bot}")
        for bot in ("bot-a", "bot-b", "bot-c")
    ]
    for store, bot in zip(stores, ("bot-a", "bot-b", "bot-c")):
        store.heartbeat("corp-1", bot, SLOT_0)
        store.register_group("corp-1", "cid-1", bot, SLOT_0)
    plan = stores[0].get_or_create_plan("corp-1", "cid-1", DAY, SLOTS)
    expected = select_speaker(plan["seed"], 0, ["bot-a", "bot-b", "bot-c"])

    assert stores[0].eligible_bots("corp-1", "cid-1", SLOT_0) == ["bot-a", "bot-b", "bot-c"]
    barrier = threading.Barrier(3)

    def claim(store, bot):
        barrier.wait()
        return store.claim("corp-1", "cid-1", DAY, 0, bot, SLOT_0)

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(
            executor.map(
                lambda args: claim(*args),
                list(zip(stores, ("bot-a", "bot-b", "bot-c"))),
            )
        )

    assert [bot for bot, token in zip(("bot-a", "bot-b", "bot-c"), results) if token] == [expected]


def test_winner_offline_is_reselected_and_recovery_cannot_duplicate(redis_client, tmp_path):
    from app.proactive_policy import select_speaker
    from app.proactive_state import ProactiveStore

    stores = [
        ProactiveStore(redis_client, state_dir=tmp_path, instance_id=f"instance-{bot}")
        for bot in ("bot-a", "bot-b", "bot-c")
    ]
    bots = ("bot-a", "bot-b", "bot-c")
    for store, bot in zip(stores, bots):
        store.heartbeat("corp-1", bot, SLOT_0)
        store.register_group("corp-1", "cid-1", bot, SLOT_0)
    plan = stores[0].get_or_create_plan("corp-1", "cid-1", DAY, SLOTS)
    original = select_speaker(plan["seed"], 0, bots)
    original_store = stores[bots.index(original)]
    redis_client.delete(original_store._heartbeat_key("corp-1", original))
    available = stores[0].eligible_bots("corp-1", "cid-1", SLOT_0)
    replacement = select_speaker(plan["seed"], 0, available)
    replacement_store = stores[bots.index(replacement)]

    token = replacement_store.claim("corp-1", "cid-1", DAY, 0, replacement, SLOT_0)
    assert token
    replacement_store.finish("corp-1", "cid-1", token)
    original_store.heartbeat("corp-1", original, SLOT_0)
    original_store.register_group("corp-1", "cid-1", original, SLOT_0)
    assert original_store.claim("corp-1", "cid-1", DAY, 0, original, SLOT_0) is None


def test_late_slot_claim_uses_actual_claim_time_for_next_90_minute_cooldown(store):
    late_slot_1 = datetime(2026, 11, 17, 14, 0, tzinfo=BEIJING).timestamp()
    late_slot_2 = datetime(2026, 11, 17, 15, 30, tzinfo=BEIJING).timestamp()
    late_slots = [SLOT_0, late_slot_1, late_slot_2]
    _register(store, "corp-1", "cid-1", "bot-a")
    store.get_or_create_plan("corp-1", "cid-1", DAY, late_slots)
    late_claim_time = late_slot_1 + 9 * 60
    store.heartbeat("corp-1", "bot-a", late_claim_time)
    store.register_group("corp-1", "cid-1", "bot-a", late_claim_time)
    token = store.claim("corp-1", "cid-1", DAY, 1, "bot-a", late_claim_time)
    assert token
    store.finish("corp-1", "cid-1", token)
    store.heartbeat("corp-1", "bot-a", late_slot_2)
    store.register_group("corp-1", "cid-1", "bot-a", late_slot_2)

    assert store.claim("corp-1", "cid-1", DAY, 2, "bot-a", late_slot_2) is None
    store.heartbeat("corp-1", "bot-a", late_slot_2 + 9 * 60)
    store.register_group("corp-1", "cid-1", "bot-a", late_slot_2 + 9 * 60)
    assert store.claim("corp-1", "cid-1", DAY, 2, "bot-a", late_slot_2 + 9 * 60) is not None


def test_claim_owns_finish_reject_epoch_change_inside_lua(redis_client, tmp_path):
    from app.proactive_state import ProactiveStateError, ProactiveStore

    claim_dir = tmp_path / "claim"
    store = ProactiveStore(redis_client, state_dir=claim_dir, instance_id="instance-a")
    from app.proactive_policy import select_speaker

    _register(store, "corp-1", "cid-1", "bot-a")
    plan = store.get_or_create_plan("corp-1", "cid-1", DAY, SLOTS)
    winner = select_speaker(plan["seed"], 0, ["bot-a"])

    original_eval = redis_client.eval

    def flush_before_eval(*args, **kwargs):
        redis_client.flushdb()
        return original_eval(*args, **kwargs)

    with patch.object(redis_client, "eval", side_effect=flush_before_eval):
        with pytest.raises(ProactiveStateError):
            store.claim("corp-1", "cid-1", DAY, 0, winner, SLOT_0)

    other_epoch = "00000000-0000-0000-0000-000000000001"
    owns_store = ProactiveStore(redis_client, state_dir=tmp_path / "owns", instance_id="instance-b")
    expected_epoch = owns_store.ensure_epoch("corp-1")
    redis_client.set(owns_store._lease_key("corp-1", "cid-1"), "token")
    original_eval = redis_client.eval

    def replace_before_eval(*args, **kwargs):
        redis_client.set(owns_store._epoch_key("corp-1"), other_epoch)
        return original_eval(*args, **kwargs)

    with patch.object(redis_client, "eval", side_effect=replace_before_eval):
        with pytest.raises(ProactiveStateError):
            owns_store.owns("corp-1", "cid-1", "token")

    redis_client.set(owns_store._epoch_key("corp-1"), expected_epoch)
    finish_store = ProactiveStore(redis_client, state_dir=tmp_path / "owns", instance_id="instance-c")
    finish_store.ensure_epoch("corp-1")
    redis_client.set(finish_store._lease_key("corp-1", "cid-1"), "token")

    def replace_finish_epoch(*args, **kwargs):
        redis_client.set(finish_store._epoch_key("corp-1"), other_epoch)
        return original_eval(*args, **kwargs)

    with patch.object(redis_client, "eval", side_effect=replace_finish_epoch):
        with pytest.raises(ProactiveStateError):
            finish_store.finish("corp-1", "cid-1", "token")


def test_plan_rejects_invalid_slots_without_writing_partial_state(store, redis_client):
    from app.proactive_state import ProactiveStateError

    with pytest.raises(ProactiveStateError):
        store.get_or_create_plan("corp-1", "cid-1", DAY, [SLOT_0])
    assert list(redis_client.scan_iter(match="*plan*")) == []


def test_plan_delete_with_mirror_present_rejects_recreation(store, redis_client):
    from app.proactive_state import ProactiveStateError

    _register(store, "corp-1", "cid-1", "bot-a")
    store.get_or_create_plan("corp-1", "cid-1", DAY, SLOTS)
    redis_client.delete(store._plan_key("corp-1", "cid-1", DAY))

    with pytest.raises(ProactiveStateError):
        store.get_or_create_plan("corp-1", "cid-1", DAY, SLOTS)
    with pytest.raises(ProactiveStateError):
        store.claim("corp-1", "cid-1", DAY, 0, "bot-a", SLOT_0)


def test_daily_state_delete_or_slot_marker_delete_is_fail_closed(store, redis_client):
    from app.proactive_state import ProactiveStateError

    _register(store, "corp-1", "cid-1", "bot-a")
    store.get_or_create_plan("corp-1", "cid-1", DAY, SLOTS)
    daily_key = store._daily_state_key("corp-1", "cid-1", DAY)
    redis_client.delete(daily_key)

    with pytest.raises(ProactiveStateError):
        store.get_or_create_plan("corp-1", "cid-1", DAY, SLOTS)
    with pytest.raises(ProactiveStateError):
        store.claim("corp-1", "cid-1", DAY, 0, "bot-a", SLOT_0)

    cid = "cid-slot-marker"
    store.register_group("corp-1", cid, "bot-a", SLOT_0)
    store.get_or_create_plan("corp-1", cid, DAY, SLOTS)
    marker_key = store._daily_state_key("corp-1", cid, DAY)
    redis_client.hdel(marker_key, "slot:0")
    with pytest.raises(ProactiveStateError):
        store.get_or_create_plan("corp-1", cid, DAY, SLOTS)
    with pytest.raises(ProactiveStateError):
        store.claim("corp-1", cid, DAY, 0, "bot-a", SLOT_0)


@pytest.mark.parametrize("missing", ["last_attempt", "last_speaker"])
def test_consumed_slot_requires_last_attempt_and_last_speaker(store, redis_client, missing):
    from app.proactive_state import ProactiveStateError

    cid = f"cid-missing-{missing}"
    _register(store, "corp-1", cid, "bot-a")
    store.get_or_create_plan("corp-1", cid, DAY, SLOTS)
    token = store.claim("corp-1", cid, DAY, 0, "bot-a", SLOT_0)
    assert token
    store.finish("corp-1", cid, token)
    if missing == "last_attempt":
        redis_client.hdel(store._daily_state_key("corp-1", cid, DAY), "last_attempt")
    else:
        redis_client.delete(store._last_speaker_key("corp-1", cid))

    with pytest.raises(ProactiveStateError):
        store.claim("corp-1", cid, DAY, 0, "bot-a", SLOT_0)


def test_claim_is_atomic_across_store_instances_and_owns_is_token_bound(redis_client, tmp_path):
    from app.proactive_state import ProactiveStore

    first = ProactiveStore(redis_client, state_dir=tmp_path)
    second = ProactiveStore(redis_client, state_dir=tmp_path)
    _register(first, "corp-1", "cid-1", "bot-a")
    _register(second, "corp-1", "cid-1", "bot-b")
    first.get_or_create_plan("corp-1", "cid-1", DAY, SLOTS)

    barrier = threading.Barrier(2)

    def claim(store, bot):
        barrier.wait()
        return store.claim("corp-1", "cid-1", DAY, 0, bot, SLOT_0)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda args: claim(*args),
                [(first, "bot-a"), (second, "bot-b")],
            )
        )

    tokens = [token for token in results if token is not None]
    assert len(tokens) == 1
    token = tokens[0]
    assert first.owns("corp-1", "cid-1", token)
    assert not first.owns("corp-1", "cid-1", "wrong-token")


def test_claim_consumes_slot_and_finish_requires_same_owner(store):
    from app.proactive_policy import select_speaker

    _register(store, "corp-1", "cid-1", "bot-a", "bot-b")
    plan = store.get_or_create_plan("corp-1", "cid-1", DAY, SLOTS)
    winner = select_speaker(plan["seed"], 0, ["bot-a", "bot-b"])
    other = "bot-b" if winner == "bot-a" else "bot-a"

    token = store.claim("corp-1", "cid-1", DAY, 0, winner, SLOT_0)
    assert token
    assert store.claim("corp-1", "cid-1", DAY, 0, other, SLOT_0) is None
    store.finish("corp-1", "cid-1", "wrong-token")
    assert store.owns("corp-1", "cid-1", token)
    store.finish("corp-1", "cid-1", token)
    assert not store.owns("corp-1", "cid-1", token)
    assert store.claim("corp-1", "cid-1", DAY, 0, other, SLOT_0) is None


def test_claim_rejects_local_winner_drift_without_consuming_slot(store):
    from app.proactive_policy import select_speaker

    _register(store, "corp-1", "cid-1", "bot-a", "bot-b")
    plan = store.get_or_create_plan("corp-1", "cid-1", DAY, SLOTS)
    winner = select_speaker(plan["seed"], 0, ["bot-a", "bot-b"])
    wrong = "bot-b" if winner == "bot-a" else "bot-a"

    assert store.claim("corp-1", "cid-1", DAY, 0, wrong, SLOT_0) is None
    assert store.claim("corp-1", "cid-1", DAY, 0, winner, SLOT_0)


def test_cooldown_rejection_does_not_consume_a_later_slot(store):
    from app.proactive_policy import select_speaker

    _register(store, "corp-1", "cid-1", "bot-a", "bot-b")
    plan = store.get_or_create_plan("corp-1", "cid-1", DAY, SLOTS)
    first_winner = select_speaker(plan["seed"], 0, ["bot-a", "bot-b"])

    token = store.claim("corp-1", "cid-1", DAY, 0, first_winner, SLOT_0 + 60)
    assert token
    store.finish("corp-1", "cid-1", token)
    second_winner = select_speaker(plan["seed"], 1, ["bot-a", "bot-b"], first_winner)
    assert store.claim("corp-1", "cid-1", DAY, 1, second_winner, SLOT_0 + 90 * 60) is None
    store.heartbeat("corp-1", second_winner, SLOT_0 + 90 * 60 + 60)
    store.register_group("corp-1", "cid-1", second_winner, SLOT_0 + 90 * 60 + 60)
    next_token = store.claim("corp-1", "cid-1", DAY, 1, second_winner, SLOT_0 + 90 * 60 + 60)
    assert next_token


def test_claim_rejects_out_of_bounds_future_expired_and_unregistered_bot(store):
    _register(store, "corp-1", "cid-1", "bot-a")
    store.get_or_create_plan("corp-1", "cid-1", DAY, SLOTS)

    assert store.claim("corp-1", "cid-1", DAY, -1, "bot-a", SLOT_0) is None
    assert store.claim("corp-1", "cid-1", DAY, len(SLOTS), "bot-a", SLOT_0) is None
    assert store.claim("corp-1", "cid-1", DAY, 1, "bot-a", SLOT_0 - 1) is None
    assert store.claim("corp-1", "cid-1", DAY, 0, "bot-x", SLOT_0) is None
    assert store.claim("corp-1", "cid-1", DAY, 0, "bot-a", SLOT_0 + 10 * 60 + 1) is None


def test_corrupt_plan_is_fail_closed_and_redis_failures_are_not_fallback(store, redis_client, tmp_path):
    from app.proactive_state import ProactiveStateError, ProactiveStore

    key = store._plan_key("corp-1", "cid-1", DAY)
    redis_client.set(key, "{not-json")
    with pytest.raises(ProactiveStateError):
        store.get_or_create_plan("corp-1", "cid-1", DAY, SLOTS)

    class BrokenRedis:
        def ping(self):
            raise OSError("redis unavailable")

        def __getattr__(self, name):
            def fail(*args, **kwargs):
                raise OSError("redis unavailable")

            return fail

    broken = ProactiveStore(BrokenRedis(), state_dir=tmp_path)
    with pytest.raises(OSError, match="redis unavailable"):
        broken.heartbeat("corp-1", "bot-a", SLOT_0)


def test_claim_lua_rejects_corrupt_plan_even_without_python_readback(store, redis_client):
    from app.proactive_state import ProactiveStateError

    _register(store, "corp-1", "cid-1", "bot-a")
    store.get_or_create_plan("corp-1", "cid-1", DAY, SLOTS)
    redis_client.set(
        store._plan_key("corp-1", "cid-1", DAY),
        '{"day":"2026-11-17","seed":"","slots":[1794880800,1794886200]}',
    )

    with pytest.raises(ProactiveStateError):
        store.claim("corp-1", "cid-1", DAY, 0, "bot-a", SLOT_0)


def test_last_speaker_is_shared_and_updated_by_claim(store):
    from app.proactive_policy import select_speaker

    _register(store, "corp-1", "cid-1", "bot-a", "bot-b")
    plan = store.get_or_create_plan("corp-1", "cid-1", DAY, SLOTS)
    assert store.last_speaker("corp-1", "cid-1") == ""

    winner = select_speaker(plan["seed"], 0, ["bot-a", "bot-b"])
    token = store.claim("corp-1", "cid-1", DAY, 0, winner, SLOT_0)
    assert token
    assert store.last_speaker("corp-1", "cid-1") == winner
