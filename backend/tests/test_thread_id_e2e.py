"""thread_id survives the full inbound -> bridge -> queue -> outbound path.

A Telegram inbound message with a forum topic thread must
land in MAX's queue payload with that thread_id preserved, and
the queue claim/dispatch must hand the thread_id back to the
adapter via the DeliveryResult.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio

from shmax.adapters.max.fake import FakeMaxAdapter
from shmax.adapters.telegram.fake import FakeTelegramAdapter
from shmax.database import Database, DatabaseSettings
from shmax.database.migrations import run_migrations_sync
from shmax.database.models import (
    MaxAccount,
    TelegramAccount,
    TelegramUser,
)
from shmax.domain import (
    ChatKind,
    ChatRef,
    NormalizedMessage,
    SenderRef,
)
from shmax.routing.repository import RoutesRepository
from shmax.runtime import AppRuntime, RuntimeConfig
from shmax.core.paths import PathLayout
from shmax.security import SecretStore
from shmax.config import RuntimeMode


@pytest_asyncio.fixture()
async def runtime(tmp_path):
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
            watchdog_interval_seconds=10.0,
            queue_lease_seconds=5.0,
            worker_poll_seconds=0.01,
            outbox_poll_seconds=0.05,
            max_concurrent_workers=1,
        ),
        runtime_mode=RuntimeMode.TEST,
    )
    rt.install_max_adapter(FakeMaxAdapter())
    await rt.install_telegram(None, FakeTelegramAdapter())  # type: ignore[arg-type]
    async with db.session() as s:
        s.add(MaxAccount(label="primary", status="connected"))
        s.add(TelegramAccount(label="primary", status="connected"))
        s.add(TelegramUser(user_id="u1", role="operator", approved=True))
        await s.commit()
    async with db.session() as s:
        # Forum-threaded route (chat id + topic id)
        await RoutesRepository(s).upsert(
            max_account_id=1,
            max_chat_id="max-thread",
            telegram_account_id=1,
            telegram_chat_id="tg-forum-1",
            telegram_thread_id="topic-42",
        )
        # Flat chat-only route for tests that do not exercise
        # topic threading. Distinct max_chat_id so route lookup
        # does not collide with the forum route above.
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


async def _drive_bridge(rt, event, source: str = "max"):
    from shmax.bridge.engine import BridgeConfig, BridgeEngine
    from shmax.bridge.message_map import MessageMapRepository
    from shmax.queue.repository import DeliveryQueueRepository
    from shmax.auth.pairing import AclService
    from shmax.users.repository import TelegramUsersRepository

    async with rt.db.session() as s:
        engine = BridgeEngine(
            config=BridgeConfig(
                max_account_id=1,
                telegram_account_id=1,
                telegram_target_chat_id=(
                    event.chat.external_id if source == "telegram" else None
                ),
            ),
            routes=RoutesRepository(s),
            mapping=MessageMapRepository(s),
            queue=DeliveryQueueRepository(s),
            max_adapter=rt.max_adapter,
            telegram_adapter=rt.telegram_adapter,
            acl=AclService(s),
            users=TelegramUsersRepository(rt.db),
        )
        if source == "max":
            return await engine.handle_max_event(event)
        return await engine.handle_telegram_event(event)


@pytest.mark.asyncio
async def test_thread_id_persists_max_to_telegram(runtime):
    evt = NormalizedMessage(
        source="max",
        chat=ChatRef(transport="max", external_id="max-thread", kind=ChatKind.GROUP),
        sender=SenderRef(transport="max", external_id="m1"),
        external_id="m-evt-thread",
        text="hello",
        timestamp=0,
    )
    accepted = await _drive_bridge(runtime, evt, source="max")
    assert accepted is True
    await runtime.wakeup()
    for _ in range(200):
        if runtime.telegram_adapter.sent_messages:  # type: ignore[attr-defined]
            break
        await asyncio.sleep(0.01)
    msgs = runtime.telegram_adapter.sent_messages  # type: ignore[attr-defined]
    # The Telegram fake must have received thread_id="topic-42"
    # (sourced from the route).
    thread_ids = [m.get("thread_id") for m in msgs]
    assert "topic-42" in thread_ids, msgs


@pytest.mark.asyncio
async def test_thread_id_persists_telegram_to_max(runtime):
    """A Telegram event tagged with a forum topic must reach MAX.

    MAX has no thread concept; the test simply confirms the
    adapter was called with the message text. Telegram thread_id
    survives on the QueueRecord's target_thread_id and is
    discarded for MAX delivery (with a structured log line if
    needed).
    """
    # The runtime fixture pre-seeds one route for tg-chat-1. The
    # TG→MAX audit-mandated ACL also needs the sender to be a
    # registered / approved operator for that target group.
    from shmax.users.repository import TelegramUsersRepository
    async with runtime.db.session() as s:
        users = TelegramUsersRepository(runtime.db)
        await users.register_or_update(
            user_id="u1", display_name="Test User", username=None
        )
        await users.set_approval(user_id="u1", approved=True, role="operator")
    evt = NormalizedMessage(
        source="telegram",
        chat=ChatRef(transport="telegram", external_id="tg-chat-1", kind=ChatKind.GROUP),
        sender=SenderRef(transport="telegram", external_id="u1"),
        external_id="t-evt-thread",
        text="from tg",
        timestamp=0,
        metadata={"thread_id": "topic-42"},
    )
    accepted = await _drive_bridge(runtime, evt, source="telegram")
    assert accepted is True
    await runtime.wakeup()
    for _ in range(200):
        if runtime.max_adapter.sent_text:  # type: ignore[attr-defined]
            break
        await asyncio.sleep(0.01)
    sent = runtime.max_adapter.sent_text  # type: ignore[attr-defined]
    assert sent and "from tg" in sent[0][1]
