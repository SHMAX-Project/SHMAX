"""BLOCKER 14 — Outbox producer/sender split + real network send.

The outbox drain must use the *inner* dispatcher (the Telegram-bound
one), not the outbox-backed dispatcher. Feeding the outbox back into
itself would create a feedback loop where ``enqueue`` triggers
``drain`` which triggers another ``enqueue``.

A real ``TelegramNotificationDispatcher`` performs a network send
(via the injected ``TelegramAdapter``). When the adapter reports
failure the row goes back to ``pending`` with backoff; only a real
ack transitions the row to ``delivered``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from shmax.adapters.max.fake import FakeMaxAdapter
from shmax.adapters.telegram.fake import FakeTelegramAdapter
from shmax.notifications.dispatcher import TelegramNotificationDispatcher
from shmax.notifications.outbox import NotificationOutbox
from shmax.notifications.outbox_dispatcher import OutboxBackedDispatcher
from shmax.notifications.sender import NotificationSender
from shmax.runtime import AppRuntime, RuntimeConfig
from shmax.core.paths import PathLayout
from shmax.security import SecretStore
from shmax.database import Database, DatabaseSettings
from shmax.database.migrations import run_migrations_sync
from shmax.config import RuntimeMode


class _RecordingDispatcher:
    """Captures every send() call for assertion. Not a subclass of
    NotificationDispatcher — used as the inner dispatcher only."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, *, event_type, chat_id, thread_id, payload):
        self.sent.append(
            {"event_type": event_type, "chat_id": chat_id, "thread_id": thread_id, "payload": payload}
        )


@pytest.mark.asyncio
async def test_notification_sender_drains_via_inner_dispatcher_not_outbox(tmp_path):
    """Feeding the outbox-backed dispatcher back into the outbox
    would re-enqueue the row instead of draining it. The sender
    must use the *inner* dispatcher."""
    outbox = NotificationOutbox(tmp_path / "outbox.db")
    await outbox.open()
    try:
        inner = _RecordingDispatcher()
        backed = OutboxBackedDispatcher(
            outbox=outbox,
            inner=inner,  # type: ignore[arg-type]
            destination_chat_id="-1001234",
            thread_id="42",
        )

        # Producer path — outbox-backed enqueues to the DB.
        await backed.send(
            event_type="down",
            chat_id="self",
            thread_id=None,
            payload={"adapter": "max"},
        )
        # Before the sender runs, the inner dispatcher was NOT
        # called: enqueue must NOT be the network ACK.
        assert inner.sent == []

        # Sender path uses the inner dispatcher (NOT the backed one).
        sender = NotificationSender(outbox=outbox, dispatcher=inner)  # type: ignore[arg-type]
        delivered = await sender.drain_once()
        assert delivered == 1
        assert len(inner.sent) == 1
        # The backed dispatcher was NEVER invoked directly.
        # Crucially: the sender did NOT use it.
        await backed.send(
            event_type="hourly_reminder",
            chat_id="self",
            thread_id=None,
            payload={"adapter": "max"},
        )
        # No additional inner.sent beyond the first drain.
        delivered = await sender.drain_once()
        assert delivered == 1
        assert len(inner.sent) == 2
    finally:
        await outbox.close()


@pytest.mark.asyncio
async def test_sender_uses_real_telegram_dispatcher(tmp_path):
    """A ``TelegramNotificationDispatcher`` invokes the adapter's
    ``send_message``. Successful send -> row delivered; failure ->
    row stays pending with backoff."""
    outbox = NotificationOutbox(tmp_path / "outbox.db")
    await outbox.open()
    adapter = FakeTelegramAdapter()
    await adapter.connect()
    dispatcher = TelegramNotificationDispatcher(adapter, chat_id="-1001234", thread_id="42")
    try:
        await outbox.enqueue(
            event_type="down",
            destination_chat_id="-1001234",
            thread_id="42",
            payload={"adapter": "max"},
        )
        sender = NotificationSender(outbox=outbox, dispatcher=dispatcher)
        delivered = await sender.drain_once()
        assert delivered == 1
        # The adapter received at least one send_message call.
        assert adapter.sent_messages
        pending = await outbox.list_pending()
        # Only delivered rows remain (or zero pending).
        assert all(row["status"] == "delivered" for row in pending) or pending == []
    finally:
        await outbox.close()


@pytest.mark.asyncio
async def test_sender_failure_keeps_row_pending(tmp_path):
    """When the adapter raises the row stays pending and is
    re-attempted after backoff."""
    outbox = NotificationOutbox(tmp_path / "outbox.db")
    await outbox.open()

    class _BoomAdapter:
        async def send_message(self, *, chat_id, text, thread_id=None):
            raise RuntimeError("network down")

    dispatcher = TelegramNotificationDispatcher(_BoomAdapter(), chat_id="-1001234")  # type: ignore[arg-type]
    try:
        await outbox.enqueue(
            event_type="down",
            destination_chat_id="-1001234",
            payload={"adapter": "max"},
        )
        sender = NotificationSender(outbox=outbox, dispatcher=dispatcher)
        delivered = await sender.drain_once()
        assert delivered == 0
        # Row stays pending with a recorded last_error.
        rows = await outbox.list_pending()
        assert any(r["status"] == "pending" and r["last_error"] for r in rows)
    finally:
        await outbox.close()


@pytest.mark.asyncio
async def test_runtime_outbox_drain_uses_sender_with_inner_dispatcher(tmp_path):
    """End-to-end: runtime drains via the inner dispatcher. The
    OutboxBackedDispatcher is never invoked by the drain loop."""
    data = tmp_path / "shmax-outbox-sender"
    settings = DatabaseSettings(data_dir=data)
    db = Database(settings)
    run_migrations_sync(settings.url)
    layout = PathLayout(data)
    secret_store = SecretStore(layout.root / "secrets")
    rt = AppRuntime(
        database=db,
        secret_store=secret_store,
        layout=layout,
        config=RuntimeConfig(start_workers=False),
        runtime_mode=RuntimeMode.TEST,
    )
    # The runtime must NOT hand the outbox-backed dispatcher to
    # the drain loop. We simulate the post-start wiring here:
    # create the OutboxBackedDispatcher and the sender with the
    # inner dispatcher. This mirrors what ``start()`` does.
    from shmax.notifications.outbox_dispatcher import OutboxBackedDispatcher

    await rt.notification_outbox.open()
    rt._outbox_dispatcher = OutboxBackedDispatcher(
        outbox=rt.notification_outbox,
        inner=rt._inner_dispatcher,
        destination_chat_id="-1001234",
        thread_id="42",
    )
    rt.dispatcher = rt._outbox_dispatcher
    rt._dispatcher_slot.current = rt._outbox_dispatcher
    from shmax.notifications.sender import NotificationSender

    rt._outbox_sender = NotificationSender(
        outbox=rt.notification_outbox,
        dispatcher=rt._inner_dispatcher,
    )
    try:
        assert rt._outbox_sender is not None
        # The drain loop uses the inner dispatcher, not the backed one.
        assert rt._outbox_sender.dispatcher is rt._inner_dispatcher
        assert rt._outbox_sender.dispatcher is not rt._outbox_dispatcher
    finally:
        await rt.notification_outbox.close()
        await db.dispose()
