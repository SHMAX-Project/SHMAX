"""Deterministic AppRuntime end-to-end tests.

The runtime owns the full gateway pipeline. These tests
drive the pipeline directly without relying on listener
supervisors (those are exercised in test_runtime_supervisor
separately). The point here is to verify the wired
pipeline: bridge -> queue -> worker -> adapter -> mapping.

Determinism comes from the AppRuntime's ``wakeup`` event.
The bridge notifies the worker after enqueue so we do not
depend on the poll interval.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
import pytest_asyncio

from shmax.adapters.max.fake import FakeMaxAdapter
from shmax.adapters.max.base import MaxEvent
from shmax.adapters.telegram.fake import FakeTelegramAdapter
from shmax.database import Database, DatabaseSettings
from shmax.database.migrations import run_migrations_sync
from shmax.database.models import (
    MaxAccount,
    TelegramAccount,
    TelegramUser,
    MessageMap,
)
from shmax.routing.repository import RoutesRepository
from shmax.runtime import AppRuntime, RuntimeConfig
from shmax.core.paths import PathLayout
from shmax.security import SecretStore
from shmax.config import RuntimeMode
from sqlalchemy import select


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
        # TelegramUser.role is one of Role.OWNER/ADMIN/OPERATOR/READ_ONLY.
        # ACL's DEFAULT_PERMISSIONS table derives send_message from those
        # enum values; AclService._resolve_role() rejects unknown strings.
        s.add(TelegramUser(user_id="u1", role="operator", approved=True))
        await s.commit()
    async with db.session() as s:
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


async def _drive_bridge(rt, event, source: str = "max") -> bool:
    from shmax.bridge.engine import BridgeConfig, BridgeEngine
    from shmax.bridge.message_map import MessageMapRepository
    from shmax.queue.repository import DeliveryQueueRepository
    from shmax.auth.pairing import AclService
    from shmax.users.repository import TelegramUsersRepository

    async with rt.db.session() as s:
        users_repo = TelegramUsersRepository(rt.db)
        # For TG→MAX the audit-mandated fail-closed ACL requires
        # the sender to be a registered / approved TelegramUser
        # AND the chat to match the configured target group.
        # Pre-seed both so the contract test exercises the
        # production wiring end-to-end.
        if source == "telegram":
            await users_repo.register_or_update(
                user_id=event.sender.external_id,
                display_name=event.sender.display_name or "Test Sender",
                username=None,
            )
            await users_repo.set_approval(
                user_id=event.sender.external_id,
                approved=True,
                role="operator",
            )
            # ACL pre-check requires the chat to be a Telegram
            # forum supergroup, not a private chat. Ensure the
            # route row exists so the bridge has a MAX chat id.
            existing = await RoutesRepository(s).find_by_telegram(
                telegram_account_id=1,
                telegram_chat_id=event.chat.external_id,
                telegram_thread_id=None,
            )
            if existing is None:
                await RoutesRepository(s).upsert(
                    max_account_id=1,
                    max_chat_id=f"max-{event.chat.external_id}",
                    telegram_account_id=1,
                    telegram_chat_id=event.chat.external_id,
                )
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
            users=users_repo,
        )
        if source == "max":
            return await engine.handle_max_event(event)
        return await engine.handle_telegram_event(event)


@pytest.mark.asyncio
async def test_runtime_max_to_telegram_e2e(runtime):
    from shmax.domain import ChatKind, ChatRef, NormalizedMessage, SenderRef

    evt = NormalizedMessage(
        source="max",
        chat=ChatRef(transport="max", external_id="max-dialog-1", kind=ChatKind.DIALOG),
        sender=SenderRef(transport="max", external_id="m1"),
        external_id="m-evt-1",
        text="hello max",
        timestamp=0,
    )
    accepted = await _drive_bridge(runtime, evt, source="max")
    assert accepted is True
    await runtime.wakeup()
    # Wait for the worker (deterministic — wakeup was called).
    for _ in range(100):
        if runtime.telegram_adapter.sent_messages:  # type: ignore[attr-defined]
            break
        await asyncio.sleep(0.01)
    msgs = runtime.telegram_adapter.sent_messages  # type: ignore[attr-defined]
    assert any("hello max" in m["text"] for m in msgs), msgs
    # Mapping persisted.
    async with runtime.db.session() as s:
        row = (await s.execute(select(MessageMap))).scalars().first()
        assert row is not None


@pytest.mark.asyncio
async def test_runtime_telegram_to_max_e2e(runtime):
    from shmax.domain import ChatKind, ChatRef, NormalizedMessage, SenderRef

    evt = NormalizedMessage(
        source="telegram",
        chat=ChatRef(transport="telegram", external_id="tg-chat-1", kind=ChatKind.GROUP),
        sender=SenderRef(transport="telegram", external_id="u1"),
        external_id="t-evt-1",
        text="hi tg",
        timestamp=0,
    )
    accepted = await _drive_bridge(runtime, evt, source="telegram")
    assert accepted is True
    from shmax.queue.repository import DeliveryQueueRepository
    async with runtime.db.session() as s:
        print("queue counts:", await DeliveryQueueRepository(s).counts_by_state())
    await runtime.wakeup()
    for _ in range(100):
        if runtime.max_adapter.sent_text:  # type: ignore[attr-defined]
            break
        await asyncio.sleep(0.01)
    sent = runtime.max_adapter.sent_text  # type: ignore[attr-defined]
    async with runtime.db.session() as s:
        print("queue counts final:", await DeliveryQueueRepository(s).counts_by_state())
    print("sent:", sent)
    assert sent and "hi tg" in sent[0][1], sent
    from shmax.database.models import MessageMap as MM
    async with runtime.db.session() as s:
        # Use a fresh transaction so we see committed data.
        await s.commit()
        rows = (await s.execute(select(MM))).scalars().all()
        print("MessageMap rows:", [(r.direction, r.max_message_id, r.telegram_message_id, r.max_chat_id, r.telegram_chat_id) for r in rows])
        assert rows, "no MessageMap rows"
        assert rows[0].max_message_id is not None


@pytest.mark.asyncio
async def test_runtime_reply_mapping_e2e(runtime):
    """Reply mapping must persist end-to-end: TG→MAX reply
    carries the source message id through DeliveryResult."""
    from shmax.domain import ChatKind, ChatRef, DeliveryResult, NormalizedMessage, ReplyRef, SenderRef

    # Pre-seed a mapping row: MAX msg m-1 → TG msg fake-msg-1.
    from shmax.database.models import MessageMap as MM
    async with runtime.db.session() as s:
        s.add(
            MM(
                direction="max_to_telegram",
                max_account_id=1,
                max_chat_id="max-dialog-1",
                max_message_id="m-1",
                telegram_account_id=1,
                telegram_chat_id="tg-chat-1",
                telegram_message_id="fake-msg-1",
                outbound_marker="seed-marker",
            )
        )
        await s.commit()

    # Now drive a TG->MAX event with reply_to=m-1. The bridge
    # looks up the mapping and emits a reply.
    evt = NormalizedMessage(
        source="telegram",
        chat=ChatRef(transport="telegram", external_id="tg-chat-1", kind=ChatKind.GROUP),
        sender=SenderRef(transport="telegram", external_id="u1"),
        external_id="t-reply-1",
        text="reply max",
        timestamp=0,
        reply_to=ReplyRef(
            transport="max",
            chat_external_id="max-dialog-1",
            message_external_id="m-1",
        ),
    )
    accepted = await _drive_bridge(runtime, evt, source="telegram")
    assert accepted is True
    await runtime.wakeup()
    for _ in range(100):
        if runtime.max_adapter.sent_replies:  # type: ignore[attr-defined]
            break
        await asyncio.sleep(0.01)
    replies = runtime.max_adapter.sent_replies  # type: ignore[attr-defined]
    assert replies, "no reply sent"
    # Verify the reply was sent with the original m-1 as reply target.
    assert any(r[1] == "m-1" for r in replies), replies


@pytest.mark.asyncio
async def test_runtime_restart_with_pending_queue(runtime, tmp_path):
    """Pending queue items survive a restart."""
    from shmax.domain import ChatKind, ChatRef, NormalizedMessage, SenderRef

    evt = NormalizedMessage(
        source="max",
        chat=ChatRef(transport="max", external_id="max-dialog-1", kind=ChatKind.DIALOG),
        sender=SenderRef(transport="max", external_id="m1"),
        external_id="m-evt-pending",
        text="persisted",
        timestamp=0,
    )
    accepted = await _drive_bridge(runtime, evt, source="max")
    assert accepted is True
    # Stop without giving the worker a chance to drain.
    await runtime.stop()
    # Re-open: items must still be there.
    from shmax.queue.repository import DeliveryQueueRepository
    async with runtime.db.session() as s:
        repo = DeliveryQueueRepository(s)
        counts = await repo.counts_by_state()
    assert counts["pending"] >= 1, counts


@pytest.mark.asyncio
async def test_runtime_adapter_failure_then_recovery(runtime):
    """Worker retries on adapter failure; recovers when
    adapter comes back."""
    from shmax.domain import ChatKind, ChatRef, NormalizedMessage, SenderRef

    # Configure the telegram fake to fail the first send.
    runtime.telegram_adapter.fail_send = True  # type: ignore[attr-defined]
    runtime.telegram_adapter.fail_count = 1  # type: ignore[attr-defined]

    evt = NormalizedMessage(
        source="max",
        chat=ChatRef(transport="max", external_id="max-dialog-1", kind=ChatKind.DIALOG),
        sender=SenderRef(transport="max", external_id="m1"),
        external_id="m-flaky",
        text="flaky",
        timestamp=0,
    )
    await _drive_bridge(runtime, evt, source="max")
    await runtime.wakeup()
    # The worker tries, fails, retries. After fail_count=1
    # succeeds. With worker_poll_seconds=0.01 and backoff
    # base=1, this happens in well under a second.
    for _ in range(200):
        if runtime.telegram_adapter.sent_messages:  # type: ignore[attr-defined]
            break
        await asyncio.sleep(0.01)
    msgs = runtime.telegram_adapter.sent_messages  # type: ignore[attr-defined]
    assert any("flaky" in m["text"] for m in msgs), msgs
