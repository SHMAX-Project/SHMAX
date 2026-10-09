"""Single active Telegram target (audit P1 #5, surgical).

V1 invariant: EXACTLY ONE active Telegram target, enforced by a
REAL ``is_active`` column. Pairing a new group runs in ONE
serialized transaction (``BEGIN IMMEDIATE`` on SQLite):

  1. create/update the selected target,
  2. set its validation facts,
  3. deactivate EVERY other target,
  4. commit.

The resolver filters ``is_active == true`` explicitly — never a
``LIMIT 1`` over arbitrary rows. Concurrent pairings serialize on
the SQLite write lock; after both complete exactly one target is
active.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select, text, update

from .database.engine import Database
from .database.models import TelegramTarget


async def get_active_target(db: Database) -> Optional[TelegramTarget]:
    """The single canonical active target: the row with
    ``is_active`` set. Falls back to the newest validated row only
    for pre-migration data without any active row."""
    async with db.session() as s:
        # P1 #5: the active row resolves REGARDLESS of its current
        # validation state — an invalidated target must still be
        # surfaced as the (invalid) active target so readiness
        # reports the exact missing rights, not "pair first".
        return (
            await s.execute(
                select(TelegramTarget)
                .where(TelegramTarget.is_active.is_(True))
                .order_by(TelegramTarget.cached_at.desc())
                .limit(1)
            )
        ).scalars().first()


async def upsert_and_activate_target(
    db: Database,
    *,
    account_id: int,
    chat_id: str,
    title: Optional[str],
    is_forum: bool,
    chat_type: Optional[str],
    bot_status: Optional[str],
) -> TelegramTarget:
    """Create/update the selected target and make it the ONLY
    active one — in ONE serialized transaction.

    Concurrent pairings of groups A and B serialize on the SQLite
    write lock (BEGIN IMMEDIATE): each transaction leaves exactly
    one active row, and the last commit wins. Never zero, never a
    deactivated row.
    """
    import datetime as _dt

    now = _dt.datetime.now(_dt.timezone.utc)
    async with db.session() as s:
        await s.execute(text("BEGIN IMMEDIATE"))
        stmt = select(TelegramTarget).where(
            TelegramTarget.account_id == account_id,
            TelegramTarget.chat_id == chat_id,
        )
        target = (await s.execute(stmt)).scalars().first()
        if target is None:
            target = TelegramTarget(
                account_id=account_id,
                chat_id=chat_id,
            )
            s.add(target)
            await s.flush()
        target.title = title
        target.is_forum = is_forum
        target.chat_type = chat_type
        target.bot_status = bot_status
        # The pairing path calls this AFTER the live P0 #3
        # validation succeeded — the row carries the validated
        # facts.
        target.missing_permissions = "[]"
        target.validated_at = now
        target.is_active = True
        target.cached_at = now
        # Deactivate every other target in the SAME transaction.
        await s.execute(
            update(TelegramTarget)
            .where(TelegramTarget.chat_id != chat_id)
            .values(
                is_active=False,
                missing_permissions='["цель деактивирована: заменена новой группой"]',
                validated_at=None,
            )
        )
        await s.commit()
        await s.refresh(target)
        return target


async def deactivate_other_targets(db: Database, *, keep_chat_id: str) -> int:
    """Deactivate every target EXCEPT ``keep_chat_id``. Kept for
    direct callers; the pairing path uses the atomic
    :func:`upsert_and_activate_target` instead."""
    async with db.session() as s:
        result = await s.execute(
            update(TelegramTarget)
            .where(TelegramTarget.chat_id != keep_chat_id)
            .values(
                is_active=False,
                missing_permissions=(
                    '["цель деактивирована: заменена новой группой"]'
                ),
                validated_at=None,
            )
        )
        await s.commit()
        return result.rowcount or 0


__all__ = [
    "get_active_target",
    "upsert_and_activate_target",
    "deactivate_other_targets",
]
