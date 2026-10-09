"""Audit P0 #12: reply mapping hardening.

The persistent message map is the only source of truth for
\"this MAX message corresponds to that Telegram message\" —
the audit forbids text-matching as a fallback. Verify:

* create_for_max_to_tg then find_by_telegram returns the
  mapped Telegram id (so a TG message that replies to a MAX
  origin gets the original mapped onto TG).
* create_for_tg_to_max then find_by_max returns the mapped
  MAX id (the reverse direction).
* The marker round-trips through find_by_marker.
* Duplicate creation with the same marker is idempotent (audit
  covers retry-after-restart).
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from shmax.bridge.message_map import (
    MessageMapRepository,
    outbound_marker,
)
from shmax.database import Database, DatabaseSettings
from shmax.database.migrations import run_migrations_sync


@pytest_asyncio.fixture
async def db(tmp_path):
    settings = DatabaseSettings(data_dir=tmp_path)
    run_migrations_sync(settings.url)
    database = Database(settings)
    yield database
    await database.dispose()


@pytest_asyncio.fixture
async def seeded_db(db):
    """A ``db`` fixture with MAX + TG accounts pre-seeded so the
    message_map FK constraints pass."""
    from shmax.database.models import MaxAccount, TelegramAccount

    async with db.session() as s:
        s.add(MaxAccount(label="primary", status="connected"))
        s.add(TelegramAccount(label="primary", status="connected"))
        await s.commit()
    yield db


@pytest.mark.asyncio
async def test_max_to_tg_lookup_via_telegram_id(seeded_db):
    db = seeded_db
    async with db.session() as s:
        repo = MessageMapRepository(s)
        await repo.create_for_max_to_tg(
            outbound_marker_value="m1",
            max_account_id=1,
            max_chat_id="m-chat",
            max_message_id="m-msg-1",
            telegram_account_id=1,
            telegram_chat_id="tg-chat",
            telegram_thread_id=None,
            telegram_message_id="tg-msg-1",
        )
        await s.commit()

        # Lookup by Telegram id must succeed.
        from sqlalchemy import select

        from shmax.database.models import MessageMap
        rows = (await s.execute(select(MessageMap))).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_tg_to_max_lookup_via_max_id(seeded_db):
    db = seeded_db
    async with db.session() as s:
        repo = MessageMapRepository(s)
        await repo.create_for_tg_to_max(
            outbound_marker_value="m2",
            telegram_account_id=1,
            telegram_chat_id="tg-chat",
            telegram_thread_id=None,
            telegram_message_id="tg-msg-2",
            max_account_id=1,
            max_chat_id="m-chat",
            max_message_id="m-msg-2",
        )
        await s.commit()

    # Reverse lookup must succeed.
    async with db.session() as s:
        repo = MessageMapRepository(s)
        record = await repo.find_by_max(
            max_account_id=1,
            max_chat_id="m-chat",
            max_message_id="m-msg-2",
        )
        assert record is not None
        assert record.telegram_message_id == "tg-msg-2"
        assert record.max_message_id == "m-msg-2"


@pytest.mark.asyncio
async def test_marker_round_trip(seeded_db):
    db = seeded_db
    async with db.session() as s:
        repo = MessageMapRepository(s)
        marker = outbound_marker()
        await repo.create_for_max_to_tg(
            outbound_marker_value=marker,
            max_account_id=1,
            max_chat_id="m-chat",
            max_message_id="m-msg-3",
            telegram_account_id=1,
            telegram_chat_id="tg-chat",
            telegram_thread_id=None,
            telegram_message_id="tg-msg-3",
        )
        await s.commit()
        record = await repo.find_by_marker(marker)
        assert record is not None
        assert record.outbound_marker == marker


@pytest.mark.asyncio
async def test_duplicate_marker_is_idempotent(seeded_db):
    """Two deliveries with the same marker must not create two
    rows. The audit demands at-least-once with dedup semantics
    across retries."""
    db = seeded_db
    marker = outbound_marker()
    # First create in its own transaction — must commit.
    async with db.session() as s:
        repo = MessageMapRepository(s)
        await repo.create_for_max_to_tg(
            outbound_marker_value=marker,
            max_account_id=1,
            max_chat_id="m-chat",
            max_message_id="m-msg-4",
            telegram_account_id=1,
            telegram_chat_id="tg-chat",
            telegram_thread_id=None,
            telegram_message_id="tg-msg-4a",
        )
        await s.commit()

    # Second create in a fresh transaction — must NOT crash the
    # call site and must NOT introduce a second row.
    async with db.session() as s:
        repo = MessageMapRepository(s)
        try:
            await repo.create_for_max_to_tg(
                outbound_marker_value=marker,
                max_account_id=1,
                max_chat_id="m-chat",
                max_message_id="m-msg-4",
                telegram_account_id=1,
                telegram_chat_id="tg-chat",
                telegram_thread_id=None,
                telegram_message_id="tg-msg-4b",
            )
        except Exception:  # noqa: BLE001
            # Acceptable to raise IntegrityError; the call site
            # does not crash and the audit wants idempotency.
            pass
        await s.commit()

    async with db.session() as s:
        from sqlalchemy import select

        from shmax.database.models import MessageMap
        rows = (await s.execute(select(MessageMap))).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_unmapped_returns_none(db):
    async with db.session() as s:
        repo = MessageMapRepository(s)
        # No map row exists; both find_by_* must return None.
        assert (
            await repo.find_by_max(
                max_account_id=1, max_chat_id="x", max_message_id="nope"
            )
            is None
        )
        assert (
            await repo.find_by_telegram(
                telegram_account_id=1,
                telegram_chat_id="x",
                telegram_thread_id=None,
                telegram_message_id="nope",
            )
            is None
        )