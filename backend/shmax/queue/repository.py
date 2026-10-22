"""Persistent delivery queue repository.

Direct DB-backed implementation: enqueue / claim / update /
dead-letter. Worker scheduling is in :mod:`shmax.queue.worker`.

Atomicity contract:

* ``claim_batch`` uses a single ``UPDATE`` guarded by status
  and lease state, then reads the rows it just claimed by
  their new lease marker. Two concurrent workers cannot
  claim the same row.
* ``lease_until`` is set per claim; an expired lease is
  re-taken automatically on the next claim pass.
* ``enqueue`` honours ``idempotency_key``: a duplicate insert
  raises :class:`DuplicateEnqueueError` instead of creating
  a second row.
"""

from __future__ import annotations

import datetime as _dt
import os
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import select, update, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import select as sa_select

from ..database.models import DeliveryQueueItem


@dataclass(frozen=True)
class QueueRecord:
    id: int
    direction: str
    target_chat_id: str
    target_thread_id: Optional[str]
    payload: dict[str, Any]
    status: str
    attempts: int
    next_attempt_at: _dt.datetime
    last_error: Optional[str]
    created_at: _dt.datetime
    updated_at: _dt.datetime
    claimed_at: Optional[_dt.datetime] = None
    lease_until: Optional[_dt.datetime] = None
    worker_id: Optional[str] = None
    idempotency_key: Optional[str] = None


def _to_record(row: DeliveryQueueItem) -> QueueRecord:
    return QueueRecord(
        id=row.id,
        direction=row.direction,
        target_chat_id=row.target_chat_id,
        target_thread_id=row.target_thread_id,
        payload=row.payload,
        status=row.status,
        attempts=row.attempts,
        next_attempt_at=row.next_attempt_at,
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        claimed_at=row.claimed_at,
        lease_until=row.lease_until,
        worker_id=row.worker_id,
        idempotency_key=row.idempotency_key,
    )


class LeaseOwnershipLostError(RuntimeError):
    """A previous worker must not ACK/retry another worker's lease."""


class DuplicateEnqueueError(Exception):
    """Raised when an enqueue collides with an existing
    ``idempotency_key`` UNIQUE constraint."""


def _worker_marker() -> str:
    return f"w-{os.getpid()}-{id(object())}"


class DeliveryQueueRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(
        self,
        *,
        direction: str,
        target_chat_id: str,
        payload: dict[str, Any],
        target_thread_id: Optional[str] = None,
        reply_mapping_id: Optional[int] = None,
        available_at: Optional[_dt.datetime] = None,
        idempotency_key: Optional[str] = None,
        depends_on_id: Optional[int] = None,
        commit: bool = True,
    ) -> QueueRecord:
        row = DeliveryQueueItem(
            direction=direction,
            target_chat_id=target_chat_id,
            target_thread_id=target_thread_id,
            payload=payload,
            reply_mapping_id=reply_mapping_id,
            status="pending",
            attempts=0,
            next_attempt_at=available_at
            or _dt.datetime.now(_dt.timezone.utc),
            idempotency_key=idempotency_key,
            depends_on_id=depends_on_id,
        )
        self._session.add(row)
        try:
            if commit:
                await self._session.commit()
            else:
                await self._session.flush()
        except IntegrityError as e:
            await self._session.rollback()
            raise DuplicateEnqueueError(
                f"idempotency_key={idempotency_key!r} already enqueued"
            ) from e
        await self._session.refresh(row)
        return _to_record(row)

    async def claim_batch(
        self,
        *,
        limit: int = 16,
        lease_seconds: float = 60.0,
        worker_id: Optional[str] = None,
    ) -> list[QueueRecord]:
        """Atomically mark up to ``limit`` ready items as ``processing``.

        Atomicity: the ``UPDATE ... WHERE id IN (SELECT ... LIMIT
        N)`` subquery bounds the number of rows that transition
        to ``processing`` in one call. Without the LIMIT, every
        eligible row would flip to ``processing`` and stay
        there until reclaim — a real bug that starved the
        queue.

        SQLite and PostgreSQL both execute the SELECT subquery
        atomically before the UPDATE, so two concurrent workers
        cannot claim the same row.
        """
        if limit <= 0:
            return []
        now = _dt.datetime.now(_dt.timezone.utc)
        lease_until = now + _dt.timedelta(seconds=lease_seconds)
        marker_worker = worker_id or _worker_marker()
        from sqlalchemy.orm import aliased
        parent = aliased(DeliveryQueueItem)
        delivered_parents = sa_select(parent.id).where(parent.status == "delivered")
        eligible_ids_subquery = (
            sa_select(DeliveryQueueItem.id)
            .where(
                DeliveryQueueItem.status.in_(["pending", "retry"]),
                DeliveryQueueItem.next_attempt_at <= now,
                DeliveryQueueItem.depends_on_id.is_(None) | DeliveryQueueItem.depends_on_id.in_(delivered_parents),
                # Either never claimed, or lease has expired.
                (DeliveryQueueItem.lease_until.is_(None))
                | (DeliveryQueueItem.lease_until < now),
            )
            .order_by(DeliveryQueueItem.id.asc())
            .limit(limit)
        )
        stmt = (
            update(DeliveryQueueItem)
            .where(DeliveryQueueItem.id.in_(eligible_ids_subquery))
            .values(
                status="processing",
                claimed_at=now,
                lease_until=lease_until,
                worker_id=marker_worker,
            )
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(stmt)
        # rowcount is the number of rows the UPDATE changed; it
        # is bounded by ``limit`` because of the subquery.
        if (result.rowcount or 0) == 0:
            await self._session.commit()
            return []
        select_stmt = (
            select(DeliveryQueueItem)
            .where(
                DeliveryQueueItem.worker_id == marker_worker,
                DeliveryQueueItem.lease_until == lease_until,
                DeliveryQueueItem.status == "processing",
            )
            .order_by(DeliveryQueueItem.id.asc())
            .limit(limit)
        )
        rows = list((await self._session.execute(select_stmt)).scalars().all())
        await self._session.commit()
        return [_to_record(r) for r in rows]

    async def renew_lease(self, *, queue_id, worker_id, lease_seconds=120):
        result = await self._session.execute(update(DeliveryQueueItem).where(
            DeliveryQueueItem.id == queue_id, DeliveryQueueItem.status == "processing",
            DeliveryQueueItem.worker_id == worker_id,
        ).values(lease_until=_dt.datetime.now(_dt.timezone.utc)+_dt.timedelta(seconds=lease_seconds)))
        if result.rowcount != 1:
            await self._session.rollback()
            raise LeaseOwnershipLostError("delivery lease ownership lost")
        await self._session.commit()

    async def checkpoint(self, *, queue_id, worker_id, payload, lease_seconds=120):
        result = await self._session.execute(update(DeliveryQueueItem).where(
            DeliveryQueueItem.id == queue_id, DeliveryQueueItem.status == "processing",
            DeliveryQueueItem.worker_id == worker_id,
        ).values(payload=payload, lease_until=_dt.datetime.now(_dt.timezone.utc)+_dt.timedelta(seconds=lease_seconds)))
        if result.rowcount != 1:
            await self._session.rollback()
            raise LeaseOwnershipLostError("delivery lease ownership lost")
        await self._session.commit()

    async def reclaim_expired(self) -> int:
        """Move expired-processing items back to ``pending``.

        Called periodically by the supervisor so a crashed
        worker's leases do not block the queue.
        """
        now = _dt.datetime.now(_dt.timezone.utc)
        stmt = (
            update(DeliveryQueueItem)
            .where(
                DeliveryQueueItem.status == "processing",
                DeliveryQueueItem.lease_until.is_not(None),
                DeliveryQueueItem.lease_until < now,
            )
            .values(
                status="pending",
                lease_until=None,
                worker_id=None,
                claimed_at=None,
                next_attempt_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(stmt)
        await self._session.commit()
        return result.rowcount or 0

    async def mark_delivered(self, *, queue_id: int) -> None:
        await self._session.execute(
            update(DeliveryQueueItem)
            .where(DeliveryQueueItem.id == queue_id)
            .values(
                status="delivered",
                last_error=None,
                lease_until=None,
                worker_id=None,
                claimed_at=None,
            )
        )
        await self._session.commit()

    async def mark_retry(
        self,
        *,
        queue_id: int,
        last_error: str,
        next_attempt_at: _dt.datetime,
    ) -> None:
        await self._session.execute(
            update(DeliveryQueueItem)
            .where(DeliveryQueueItem.id == queue_id)
            .values(
                status="retry",
                last_error=last_error,
                next_attempt_at=next_attempt_at,
                attempts=DeliveryQueueItem.attempts + 1,
                lease_until=None,
                worker_id=None,
                claimed_at=None,
            )
        )
        await self._session.commit()

    async def mark_failed(self, *, queue_id: int, last_error: str) -> None:
        await self._session.execute(
            update(DeliveryQueueItem)
            .where(DeliveryQueueItem.id == queue_id)
            .values(
                status="failed",
                last_error=last_error,
                lease_until=None,
                worker_id=None,
                claimed_at=None,
            )
        )
        await self._session.execute(update(DeliveryQueueItem).where(
            DeliveryQueueItem.depends_on_id == queue_id,
            DeliveryQueueItem.status.in_(["pending", "retry"])
        ).values(status="failed", last_error="DependencyFailed", lease_until=None))
        await self._session.commit()

    async def reset_for_retry(self, *, queue_id: int) -> bool:
        """Manually move a failed item back to ``pending``.

        Operator-facing action surfaced through the Web UI as
        "Повторить". Starts a fresh retry budget while preserving the
        same durable item and its idempotency key.
        """
        row = await self._session.get(DeliveryQueueItem, queue_id)
        if row is None or row.status not in {'failed','retry'}:
            return False
        if row.depends_on_id is not None:
            parent=await self._session.get(DeliveryQueueItem,row.depends_on_id)
            if parent is None or parent.status=='failed':return False
        row.status = "pending"
        row.attempts = 0
        row.last_error = None
        row.next_attempt_at = _dt.datetime.now(_dt.timezone.utc)
        await self._session.execute(update(DeliveryQueueItem).where(
            DeliveryQueueItem.depends_on_id == queue_id,
            DeliveryQueueItem.status == 'failed', DeliveryQueueItem.last_error == 'DependencyFailed'
        ).values(status='pending', attempts=0, last_error=None, next_attempt_at=row.next_attempt_at))
        await self._session.commit()
        return True

    async def purge(self, *, queue_id:int) -> bool:
        row=await self._session.get(DeliveryQueueItem,queue_id)
        if row is None or row.status=='processing':return False
        children=update(DeliveryQueueItem).where(DeliveryQueueItem.depends_on_id==queue_id)
        if row.status=='delivered':
            # Their confirmed MAX mapping remains durable after queue cleanup.
            await self._session.execute(children.values(depends_on_id=None))
        else:
            await self._session.execute(children.where(DeliveryQueueItem.status.in_(['pending','retry']))
                .values(status='failed',last_error='DependencyFailed'))
        await self._session.execute(delete(DeliveryQueueItem).where(DeliveryQueueItem.id==queue_id))
        await self._session.commit()
        return True

    async def counts_by_state(self) -> dict[str, int]:
        rows = (
            await self._session.execute(
                select(DeliveryQueueItem.status, DeliveryQueueItem.id)
            )
        ).all()
        out: dict[str, int] = {"pending": 0, "processing": 0, "retry": 0, "delivered": 0, "failed": 0}
        for status, _id in rows:
            out[status] = out.get(status, 0) + 1
        return out

    async def get(self, queue_id: int) -> Optional[QueueRecord]:
        row = await self._session.get(DeliveryQueueItem, queue_id)
        return _to_record(row) if row else None


__all__ = [
    "DeliveryQueueRepository",
    "DuplicateEnqueueError",
    "QueueRecord",
]
