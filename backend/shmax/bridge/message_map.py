"""Persistent MAX ↔ Telegram message id mapping.

This is the foundation for native reply in both directions:
when we deliver a MAX message to Telegram, we record the
resulting Telegram message id; when Telegram sends a reply,
we look the originating MAX message id back up.

The table also stores an ``outbound_marker`` UUID. That marker
travels with every outbound message (e.g. as a hidden prefix
in the text or in an attachment payload), and we use it to
recognise echoes of our own deliveries to suppress loops.
"""

from __future__ import annotations

import datetime as _dt
import secrets
import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..database.models import MessageMap


def outbound_marker() -> str:
    """Generate a fresh outbound marker (UUID4, hex)."""
    return uuid.UUID(int=secrets.randbits(128)).hex


@dataclass(frozen=True)
class MappingRecord:
    id: int
    direction: str
    max_account_id: Optional[int]
    max_chat_id: Optional[str]
    max_message_id: Optional[str]
    telegram_account_id: Optional[int]
    telegram_chat_id: Optional[str]
    telegram_thread_id: Optional[str]
    telegram_message_id: Optional[str]
    outbound_marker: str
    created_at: _dt.datetime


def _to_record(row: MessageMap) -> MappingRecord:
    return MappingRecord(
        id=row.id,
        direction=row.direction,
        max_account_id=row.max_account_id,
        max_chat_id=row.max_chat_id,
        max_message_id=row.max_message_id,
        telegram_account_id=row.telegram_account_id,
        telegram_chat_id=row.telegram_chat_id,
        telegram_thread_id=row.telegram_thread_id,
        telegram_message_id=row.telegram_message_id,
        outbound_marker=row.outbound_marker,
        created_at=row.created_at,
    )


class MessageMapRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_for_max_to_tg(
        self,
        *,
        max_account_id: int,
        max_chat_id: str,
        max_message_id: str,
        telegram_account_id: int,
        telegram_chat_id: str,
        telegram_thread_id: Optional[str],
        telegram_message_id: str,
        outbound_marker_value: str,
    ) -> MappingRecord:
        """Record a successful MAX→Telegram delivery.

        ``outbound_marker_value`` is the marker that was
        generated upstream (in the bridge when the event was
        first enqueued). The runtime MUST NOT regenerate the
        marker — the same UUID travels through the queue
        payload, the outbound message body, the dedup hash,
        and the persistent mapping row.

        The repository commits the row before returning so the
        audit-mandated at-least-once delivery semantics can rely
        on the mapping being persisted atomically with the
        external send.
        """
        row = MessageMap(
            direction="max_to_telegram",
            max_account_id=max_account_id,
            max_chat_id=max_chat_id,
            max_message_id=max_message_id,
            telegram_account_id=telegram_account_id,
            telegram_chat_id=telegram_chat_id,
            telegram_thread_id=telegram_thread_id,
            telegram_message_id=telegram_message_id,
            outbound_marker=outbound_marker_value,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError:
            # Already mapped (e.g. retry after restart). Look it up.
            await self._session.rollback()
            existing = await self.find_by_max(
                max_account_id=max_account_id,
                max_chat_id=max_chat_id,
                max_message_id=max_message_id,
            )
            if existing is None:
                raise
            return existing
        await self._session.commit()
        await self._session.refresh(row)
        return _to_record(row)

    async def create_for_tg_to_max(
        self,
        *,
        telegram_account_id: int,
        telegram_chat_id: str,
        telegram_thread_id: Optional[str],
        telegram_message_id: str,
        max_account_id: int,
        max_chat_id: str,
        max_message_id: str,
        outbound_marker_value: str,
    ) -> MappingRecord:
        """Record a successful Telegram→MAX delivery.

        Same marker contract as ``create_for_max_to_tg``.
        """
        row = MessageMap(
            direction="telegram_to_max",
            telegram_account_id=telegram_account_id,
            telegram_chat_id=telegram_chat_id,
            telegram_thread_id=telegram_thread_id,
            telegram_message_id=telegram_message_id,
            max_account_id=max_account_id,
            max_chat_id=max_chat_id,
            max_message_id=max_message_id,
            outbound_marker=outbound_marker_value,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError:
            await self._session.rollback()
            existing = await self.find_by_telegram(
                telegram_account_id=telegram_account_id,
                telegram_chat_id=telegram_chat_id,
                telegram_thread_id=telegram_thread_id,
                telegram_message_id=telegram_message_id,
            )
            if existing is None:
                raise
            return existing
        await self._session.commit()
        await self._session.refresh(row)
        return _to_record(row)

    async def find_by_max(
        self,
        *,
        max_account_id: int,
        max_chat_id: str,
        max_message_id: str,
    ) -> Optional[MappingRecord]:
        stmt = select(MessageMap).where(
            MessageMap.max_account_id == max_account_id,
            MessageMap.max_chat_id == max_chat_id,
            MessageMap.max_message_id == max_message_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_record(row) if row else None

    async def find_by_telegram(
        self,
        *,
        telegram_account_id: int,
        telegram_chat_id: str,
        telegram_thread_id: Optional[str],
        telegram_message_id: str,
    ) -> Optional[MappingRecord]:
        stmt = select(MessageMap).where(
            MessageMap.telegram_account_id == telegram_account_id,
            MessageMap.telegram_chat_id == telegram_chat_id,
            MessageMap.telegram_thread_id.is_(None)
            if telegram_thread_id is None
            else MessageMap.telegram_thread_id == telegram_thread_id,
            MessageMap.telegram_message_id == telegram_message_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_record(row) if row else None

    async def find_by_marker(self, marker: str) -> Optional[MappingRecord]:
        stmt = select(MessageMap).where(MessageMap.outbound_marker == marker)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_record(row) if row else None


__all__ = [
    "MappingRecord",
    "MessageMapRepository",
    "outbound_marker",
]
