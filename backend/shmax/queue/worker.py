"""Background worker that drains the persistent delivery queue.

The worker is a pure function over the queue repository. The
caller (the bridge runtime) is responsible for actually
sending the message via an adapter; this module only owns the
state-machine and retry / backoff / dead-letter policy.

We use exponential backoff with full jitter::

    delay = min(cap, base * 2 ** attempt) ; sleep random(0, delay)

A configurable ``max_attempts`` defaults to 8 (≈ ~17 minutes
total before giving up with ``base=1s`` and ``cap=300s``).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import random
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from .repository import DeliveryQueueRepository, QueueRecord


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryPolicy:
    base_seconds: float = 1.0
    cap_seconds: float = 300.0
    max_attempts: int = 8

    def next_delay(self, attempt: int) -> float:
        """Return the next delay in seconds (before jitter)."""
        # attempt is 0-based; first retry uses base_seconds.
        raw = self.base_seconds * (2 ** max(0, attempt))
        return min(self.cap_seconds, raw)


# A delivery handler receives the QueueRecord and returns
# ``True`` for delivered, ``False`` to schedule a retry.
DeliveryHandler = Callable[[QueueRecord], Awaitable[bool]]


class DeliveryWorker:
    """Drain the queue in a loop.

    Stop by cancelling the task running ``run()``.
    """

    def __init__(
        self,
        repo: DeliveryQueueRepository,
        handler: DeliveryHandler,
        policy: RetryPolicy | None = None,
        *,
        idle_sleep: float = 0.5,
    ) -> None:
        self._repo = repo
        self._handler = handler
        self._policy = policy or RetryPolicy()
        self._idle_sleep = idle_sleep

    def compute_next_attempt_at(self, attempt: int) -> _dt.datetime:
        delay = self._policy.next_delay(attempt - 1)
        # Full jitter.
        delay = random.uniform(0, max(delay, self._policy.base_seconds))
        return _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=delay)

    async def process_one(self) -> bool:
        """Process exactly one batch if available.

        Returns ``True`` if a batch was processed, ``False`` if
        the queue was empty.
        """
        batch = await self._repo.claim_batch(limit=1)
        if not batch:
            return False
        await self._process(batch[0])
        return True

    async def _process(self, item: QueueRecord) -> None:
        try:
            delivered = await self._handler(item)
        except Exception as e:  # noqa: BLE001 - we want to capture every failure
            logger.exception("delivery handler raised on queue_id=%s", item.id)
            await self._fail_or_retry(item, last_error=f"handler exception: {e!r}")
            return
        if delivered:
            await self._repo.mark_delivered(queue_id=item.id)
            return
        await self._fail_or_retry(item, last_error="handler returned False")

    async def _fail_or_retry(self, item: QueueRecord, *, last_error: str) -> None:
        attempts = item.attempts + 1
        if attempts >= self._policy.max_attempts:
            await self._repo.mark_failed(queue_id=item.id, last_error=last_error)
            return
        next_at = self.compute_next_attempt_at(attempts)
        await self._repo.mark_retry(
            queue_id=item.id,
            last_error=last_error,
            next_attempt_at=next_at,
        )

    async def run(self) -> None:
        while True:
            worked = await self.process_one()
            if not worked:
                await asyncio.sleep(self._idle_sleep)


__all__ = ["DeliveryWorker", "RetryPolicy", "DeliveryHandler"]
