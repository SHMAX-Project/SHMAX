"""P1 #4 — cached media survives a retry.

send succeeds → mapping write fails → queue goes to RETRY →
the media file must still exist for the retrying attempt. The file
is removed only when the item is genuinely finalized.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio

from shmax.config import RuntimeMode
from shmax.core.paths import PathLayout
from shmax.database import Database, DatabaseSettings
from shmax.database.migrations import run_migrations_sync
from shmax.domain.enums import DeliveryState, Direction
from shmax.queue.repository import DeliveryQueueRepository
from shmax.runtime import AppRuntime, RuntimeConfig
from shmax.security import SecretStore


@pytest_asyncio.fixture()
async def rt(tmp_path):
    data = tmp_path / "data"
    settings = DatabaseSettings(data_dir=data)
    db = Database(settings)
    run_migrations_sync(settings.url)
    runtime = AppRuntime(
        database=db,
        secret_store=SecretStore(data / "secrets"),
        layout=PathLayout(data),
        config=RuntimeConfig(start_workers=False),
        runtime_mode=RuntimeMode.TEST,
    )
    yield runtime
    await db.dispose()


class _SentOk:
    external_message_id = "tg-777"


@pytest.mark.asyncio
async def test_media_survives_mapping_failure_retry(rt, tmp_path):
    media_file = tmp_path / "cache-media" / "pic.jpg"
    media_file.parent.mkdir(parents=True, exist_ok=True)
    media_file.write_bytes(b"JPEGDATA")
    assert media_file.exists()

    async with rt.db.session() as s:
        q = DeliveryQueueRepository(s)
        rec = await q.enqueue(
            direction=Direction.MAX_TO_TELEGRAM.value,
            target_chat_id="-100999",
            payload={
                "direction": "max_to_telegram",
                "max_account_id": 1,
                "max_chat_id": "max-chat",
                "max_message_id": "mx-1",
                "telegram_account_id": 1,
                "text": "hello",
                "marker": "mk1",
                "media_path": str(media_file),
            },
            target_thread_id=None,
            idempotency_key="k-media-1",
        )
        await s.commit()

    class _BrokenMapping:
        async def create_for_max_to_tg(self, **kwargs):
            raise RuntimeError("mapping db exploded")

    original_session = rt.db.session

    class _SessionProxy:
        """First session (used by _record_mapping) hands out the
        broken repository; later sessions behave normally."""

        def __init__(self, fail: bool):
            self._fail = fail

        async def __aenter__(self):
            self._s = await original_session().__aenter__()
            return self

        async def __aexit__(self, *exc):
            return await self._s.__aexit__(*exc)

        async def execute(self, *a, **k):
            return await self._s.execute(*a, **k)

        async def commit(self):
            await self._s.commit()

        async def rollback(self):
            await self._s.rollback()

    calls = {"n": 0}

    async def _record_mapping(rec_, result_):
        calls["n"] += 1
        # First attempt: mapping write fails AFTER the external send.
        if calls["n"] == 1:
            raise RuntimeError("mapping db exploded")
        return None

    rt._record_mapping = _record_mapping  # type: ignore[method-assign]

    # Attempt 1: external send "succeeds", mapping explodes.
    outcome = None
    try:
        await rt._drain_one(rec)
    except RuntimeError:
        pass  # the worker layer catches; the row must be retried
    assert media_file.exists(), "media deleted despite pending retry"

    # The queue row must NOT be delivered.
    async with rt.db.session() as s:
        row = await DeliveryQueueRepository(s).get(rec.id)
        assert row is not None
        assert row.status in ("pending", "retry", "processing"), (
            f"row status={row.status}; must be retried, not delivered"
        )

    # Attempt 2: mapping succeeds → finalized → file removed.
    async def _record_mapping_ok(rec_, result_):
        return None

    rt._record_mapping = _record_mapping_ok  # type: ignore[method-assign]
    fresh = await (await rt.db.session().__aenter__()).get(
        __import__("shmax.database.models", fromlist=["DeliveryQueueItem"]).DeliveryQueueItem,
        rec.id,
    )
    await rt._drain_one(fresh)
    assert not media_file.exists(), "media must be released after final delivery"


@pytest.mark.asyncio
async def test_media_survives_retry_outcome(rt, tmp_path):
    """An explicit RETRY outcome keeps the file; a FAILED outcome
    finalizes and releases it."""
    media_file = tmp_path / "cache-media2" / "clip.mp4"
    media_file.parent.mkdir(parents=True, exist_ok=True)
    media_file.write_bytes(b"MP4")

    async with rt.db.session() as s:
        q = DeliveryQueueRepository(s)
        rec = await q.enqueue(
            direction=Direction.MAX_TO_TELEGRAM.value,
            target_chat_id="-100999",
            payload={
                "direction": "max_to_telegram",
                "text": "x",
                "marker": "mk2",
                "max_account_id": 1,
                "max_chat_id": "c",
                "max_message_id": "m",
                "telegram_account_id": 1,
                "media_path": str(media_file),
            },
            target_thread_id=None,
            idempotency_key="k-media-2",
        )
        await s.commit()

    class _RetryOutcome:
        state = DeliveryState.RETRY
        error = "transient"

    class _FailedOutcome:
        state = DeliveryState.FAILED
        error = "permanent"

    original = rt._deliver

    async def _deliver_retry(rec_):
        return _RetryOutcome()

    rt._deliver = _deliver_retry  # type: ignore[method-assign]
    await rt._drain_one(rec)
    assert media_file.exists(), "media deleted on RETRY"

    async def _deliver_fail(rec_):
        return _FailedOutcome()

    rt._deliver = _deliver_fail  # type: ignore[method-assign]
    async with rt.db.session() as s:
        row = await DeliveryQueueRepository(s).get(rec.id)
    await rt._drain_one(row)
    assert not media_file.exists(), "media must be released on terminal FAILED"
