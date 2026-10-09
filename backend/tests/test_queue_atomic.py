"""Regression tests for queue atomic claim, lease, idempotency."""

from __future__ import annotations

import asyncio
import datetime as _dt

import pytest

from shmax.queue.repository import (
    DeliveryQueueRepository,
    DuplicateEnqueueError,
)


@pytest.mark.asyncio
async def test_enqueue_rejects_duplicate_idempotency_key(db_session):
    repo = DeliveryQueueRepository(db_session)
    rec = await repo.enqueue(
        direction="max_to_telegram",
        target_chat_id="tg",
        payload={"text": "hello"},
        idempotency_key="k-1",
    )
    assert rec.idempotency_key == "k-1"
    with pytest.raises(DuplicateEnqueueError):
        await repo.enqueue(
            direction="max_to_telegram",
            target_chat_id="tg",
            payload={"text": "world"},
            idempotency_key="k-1",
        )


@pytest.mark.asyncio
async def test_atomic_claim_only_one_worker_gets_each_row(db_session):
    """Two concurrent claim_batch calls must not return the same row."""
    repo = DeliveryQueueRepository(db_session)
    for i in range(5):
        await repo.enqueue(
            direction="max_to_telegram",
            target_chat_id=f"tg-{i}",
            payload={"text": f"msg-{i}"},
            idempotency_key=f"idem-{i}",
        )

    # Two workers race.
    async def worker(worker_id: str):
        async with db_session.bind.connect() as _:
            pass
        # The fixture already gives us a session; create two
        # repos backed by independent connections via two
        # separate session objects created from the same
        # engine.
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(db_session.bind, expire_on_commit=False) as s:
            r = DeliveryQueueRepository(s)
            return await r.claim_batch(limit=10, lease_seconds=10, worker_id=worker_id)

    # Use the existing fixture session; simulate concurrency
    # by interleaving at the coroutine level.
    async def run():
        return await asyncio.gather(
            worker("A"),
            worker("B"),
        )

    a_records, b_records = await run()
    all_ids = sorted([r.id for r in a_records] + [r.id for r in b_records])
    # 5 unique ids, no duplicates between workers.
    assert len(all_ids) == 5
    assert len(set(all_ids)) == 5
    for rec in a_records + b_records:
        assert rec.status == "processing"
        assert rec.worker_id in ("A", "B")
        assert rec.lease_until is not None


@pytest.mark.asyncio
async def test_lease_expiry_releases_row_for_reclaim(db_session):
    repo = DeliveryQueueRepository(db_session)
    await repo.enqueue(
        direction="max_to_telegram",
        target_chat_id="tg-x",
        payload={"text": "hi"},
    )
    claimed = await repo.claim_batch(limit=1, lease_seconds=0.0)
    assert len(claimed) == 1
    # Manually rewind the lease so the next reclaim sees it
    # as expired.
    from shmax.database.models import DeliveryQueueItem
    from sqlalchemy import update

    await db_session.execute(
        update(DeliveryQueueItem)
        .where(DeliveryQueueItem.id == claimed[0].id)
        .values(lease_until=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=1))
    )
    await db_session.commit()
    # reclaim_expired moves processing -> pending.
    moved = await repo.reclaim_expired()
    assert moved == 1
    next_claim = await repo.claim_batch(limit=1, lease_seconds=10)
    assert len(next_claim) == 1
    assert next_claim[0].id == claimed[0].id


@pytest.mark.asyncio
async def test_reclaim_expired_noop_when_nothing_expired(db_session):
    repo = DeliveryQueueRepository(db_session)
    assert await repo.reclaim_expired() == 0


@pytest.mark.asyncio
async def test_reset_for_retry_only_on_failed(db_session):
    repo = DeliveryQueueRepository(db_session)
    rec = await repo.enqueue(
        direction="max_to_telegram",
        target_chat_id="tg-z",
        payload={"text": "x"},
    )
    # Cannot reset a pending item.
    assert (await repo.reset_for_retry(queue_id=rec.id)) is False
    # Mark failed then reset.
    await repo.mark_failed(queue_id=rec.id, last_error="x")
    assert (await repo.reset_for_retry(queue_id=rec.id)) is True
    fresh = await repo.get(rec.id)
    assert fresh is not None
    assert fresh.status == "pending"
