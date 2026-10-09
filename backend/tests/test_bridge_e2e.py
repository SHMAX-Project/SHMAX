"""Offline E2E: TG→MAX path (Stage 1B V1 completion)."""

from __future__ import annotations

import asyncio
import datetime as _dt

import pytest

from shmax.adapters.max.fake import FakeMaxAdapter
from shmax.adapters.telegram.fake import FakeTelegramAdapter
from shmax.auth.pairing import AclService
from shmax.bridge.engine import BridgeConfig, BridgeEngine
from shmax.database.models import MaxAccount, TelegramAccount
from shmax.domain import ChatKind, ChatRef, NormalizedMessage, SenderRef
from shmax.queue.repository import DeliveryQueueRepository
from shmax.routing.repository import RoutesRepository
from shmax.users.repository import TelegramUsersRepository


@pytest.mark.asyncio
async def test_telegram_to_max_e2e(db, db_session):
    db_session.add(MaxAccount(label="primary", status="connected"))
    db_session.add(TelegramAccount(label="primary", status="connected"))
    # Pair the TG sender with operator role so ACL pass.
    from shmax.database.models import TelegramUser
    db_session.add(TelegramUser(user_id="tg-user-1", display_name="Иван", role="operator", approved=True))
    await db_session.commit()
    max_id = (await db_session.execute(__import__("sqlalchemy").select(MaxAccount))).scalars().first().id
    tg_id = (await db_session.execute(__import__("sqlalchemy").select(TelegramAccount))).scalars().first().id

    max_adapter = FakeMaxAdapter()
    tg_adapter = FakeTelegramAdapter()
    routes = RoutesRepository(db_session)
    queue = DeliveryQueueRepository(db_session)
    mapping = __import__("shmax.bridge.message_map", fromlist=["MessageMapRepository"]).MessageMapRepository(db_session)
    bridge = BridgeEngine(
        config=BridgeConfig(
            max_account_id=max_id,
            telegram_account_id=tg_id,
            telegram_target_chat_id="tg-chat-1",
        ),
        routes=routes,
        mapping=mapping,
        queue=queue,
        max_adapter=max_adapter,
        telegram_adapter=tg_adapter,
        acl=AclService(db_session),
        users=TelegramUsersRepository(db),
        default_permission="send_message",
    )
    # Route TG-chat-1 -> MAX dialog "max-dialog-1"
    await routes.upsert(
        max_account_id=max_id,
        max_chat_id="max-dialog-1",
        telegram_account_id=tg_id,
        telegram_chat_id="tg-chat-1",
    )

    event = NormalizedMessage(
        source="telegram",
        chat=ChatRef(transport="telegram", external_id="tg-chat-1", kind=ChatKind.GROUP),
        sender=SenderRef(transport="telegram", external_id="tg-user-1", display_name="Иван"),
        external_id="tg-msg-1",
        text="привет из TG",
        timestamp=int(_dt.datetime.now().timestamp()),
    )
    accepted = await bridge.handle_telegram_event(event)
    assert accepted is True
    counts = await queue.counts_by_state()
    assert counts["pending"] == 1


@pytest.mark.asyncio
async def test_e2e_both_directions_share_queue(db, db_session):
    db_session.add(MaxAccount(label="primary", status="connected"))
    db_session.add(TelegramAccount(label="primary", status="connected"))
    # Pair senders with operator role for fail-closed ACL.
    from shmax.database.models import TelegramUser
    # NOTE: no fake TelegramUser("m1") here — the audit (P0 #6)
    # forbids fixtures that make a MAX sender pass the Telegram
    # ACL. MAX→TG is attribution, not authorization.
    db_session.add(TelegramUser(user_id="u1", role="operator", approved=True))
    await db_session.commit()
    max_id = (await db_session.execute(__import__("sqlalchemy").select(MaxAccount))).scalars().first().id
    tg_id = (await db_session.execute(__import__("sqlalchemy").select(TelegramAccount))).scalars().first().id

    max_adapter = FakeMaxAdapter()
    tg_adapter = FakeTelegramAdapter()
    routes = RoutesRepository(db_session)
    queue = DeliveryQueueRepository(db_session)
    mapping = __import__("shmax.bridge.message_map", fromlist=["MessageMapRepository"]).MessageMapRepository(db_session)
    bridge = BridgeEngine(
        config=BridgeConfig(
            max_account_id=max_id,
            telegram_account_id=tg_id,
            # P0 #7 fail-closed ACL needs the configured target.
            telegram_target_chat_id="tg-chat-1",
        ),
        routes=routes,
        mapping=mapping,
        queue=queue,
        max_adapter=max_adapter,
        telegram_adapter=tg_adapter,
        acl=AclService(db_session),
        users=TelegramUsersRepository(db),
        default_permission="send_message",
    )
    # One MAX→TG route, one TG→MAX route.
    await routes.upsert(
        max_account_id=max_id,
        max_chat_id="max-dialog-1",
        telegram_account_id=tg_id,
        telegram_chat_id="tg-chat-1",
    )

    from shmax.domain import ChatRef as CR
    # Inbound from MAX
    ev_max = NormalizedMessage(
        source="max",
        chat=CR(transport="max", external_id="max-dialog-1", kind=ChatKind.DIALOG),
        sender=SenderRef(transport="max", external_id="m1"),
        external_id="m-msg-1",
        text="hello",
    )
    assert await bridge.handle_max_event(ev_max) is True
    # Inbound from Telegram
    ev_tg = NormalizedMessage(
        source="telegram",
        chat=CR(transport="telegram", external_id="tg-chat-1", kind=ChatKind.GROUP),
        sender=SenderRef(transport="telegram", external_id="u1"),
        external_id="t-msg-1",
        text="reply",
    )
    assert await bridge.handle_telegram_event(ev_tg) is True
    counts = await queue.counts_by_state()
    assert counts["pending"] == 2


@pytest.mark.asyncio
async def test_acl_read_only_blocks_send(db, db_session):
    db_session.add(MaxAccount(label="primary", status="connected"))
    db_session.add(TelegramAccount(label="primary", status="connected"))
    db_session.add(
        __import__("shmax.database.models", fromlist=["AclRule"]).AclRule(
            scope="global", principal="tg-spammer", permission="send_message", allow=False
        )
    )
    await db_session.commit()
    max_id = (await db_session.execute(__import__("sqlalchemy").select(MaxAccount))).scalars().first().id
    tg_id = (await db_session.execute(__import__("sqlalchemy").select(TelegramAccount))).scalars().first().id

    max_adapter = FakeMaxAdapter()
    tg_adapter = FakeTelegramAdapter()
    routes = RoutesRepository(db_session)
    queue = DeliveryQueueRepository(db_session)
    mapping = __import__("shmax.bridge.message_map", fromlist=["MessageMapRepository"]).MessageMapRepository(db_session)
    bridge = BridgeEngine(
        config=BridgeConfig(
            max_account_id=max_id,
            telegram_account_id=tg_id,
            # P0 #7 fail-closed ACL needs the configured target.
            telegram_target_chat_id="tg-chat-1",
        ),
        routes=routes,
        mapping=mapping,
        queue=queue,
        max_adapter=max_adapter,
        telegram_adapter=tg_adapter,
        acl=AclService(db_session),
        users=TelegramUsersRepository(db),
        default_permission="send_message",
    )
    await routes.upsert(
        max_account_id=max_id,
        max_chat_id="max-dialog-1",
        telegram_account_id=tg_id,
        telegram_chat_id="tg-chat-1",
    )

    event = NormalizedMessage(
        source="telegram",
        chat=ChatRef(transport="telegram", external_id="tg-chat-1", kind=ChatKind.GROUP),
        sender=SenderRef(transport="telegram", external_id="tg-spammer"),
        external_id="blocked-1",
        text="spam",
    )
    assert await bridge.handle_telegram_event(event) is False
    counts = await queue.counts_by_state()
    assert counts["pending"] == 0
