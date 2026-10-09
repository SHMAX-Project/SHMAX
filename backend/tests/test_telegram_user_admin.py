"""Audit P0 #7: web Users approval / block / role / nickname endpoints."""

from __future__ import annotations

import pytest

from shmax.database import Database, DatabaseSettings
from shmax.database.migrations import run_migrations_sync
from shmax.database.models import TelegramUser


@pytest.fixture
def db(tmp_path):
    settings = DatabaseSettings(data_dir=tmp_path)
    run_migrations_sync(settings.url)
    database = Database(settings)
    yield database


@pytest.mark.asyncio
async def test_users_endpoint_returns_all_columns(db):
    """The GET /api/telegram/users endpoint must surface approved /
    nickname / blocked. Verified directly against the repository
    that backs the response payload."""
    from shmax.users.repository import TelegramUsersRepository

    async with db.session() as s:
        s.add(
            TelegramUser(
                user_id="u1",
                display_name="Alice",
                username="alice",
                nickname="ali",
                role="operator",
                approved=True,
                blocked=False,
            )
        )
        await s.commit()

    repo = TelegramUsersRepository(db)
    rows = await repo.list_all()
    assert len(rows) == 1
    u = rows[0]
    # The /api/telegram/users payload projects every one of these
    # fields; if the audit-required columns were dropped from the ORM
    # the projection would silently disappear. Verify at the data
    # source.
    assert u.user_id == "u1"
    assert u.nickname == "ali"
    assert u.approved is True
    assert u.blocked is False
    assert u.role == "operator"


@pytest.mark.asyncio
async def test_set_approval_updates_role(db):
    """Approving a user must persist approved + role."""
    from shmax.users.repository import TelegramUsersRepository

    async with db.session() as s:
        s.add(TelegramUser(user_id="u1", role="read_only"))
        await s.commit()

    repo = TelegramUsersRepository(db)
    row = await repo.set_approval(user_id="u1", approved=True, role="operator")
    assert row is not None
    assert row.approved is True
    assert row.role == "operator"


@pytest.mark.asyncio
async def test_set_blocked_blocks_user(db):
    from shmax.users.repository import TelegramUsersRepository

    async with db.session() as s:
        s.add(TelegramUser(user_id="u1", role="operator", approved=True))
        await s.commit()

    repo = TelegramUsersRepository(db)
    row = await repo.set_blocked(user_id="u1", blocked=True)
    assert row is not None
    assert row.blocked is True

    async with db.session() as s:
        from sqlalchemy import select

        rows = (await s.execute(select(TelegramUser))).scalars().all()
        assert rows[0].blocked is True
        # role and approval must NOT be touched
        assert rows[0].approved is True
        assert rows[0].role == "operator"


@pytest.mark.asyncio
async def test_set_nickname_isolated_from_register(db):
    """Telegram's display_name must survive a /setname command."""
    from shmax.users.repository import TelegramUsersRepository

    async with db.session() as s:
        s.add(
            TelegramUser(
                user_id="u1",
                display_name="Alice",
                username="alice",
                role="read_only",
            )
        )
        await s.commit()

    repo = TelegramUsersRepository(db)
    row = await repo.set_nickname(user_id="u1", nickname="ali")
    assert row.nickname == "ali"
    # display_name and username must NOT be touched.
    assert row.display_name == "Alice"
    assert row.username == "alice"


@pytest.mark.asyncio
async def test_register_or_update_preserves_operator_fields(db):
    """Audit #8: register_or_update must not overwrite operator-
    controlled fields (approved, nickname, role, blocked)."""
    from shmax.users.repository import TelegramUsersRepository

    async with db.session() as s:
        s.add(
            TelegramUser(
                user_id="u1",
                display_name="Alice",
                nickname="ali",
                role="operator",
                approved=True,
                blocked=True,
            )
        )
        await s.commit()

    repo = TelegramUsersRepository(db)
    row = await repo.register_or_update(
        user_id="u1", display_name="NewName", username="alice"
    )
    assert row.display_name == "NewName"  # contact info refreshed
    # Operator-controlled fields MUST be preserved.
    assert row.nickname == "ali"
    assert row.role == "operator"
    assert row.approved is True
    assert row.blocked is True