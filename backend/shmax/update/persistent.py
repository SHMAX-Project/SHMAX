"""Persistent update transactions.

The state machine in :mod:`shmax.update.state_machine` is pure;
this module persists the FSM state to the ``update_transactions``
table so an interrupted update can be inspected (or rolled back)
after the web container restarts.

Lifecycle:

* :meth:`start` writes the initial row (CHECK stage)
* :meth:`advance` moves the row through the stages
* :meth:`fail` writes the error and the FAILED state
* :meth:`complete` writes DONE
* :meth:`latest` returns the most recent transaction
"""

from __future__ import annotations

import datetime as _dt
import secrets
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database.models import UpdateTransaction
from .state_machine import UpdateStage


@dataclass(frozen=True)
class UpdateTransactionRecord:
    transaction_id: str
    from_version: str
    to_version: str
    state: str
    backup_id: Optional[str]
    old_schema: Optional[str]
    new_schema: Optional[str]
    started_at: int
    updated_at: int
    error: Optional[str]


def _utc_now() -> int:
    return int(_dt.datetime.now(_dt.timezone.utc).timestamp())


class UpdateTransactionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def start(
        self,
        *,
        from_version: str,
        to_version: str,
        old_schema: str = "",
    ) -> UpdateTransactionRecord:
        txn_id = secrets.token_hex(16)
        now = _utc_now()
        row = UpdateTransaction(
            transaction_id=txn_id,
            from_version=from_version,
            to_version=to_version,
            state=UpdateStage.CHECK.value,
            backup_id=None,
            old_schema=old_schema or None,
            new_schema=None,
            started_at=now,
            updated_at=now,
            error=None,
        )
        self._session.add(row)
        await self._session.commit()
        return _to_record(row)

    async def advance(
        self,
        transaction_id: str,
        *,
        new_state: UpdateStage,
        backup_id: Optional[str] = None,
        new_schema: Optional[str] = None,
    ) -> UpdateTransactionRecord:
        row = await self._find(transaction_id)
        row.state = new_state.value
        if backup_id is not None:
            row.backup_id = backup_id
        if new_schema is not None:
            row.new_schema = new_schema
        row.updated_at = _utc_now()
        await self._session.commit()
        return _to_record(row)

    async def fail(
        self, transaction_id: str, *, error: str
    ) -> UpdateTransactionRecord:
        row = await self._find(transaction_id)
        row.state = UpdateStage.FAILED.value
        row.error = error[:4096]
        row.updated_at = _utc_now()
        await self._session.commit()
        return _to_record(row)

    async def complete(self, transaction_id: str) -> UpdateTransactionRecord:
        row = await self._find(transaction_id)
        row.state = UpdateStage.DONE.value
        row.updated_at = _utc_now()
        await self._session.commit()
        return _to_record(row)

    async def latest(self) -> Optional[UpdateTransactionRecord]:
        stmt = (
            sa_select(UpdateTransaction)
            .order_by(UpdateTransaction.started_at.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _to_record(row) if row is not None else None

    async def get(
        self, transaction_id: str
    ) -> Optional[UpdateTransactionRecord]:
        row = await self._find_or_none(transaction_id)
        return _to_record(row) if row is not None else None

    async def _find(self, transaction_id: str) -> UpdateTransaction:
        row = await self._find_or_none(transaction_id)
        if row is None:
            raise KeyError(f"unknown update transaction: {transaction_id}")
        return row

    async def _find_or_none(self, transaction_id: str) -> Optional[UpdateTransaction]:
        stmt = sa_select(UpdateTransaction).where(
            UpdateTransaction.transaction_id == transaction_id
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()


def _to_record(row: UpdateTransaction) -> UpdateTransactionRecord:
    return UpdateTransactionRecord(
        transaction_id=row.transaction_id,
        from_version=row.from_version,
        to_version=row.to_version,
        state=row.state,
        backup_id=row.backup_id,
        old_schema=row.old_schema,
        new_schema=row.new_schema,
        started_at=row.started_at,
        updated_at=row.updated_at,
        error=row.error,
    )


__all__ = [
    "UpdateTransactionRecord",
    "UpdateTransactionRepository",
]