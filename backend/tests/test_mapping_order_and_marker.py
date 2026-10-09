"""BLOCKER 10 — message mapping order and marker uniqueness.

The runtime MUST:
  * persist the message mapping BEFORE marking the queue
    row delivered;
  * use a single marker UUID generated upstream (in the
    bridge when enqueueing) and travel through the queue
    payload, outbound body, mapping row, and dedup hash;
  * not regenerate the marker inside MessageMapRepository;
  * pass all required account IDs / thread IDs explicitly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from shmax.bridge.message_map import (
    MessageMapRepository,
    outbound_marker,
)
from shmax.database import Database, DatabaseSettings
from shmax.database.migrations import run_migrations_sync
from shmax.config import RuntimeMode


@pytest_asyncio.fixture()
async def db(tmp_path: Path):
    settings = DatabaseSettings(data_dir=tmp_path)
    database = Database(settings)
    run_migrations_sync(settings.url)
    from shmax.database.models import MaxAccount, TelegramAccount
    # Parent rows for MessageMap FK.
    async with database.session() as s:
        s.add(MaxAccount(label="primary", status="connected"))
        s.add(TelegramAccount(label="primary", status="connected"))
        await s.commit()
    try:
        yield database
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_create_for_max_to_tg_uses_passed_marker(db):
    marker = outbound_marker()
    async with db.session() as s:
        rec = await MessageMapRepository(s).create_for_max_to_tg(
            max_account_id=1,
            max_chat_id="max-1",
            max_message_id="m-1",
            telegram_account_id=1,
            telegram_chat_id="tg-1",
            telegram_thread_id=None,
            telegram_message_id="tg-msg-1",
            outbound_marker_value=marker,
        )
    assert rec.outbound_marker == marker


@pytest.mark.asyncio
async def test_create_for_tg_to_max_uses_passed_marker(db):
    marker = outbound_marker()
    async with db.session() as s:
        rec = await MessageMapRepository(s).create_for_tg_to_max(
            telegram_account_id=1,
            telegram_chat_id="tg-1",
            telegram_thread_id=None,
            telegram_message_id="tg-msg-1",
            max_account_id=1,
            max_chat_id="max-1",
            max_message_id="m-1",
            outbound_marker_value=marker,
        )
    assert rec.outbound_marker == marker


@pytest.mark.asyncio
async def test_same_marker_idempotent(db):
    """Re-inserting with the same marker must not raise; the
    repository returns the existing row."""
    marker = outbound_marker()
    async with db.session() as s:
        await MessageMapRepository(s).create_for_max_to_tg(
            max_account_id=1,
            max_chat_id="max-2",
            max_message_id="m-2",
            telegram_account_id=1,
            telegram_chat_id="tg-2",
            telegram_thread_id=None,
            telegram_message_id="tg-msg-2",
            outbound_marker_value=marker,
        )
    async with db.session() as s:
        rec = await MessageMapRepository(s).create_for_max_to_tg(
            max_account_id=1,
            max_chat_id="max-2",
            max_message_id="m-2",
            telegram_account_id=1,
            telegram_chat_id="tg-2",
            telegram_thread_id=None,
            telegram_message_id="tg-msg-2",
            outbound_marker_value=marker,
        )
    assert rec.outbound_marker == marker


@pytest.mark.asyncio
async def test_create_requires_explicit_marker(db):
    """The repository refuses to invent a marker."""
    import asyncio
    with pytest.raises((KeyError, TypeError)):
        async with db.session() as s:
            await MessageMapRepository(s).create_for_max_to_tg(
                max_account_id=1,
                max_chat_id="x",
                max_message_id="y",
                telegram_account_id=1,
                telegram_chat_id="z",
                telegram_thread_id=None,
                telegram_message_id="w",
                # no outbound_marker_value
            )


@pytest.mark.asyncio
async def test_runtime_records_mapping_before_marking_delivered(db, tmp_path):
    """The runtime's _drain_one persists mapping BEFORE
    updating the queue row. We assert the temporal order by
    monkey-patching the two operations and verifying which
    ran first."""
    from shmax.runtime import AppRuntime, RuntimeConfig
    from shmax.core.paths import PathLayout
    from shmax.security import SecretStore

    layout = PathLayout(db.settings.data_dir)
    secret_store = SecretStore(layout.root / "secrets")
    rt = AppRuntime(
        database=db,
        secret_store=secret_store,
        layout=layout,
        config=RuntimeConfig(start_workers=False, watchdog_interval_seconds=1.0),
        runtime_mode=RuntimeMode.TEST,
    )

    call_log: list[str] = []

    async def fake_record_mapping(rec, result):
        call_log.append("mapping")

    async def fake_mark_delivered(queue_id):
        call_log.append("delivered")

    rt._record_mapping = fake_record_mapping  # type: ignore[assignment]

    # Patch DeliveryQueueRepository.mark_delivered on a session
    # is awkward; instead, drive a fake DeliveryOutcome through
    # _drain_one and observe ordering.
    from shmax.domain.enums import DeliveryState
    from shmax.domain.models import DeliveryOutcome

    class _Rec:
        id = 1
        attempts = 0
        direction = "max_to_telegram"
        target_chat_id = "tg-1"
        target_thread_id = None
        payload = {
            "marker": "abc",
            "max_account_id": 1,
            "max_chat_id": "max-1",
            "max_message_id": "m-1",
            "telegram_account_id": 1,
        }

    class _Result:
        external_message_id = "tg-msg-1"

    # Monkey-patch mark_delivered to log.
    from shmax.queue.repository import DeliveryQueueRepository
    orig_mark_delivered = DeliveryQueueRepository.mark_delivered

    async def patched(self, queue_id):
        call_log.append("delivered")
        await orig_mark_delivered(self, queue_id=queue_id)

    DeliveryQueueRepository.mark_delivered = patched
    try:
        # _drain_one reads the delivery result by calling
        # ``self._deliver(rec)``; intercept that to inject ours.
        async def fake_deliver(rec):
            return _Result()
        rt._deliver = fake_deliver  # type: ignore[assignment]
        await rt._drain_one(_Rec())
    finally:
        DeliveryQueueRepository.mark_delivered = orig_mark_delivered

    # Mapping must precede delivered.
    assert call_log == ["mapping", "delivered"], call_log
