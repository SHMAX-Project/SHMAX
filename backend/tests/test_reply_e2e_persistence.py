"""BLOCKER 11 — Reply mapping persistence across restart.

The runtime lifecycle owns the DB / SecretStore / mappings.
A reply that arrives right after a restart must still
resolve to the original MAX message id via the persisted
MessageMap.

The MAX→TG round-trip itself is covered by
``test_runtime_e2e::test_runtime_reply_mapping_e2e``. This
file adds the restart-survives check.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from shmax.adapters.max.fake import FakeMaxAdapter
from shmax.adapters.telegram.fake import FakeTelegramAdapter
from shmax.bridge.engine import BridgeConfig, BridgeEngine
from shmax.users.repository import TelegramUsersRepository
from shmax.bridge.message_map import MessageMapRepository
from shmax.core.paths import PathLayout
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
from shmax.queue.repository import DeliveryQueueRepository
from shmax.routing.repository import RoutesRepository
from shmax.runtime import AppRuntime, RuntimeConfig
from shmax.security import SecretStore
from shmax.auth.pairing import AclService
from shmax.config import RuntimeMode


async def _seed(tmp_path: Path):
    settings = DatabaseSettings(data_dir=tmp_path)
    db = Database(settings)
    run_migrations_sync(settings.url)
    async with db.session() as s:
        s.add(MaxAccount(label="primary", status="connected"))
        s.add(TelegramAccount(label="primary", status="connected"))
        s.add(TelegramUser(user_id="u1", role="operator"))
        # Commit parent rows first so the route insert below
        # sees the FK targets.
        await s.commit()
    async with db.session() as s:
        await RoutesRepository(s).upsert(
            max_account_id=1,
            max_chat_id="max-dialog-1",
            telegram_account_id=1,
            telegram_chat_id="tg-chat-1",
            telegram_thread_id=None,
        )
    return db


async def _drive(rt, evt):
    db = rt.db
    async with db.session() as s:
        engine = BridgeEngine(
            config=BridgeConfig(max_account_id=1, telegram_account_id=1),
            routes=RoutesRepository(s),
            mapping=MessageMapRepository(s),
            queue=DeliveryQueueRepository(s),
            max_adapter=FakeMaxAdapter(),
            telegram_adapter=FakeTelegramAdapter(),
            acl=AclService(s),
            users=TelegramUsersRepository(db),
        )
        return await engine.handle_max_event(evt)


async def _wait_for_delivery(rt):
    for _ in range(200):
        if rt.telegram_adapter.sent_messages:  # type: ignore[attr-defined]
            return rt.telegram_adapter.sent_messages  # type: ignore[attr-defined]
        await asyncio.sleep(0.01)
    return []


@pytest.mark.asyncio
async def test_mapping_survives_runtime_restart(tmp_path):
    data = tmp_path / "shmax-data-restart"
    db = await _seed(data)
    layout = PathLayout(data)
    secret_store = SecretStore(layout.root / "secrets")
    cfg = RuntimeConfig(max_concurrent_workers=1)

    rt1 = AppRuntime(
        database=db,
        secret_store=secret_store,
        layout=layout,
        config=cfg,
        runtime_mode=RuntimeMode.TEST,
    )
    rt1.install_max_adapter(FakeMaxAdapter())
    await rt1.install_telegram(None, FakeTelegramAdapter())  # type: ignore[arg-type]
    await rt1.start()
    try:
        evt = NormalizedMessage(
            source="max",
            chat=ChatRef(
                transport="max",
                external_id="max-dialog-1",
                kind=ChatKind.DIALOG,
            ),
            sender=SenderRef(transport="max", external_id="m1"),
            external_id="m-evt-restart-1",
            text="hi",
            timestamp=0,
        )
        await _drive(rt1, evt)
        await rt1.wakeup()
        delivered = await _wait_for_delivery(rt1)
        assert delivered
        first_tg_msg_id = delivered[0]["message_id"]
    finally:
        await rt1.stop()

    # Second runtime with the SAME data dir.
    rt2 = AppRuntime(
        database=db,
        secret_store=secret_store,
        layout=layout,
        config=cfg,
        runtime_mode=RuntimeMode.TEST,
    )
    rt2.install_max_adapter(FakeMaxAdapter())
    await rt2.install_telegram(None, FakeTelegramAdapter())  # type: ignore[arg-type]
    await rt2.start()
    try:
        async with db.session() as s:
            found = await MessageMapRepository(s).find_by_telegram(
                telegram_account_id=1,
                telegram_chat_id="tg-chat-1",
                telegram_thread_id=None,
                telegram_message_id=first_tg_msg_id,
            )
        assert found is not None, (
            "mapping was lost across restart"
        )
        assert found.max_message_id == "m-evt-restart-1"
    finally:
        await rt2.stop()
        await db.dispose()
