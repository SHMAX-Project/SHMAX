"""Offline regression tests for the production AppRuntime.

The tests use Fake adapters so they do not depend on real
MAX / Telegram credentials. They drive the full end-to-end
flow:

    Fake MAX emits
        → real event consumer
        → real BridgeEngine
        → real delivery queue
        → real DeliveryWorker
        → real adapter (Fake Telegram)
        → real message_map row
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
import pytest_asyncio

from shmax.adapters.max.fake import FakeMaxAdapter
from shmax.adapters.max.base import MaxEvent
from shmax.adapters.telegram.fake import FakeTelegramAdapter
from shmax.adapters.telegram.base import TelegramEvent
from shmax.database import Database, DatabaseSettings
from shmax.database.migrations import run_migrations_sync
from shmax.database.models import (
    AdminUser,
    MaxAccount,
    MessageMap,
    TelegramAccount,
    TelegramUser,
)
from shmax.auth.passwords import PasswordHasher
from shmax.runtime import AppRuntime, RuntimeConfig
from shmax.security import SecretStore
from shmax.notifications.outbox import NotificationOutbox
from shmax.notifications.dispatcher import LoggingNotificationDispatcher
from shmax.config import RuntimeMode


@pytest_asyncio.fixture()
async def runtime(tmp_path):
    from shmax.core.paths import PathLayout
    data = tmp_path / "shmax-data"
    settings = DatabaseSettings(data_dir=data)
    db = Database(settings)
    run_migrations_sync(settings.url)
    secret_store = SecretStore(data / "secrets")
    layout = PathLayout(data)
    rt = AppRuntime(
        database=db,
        secret_store=secret_store,
        layout=layout,
        config=RuntimeConfig(
            watchdog_interval_seconds=0.05,
            queue_lease_seconds=5.0,
            worker_poll_seconds=0.05,
            outbox_poll_seconds=0.05,
            max_concurrent_workers=2,
            start_workers=True,
        ),
        runtime_mode=RuntimeMode.TEST,
    )
    rt.install_max_adapter(FakeMaxAdapter())
    await rt.install_telegram(None, FakeTelegramAdapter())  # type: ignore[arg-type]
    async with db.session() as s:
        s.add(MaxAccount(label="primary", status="connected"))
        s.add(TelegramAccount(label="primary", status="connected"))
        await s.commit()
    async with db.session() as s:
        from shmax.routing.repository import RoutesRepository
        await RoutesRepository(s).upsert(
            max_account_id=1,
            max_chat_id="max-dialog-1",
            telegram_account_id=1,
            telegram_chat_id="tg-chat-1",
        )
        await s.commit()

    await rt.start()
    # Fake adapter never goes through real MAX auth; manually
    # enable the listener so MAX→TG events flow in tests.
    await rt.start_max_listener()
    try:
        yield rt
    finally:
        await rt.stop()
        await db.dispose()


@pytest.mark.asyncio
async def test_runtime_starts_and_stops_cleanly(runtime):
    snap = runtime.status
    assert snap is not None


@pytest.mark.asyncio
async def test_max_to_telegram_flow(runtime):
    """A Fake MAX event must reach the Fake Telegram adapter
    via the real worker."""
    tg_adapter = runtime.telegram_adapter  # type: ignore[assignment]
    # Inject the actual MAX event.
    await runtime.max_adapter.inject_message(
        MaxEvent(
            event_id="ev-1",
            chat_id="max-dialog-1",
            sender_id="m1",
            text="hello from MAX",
            raw=None,
        )
    )

    deadline = asyncio.get_event_loop().time() + 10.0
    while asyncio.get_event_loop().time() < deadline:
        if tg_adapter.sent_messages:  # type: ignore[attr-defined]
            break
        await asyncio.sleep(0.1)
    # Debug: check queue state
    from shmax.queue.repository import DeliveryQueueRepository
    async with runtime.db.session() as s:
        repo = DeliveryQueueRepository(s)
        counts = await repo.counts_by_state()
    assert any(
        "hello from MAX" in m["text"] for m in tg_adapter.sent_messages  # type: ignore[attr-defined]
    ), f"sent={tg_adapter.sent_messages} queue={counts}"  # type: ignore[attr-defined]

    async with runtime.db.session() as s:
        from sqlalchemy import select
        row = (await s.execute(select(MessageMap))).scalars().first()
        assert row is not None


@pytest.mark.asyncio
async def test_runtime_shuts_down_idempotently(runtime):
    await runtime.stop()
    await runtime.stop()  # second call must not raise


@pytest.mark.asyncio
async def test_outbox_coalesces_same_key(tmp_path):
    outbox = NotificationOutbox(tmp_path / "outbox.db")
    await outbox.open()
    try:
        await outbox.enqueue(
            event_type="max_down",
            destination_chat_id="tg-chat-1",
            payload={"text": "first"},
            coalesce_key="max_down",
        )
        await outbox.enqueue(
            event_type="max_down",
            destination_chat_id="tg-chat-1",
            payload={"text": "second"},
            coalesce_key="max_down",
        )
        await outbox.enqueue(
            event_type="max_down",
            destination_chat_id="tg-chat-1",
            payload={"text": "third"},
            coalesce_key="max_down",
        )
        rows = await outbox.list_pending()
        assert len(rows) == 1
        # Read payload back via SQLite directly.
        conn = sqlite3.connect(str(tmp_path / "outbox.db"))
        try:
            text = conn.execute(
                "SELECT payload_json FROM notification_outbox LIMIT 1"
            ).fetchone()[0]
        finally:
            conn.close()
        assert json.loads(text)["text"] == "third"
    finally:
        await outbox.close()


@pytest.mark.asyncio
async def test_outbox_marks_delivered(tmp_path):
    outbox = NotificationOutbox(tmp_path / "outbox.db")
    await outbox.open()
    try:
        await outbox.enqueue(
            event_type="ping",
            destination_chat_id="tg-chat-1",
            payload={"text": "hi"},
        )
        dispatcher = LoggingNotificationDispatcher()
        n = await outbox.drain_pending(dispatcher)
        assert n == 1
        rows = await outbox.list_pending()
        assert rows == []
    finally:
        await outbox.close()

