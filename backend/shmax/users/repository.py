"""Repository for :class:`shmax.database.models.TelegramUser`.

Pure persistence layer; the bridge / API handlers stay
free of SQLAlchemy ORM specifics. Operations are idempotent:
``register_or_update`` is safe to call on every inbound event
because it only updates ``last_seen_at`` and the Telegram-supplied
``display_name`` / ``username``; never the operator-controlled
``nickname`` or ``approved`` flags.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..database.engine import Database
from ..database.models import TelegramUser
from .nickname import normalize_nickname


@dataclass(frozen=True)
class TelegramUserRow:
    """Public view of a :class:`TelegramUser` row.

    The dataclass deliberately excludes internal autoincrement
    ids and timestamps so the bridge engine can pass it
    around without leaking mutable ORM references.
    """

    user_id: str
    display_name: Optional[str]
    username: Optional[str]
    nickname: Optional[str]
    role: str
    approved: bool
    blocked: bool
    membership: Optional[str] = None
    is_admin: bool = False
    source: Optional[str] = None


def _from_orm(row: TelegramUser) -> TelegramUserRow:
    return TelegramUserRow(
        user_id=row.user_id,
        display_name=row.display_name,
        username=row.username,
        nickname=row.nickname,
        role=row.role,
        approved=row.approved,
        blocked=row.blocked,
        membership=row.membership,
        is_admin=row.is_admin,
        source=row.source,
    )


class TelegramUsersRepository:
    """Owner-aware persistence for TelegramUser rows."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def get(self, user_id: str) -> Optional[TelegramUserRow]:
        async with self._db.session() as session:  # type: ignore[attr-defined]
            row = (
                await session.execute(
                    select(TelegramUser).where(TelegramUser.user_id == user_id)
                )
            ).scalar_one_or_none()
            return _from_orm(row) if row is not None else None

    async def register_or_update(
        self,
        *,
        user_id: str,
        display_name: Optional[str],
        username: Optional[str],
    ) -> TelegramUserRow:
        """Insert the row on first sight; refresh contact info on
        subsequent calls. Operator-controlled fields (``approved``,
        ``nickname``, ``role``, ``blocked``) are NEVER overwritten
        here — those are owned by the web Users page.
        """
        now = datetime.now(timezone.utc)
        values = dict(user_id=user_id, display_name=display_name, username=username,
                      last_seen_at=now, first_seen_at=now, role="read_only",
                      approved=False, blocked=False, source="message")
        async with self._db.session() as session:
            await session.execute(insert(TelegramUser).values(**values).on_conflict_do_update(
                index_elements=[TelegramUser.user_id],
                set_={k: values[k] for k in ("display_name", "username", "last_seen_at")},
            ))
            await session.commit()
            row = (await session.execute(select(TelegramUser).where(
                TelegramUser.user_id == user_id))).scalar_one()
            return _from_orm(row)

    async def set_nickname(
        self, *, user_id: str, nickname: Optional[str]
    ) -> TelegramUserRow:
        """Update only the local attribution override, never Telegram identity/ACL."""
        nickname = normalize_nickname(nickname)
        async with self._db.session() as session:  # type: ignore[attr-defined]
            row = (
                await session.execute(
                    select(TelegramUser).where(TelegramUser.user_id == user_id)
                )
            ).scalar_one_or_none()
            if row is None:
                row = TelegramUser(
                    user_id=user_id,
                    nickname=nickname,
                    role="read_only",
                    approved=False,
                    blocked=False,
                )
                session.add(row)
            else:
                row.nickname = nickname
            await session.commit()
            return _from_orm(row)

    async def set_approval(
        self, *, user_id: str, approved: bool, role: Optional[str] = None
    ) -> TelegramUserRow:
        async with self._db.session() as session:  # type: ignore[attr-defined]
            row = (
                await session.execute(
                    select(TelegramUser).where(TelegramUser.user_id == user_id)
                )
            ).scalar_one_or_none()
            if row is None:
                # New rows get a REAL Role enum value — the old
                # "send_message" default was not a valid role and
                # produced principals the ACL could never resolve.
                row = TelegramUser(
                    user_id=user_id,
                    approved=approved,
                    role=role or ("operator" if approved else "read_only"),
                )
                session.add(row)
            else:
                row.approved = approved
                if role is not None:
                    row.role = role
            await session.commit()
            return _from_orm(row)

    async def set_blocked(
        self, *, user_id: str, blocked: bool
    ) -> TelegramUserRow:
        async with self._db.session() as session:  # type: ignore[attr-defined]
            row = (
                await session.execute(
                    select(TelegramUser).where(TelegramUser.user_id == user_id)
                )
            ).scalar_one_or_none()
            if row is None:
                row = TelegramUser(user_id=user_id, blocked=blocked, role="read_only")
                session.add(row)
            else:
                row.blocked = blocked
            await session.commit()
            return _from_orm(row)

    async def observe_member(self, *, user_id: str, username: Optional[str],
                             display_name: Optional[str], membership: str,
                             is_admin: bool, source: str) -> tuple[TelegramUserRow, bool, bool]:
        """Atomic observation; serialize against concurrent message/event discovery.

        Membership metadata never changes authorization or operator names.
        """
        from sqlalchemy import text
        now = datetime.now(timezone.utc)
        async with self._db.session() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            row = (await session.execute(select(TelegramUser).where(
                TelegramUser.user_id == user_id))).scalar_one_or_none()
            created = row is None
            fields = dict(username=username, display_name=display_name,
                          membership=membership, is_admin=is_admin)
            changed = row is not None and any(getattr(row, k) != v for k, v in fields.items())
            if row is None:
                row = TelegramUser(user_id=user_id, role="read_only", approved=False,
                                   blocked=False, first_seen_at=now)
                session.add(row)
            for k, v in fields.items():
                setattr(row, k, v)
            row.source = source
            row.last_seen_at = now
            await session.commit()
            return _from_orm(row), created, changed

    async def list_all(self) -> list[TelegramUserRow]:
        async with self._db.session() as session:  # type: ignore[attr-defined]
            rows = (
                (await session.execute(select(TelegramUser).order_by(TelegramUser.first_seen_at.desc())))
                .scalars()
                .all()
            )
            return [_from_orm(r) for r in rows]


__all__ = ["TelegramUserRow", "TelegramUsersRepository"]