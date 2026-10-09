"""Outbox-backed notification dispatcher.

Wraps an existing :class:`NotificationDispatcher` so every
call goes through the persistent :class:`NotificationOutbox`
first. The outbox row is the durable copy; the inner dispatcher
is only invoked when the outbox drainer picks the row up.

Coalescing keys are derived from the watch semantics:

* initial down alert: ``{adapter}_down`` (replaces any older row);
* hourly reminder:    ``{adapter}_down_reminder`` (replaces);
* recovery:           ``{adapter}_recovered`` (replaces);
* degraded:           ``{adapter}_degraded`` (replaces).

Because the watchdogs coalesce by ``adapter``, a Telegram
outage cannot produce five reminders that all fire together
when Telegram comes back: only the latest reminder stays in
the outbox.
"""

from __future__ import annotations

from typing import Optional

from ..watchdog.base import NotificationDispatcher
from .outbox import NotificationOutbox


class OutboxBackedDispatcher(NotificationDispatcher):
    """Persist every event to the outbox; let the outbox drainer
    deliver via ``inner``.

    The outbox row is written even if the inner dispatcher later
    fails — at-least-once delivery.
    """

    def __init__(
        self,
        *,
        outbox: NotificationOutbox,
        inner: NotificationDispatcher,
        destination_chat_id: str,
        thread_id: Optional[str] = None,
    ) -> None:
        self._outbox = outbox
        self._inner = inner
        self._chat_id = destination_chat_id
        self._thread_id = thread_id

    @property
    def inner(self) -> NotificationDispatcher:
        return self._inner

    async def send(
        self,
        *,
        event_type: str,
        chat_id: str,
        thread_id: str | None,
        payload: dict,
    ) -> None:
        adapter = (payload.get("adapter") or chat_id or "unknown").lower()
        # Map watchdog event types onto stable coalesce keys.
        coalesce_key = None
        if event_type == "down":
            coalesce_key = f"{adapter}_down"
        elif event_type == "hourly_reminder":
            coalesce_key = f"{adapter}_down_reminder"
        elif event_type == "recovered":
            coalesce_key = f"{adapter}_recovered"
        elif event_type == "degraded":
            coalesce_key = f"{adapter}_degraded"
        await self._outbox.enqueue(
            event_type=event_type,
            destination_chat_id=self._chat_id,
            thread_id=thread_id or self._thread_id,
            payload=payload,
            coalesce_key=coalesce_key,
        )


__all__ = ["OutboxBackedDispatcher"]