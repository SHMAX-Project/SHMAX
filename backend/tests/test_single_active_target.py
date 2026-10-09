"""P1 #6 — exactly ONE active Telegram target.

Pairing group A then group B must leave ONLY B active:
  * deactivate_other_targets() atomically deactivates A;
  * the canonical resolver (active_target.get_active_target)
    returns B deterministically;
  * readiness / the wizard Test probe resolve B.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from shmax.active_target import (
    deactivate_other_targets,
    get_active_target,
)
from shmax.database import Database, DatabaseSettings
from shmax.database.migrations import run_migrations_sync
from shmax.database.models import (
    TelegramAccount,
    TelegramTarget,
    Route,
    MaxAccount,
)


@pytest_asyncio.fixture
async def db(tmp_path):
    settings = DatabaseSettings(data_dir=tmp_path)
    run_migrations_sync(settings.url)
    database = Database(settings)
    async with database.session() as s:
        s.add(TelegramAccount(label="primary", bot_id="1", status="verified"))
        s.add(MaxAccount(label="primary", status="connected"))
        await s.commit()
    yield database
    await database.dispose()


async def _pair(db, chat_id: str, title: str):
    """Pair exactly the way the production handler does: ONE
    serialized transaction (upsert + validate + deactivate)."""
    from shmax.active_target import upsert_and_activate_target

    await upsert_and_activate_target(
        db,
        account_id=1,
        chat_id=chat_id,
        title=title,
        is_forum=True,
        chat_type="supergroup",
        bot_status="administrator",
    )


@pytest.mark.asyncio
async def test_pair_b_replaces_a(db):
    await _pair(db, "-100AAA", "Group A")
    active = await get_active_target(db)
    assert active is not None and active.chat_id == "-100AAA"

    await _pair(db, "-100BBB", "Group B")
    active = await get_active_target(db)
    assert active is not None
    assert active.chat_id == "-100BBB", "group B must be the active target"

    from sqlalchemy import func

    async with db.session() as s:
        rows = (await s.execute(select(TelegramTarget))).scalars().all()
    assert len(rows) == 2, "A is kept for diagnostics"
    a = next(r for r in rows if r.chat_id == "-100AAA")
    b = next(r for r in rows if r.chat_id == "-100BBB")
    # A is deactivated: flag off, no validated facts, marker recorded.
    assert a.is_active is False
    assert a.validated_at is None
    assert "деактивирована" in (a.missing_permissions or "")
    # B is the ONLY active row, fully validated.
    assert b.is_active is True
    assert b.validated_at is not None
    assert (b.missing_permissions or "[]") == "[]"
    active_rows = [r for r in rows if r.is_active]
    assert len(active_rows) == 1, "exactly one active target"


@pytest.mark.asyncio
async def test_repair_of_deactivated_target_not_possible_via_active_lookup(db):
    """A deactivated target must never resolve as active even when
    it is the newest row by cache time."""
    import datetime as dt

    await _pair(db, "-100AAA", "Group A")
    # Touch A's cached_at forward so a cached_at-ordered lookup
    # would prefer the DEACTIVATED row.
    async with db.session() as s:
        row = (
            await s.execute(
                select(TelegramTarget).where(TelegramTarget.chat_id == "-100AAA")
            )
        ).scalars().first()
        row.cached_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        await s.commit()

    await _pair(db, "-100BBB", "Group B")
    active = await get_active_target(db)
    assert active.chat_id == "-100BBB", (
        "validated_at-first ordering must win over stale cached_at"
    )


@pytest.mark.asyncio
async def test_wizard_readiness_uses_active_target(db, monkeypatch):
    """The readiness gate resolves the paired group through the
    canonical active target (B after A→B)."""
    from pathlib import Path as _Path

    from shmax.core.paths import PathLayout
    from shmax.wizard_readiness import wizard_readiness

    # Reuse the db fixture's on-disk DB via a shim runtime.
    _db = db

    class _ShimRuntime:
        db = _db
        layout = PathLayout(_Path("."))

        async def integration_snapshot(self):
            return {
                "max": {"state": "HEALTHY"},
                "telegram": {"state": "HEALTHY"},
                "bridge": {"state": "HEALTHY"},
            }

    await _pair(db, "-100AAA", "Group A")
    await _pair(db, "-100BBB", "Group B")

    readiness = await wizard_readiness(_ShimRuntime())
    tg_group = readiness["telegram_group"]
    assert tg_group.ready is True
    assert "Group B" in tg_group.reason


# ----------------------------------------------------------------------
# P1 #5 (surgical) — concurrent replacement atomicity
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_concurrent_pairing_leaves_exactly_one_active(db):
    """Concurrent pair A and B (interleaved tasks on the serialized
    transaction): after both complete — exactly ONE active target,
    the resolver returns an ACTIVE row, never 0, never inactive."""
    import asyncio

    from shmax.active_target import upsert_and_activate_target

    async def pair(chat_id: str, title: str):
        await upsert_and_activate_target(
            db,
            account_id=1,
            chat_id=chat_id,
            title=title,
            is_forum=True,
            chat_type="supergroup",
            bot_status="administrator",
        )

    await asyncio.gather(
        pair("-100AAA", "Group A"),
        pair("-100BBB", "Group B"),
    )

    from sqlalchemy import select

    async with db.session() as s:
        rows = (await s.execute(select(TelegramTarget))).scalars().all()
    active = [r for r in rows if r.is_active]
    assert len(rows) == 2
    assert len(active) == 1, (
        f"exactly one active target required, got {len(active)}: "
        f"{[(r.chat_id, r.is_active) for r in rows]}"
    )

    resolver = await get_active_target(db)
    assert resolver is not None
    assert resolver.is_active is True
    assert resolver.chat_id == active[0].chat_id
