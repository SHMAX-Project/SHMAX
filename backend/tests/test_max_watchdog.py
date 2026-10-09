"""Tests for the MAX watchdog state machine + dispatcher."""

from __future__ import annotations

import asyncio

import pytest

from shmax.adapters.max.fake import FakeMaxAdapter
from shmax.watchdog.base import (
    AdapterHealthState,
    NotificationDispatcher,
    WatchdogConfig,
    WatchdogStateStore,
)
from shmax.watchdog.max_watchdog import MaxWatchdog


class _RecordingDispatcher(NotificationDispatcher):
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def send(
        self, *, event_type, chat_id, thread_id, payload
    ) -> None:
        self.events.append((event_type, chat_id, payload))


@pytest.mark.asyncio
async def test_max_watchdog_starts_healthy(data_dir):
    store = WatchdogStateStore(data_dir / "watchdogs.json")
    adapter = FakeMaxAdapter()
    await adapter.connect()  # mark authenticated so health() reports connected+authenticated
    dispatcher = _RecordingDispatcher()
    wd = MaxWatchdog(adapter=adapter, store=store, dispatcher=dispatcher)
    snap = await wd.tick()
    assert snap.state == AdapterHealthState.HEALTHY
    assert snap.consecutive_failures == 0


@pytest.mark.asyncio
async def test_max_watchdog_health_to_degraded_to_down(data_dir):
    store = WatchdogStateStore(data_dir / "watchdogs.json")
    adapter = FakeMaxAdapter()
    await adapter.connect()  # mark authenticated
    adapter.set_connected(False)
    dispatcher = _RecordingDispatcher()
    cfg = WatchdogConfig(down_threshold=3, downtime_grace_seconds=0)
    wd = MaxWatchdog(adapter=adapter, store=store, dispatcher=dispatcher, config=cfg)

    # Three failures -> DOWN.
    for _ in range(3):
        await wd.tick()
    snap = store.load("max")
    assert snap is not None
    assert snap.state == AdapterHealthState.DOWN


@pytest.mark.asyncio
async def test_max_watchdog_recovery_emits_recovered(data_dir):
    store = WatchdogStateStore(data_dir / "watchdogs.json")
    adapter = FakeMaxAdapter()
    await adapter.connect()  # mark authenticated
    dispatcher = _RecordingDispatcher()
    cfg = WatchdogConfig(down_threshold=2, downtime_grace_seconds=0)
    wd = MaxWatchdog(adapter=adapter, store=store, dispatcher=dispatcher, config=cfg)

    # Drive to DOWN.
    adapter.set_connected(False)
    await wd.tick()
    await wd.tick()
    snap = store.load("max")
    assert snap is not None and snap.state == AdapterHealthState.DOWN
    # Recover.
    adapter.set_connected(True)
    snap = await wd.tick()
    assert snap.state == AdapterHealthState.HEALTHY
    recovered = [e for e in dispatcher.events if e[0] == "recovered"]
    assert recovered


@pytest.mark.asyncio
async def test_max_watchdog_persists_across_restarts(data_dir):
    store = WatchdogStateStore(data_dir / "watchdogs.json")
    adapter = FakeMaxAdapter()
    await adapter.connect()  # mark authenticated so health() reports connected+authenticated
    adapter.set_connected(False)  # then disconnect — should drive DOWN
    cfg = WatchdogConfig(down_threshold=2, downtime_grace_seconds=0)
    wd1 = MaxWatchdog(adapter=adapter, store=store, dispatcher=_RecordingDispatcher(), config=cfg)
    await wd1.tick()
    await wd1.tick()
    snap1 = store.load("max")
    assert snap1 is not None and snap1.state == AdapterHealthState.DOWN

    # Fresh watchdog, same store — must remember.
    wd2 = MaxWatchdog(adapter=adapter, store=store, dispatcher=_RecordingDispatcher(), config=cfg)
    snap2 = await wd2.tick()
    assert snap2.consecutive_failures >= 1
