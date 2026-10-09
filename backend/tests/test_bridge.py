"""Bridge engine: end-to-end with FakeMaxAdapter + FakeTelegramAdapter."""

from __future__ import annotations

import asyncio
import datetime as _dt

import pytest

from shmax.adapters.max.fake import FakeMaxAdapter
from shmax.adapters.telegram.fake import FakeTelegramAdapter
from shmax.auth.pairing import AclService
from shmax.bridge.engine import BridgeConfig, BridgeEngine, _strip_marker
from shmax.users.repository import TelegramUsersRepository
from shmax.database.models import (
    MaxAccount,
    MaxChat,
    TelegramAccount,
)
from shmax.domain import (
    ChatKind,
    ChatRef,
    Direction,
    NormalizedMessage,
    SenderRef,
)
from shmax.queue.repository import DeliveryQueueRepository
from shmax.routing.repository import RoutesRepository


@pytest.fixture()
def bridge(db_session, db):
    # Seed accounts.
    from sqlalchemy import update as sa_update

    db_session.add(MaxAccount(label="primary", phone=None, status="connected"))
    db_session.add(TelegramAccount(label="primary", status="connected"))
    db_session.commit()

    async def _seed():
        async with db.session() as s:
            max_acc = (await s.execute(db_session.query(MaxAccount) if False else __import__("sqlalchemy").select(MaxAccount))).scalars().first()
            tg_acc = (await s.execute(__import__("sqlalchemy").select(TelegramAccount))).scalars().first()
            return max_acc.id, tg_acc.id

    return db, db_session


@pytest.mark.asyncio
async def test_strip_marker():
    body, marker = _strip_marker("\u200Bshmax:deadbeef00000000deadbeef00000000\nhello")
    assert marker == "deadbeef00000000deadbeef00000000"
    assert body == "hello"


@pytest.mark.asyncio
async def test_bridge_max_to_telegram_e2e(db, db_session):
    # Seed accounts.
    db_session.add(MaxAccount(label="primary", status="connected"))
    db_session.add(TelegramAccount(label="primary", status="connected"))
    await db_session.commit()
    max_id = (await db_session.execute(__import__("sqlalchemy").select(MaxAccount))).scalars().first().id
    tg_id = (await db_session.execute(__import__("sqlalchemy").select(TelegramAccount))).scalars().first().id

    max_adapter = FakeMaxAdapter()
    tg_adapter = FakeTelegramAdapter()
    routes = RoutesRepository(db_session)
    queue = DeliveryQueueRepository(db_session)
    mapping = __import__("shmax.bridge.message_map", fromlist=["MessageMapRepository"]).MessageMapRepository(db_session)
    bridge = BridgeEngine(
        config=BridgeConfig(max_account_id=max_id, telegram_account_id=tg_id),
        routes=routes,
        mapping=mapping,
        queue=queue,
        max_adapter=max_adapter,
        telegram_adapter=tg_adapter,
        acl=AclService(db_session),
        users=TelegramUsersRepository(db),
        default_permission="view_chat",  # MAX -> TG requires view_chat
    )

    # Create a route MAX-dialog-1 -> TG chat
    await routes.upsert(
        max_account_id=max_id,
        max_chat_id="max-dialog-1",
        telegram_account_id=tg_id,
        telegram_chat_id="tg-chat-1",
    )

    # Pair the test sender with the operator role so the
    # fail-closed ACL allows view_chat. In production this
    # happens through PairingCodeService.consume().
    from shmax.auth.pairing import AclRule
    from shmax.database.models import TelegramUser

    db_session.add(
        TelegramUser(
            user_id="m1",
            display_name="M",
            role="operator",
            blocked=False,
        )
    )
    await db_session.commit()

    event = NormalizedMessage(
        source="max",
        chat=ChatRef(transport="max", external_id="max-dialog-1", kind=ChatKind.DIALOG),
        sender=SenderRef(transport="max", external_id="m1", display_name="M"),
        external_id="m-msg-1",
        text="привет",
        timestamp=int(_dt.datetime.now().timestamp()),
    )
    accepted = await bridge.handle_max_event(event)
    assert accepted is True
    counts = await queue.counts_by_state()
    assert counts["pending"] == 1


@pytest.mark.asyncio
async def test_bridge_dedup_drops_known_max_event(db, db_session):
    db_session.add(MaxAccount(label="primary", status="connected"))
    db_session.add(TelegramAccount(label="primary", status="connected"))
    await db_session.commit()
    max_id = (await db_session.execute(__import__("sqlalchemy").select(MaxAccount))).scalars().first().id
    tg_id = (await db_session.execute(__import__("sqlalchemy").select(TelegramAccount))).scalars().first().id

    routes = RoutesRepository(db_session)
    queue = DeliveryQueueRepository(db_session)
    mapping = __import__("shmax.bridge.message_map", fromlist=["MessageMapRepository"]).MessageMapRepository(db_session)
    bridge = BridgeEngine(
        config=BridgeConfig(max_account_id=max_id, telegram_account_id=tg_id),
        routes=routes,
        mapping=mapping,
        queue=queue,
        max_adapter=FakeMaxAdapter(),
        telegram_adapter=FakeTelegramAdapter(),
        acl=AclService(db_session),
        users=TelegramUsersRepository(db),
        default_permission="view_chat",
    )
    await routes.upsert(
        max_account_id=max_id,
        max_chat_id="max-dialog-1",
        telegram_account_id=tg_id,
        telegram_chat_id="tg-chat-1",
    )
    from shmax.database.models import TelegramUser
    await db_session.commit()
    event = NormalizedMessage(
        source="max",
        chat=ChatRef(transport="max", external_id="max-dialog-1", kind=ChatKind.DIALOG),
        sender=SenderRef(transport="max", external_id="m1"),
        external_id="dup-id",
        text="x",
    )
    assert await bridge.handle_max_event(event) is True
    # Second time: mapping exists for this id, so we drop.
    # Pre-seed the mapping manually since the queue has not been drained.
    from shmax.database.models import MessageMap

    db_session.add(MessageMap(
        direction="max_to_telegram",
        max_account_id=max_id,
        max_chat_id="max-dialog-1",
        max_message_id="dup-id",
        telegram_account_id=tg_id,
        telegram_chat_id="tg-chat-1",
        outbound_marker="abc",
    ))
    await db_session.commit()
    assert await bridge.handle_max_event(event) is False


@pytest.mark.asyncio
async def test_bridge_drops_echo_of_our_own_message(db, db_session):
    db_session.add(MaxAccount(label="primary", status="connected"))
    db_session.add(TelegramAccount(label="primary", status="connected"))
    await db_session.commit()
    max_id = (await db_session.execute(__import__("sqlalchemy").select(MaxAccount))).scalars().first().id
    tg_id = (await db_session.execute(__import__("sqlalchemy").select(TelegramAccount))).scalars().first().id

    routes = RoutesRepository(db_session)
    queue = DeliveryQueueRepository(db_session)
    mapping = __import__("shmax.bridge.message_map", fromlist=["MessageMapRepository"]).MessageMapRepository(db_session)
    bridge = BridgeEngine(
        config=BridgeConfig(max_account_id=max_id, telegram_account_id=tg_id),
        routes=routes,
        mapping=mapping,
        queue=queue,
        max_adapter=FakeMaxAdapter(),
        telegram_adapter=FakeTelegramAdapter(),
        acl=AclService(db_session),
        users=TelegramUsersRepository(db),
        default_permission="view_chat",
    )
    await routes.upsert(
        max_account_id=max_id,
        max_chat_id="max-dialog-1",
        telegram_account_id=tg_id,
        telegram_chat_id="tg-chat-1",
    )
    from shmax.database.models import TelegramUser
    await db_session.commit()

    # Pre-seed a mapping whose outbound_marker matches the marker we'll embed.
    marker = "deadbeef00000000deadbeef00000000"
    from shmax.database.models import MessageMap

    db_session.add(MessageMap(
        direction="max_to_telegram",
        max_account_id=max_id,
        max_chat_id="max-dialog-1",
        max_message_id="echo-source",
        telegram_account_id=tg_id,
        telegram_chat_id="tg-chat-1",
        outbound_marker=marker,
    ))
    await db_session.commit()

    event = NormalizedMessage(
        source="max",
        chat=ChatRef(transport="max", external_id="max-dialog-1", kind=ChatKind.DIALOG),
        sender=SenderRef(transport="max", external_id="m1"),
        external_id="echo-id",
        text=f"\u200Bshmax:{marker}\nhello",
    )
    assert await bridge.handle_max_event(event) is False
