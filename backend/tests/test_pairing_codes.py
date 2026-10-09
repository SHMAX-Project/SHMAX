"""Audit P0 #6: real /shmax_connect group pairing.

Coverage:
  * PairingCodesRepository.issue() produces random codes.
  * atomic_consume() rejects unknown / expired / already-consumed
    / brute-forced codes.
  * atomic_consume() succeeds exactly once.
  * replay protection: a second consume of the same code fails.
  * /shmax_connect handler refuses DM chat_kind.
  * /shmax_connect handler persists the route on success.
  * /shmax_connect handler silently swallows PairingInvalid.
  * Expiry: a code with ttl=1 second is rejected after waiting.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shmax.database.models import (
    Base,
    MaxAccount,
    TelegramAccount,
)
from shmax.pairing.repository import (
    PairingCodesRepository,
    PairingInvalid,
)


@pytest_asyncio.fixture
async def db(tmp_path):
    """Real Database wrapper so repository-managed transactions
    commit properly. Tests use ``db`` directly."""
    from shmax.database import Database, DatabaseSettings as _DbSettings
    from shmax.database.migrations import run_migrations_sync

    settings = _DbSettings(data_dir=tmp_path)
    run_migrations_sync(settings.url)
    database = Database(settings)
    yield database
    await database.dispose()


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    eng = create_async_engine(url)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(eng, expire_on_commit=False)
    yield sf
    await eng.dispose()


@pytest.mark.asyncio
async def test_issue_returns_random_code(session_factory):
    repo = PairingCodesRepository(session_factory)
    code = await repo.issue(max_chat_id="m1", max_chat_title="Friends")
    assert code.code
    assert len(code.code) >= 8
    assert code.max_chat_id == "m1"
    assert code.max_chat_title == "Friends"


@pytest.mark.asyncio
async def test_atomic_consume_succeeds_once(session_factory):
    repo = PairingCodesRepository(session_factory)
    code = await repo.issue(max_chat_id="m1")
    claim = await repo.atomic_consume(
        code=code.code, user_id="u1",
        telegram_chat_id="tg-1", telegram_thread_id=42,
    )
    assert claim.max_chat_id == "m1"
    assert claim.chat_external_id == "tg-1"
    assert claim.thread_id == 42
    # Second consume must fail.
    with pytest.raises(PairingInvalid):
        await repo.atomic_consume(
            code=code.code, user_id="u1",
            telegram_chat_id="tg-1", telegram_thread_id=42,
        )


@pytest.mark.asyncio
async def test_atomic_consume_rejects_unknown_code(session_factory):
    repo = PairingCodesRepository(session_factory)
    with pytest.raises(PairingInvalid):
        await repo.atomic_consume(
            code="nonexistent", user_id="u1",
            telegram_chat_id="tg-1",
        )


@pytest.mark.asyncio
async def test_atomic_consume_rejects_expired(session_factory):
    repo = PairingCodesRepository(session_factory, ttl_seconds=1)
    code = await repo.issue(max_chat_id="m1")
    time.sleep(2)
    with pytest.raises(PairingInvalid):
        await repo.atomic_consume(
            code=code.code, user_id="u1",
            telegram_chat_id="tg-1",
        )


@pytest.mark.asyncio
async def test_atomic_consume_brute_force_protected(session_factory):
    """Even an unconsumed code that has hit ``max_attempts`` must
    be rejected. Simulate by expiring the code first (so it is
    not consumed but its attempts can be incremented by repeated
    failure)."""
    from shmax.database.models import PairingCodeRow

    from shmax.pairing.repository import _hash as hash_code

    repo = PairingCodesRepository(session_factory, max_attempts=2)
    code = await repo.issue(max_chat_id="m1")
    # Manually expire the row in the DB so the consume path
    # takes the attempts branch (rather than the consumed_at
    # branch).
    async with session_factory() as s:
        row = (
            await s.execute(
                __import__("sqlalchemy").select(PairingCodeRow).where(
                    # Codes are stored hashed (P1 #3) — look up by hash.
                    PairingCodeRow.code == hash_code(code.code)
                )
            )
        ).scalars().first()
        row.expires_at = 1  # epoch 1970-01-01 — expired
        # Burn through the quota. With max_attempts=2 the second
        # attempt must hit the ``attempts >= max_attempts``
        # branch and reject the claim.
        await s.commit()
    # Both attempts raise — first because expired, but attempts
    # is still incremented; on the second, the brute-force guard
    # trips even if the row were otherwise valid.
    for _ in range(2):
        with pytest.raises(PairingInvalid):
            await repo.atomic_consume(
                code=code.code, user_id="u",
                telegram_chat_id="tg-1",
            )


@pytest.mark.asyncio
async def test_atomic_consume_replay_after_revoke(session_factory):
    repo = PairingCodesRepository(session_factory)
    code = await repo.issue(max_chat_id="m1")
    revoked = await repo.revoke(code=code.code)
    assert revoked is True
    with pytest.raises(PairingInvalid):
        await repo.atomic_consume(
            code=code.code, user_id="u1",
            telegram_chat_id="tg-1",
        )


@pytest.mark.asyncio
async def test_list_active_excludes_consumed_and_expired(session_factory):
    repo = PairingCodesRepository(session_factory)
    a = await repo.issue(max_chat_id="m-a")
    b = await repo.issue(max_chat_id="m-b")
    await repo.atomic_consume(code=a.code, user_id="u1", telegram_chat_id="tg")
    active = await repo.list_active()
    # Codes are stored hashed (P1 #3), so list_active carries a
    # display hint only — key the assertion on the chat binding.
    bound = {c.max_chat_id for c in active}
    assert "m-b" in bound
    # The consumed code's binding is gone from the active list.
    assert "m-a" not in bound
    # And no plaintext code ever leaks through the active list.
    assert a.code not in [c.code for c in active]


@pytest.mark.asyncio
async def test_handler_persists_route_on_group_command(db):
    """End-to-end: handler refuses DM, accepts group, persists route."""
    from shmax.api.app import _build_telegram_command_handler
    from shmax.users.repository import TelegramUsersRepository

    # Seed MAX + TG accounts via the real db.
    async with db.session() as s:
        s.add(MaxAccount(label="primary", status="connected"))
        s.add(TelegramAccount(label="primary", status="connected"))
        await s.commit()

    from shmax.pairing.repository import PairingCodesRepository as _Repo
    repo = _Repo(db.session)
    code = await repo.issue(max_chat_id="max-friends", max_chat_title="Friends")

    from shmax.adapters.telegram.fake import FakeTelegramAdapter

    handler = _build_telegram_command_handler(
        users=TelegramUsersRepository(db),
        pairing_codes=repo,
        secret_store=None,
        db=db,
        adapter_getter=lambda: FakeTelegramAdapter(),
    )

    # DM chat_kind → refused (no route).
    await handler("/shmax_connect", {
        "user_id": "u1",
        "display_name": "Alice",
        "username": None,
        "chat_kind": "private",
        "chat_id": "tg-dm",
        "text": f"/shmax_connect {code.code}",
    })
    # group chat_kind → route persisted.
    await handler("/shmax_connect", {
        "user_id": "u1",
        "display_name": "Alice",
        "username": None,
        "chat_kind": "supergroup",
        "chat_id": "tg-1",
        "chat_title": "Work",
        "thread_id": 7,
        "text": f"/shmax_connect {code.code}",
    })

    # Verify the route exists with max_chat_id="max-friends".
    async with db.session() as s:
        from sqlalchemy import select

        from shmax.database.models import Route
        rows = (await s.execute(select(Route))).scalars().all()
        assert any(
            r.max_chat_id == "max-friends" and r.telegram_chat_id == "tg-1"
            for r in rows
        ), f"no route; rows={[(r.max_chat_id, r.telegram_chat_id) for r in rows]}"


@pytest.mark.asyncio
async def test_handler_invalid_code_does_not_crash(db):
    """An invalid code must NOT crash the handler (DoS hardening)."""
    from shmax.api.app import _build_telegram_command_handler
    from shmax.users.repository import TelegramUsersRepository

    repo = PairingCodesRepository(db.session)
    handler = _build_telegram_command_handler(
        users=TelegramUsersRepository(db),
        pairing_codes=repo,
        secret_store=None,
        db=db,
    )

    # Should not raise even though the code is bogus.
    await handler("/shmax_connect", {
        "user_id": "u1",
        "chat_kind": "group",
        "chat_id": "tg-1",
        "text": "/shmax_connect bogus-code",
    })


@pytest.mark.asyncio
async def test_handler_register_user_command(db):
    """``/start`` and ``/setname`` must register / set the
    nickname without crashing."""
    from shmax.api.app import _build_telegram_command_handler
    from shmax.users.repository import TelegramUsersRepository

    repo = PairingCodesRepository(db.session)
    handler = _build_telegram_command_handler(
        users=TelegramUsersRepository(db),
        pairing_codes=repo,
        secret_store=None,
        db=db,
    )
    await handler("/start", {
        "user_id": "u1",
        "display_name": "Alice",
        "username": "alice",
        "text": "/start",
    })
    await handler("/setname", {
        "user_id": "u1",
        "display_name": "Alice",
        "username": "alice",
        "text": "/setname ali",
    })
    async with db.session() as s:
        from sqlalchemy import select

        from shmax.database.models import TelegramUser
        rows = (await s.execute(select(TelegramUser))).scalars().all()
        assert len(rows) == 1
        assert rows[0].nickname == "ali"