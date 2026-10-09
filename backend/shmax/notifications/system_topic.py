"""System topic service.

The system topic is the Telegram forum topic that SHMAX uses
to deliver watchdog events, backup status, update progress,
and admin-required warnings.

When the operator's Telegram account is paired with SHMAX the
service ensures the topic exists exactly once and stores the
mapping under ``routes.telegram_chat_id``/``telegram_thread_id``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters.telegram.base import TelegramAdapter
from ..database.models import Route


logger = logging.getLogger(__name__)


SYSTEM_TOPIC_TITLE = "🔧 SHMAX"


@dataclass(frozen=True)
class SystemTopicRecord:
    chat_id: str
    thread_id: Optional[str]


class SystemTopicService:
    """Ensure-system-topic + lookup helper."""

    def __init__(self, session: AsyncSession, telegram_account_id: int) -> None:
        self._session = session
        self._telegram_account_id = telegram_account_id

    async def ensure(
        self,
        *,
        telegram: TelegramAdapter,
        chat_id: str,
    ) -> SystemTopicRecord:
        """Create the topic if missing; return the mapping either way."""
        existing = await self._find(chat_id)
        if existing is not None:
            return existing
        thread_id = await telegram.create_topic(chat_id=chat_id, title=SYSTEM_TOPIC_TITLE)
        route = Route(
            max_account_id=0,  # system route is one-way; no MAX side
            max_chat_id=f"system:{chat_id}:{thread_id}",
            telegram_account_id=self._telegram_account_id,
            telegram_chat_id=chat_id,
            telegram_thread_id=str(thread_id),
            label="system",
            enabled=True,
        )
        self._session.add(route)
        await self._session.commit()
        return SystemTopicRecord(chat_id=chat_id, thread_id=str(thread_id))

    async def _find(self, chat_id: str) -> Optional[SystemTopicRecord]:
        stmt = select(Route).where(
            Route.telegram_account_id == self._telegram_account_id,
            Route.telegram_chat_id == chat_id,
            Route.label == "system",
        )
        row = (await self._session.execute(stmt)).scalars().first()
        if row is None:
            return None
        return SystemTopicRecord(chat_id=chat_id, thread_id=row.telegram_thread_id)


__all__ = ["SystemTopicRecord", "SystemTopicService", "SYSTEM_TOPIC_TITLE"]
