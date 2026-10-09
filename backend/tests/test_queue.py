"""Persistent queue tests."""

from __future__ import annotations

import asyncio
import datetime as _dt

import pytest

from shmax.queue.repository import DeliveryQueueRepository


@pytest.mark.asyncio
async def test_enqueue_and_claim(db_session):
    repo = DeliveryQueueRepository(db_session)
    rec = await repo.enqueue(
        direction="max_to_telegram",
        target_chat_id="t1",
        payload={"text": "hi"},
    )
    assert rec.status == "pending"
    batch = await repo.claim_batch(limit=4)
    assert len(batch) == 1
    assert batch[0].id == rec.id
    assert batch[0].status == "processing"


@pytest.mark.asyncio
async def test_claim_respects_next_attempt_at(db_session):
    from shmax.database.models import DeliveryQueueItem

    repo = DeliveryQueueRepository(db_session)
    await repo.enqueue(
        direction="max_to_telegram",
        target_chat_id="t1",
        payload={},
        available_at=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=1),
    )
    batch = await repo.claim_batch()
    assert batch == []


@pytest.mark.asyncio
async def test_retry_then_fail(db_session):
    repo = DeliveryQueueRepository(db_session)
    rec = await repo.enqueue(direction="telegram_to_max", target_chat_id="t1", payload={})
    await repo.claim_batch()
    await repo.mark_retry(
        queue_id=rec.id,
        last_error="transient",
        next_attempt_at=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=5),
    )
    after = await repo.get(rec.id)
    assert after.status == "retry"
    await repo.mark_failed(queue_id=rec.id, last_error="exhausted")
    after = await repo.get(rec.id)
    assert after.status == "failed"


@pytest.mark.asyncio
async def test_queue_survives_db_reopen(data_dir):
    """Restart test: write queue item, close engine, reopen, item remains."""
    from shmax.database import Database, DatabaseSettings, create_all

    settings = DatabaseSettings(data_dir=data_dir)
    db1 = Database(settings)
    await create_all(db1.engine)
    async with db1.session() as s:
        repo = DeliveryQueueRepository(s)
        await repo.enqueue(direction="max_to_telegram", target_chat_id="t1", payload={"text": "x"})
    await db1.dispose()

    db2 = Database(settings)
    try:
        async with db2.session() as s:
            repo2 = DeliveryQueueRepository(s)
            batch = await repo2.claim_batch()
            assert len(batch) == 1
            assert batch[0].payload["text"] == "x"
    finally:
        await db2.dispose()


@pytest.mark.asyncio
async def test_worker_marks_delivered_on_success(db_session):
    from shmax.queue.worker import DeliveryWorker

    repo = DeliveryQueueRepository(db_session)
    await repo.enqueue(direction="max_to_telegram", target_chat_id="t1", payload={"k": 1})

    async def _ok(item):
        return True

    worker = DeliveryWorker(repo, handler=_ok)
    worked = await worker.process_one()
    assert worked is True
    counts = await repo.counts_by_state()
    assert counts["delivered"] == 1


@pytest.mark.asyncio
async def test_worker_retries_then_fails(db, db_session):
    from sqlalchemy import update
    from shmax.database.models import DeliveryQueueItem
    from shmax.queue.worker import DeliveryWorker, RetryPolicy

    repo = DeliveryQueueRepository(db_session)
    await repo.enqueue(direction="max_to_telegram", target_chat_id="t1", payload={})

    async def _fail(item):
        return False

    worker = DeliveryWorker(
        repo,
        handler=_fail,
        policy=RetryPolicy(base_seconds=0.01, cap_seconds=0.05, max_attempts=2),
    )
    # First pass: handler returns False -> status=retry, attempts=1.
    await worker.process_one()
    counts = await repo.counts_by_state()
    assert counts["retry"] == 1
    # Bump next_attempt_at to the past so the retry is claimable.
    async with db.engine.begin() as conn:
        await conn.execute(
            update(DeliveryQueueItem).values(
                next_attempt_at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=1)
            )
        )
    # Second pass: attempts=2 -> max_attempts reached -> status=failed.
    await worker.process_one()
    counts = await repo.counts_by_state()
    assert counts["failed"] == 1
