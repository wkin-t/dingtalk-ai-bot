"""处理表情回应生命周期的离线测试。"""

import asyncio
from unittest.mock import AsyncMock

import pytest


class FakeReactionClient:
    def __init__(self, *, side_effect=None, return_value=True):
        self.set_processing_reaction = AsyncMock(
            side_effect=side_effect,
            return_value=return_value,
        )


def test_collect_processing_message_ids_follows_source_priority_and_fallbacks():
    from app.dingtalk_reactions import collect_processing_message_ids

    incoming = {"message_id": "single"}
    assert collect_processing_message_ids(
        incoming,
        current_message_ids=["", "explicit", "explicit"],
        incoming_messages=[{"message_id": "incoming"}],
    ) == ["explicit"]
    assert collect_processing_message_ids(
        incoming,
        incoming_messages=[{"message_id": "incoming"}, {"message_id": "incoming"}],
    ) == ["incoming"]
    assert collect_processing_message_ids(incoming) == ["single"]


@pytest.mark.asyncio
async def test_scope_adds_unique_targets_and_removes_every_attempted_target():
    from app.dingtalk_reactions import ProcessingReactionScope

    client = FakeReactionClient()
    scope = ProcessingReactionScope(
        client,
        "conversation-1",
        ["m1", "", "m1", "m2"],
    )

    await scope.start()
    await scope.close()

    assert client.set_processing_reaction.await_count == 4
    calls = [call.args for call in client.set_processing_reaction.await_args_list]
    assert calls == [
        ("conversation-1", "m1"),
        ("conversation-1", "m2"),
        ("conversation-1", "m1"),
        ("conversation-1", "m2"),
    ]
    assert [call.kwargs["active"] for call in client.set_processing_reaction.await_args_list] == [
        True,
        True,
        False,
        False,
    ]


@pytest.mark.asyncio
async def test_scope_continues_after_one_add_failure_and_cleans_failed_attempt():
    from app.dingtalk_reactions import ProcessingReactionScope

    client = FakeReactionClient(side_effect=[RuntimeError("add failed"), True, False])
    scope = ProcessingReactionScope(client, "conversation-2", ["m1", "m2"])

    await scope.start()
    await scope.close()

    calls = [call.args + (call.kwargs["active"],) for call in client.set_processing_reaction.await_args_list]
    assert calls == [
        ("conversation-2", "m1", True),
        ("conversation-2", "m2", True),
        ("conversation-2", "m1", False),
        ("conversation-2", "m2", False),
    ]


@pytest.mark.asyncio
async def test_scope_close_does_not_replace_core_cancellation_or_cleanup_failure():
    from app.dingtalk_reactions import ProcessingReactionScope

    async def reaction(*_args, active):
        if not active:
            raise RuntimeError("remove failed")
        return True

    client = FakeReactionClient(side_effect=reaction)
    scope = ProcessingReactionScope(client, "conversation-3", ["m1"])
    await scope.start()

    await scope.close()
    assert client.set_processing_reaction.await_count == 2


@pytest.mark.asyncio
async def test_scope_add_timeout_is_bounded_and_does_not_leave_running_task():
    from app.dingtalk_reactions import ProcessingReactionScope

    started = asyncio.Event()
    finished = asyncio.Event()

    async def reaction(*_args, active):
        if active:
            started.set()
            await finished.wait()
        return True

    client = FakeReactionClient(side_effect=reaction)
    scope = ProcessingReactionScope(
        client,
        "conversation-4",
        ["m1"],
        timeout_seconds=0.05,
    )

    await scope.start()
    await started.wait()
    await scope.close()

    assert client.set_processing_reaction.await_count >= 1
    assert scope._add_task is not None and scope._add_task.done()
