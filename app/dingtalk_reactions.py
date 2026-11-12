"""钉钉用户消息上的处理中表情生命周期。

这里仅负责钉钉平台层的生命周期编排；实际 HTTP 请求由
``DingTalkCardHelper.set_processing_reaction`` 负责。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Iterable
from typing import Any


logger = logging.getLogger(__name__)

# 一次请求最多允许表情生命周期占用这段时间。helper 自己还会为单个 HTTP
# 操作设置超时；这里的 deadline 用来防止多个失败分支叠加后无限等待。
REACTION_LIFECYCLE_TIMEOUT_SECONDS = 5.0

# close() 在收到取消时不能把清理 task 一并取消；集合提供一个强引用，直到
# task 自己在上面的 deadline 内结束，避免事件循环只保留弱引用导致清理丢失。
_LIVE_CLEANUP_TASKS: set[asyncio.Task[Any]] = set()
_CREATE_TASK = asyncio.create_task


def _normalise_ids(values: Iterable[Any] | None) -> list[str]:
    """保留原顺序去重，并过滤空消息 ID。"""

    result: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        if value is None:
            continue
        value = str(value).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _message_id(message: Any) -> Any:
    if isinstance(message, dict):
        for key in ("message_id", "messageId", "msgId", "msg_id"):
            value = message.get(key)
            if value:
                return value
        return None
    return getattr(message, "message_id", None)


def collect_processing_message_ids(
    incoming_message: Any = None,
    *,
    buffered_context: Any = None,
    current_message_ids: Iterable[Any] | None = None,
    incoming_messages: Iterable[Any] | None = None,
) -> list[str]:
    """按钉钉当前消息来源的优先级收集 reaction 目标。

    只接受 handler 入口携带的原生消息对象和 ID。dws 展开的历史 ID、卡片
    ``outTrackId`` 以及 ``at_user_ids`` 都不属于当前用户消息，不能被拿来
    加 reaction。
    """

    if buffered_context is not None:
        buffered_ids = getattr(buffered_context, "current_message_ids", None)
        ids = _normalise_ids(buffered_ids)
        if ids:
            return ids

    ids = _normalise_ids(current_message_ids)
    if ids:
        return ids

    incoming_ids = _normalise_ids(
        _message_id(message) for message in (incoming_messages or ())
    )
    if incoming_ids:
        return incoming_ids

    return _normalise_ids([_message_id(incoming_message)])


class ProcessingReactionScope:
    """为一轮 AI 处理添加并最终移除“处理中”表情。

    add task 在进入核心处理前通过 ``shield`` 等待；cleanup 首先给 add task
    一个有界的收尾机会，再对所有“已经尝试过 add”的目标发 remove。即使 add
    请求在服务端已接收后才抛错，也不会留下本轮自己创建的表情。
    """

    def __init__(
        self,
        card_helper: Any,
        conversation_id: Any,
        message_ids: Iterable[Any] | None,
        *,
        timeout_seconds: float = REACTION_LIFECYCLE_TIMEOUT_SECONDS,
    ) -> None:
        self.card_helper = card_helper
        self.conversation_id = conversation_id
        self.message_ids = _normalise_ids(message_ids)
        self.timeout_seconds = max(0.0, float(timeout_seconds))
        self._deadline: float | None = None
        self._add_task: asyncio.Task[Any] | None = None
        self._cleanup_task: asyncio.Task[Any] | None = None
        self._attempted_message_ids: list[str] = []
        self._attempted_set: set[str] = set()

    async def start(self) -> None:
        """启动 add，并在不取消 add task 的前提下有界等待它。"""

        if self._add_task is not None:
            return
        loop = asyncio.get_running_loop()
        self._deadline = loop.time() + self.timeout_seconds
        self._add_task = _CREATE_TASK(self._add_all())
        try:
            await self._wait_task_until_deadline(self._add_task)
        except asyncio.CancelledError:
            # shield 已确保取消不会传播给 add task；由 finally 中的 close
            # 再决定是否还有预算等待它并执行 remove。
            raise
        except Exception as exc:  # pragma: no cover - 防御异常，不影响核心请求
            logger.debug("处理中 reaction add task 异常: %s", type(exc).__name__)

    async def close(self) -> None:
        """幂等地完成 bounded cleanup，且不把清理异常抛给调用方。"""

        if self._cleanup_task is None:
            self._cleanup_task = _CREATE_TASK(self._cleanup_impl())
            _LIVE_CLEANUP_TASKS.add(self._cleanup_task)
            self._cleanup_task.add_done_callback(_LIVE_CLEANUP_TASKS.discard)

        try:
            await asyncio.shield(self._cleanup_task)
        except asyncio.CancelledError:
            # 不取消 cleanup task，让其在自己的 5 秒 deadline 内完成。当前
            # task 的取消语义由调用方保留；强引用集合保证 task 不会丢失。
            raise
        except Exception as exc:  # pragma: no cover - cleanup_impl 已经吞掉
            logger.debug("处理中 reaction cleanup 异常: %s", type(exc).__name__)

    async def _add_all(self) -> None:
        if not self.message_ids:
            return
        await asyncio.gather(
            *(self._add_one(message_id) for message_id in self.message_ids),
            return_exceptions=True,
        )

    async def _add_one(self, message_id: str) -> None:
        self._mark_attempted(message_id)
        await self._call_helper(message_id, active=True)

    async def _cleanup_impl(self) -> None:
        if self._add_task is not None:
            completed = await self._wait_task_until_deadline(self._add_task)
            if not completed and not self._add_task.done():
                await self._cancel_task_bounded(self._add_task)

        targets = list(self._attempted_message_ids)
        if not targets:
            return

        # add 与 remove 各有独立的 bounded phase。helper 自身保证每个 HTTP
        # 操作总预算为 5 秒；不能因为 add 恰好用尽预算，就不给已尝试目标
        # 发 remove。
        self._deadline = asyncio.get_running_loop().time() + self.timeout_seconds
        remaining = self._remaining_seconds()
        if remaining <= 0:
            # add 已用完预算时，仍给 remove 一个 bounded opportunity；helper
            # 自身也有超时，直接调用比永久保留 reaction 更安全。
            remaining = 0.01
        remove_task = _CREATE_TASK(self._remove_all(targets))
        try:
            await asyncio.wait_for(asyncio.shield(remove_task), timeout=remaining)
        except asyncio.TimeoutError:
            await self._cancel_task_bounded(remove_task)
        except asyncio.CancelledError:
            await self._cancel_task_bounded(remove_task)
            raise
        except Exception as exc:  # pragma: no cover - gather return_exceptions 已兜底
            logger.debug("处理中 reaction remove task 异常: %s", type(exc).__name__)

    async def _remove_all(self, targets: list[str]) -> None:
        await asyncio.gather(
            *(self._call_helper(message_id, active=False) for message_id in targets),
            return_exceptions=True,
        )

    def _mark_attempted(self, message_id: str) -> None:
        if message_id not in self._attempted_set:
            self._attempted_set.add(message_id)
            self._attempted_message_ids.append(message_id)

    def _remaining_seconds(self) -> float:
        if self._deadline is None:
            return 0.0
        return max(0.0, self._deadline - asyncio.get_running_loop().time())

    async def _wait_task_until_deadline(self, task: asyncio.Task[Any]) -> bool:
        remaining = self._remaining_seconds()
        if task.done() or remaining <= 0:
            return task.done()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
        except asyncio.TimeoutError:
            return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("处理中 reaction task 异常: %s", type(exc).__name__)
        return task.done()

    async def _cancel_task_bounded(self, task: asyncio.Task[Any]) -> None:
        if task.done():
            return
        task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(task, return_exceptions=True),
                timeout=0.1,
            )
        except asyncio.TimeoutError:
            # 生产 helper 明确传播 CancelledError；不支持吞掉取消的替代实现。
            pass

    async def _call_helper(self, message_id: str, *, active: bool) -> bool:
        method = getattr(self.card_helper, "set_processing_reaction", None)
        if not callable(method):
            return False
        try:
            result = method(self.conversation_id, message_id, active=active)
            if not inspect.isawaitable(result):
                return False
            result = await result
            return result is not False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(
                "处理中 reaction %s 失败 conversation=%s message=%s: %s",
                "add" if active else "remove",
                self.conversation_id,
                message_id,
                type(exc).__name__,
            )
            return False


__all__ = ["ProcessingReactionScope", "collect_processing_message_ids"]
