"""Notification sender — drains the outbox via a real dispatcher.

The outbox is the durable producer-side log; the sender is the
network-side counterpart. The drainer passes the *inner* dispatcher
(not the outbox-backed one) so rows do not re-enqueue themselves.

Production wiring::

    NotificationOutbox                  -- producer (durable log)
      │
      │  enqueue()  ← watchdog / API
      ▼
    SQLite notification_outbox table
      │
      │  claim pending rows
      ▼
    NotificationSender.drain_once()     -- sender (this module)
      │
      │  invoke inner dispatcher
      ▼
    TelegramNotificationDispatcher      -- real network call

The drainer must NEVER hand the outbox-backed dispatcher back to
the outbox: that would create a feedback loop where ``enqueue``
triggers ``drain`` which triggers another ``enqueue``.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..watchdog.base import NotificationDispatcher
from .outbox import NotificationOutbox


logger = logging.getLogger(__name__)


class NotificationSender:
    """Network-side counterpart to :class:`NotificationOutbox`.

    The sender is intentionally thin — it owns the dispatch policy
    and the dispatcher's lifecycle around the drain call. The
    outbox remains the single source of truth for pending rows.
    """

    def __init__(
        self,
        *,
        outbox: NotificationOutbox,
        dispatcher: NotificationDispatcher,
        batch_size: int = 32,
    ) -> None:
        if dispatcher is None:
            raise ValueError("dispatcher is required")
        self._outbox = outbox
        self._dispatcher = dispatcher
        self._batch_size = max(1, int(batch_size))

    @property
    def dispatcher(self) -> NotificationDispatcher:
        return self._dispatcher

    async def drain_once(self) -> int:
        """One drain pass. Returns rows successfully delivered."""
        # NOTE: this dispatcher is the *inner* (Telegram-bound)
        # dispatcher. The outbox-backed dispatcher is intentionally
        # not used here — feeding the outbox back into itself
        # would produce an infinite loop.
        return await self._outbox.drain_pending(self._dispatcher)


__all__ = ["NotificationSender"]