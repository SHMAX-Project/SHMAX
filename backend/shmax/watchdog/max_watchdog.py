"""MAX watchdog.

Driven by an injectable clock so tests can simulate long
downtimes in microseconds.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Awaitable, Callable, Optional

from ..adapters.max.base import MaxAdapter
from ..adapters.max.errors import (
    MaxAuthError,
    MaxConnectionError,
    MaxSessionError,
)
from .base import (
    AdapterHealthState,
    Clock,
    IncidentRecord,
    NotificationDispatcher,
    WatchdogConfig,
    WatchdogEvent,
    WatchdogSnapshot,
    WatchdogStateStore,
    _DispatcherSlot,
)


logger = logging.getLogger(__name__)


# Reconnect / self-heal hook. Returns True if it succeeded.
SelfHealer = Callable[[], Awaitable[bool]]


class MaxWatchdog:
    """Health monitor for the MAX adapter.

    The watchdog exposes:

    * ``tick()`` — one pass: probe + state machine + emit events.
    * ``run_forever()`` — periodic loop with the injected clock.
    * ``probe_now()`` — operator-triggered immediate probe (used
      by the "Проверить сейчас" button).
    * ``self_heal_now()`` — operator-triggered recovery sequence.

    State transitions:

    * HEALTHY → DEGRADED on first failure (configurable threshold).
    * DEGRADED → DOWN after ``down_threshold`` consecutive failures.
    * DOWN → HEALTHY on first success after recovery.

    DOWN emits a single ``down`` event. ``recovered`` is emitted
    once on the way back up. While DOWN, the watchdog emits an
    ``hourly_reminder`` event every ``reminder_interval_seconds``.
    """

    def __init__(
        self,
        *,
        adapter: MaxAdapter,
        store: WatchdogStateStore,
        dispatcher: NotificationDispatcher | _DispatcherSlot,
        config: WatchdogConfig | None = None,
        clock: Clock | None = None,
        self_healer: SelfHealer | None = None,
    ) -> None:
        self._adapter = adapter
        self._store = store
        # Accept either a concrete dispatcher or a slot whose
        # ``current`` attribute is mutated at runtime when the
        # dispatcher stack swaps (e.g. logging -> outbox-backed).
        self._dispatcher_slot: _DispatcherSlot
        if isinstance(dispatcher, _DispatcherSlot):
            self._dispatcher_slot = dispatcher
        else:
            self._dispatcher_slot = _DispatcherSlot(dispatcher)
        self._config = config or WatchdogConfig()
        self._clock = clock or _real_clock
        self._self_healer = self_healer or _default_self_heal
        self._snapshot = (
            store.load("max")
            or WatchdogSnapshot(
                adapter="max",
                state=AdapterHealthState.HEALTHY,
            )
        )
        # The snapshot's adapter field should be normalised.
        self._snapshot = replace(self._snapshot, adapter="max")
        self._incident = store.load_incident("max") or IncidentRecord(adapter="max")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @property
    def snapshot(self) -> WatchdogSnapshot:
        return self._snapshot

    @property
    def incident(self) -> IncidentRecord:
        return self._incident

    @property
    def _dispatcher(self) -> NotificationDispatcher:
        """Current dispatcher — reads through the slot so swaps are
        picked up immediately. Tests that compare watchdogs by
        identity get a consistent view via ``_dispatcher``."""
        return self._dispatcher_slot.current

    async def probe_now(self) -> WatchdogSnapshot:
        """One synchronous probe cycle, used by the UI button."""
        await self._tick()
        self._store.save(self._snapshot)
        return self._snapshot

    async def self_heal_now(self) -> WatchdogSnapshot:
        """Operator-triggered self-heal sequence."""
        logger.info("max watchdog: operator-triggered self-heal begin")
        ok = await self._self_healer()
        if ok:
            self._snapshot = replace(
                self._snapshot,
                consecutive_failures=0,
                reconnect_attempts=self._snapshot.reconnect_attempts + 1,
            )
        else:
            self._snapshot = replace(
                self._snapshot,
                consecutive_failures=self._config.down_threshold,
                reconnect_attempts=self._snapshot.reconnect_attempts + 1,
                last_error_class="SelfHealFailed",
            )
        self._store.save(self._snapshot)
        await self._tick()
        self._store.save(self._snapshot)
        return self._snapshot

    async def tick(self) -> WatchdogSnapshot:
        await self._tick()
        self._store.save(self._snapshot)
        return self._snapshot

    async def reconnect_now(self) -> None:
        """BLOCKER 28 — drive a real reconnect through the live
        adapter. The watchdog's :pyattr:`_adapter` exposes a
        ``reconnect()`` coroutine (PyMax client has its own
        ``reconnect=True`` extra; for the live adapter we issue
        an explicit disconnect + connect). If the adapter has no
        ``reconnect`` method we fall back to ``disconnect`` /
        ``connect``.
        """
        adapter = self._adapter
        reconnect = getattr(adapter, "reconnect", None)
        if callable(reconnect):
            try:
                await reconnect()
                return
            except Exception:  # noqa: BLE001
                logger.exception("adapter.reconnect failed; falling back")
        # Fallback: explicit disconnect + connect.
        try:
            disconnect = getattr(adapter, "disconnect", None)
            if callable(disconnect):
                await disconnect()
        except Exception:  # noqa: BLE001
            logger.exception("adapter.disconnect failed during reconnect")
        connect = getattr(adapter, "connect", None)
        if callable(connect):
            await connect()

    async def run_forever(self) -> None:
        interval = max(self._config.probe_interval_seconds, 0.01)
        while True:
            await self._tick()
            self._store.save(self._snapshot)
            await asyncio.sleep(interval)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    async def _tick(self) -> None:
        now = int(self._clock())
        prev = self._snapshot
        try:
            health = await self._adapter.health()
        except Exception as e:  # noqa: BLE001
            await self._on_failure(
                detail=str(e),
                error_class="ProbeException",
                now=now,
            )
            return
        connected = bool(health.get("connected"))
        authenticated = bool(health.get("authenticated"))
        if connected and authenticated:
            await self._on_success(now)
            return
        if not authenticated:
            await self._on_failure(
                detail="not authenticated",
                error_class="AuthRequired",
                now=now,
                next_state=AdapterHealthState.AUTH_REQUIRED,
            )
            return
        # connected False but authenticated True -> transient
        await self._on_failure(
            detail="adapter reports not connected",
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
        # Recovery alert — exactly once per incident.
        if prev.state in (AdapterHealthState.DOWN, AdapterHealthState.AUTH_REQUIRED):
            if not self._incident.recovery_sent:
                self._incident = IncidentRecord(
                    adapter="max",
                    incident_id=self._incident.incident_id,
                    down_since=self._incident.down_since,
                    initial_alert_sent=self._incident.initial_alert_sent,
                    last_reminder_at=self._incident.last_reminder_at,
                    recovered_at=now,
                    recovery_sent=True,
                )
                self._store.save_incident(self._incident)
                event = WatchdogEvent(
                    kind="recovered",
                    adapter="max",
                    state=new.state,
                    snapshot=new,
                )
                await self._dispatcher_slot.current.send(
            event_type=event.kind,
            chat_id=event.adapter,
            thread_id=None,
            payload={
                "snapshot": event.snapshot.__dict__ if hasattr(event.snapshot, '__dict__') else {},
            },
        )
        self._store.save(new)

    async def _on_failure(
        self,
        *,
        detail: str,
        error_class: str,
        now: int,
        next_state: AdapterHealthState | None = None,
    ) -> None:
        prev = self._snapshot
        new_failures = prev.consecutive_failures + 1
        if new_failures >= self._config.down_threshold and next_state is None:
            next_state = AdapterHealthState.DOWN
        elif next_state is None and prev.state != AdapterHealthState.HEALTHY:
            next_state = prev.state  # stay
        else:
            next_state = next_state or AdapterHealthState.DEGRADED
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
        # Emit events on transitions.
        if prev.state == AdapterHealthState.HEALTHY and new.state != AdapterHealthState.HEALTHY:
            await self._dispatcher_slot.current.send(
                event_type="degraded",
                chat_id="max",
                thread_id=None,
                payload={
                    "snapshot": new.__dict__ if hasattr(new, '__dict__') else {},
                },
            )
        if new.state == AdapterHealthState.DOWN and prev.state != AdapterHealthState.DOWN:
            # Open a new incident.
            self._incident = IncidentRecord(
                adapter="max",
                incident_id=self._incident.incident_id + 1,
                down_since=down_since,
                initial_alert_sent=False,
                last_reminder_at=None,
                recovered_at=None,
                recovery_sent=False,
            )
            # Run self-heal sequence.
            ok = await self._self_healer()
            if ok:
                self._snapshot = replace(
                    self._snapshot,
                    consecutive_failures=0,
                    reconnect_attempts=self._snapshot.reconnect_attempts + 1,
                    last_error_class=None,
                    last_error_detail=None,
                )
                await self._tick()
                self._store.save_incident(self._incident)
                return
            self._snapshot = replace(
                self._snapshot,
                reconnect_attempts=self._snapshot.reconnect_attempts + 1,
                last_error_class="SelfHealFailed",
                last_error_detail="automatic recovery failed",
            )
            # Only emit DOWN alert after the grace period so a
            # short blip doesn't page the operator.
            if (
                self._snapshot.down_since is not None
                and now - self._snapshot.down_since
                >= self._config.downtime_grace_seconds
                and not self._incident.initial_alert_sent
            ):
                await self._dispatcher_slot.current.send(
                    event_type="down",
                    chat_id="max",
                    thread_id=None,
                    payload={
                        "snapshot": self._snapshot.__dict__ if hasattr(self._snapshot, '__dict__') else {},
                    },
                )
                self._incident = IncidentRecord(
                    adapter="max",
                    incident_id=self._incident.incident_id,
                    down_since=self._incident.down_since,
                    initial_alert_sent=True,
                    last_reminder_at=now,
                    recovered_at=None,
                    recovery_sent=False,
                )
                self._snapshot = replace(
                    self._snapshot,
                    last_notification_at=now,
                )
            self._store.save_incident(self._incident)
        elif new.state == AdapterHealthState.DOWN and self._should_remind(now):
            await self._dispatcher_slot.current.send(
                event_type="hourly_reminder",
                chat_id="max",
                thread_id=None,
                payload={
                    "snapshot": self._snapshot.__dict__ if hasattr(self._snapshot, '__dict__') else {},
                },
            )
            self._incident = IncidentRecord(
                adapter="max",
                incident_id=self._incident.incident_id,
                down_since=self._incident.down_since,
                initial_alert_sent=self._incident.initial_alert_sent,
                last_reminder_at=now,
                recovered_at=None,
                recovery_sent=self._incident.recovery_sent,
            )
            self._snapshot = replace(self._snapshot, last_notification_at=now)
            self._store.save_incident(self._incident)
        self._store.save(self._snapshot)

    def _should_remind(self, now: int) -> bool:
        if self._snapshot.last_notification_at is None:
            return True
        return (now - self._snapshot.last_notification_at) >= self._config.reminder_interval_seconds


def _real_clock() -> float:
    import time

    return time.time()


async def _default_self_heal() -> bool:
    """Best-effort reconnect for adapters that expose ``reconnect``.

    Returns False if self-heal is not available. The watcher
    treats False as "give up and emit DOWN".
    """
    return False


__all__ = ["MaxWatchdog", "SelfHealer"]
