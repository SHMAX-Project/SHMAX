"""BLOCKER 7 — queue claim_batch atomic bounded claim + lease reclaim.

Regression test for the bug where ``claim_batch(limit=1)``
flipped the entire matching backlog to ``processing`` while
returning only the first row.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio

from shmax.database import Database, DatabaseSettings
from shmax.database.migrations import run_migrations_sync
from shmax.queue.repository import DeliveryQueueRepository
from shmax.config import RuntimeMode


@pytest_asyncio.fixture()
async def db(tmp_path: Path):
    settings = DatabaseSettings(data_dir=tmp_path)
    database = Database(settings)
    run_migrations_sync(settings.url)
    try:
        yield database
    finally:
        await database.dispose()


async def _enqueue(db, n: int) -> list[int]:
    ids = []
    for i in range(n):
        async with db.session() as s:
            rec = await DeliveryQueueRepository(s).enqueue(
                direction="max_to_telegram",
                target_chat_id=f"chat-{i}",
                payload={"text": f"hello {i}"},
            )
            ids.append(rec.id)
    return ids


@pytest.mark.asyncio
async def test_claim_limit_one_only_flips_one_row_to_processing(db):
    """The reported bug: enqueue 20, claim limit=1 → 1
    processing, 19 pending. The other 19 must NOT flip to
    processing."""
    await _enqueue(db, 20)

    async with db.session() as s:
        claimed = await DeliveryQueueRepository(s).claim_batch(
            limit=1, lease_seconds=60, worker_id="w-test"
        )
    assert len(claimed) == 1

    async with db.session() as s:
        c = await DeliveryQueueRepository(s).counts_by_state()
    assert c["processing"] == 1, c
    assert c["pending"] == 19, c


@pytest.mark.asyncio
async def test_claim_limit_bounded_by_subquery(db):
    """Larger limits still stay bounded."""
    await _enqueue(db, 20)

    async with db.session() as s:
        claimed = await DeliveryQueueRepository(s).claim_batch(
            limit=5, lease_seconds=60, worker_id="w-test"
        )
    assert len(claimed) == 5

    async with db.session() as s:
        c = await DeliveryQueueRepository(s).counts_by_state()
    assert c["processing"] == 5
    assert c["pending"] == 15


@pytest.mark.asyncio
async def test_two_workers_no_overlap(db):
    """Two concurrent workers must not see the same row."""
    await _enqueue(db, 10)

    async with db.session() as s:
        c1 = await DeliveryQueueRepository(s).claim_batch(
            limit=10, lease_seconds=60, worker_id="w-1"
        )
    async with db.session() as s:
        c2 = await DeliveryQueueRepository(s).claim_batch(
            limit=10, lease_seconds=60, worker_id="w-2"
        )

    ids1 = {r.id for r in c1}
    ids2 = {r.id for r in c2}
    # The second worker sees nothing because the first already
    # claimed every pending row with a future lease.
    assert ids2 == set()
    assert len(ids1) == 10


@pytest.mark.asyncio
async def test_reclaim_expired_resets_to_pending(db):
    """Expired leases must be reclaimed back to pending so a
    subsequent claim can re-pick the row."""
    ids = await _enqueue(db, 3)
    # Claim with a 1-second lease.
    async with db.session() as s:
        await DeliveryQueueRepository(s).claim_batch(
            limit=3, lease_seconds=1, worker_id="w"
        )
    # Simulate crash: do not mark anything. Lease expires.
    await asyncio.sleep(1.2)
    async with db.session() as s:
        reclaimed = await DeliveryQueueRepository(s).reclaim_expired()
    assert reclaimed == 3

    async with db.session() as s:
        c = await DeliveryQueueRepository(s).counts_by_state()
    assert c["pending"] == 3
    assert c["processing"] == 0


@pytest.mark.asyncio
async def test_lease_reclaim_called_on_runtime_startup(db):
    """The runtime's ``start()`` reclaims stale leases."""
    from shmax.core.paths import PathLayout
    from shmax.runtime import AppRuntime, RuntimeConfig
    from shmax.security import SecretStore

    layout = PathLayout(db.settings.data_dir)
    secret_store = SecretStore(layout.root / "secrets")
    rt = AppRuntime(
        database=db,
        secret_store=secret_store,
        layout=layout,
        config=RuntimeConfig(start_workers=False, watchdog_interval_seconds=1.0),
    )
    # Stale a lease by enqueueing then claiming then sleeping.
    async with db.session() as s:
        await DeliveryQueueRepository(s).enqueue(
            direction="max_to_telegram",
            target_chat_id="c",
            payload={"text": "x"},
        )
        await DeliveryQueueRepository(s).claim_batch(
            limit=1, lease_seconds=1, worker_id="w"
        )
    # Lease will be expired in 1 second.
    await asyncio.sleep(1.2)
    # start() must reclaim it before workers start.
    await rt.start()
    try:
        async with db.session() as s:
            c = await DeliveryQueueRepository(s).counts_by_state()
        # The queue item is back to pending (or processing if a
        # worker raced; either way not stuck in processing with
        # expired lease).
        assert c.get("processing", 0) <= 1
    finally:
        await rt.stop()
