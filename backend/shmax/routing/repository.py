"""Routes repository: persistent MAX chat ↔ Telegram topic mapping."""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database.models import Route


@dataclass(frozen=True)
class RouteRecord:
    id: int
    max_account_id: int
    max_chat_id: str
    telegram_account_id: int
    telegram_chat_id: str
    telegram_thread_id: Optional[str]
    enabled: bool
    label: Optional[str]
    created_at: _dt.datetime
    updated_at: _dt.datetime


def _to_record(row: Route) -> RouteRecord:
    return RouteRecord(
        id=row.id,
        max_account_id=row.max_account_id,
        max_chat_id=row.max_chat_id,
        telegram_account_id=row.telegram_account_id,
        telegram_chat_id=row.telegram_chat_id,
        telegram_thread_id=row.telegram_thread_id,
        enabled=row.enabled,
        label=row.label,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class RoutesRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self,
        *,
        max_account_id: int,
        max_chat_id: str,
        telegram_account_id: int,
        telegram_chat_id: str,
        telegram_thread_id: Optional[str] = None,
        label: Optional[str] = None,
        enabled: bool = True,
    ) -> RouteRecord:
        stmt = select(Route).where(
            Route.max_account_id == max_account_id,
            Route.max_chat_id == max_chat_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            row = Route(
                max_account_id=max_account_id,
                max_chat_id=max_chat_id,
                telegram_account_id=telegram_account_id,
                telegram_chat_id=telegram_chat_id,
                telegram_thread_id=telegram_thread_id,
                label=label,
                enabled=enabled,
            )
            self._session.add(row)
        else:
            row.telegram_account_id = telegram_account_id
            row.telegram_chat_id = telegram_chat_id
            row.telegram_thread_id = telegram_thread_id
            row.label = label
            row.enabled = enabled
        await self._session.commit()
        await self._session.refresh(row)
        return _to_record(row)

    async def find_by_max(self, *, max_account_id: int, max_chat_id: str) -> Optional[RouteRecord]:
        stmt = select(Route).where(
            Route.max_account_id == max_account_id,
            Route.max_chat_id == max_chat_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_record(row) if row else None

    async def find_by_telegram(
        self,
        *,
        telegram_account_id: int,
        telegram_chat_id: str,
        telegram_thread_id: Optional[str] = None,
    ) -> Optional[RouteRecord]:
        stmt = select(Route).where(
            Route.telegram_account_id == telegram_account_id,
            Route.telegram_chat_id == telegram_chat_id,
            Route.telegram_thread_id.is_(None)
            if telegram_thread_id is None
            else Route.telegram_thread_id == telegram_thread_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_record(row) if row else None

    async def list_all(self, *, only_enabled: bool = False) -> list[RouteRecord]:
        stmt = select(Route)
        if only_enabled:
            stmt = stmt.where(Route.enabled.is_(True))
        stmt = stmt.order_by(Route.created_at.asc())
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_record(r) for r in rows]

    async def find_system_route(self) -> Optional[dict]:
        """Return the registered system route as a dict, or None.

        The system route is a one-way route used by SHMAX to
        publish watchdog/backup/update notifications to the
        operator's chosen Telegram topic. Exactly one row is
        expected to have ``label='system'``.
        """
        stmt = select(Route).where(Route.label == "system").limit(1)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        return {
            "id": row.id,
            "telegram_chat_id": row.telegram_chat_id,
            "telegram_thread_id": row.telegram_thread_id,
        }

    async def set_enabled(self, *, route_id: int, enabled: bool) -> Optional[RouteRecord]:
        row = await self._session.get(Route, route_id)
        if row is None:
            return None
        row.enabled = enabled
        await self._session.commit()
        await self._session.refresh(row)
        return _to_record(row)

    async def delete(self, *, route_id: int) -> bool:
        row = await self._session.get(Route, route_id)
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.commit()
        return True


__all__ = ["RouteRecord", "RoutesRepository"]
