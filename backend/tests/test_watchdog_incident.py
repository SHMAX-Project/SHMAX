"""Watchdog incident + outbox flow tests.

Covers the exact down -> reminder -> recovery contract::

  HEALTHY
    \\ failure 1
     DEGRADED
      \\ failure threshold + self-heal fail
       DOWN
        \\ INITIAL DOWN alert exactly once
         \\ +59 min: no reminder
          \\ +60 min: one reminder
           \\ +60 min: one more reminder
            \\ recovery
             \\ RECOVERY alert exactly once
              \\ future: no reminder

A long period of downtime (5h) must coalesce in the outbox so
the recipient does not get five reminders in a burst. Restarting
the watchdog must preserve the incident state.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from shmax.adapters.max.fake import FakeMaxAdapter
from shmax.notifications.outbox import NotificationOutbox
from shmax.notifications.outbox_dispatcher import OutboxBackedDispatcher
from shmax.watchdog.base import (
    AdapterHealthState,
    LoggingNotificationDispatcher,
    WatchdogConfig,
    WatchdogStateStore,
)
from shmax.watchdog.max_watchdog import MaxWatchdog


def _clock(start: int):
    state = {"now": start}
    def get() -> int:
        return state["now"]
    def advance(seconds: float) -> None:
        state["now"] += int(seconds)
    return get, advance


async def _drain_all(outbox: NotificationOutbox):
    """Drain every ready row through the inner dispatcher.

    Returns the list of events the inner dispatcher received.
    """
    received: list[dict] = []

    class _Recorder:
        async def send(self, *, event_type, chat_id, thread_id, payload):
            received.append(
                {"event_type": event_type, "chat_id": chat_id, "payload": payload}
            )

    recorder = _Recorder()
    while True:
        delivered = await outbox.drain_pending(recorder)
        if delivered == 0:
            break
    return received


@pytest_asyncio.fixture()
async def outbox(tmp_path: Path):
    box = NotificationOutbox(tmp_path / "outbox.db")
    await box.open()
    try:
        yield box
    finally:
        await box.close()


@pytest.mark.asyncio
async def test_initial_down_emitted_once(outbox, tmp_path):
    store = WatchdogStateStore(tmp_path / "wd.json")
    adapter = FakeMaxAdapter()
    await adapter.connect()
    adapter.set_connected(False)
    clock, _ = _clock(1_700_000_000)
    cfg = WatchdogConfig(
        down_threshold=2,
        downtime_grace_seconds=0,
        reminder_interval_seconds=3600,
    )
    wd = MaxWatchdog(
        adapter=adapter,
        store=store,
        dispatcher=OutboxBackedDispatcher(
            outbox=outbox,
            inner=LoggingNotificationDispatcher(),
            destination_chat_id="self",
        ),
        config=cfg,
        clock=clock,
    )
    # Two failures -> DOWN.
    await wd.tick()
    await wd.tick()
    received = await _drain_all(outbox)
    downs = [r for r in received if r["event_type"] == "down"]
    assert len(downs) == 1, received


@pytest.mark.asyncio
async def test_no_reminder_before_one_hour(outbox, tmp_path):
    store = WatchdogStateStore(tmp_path / "wd.json")
    adapter = FakeMaxAdapter()
    await adapter.connect()
    adapter.set_connected(False)
    clock, advance = _clock(1_700_000_000)
    cfg = WatchdogConfig(
        down_threshold=2,
        downtime_grace_seconds=0,
        reminder_interval_seconds=3600,
    )
    wd = MaxWatchdog(
        adapter=adapter,
        store=store,
        dispatcher=OutboxBackedDispatcher(
            outbox=outbox,
            inner=LoggingNotificationDispatcher(),
            destination_chat_id="self",
        ),
        config=cfg,
        clock=clock,
    )
    await wd.tick()
    await wd.tick()
    # 59 minutes later: still nothing.
    advance(59 * 60)
    await wd.tick()
    received = await _drain_all(outbox)
    reminders = [r for r in received if r["event_type"] == "hourly_reminder"]
    assert reminders == [], received


@pytest.mark.asyncio
async def test_reminder_at_one_hour(outbox, tmp_path):
    store = WatchdogStateStore(tmp_path / "wd.json")
    adapter = FakeMaxAdapter()
    await adapter.connect()
    adapter.set_connected(False)
    clock, advance = _clock(1_700_000_000)
    cfg = WatchdogConfig(
        down_threshold=2,
        downtime_grace_seconds=0,
        reminder_interval_seconds=3600,
    )
    wd = MaxWatchdog(
        adapter=adapter,
        store=store,
        dispatcher=OutboxBackedDispatcher(
            outbox=outbox,
            inner=LoggingNotificationDispatcher(),
            destination_chat_id="self",
        ),
        config=cfg,
        clock=clock,
    )
    await wd.tick()
    await wd.tick()
    advance(60 * 60)
    await wd.tick()
    received = await _drain_all(outbox)
    reminders = [r for r in received if r["event_type"] == "hourly_reminder"]
    assert len(reminders) == 1, received


@pytest.mark.asyncio
async def test_recovery_emitted_once(outbox, tmp_path):
    store = WatchdogStateStore(tmp_path / "wd.json")
    adapter = FakeMaxAdapter()
    await adapter.connect()
    adapter.set_connected(False)
    clock, _advance = _clock(1_700_000_000)
    cfg = WatchdogConfig(
        down_threshold=2, downtime_grace_seconds=0, reminder_interval_seconds=3600
    )
    wd = MaxWatchdog(
        adapter=adapter,
        store=store,
        dispatcher=OutboxBackedDispatcher(
            outbox=outbox,
            inner=LoggingNotificationDispatcher(),
            destination_chat_id="self",
        ),
        config=cfg,
        clock=clock,
    )
    await wd.tick()
    await wd.tick()
    adapter.set_connected(True)
    snap = await wd.tick()
    assert snap.state == AdapterHealthState.HEALTHY
    received = await _drain_all(outbox)
    recovered = [r for r in received if r["event_type"] == "recovered"]
    assert len(recovered) == 1, received


@pytest.mark.asyncio
async def test_no_reminder_after_recovery(outbox, tmp_path):
    store = WatchdogStateStore(tmp_path / "wd.json")
    adapter = FakeMaxAdapter()
    await adapter.connect()
    adapter.set_connected(False)
    clock, advance = _clock(1_700_000_000)
    cfg = WatchdogConfig(
        down_threshold=2, downtime_grace_seconds=0, reminder_interval_seconds=3600
    )
    wd = MaxWatchdog(
        adapter=adapter,
        store=store,
        dispatcher=OutboxBackedDispatcher(
            outbox=outbox,
            inner=LoggingNotificationDispatcher(),
            destination_chat_id="self",
        ),
        config=cfg,
        clock=clock,
    )
    await wd.tick()
    await wd.tick()
    adapter.set_connected(True)
    await wd.tick()
    # Many hours later, still HEALTHY: no reminders.
    advance(5 * 60 * 60)
    snap = await wd.tick()
    assert snap.state == AdapterHealthState.HEALTHY
    received = await _drain_all(outbox)
    reminders = [r for r in received if r["event_type"] == "hourly_reminder"]
    assert reminders == [], received


@pytest.mark.asyncio
async def test_5h_outage_does_not_create_spam_storm(outbox, tmp_path):
    """Coalesce five reminders into one pending outbox row."""
    store = WatchdogStateStore(tmp_path / "wd.json")
    adapter = FakeMaxAdapter()
    await adapter.connect()
    adapter.set_connected(False)
    clock, advance = _clock(1_700_000_000)
    cfg = WatchdogConfig(
        down_threshold=2, downtime_grace_seconds=0, reminder_interval_seconds=3600
    )
    wd = MaxWatchdog(
        adapter=adapter,
        store=store,
        dispatcher=OutboxBackedDispatcher(
            outbox=outbox,
            inner=LoggingNotificationDispatcher(),
            destination_chat_id="self",
        ),
        config=cfg,
        clock=clock,
    )
    await wd.tick()
    await wd.tick()
    for hour in range(1, 6):
        advance(60 * 60)
        await wd.tick()
    # Only one reminder row should be queued (coalesced).
    pending = await outbox.list_pending()
    reminder_rows = [r for r in pending if r["event_type"] == "hourly_reminder"]
    assert len(reminder_rows) == 1, pending


@pytest.mark.asyncio
async def test_restart_preserves_incident(outbox, tmp_path):
    store = WatchdogStateStore(tmp_path / "wd.json")
    adapter = FakeMaxAdapter()
    await adapter.connect()
    adapter.set_connected(False)
    clock, advance = _clock(1_700_000_000)
    cfg = WatchdogConfig(
        down_threshold=2, downtime_grace_seconds=0, reminder_interval_seconds=3600
    )
    wd = MaxWatchdog(
        adapter=adapter,
        store=store,
        dispatcher=OutboxBackedDispatcher(
            outbox=outbox,
            inner=LoggingNotificationDispatcher(),
            destination_chat_id="self",
        ),
        config=cfg,
        clock=clock,
    )
    await wd.tick()
    await wd.tick()
    advance(60 * 60)
    await wd.tick()
    # Restart: fresh watchdog, same store, same clock.
    wd2 = MaxWatchdog(
        adapter=adapter,
        store=store,
        dispatcher=OutboxBackedDispatcher(
            outbox=outbox,
            inner=LoggingNotificationDispatcher(),
            destination_chat_id="self",
        ),
        config=cfg,
        clock=clock,
    )
    assert wd2.incident.initial_alert_sent is True
    assert wd2.incident.last_reminder_at is not None
    assert wd2.incident.incident_id >= 1