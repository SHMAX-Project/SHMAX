"""Registry of our own outbound native message ids (live A1).

Echo suppression WITHOUT visible markers: every native message id
produced by SHMAX's own sends (media, follow-up text, fallback)
is registered here at delivery time. An inbound event whose
(transport, chat, message id) matches a registry row is OUR OWN
message coming back — it is dropped before any bridging.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database.models import SentNative


class SentNativeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def register(
        self,
        *,
        transport: str,
        chat_external_id: str,
        message_external_id: str,
        marker: Optional[str] = None,
    ) -> None:
        """Idempotent insert (a retried delivery may re-register)."""
        row = SentNative(
            transport=transport,
            chat_external_id=str(chat_external_id),
            message_external_id=str(message_external_id),
            marker=marker,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except Exception:  # noqa: BLE001 — already registered
            await self._session.rollback()
        else:
            await self._session.commit()

    async def exists(
        self, *, transport: str, chat_external_id: str, message_external_id: str
    ) -> bool:
        stmt = select(SentNative.id).where(
            SentNative.transport == transport,
            SentNative.chat_external_id == str(chat_external_id),
            SentNative.message_external_id == str(message_external_id),
        )
        return (
            await self._session.execute(stmt)
        ).scalar_one_or_none() is not None


__all__ = ["SentNativeRepository"]
