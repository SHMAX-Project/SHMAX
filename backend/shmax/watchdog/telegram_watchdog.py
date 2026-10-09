"""Telegram watchdog — symmetric with the MAX watchdog."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

from ..adapters.telegram.base import TelegramAdapter
from .base import (
    AdapterHealthState,
    Clock,
    NotificationDispatcher,
    WatchdogConfig,
    WatchdogEvent,
    WatchdogSnapshot,
    WatchdogStateStore,
    _DispatcherSlot,
)
from .max_watchdog import SelfHealer


logger = logging.getLogger(__name__)


class TelegramWatchdog:
    def __init__(
        self,
        *,
        adapter: TelegramAdapter,
        store: WatchdogStateStore,
        dispatcher: NotificationDispatcher | _DispatcherSlot,
        config: WatchdogConfig | None = None,
        clock: Clock | None = None,
        self_healer: SelfHealer | None = None,
    ) -> None:
        self._adapter = adapter
        self._store = store
        if isinstance(dispatcher, _DispatcherSlot):
            self._dispatcher_slot = dispatcher
        else:
            self._dispatcher_slot = _DispatcherSlot(dispatcher)
        self._config = config or WatchdogConfig()
        self._clock = clock or _real_clock
        self._self_healer = self_healer or _default_self_heal
        self._snapshot = (
            store.load("telegram")
            or WatchdogSnapshot(
                adapter="telegram",
                state=AdapterHealthState.HEALTHY,
            )
        )
        self._snapshot = replace(self._snapshot, adapter="telegram")

    @property
    def snapshot(self) -> WatchdogSnapshot:
        return self._snapshot

    @property
    def _dispatcher(self) -> NotificationDispatcher:
        """Current dispatcher — reads through the slot so swaps are
        picked up immediately (matches MaxWatchdog contract)."""
        return self._dispatcher_slot.current

    async def tick(self) -> WatchdogSnapshot:
        await self._tick()
        self._store.save(self._snapshot)
        return self._snapshot

    async def probe_now(self) -> WatchdogSnapshot:
        return await self.tick()

    async def self_heal_now(self) -> WatchdogSnapshot:
        ok = await self._self_healer()
        if ok:
            self._snapshot = replace(self._snapshot, consecutive_failures=0)
        self._store.save(self._snapshot)
        await self._tick()
        self._store.save(self._snapshot)
        return self._snapshot

    async def run_forever(self) -> None:
        interval = max(self._config.probe_interval_seconds, 0.01)
        while True:
            await self._tick()
            self._store.save(self._snapshot)
            await asyncio.sleep(interval)

    async def _tick(self) -> None:
        now = int(self._clock())
        try:
            health = await self._adapter.health()
        except Exception as e:  # noqa: BLE001
            await self._on_failure(detail=str(e), error_class="ProbeException", now=now)
            return
        # Adapter contract is `async def health() -> bool` returning
        # True only when both connected and authenticated. Some
        # adapters historically returned a dict; we accept either
        # shape so the watchdog stays forward-compatible.
        if isinstance(health, dict):
            # P1 #1 (surgical): the receive path is part of health —
            # a connected+authenticated transport with a dead or
            # cancelled polling task receives NOTHING and must be
            # treated as a watchdog failure (DEGRADED per thresholds).
            polling_flag = health.get("polling")
            ok = bool(
                health.get("connected")
                and health.get("authenticated")
                and polling_flag is not False
            )
        else:
            ok = bool(health)
        if ok:
            await self._on_success(now)
            return
        await self._on_failure(
            detail="adapter reports not connected / not authenticated",
            error_class="Disconnected",
            now=now,
        )

    async def _on_success(self, now: int) -> None:
        prev = self._snapshot
        new = replace(
            prev,
            state=AdapterHealthState.HEALTHY,
            last_success_at=now,
            last_probe_at=now,
            consecutive_failures=0,
            down_since=None,
            last_error_class=None,
            last_error_detail=None,
        )
        self._snapshot = new
        if prev.state in (AdapterHealthState.DOWN, AdapterHealthState.AUTH_REQUIRED):
            await self._dispatcher_slot.current.dispatch(
                WatchdogEvent(kind="recovered", adapter="telegram", state=new.state, snapshot=new)
            )
        self._store.save(new)

    async def _on_failure(self, *, detail: str, error_class: str, now: int) -> None:
        prev = self._snapshot
        new_failures = prev.consecutive_failures + 1
        next_state = AdapterHealthState.DEGRADED
        if new_failures >= self._config.down_threshold:
            next_state = AdapterHealthState.DOWN
        down_since = prev.down_since
        if next_state == AdapterHealthState.DOWN and prev.state != AdapterHealthState.DOWN:
            down_since = prev.down_since or now
        new = replace(
            prev,
            state=next_state,
            last_probe_at=now,
            consecutive_failures=new_failures,
            down_since=down_since,
            last_error_class=error_class,
            last_error_detail=detail[:512],
        )
        self._snapshot = new
        if new.state == AdapterHealthState.DOWN and prev.state != AdapterHealthState.DOWN:
            ok = await self._self_healer()
            if ok:
                self._snapshot = replace(
                    self._snapshot,
                    consecutive_failures=0,
                    reconnect_attempts=self._snapshot.reconnect_attempts + 1,
                )
                await self._tick()
                return
            self._snapshot = replace(
                self._snapshot,
                reconnect_attempts=self._snapshot.reconnect_attempts + 1,
            )
            if (
                self._snapshot.down_since is not None
                and now - self._snapshot.down_since
                >= self._config.downtime_grace_seconds
            ):
                await self._dispatcher_slot.current.dispatch(
                    WatchdogEvent(kind="down", adapter="telegram", state=new.state, snapshot=self._snapshot)
                )
                self._snapshot = replace(self._snapshot, last_notification_at=now)
        elif new.state == AdapterHealthState.DOWN and self._should_remind(now):
            await self._dispatcher_slot.current.dispatch(
                WatchdogEvent(kind="hourly_reminder", adapter="telegram", state=new.state, snapshot=self._snapshot)
            )
            self._snapshot = replace(self._snapshot, last_notification_at=now)
        self._store.save(self._snapshot)

    def _should_remind(self, now: int) -> bool:
        if self._snapshot.last_notification_at is None:
            return True
        return (now - self._snapshot.last_notification_at) >= self._config.reminder_interval_seconds


def _real_clock() -> float:
    import time

    return time.time()


async def _default_self_heal() -> bool:
    return False


__all__ = ["TelegramWatchdog"]
