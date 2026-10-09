"""Common watchdog types."""

from __future__ import annotations

import abc
import asyncio
import datetime as _dt
import json
import logging
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional


logger = logging.getLogger(__name__)


class AdapterHealthState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    AUTH_REQUIRED = "auth_required"


@dataclass(frozen=True)
class WatchdogConfig:
    probe_interval_seconds: float = 60.0
    degraded_threshold: int = 1     # 1 failure -> DEGRADED
    down_threshold: int = 3         # 3 consecutive failures -> DOWN
    self_heal_attempts: int = 1
    reminder_interval_seconds: float = 3600.0
    downtime_grace_seconds: float = 60.0  # wait at least this long before DOWN alert


@dataclass
class WatchdogSnapshot:
    adapter: str
    state: AdapterHealthState
    last_success_at: Optional[int] = None
    last_probe_at: Optional[int] = None
    down_since: Optional[int] = None
    consecutive_failures: int = 0
    reconnect_attempts: int = 0
    last_notification_at: Optional[int] = None
    last_error_class: Optional[str] = None
    last_error_detail: Optional[str] = None


@dataclass
class IncidentRecord:
    """Persistent incident state for a watchdog.

    Mirrors the rows the watchdogs need to remember across
    restarts. ``incident_id`` is a monotonically-increasing
    counter that increments every time the adapter transitions
    HEALTHY → DOWN. Reminders are coalesced by ``incident_id``
    so a post-recovery restart cannot fire stale reminders.
    """

    adapter: str
    incident_id: int = 0
    down_since: Optional[int] = None
    initial_alert_sent: bool = False
    last_reminder_at: Optional[int] = None
    recovered_at: Optional[int] = None
    recovery_sent: bool = False


@dataclass(frozen=True)
class WatchdogEvent:
    kind: str  # "down" | "recovered" | "hourly_reminder" | "degraded"
    adapter: str
    state: AdapterHealthState
    snapshot: WatchdogSnapshot


class WatchdogStateStore:
    """File-backed JSON store for the watchdog snapshot.

    Persistence matters: after a restart SHMAX must remember that
    MAX has been DOWN for several hours so the next alert's
    downtime count is honest.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        if not self._path.exists():
            return

    def load(self, adapter: str) -> Optional[WatchdogSnapshot]:
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        snap = data.get(adapter)
        if not snap:
            return None
        return WatchdogSnapshot(
            adapter=snap["adapter"],
            state=AdapterHealthState(snap["state"]),
            last_success_at=snap.get("last_success_at"),
            last_probe_at=snap.get("last_probe_at"),
            down_since=snap.get("down_since"),
            consecutive_failures=int(snap.get("consecutive_failures", 0)),
            reconnect_attempts=int(snap.get("reconnect_attempts", 0)),
            last_notification_at=snap.get("last_notification_at"),
            last_error_class=snap.get("last_error_class"),
            last_error_detail=snap.get("last_error_detail"),
        )

    def save(self, snapshot: WatchdogSnapshot) -> None:
        data: dict[str, Any] = {}
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text())
            except (OSError, json.JSONDecodeError):
                data = {}
        data[snapshot.adapter] = asdict(snapshot)
        data[snapshot.adapter]["state"] = snapshot.state.value
        self._path.write_text(json.dumps(data, indent=2, sort_keys=True))

    # ------------------------------------------------------------------
    # Incident persistence (Priority B: persistent incident model)
    # ------------------------------------------------------------------
    def load_incident(self, adapter: str) -> Optional[IncidentRecord]:
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        raw = data.get(f"{adapter}__incident")
        if not raw:
            return None
        return IncidentRecord(
            adapter=raw["adapter"],
            incident_id=int(raw.get("incident_id", 0)),
            down_since=raw.get("down_since"),
            initial_alert_sent=bool(raw.get("initial_alert_sent", False)),
            last_reminder_at=raw.get("last_reminder_at"),
            recovered_at=raw.get("recovered_at"),
            recovery_sent=bool(raw.get("recovery_sent", False)),
        )

    def save_incident(self, incident: IncidentRecord) -> None:
        data: dict[str, Any] = {}
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text())
            except (OSError, json.JSONDecodeError):
                data = {}
        data[f"{incident.adapter}__incident"] = asdict(incident)
        self._path.write_text(json.dumps(data, indent=2, sort_keys=True))

    def clear_incident(self, adapter: str) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if f"{adapter}__incident" in data:
            del data[f"{adapter}__incident"]
            self._path.write_text(json.dumps(data, indent=2, sort_keys=True))


class NotificationDispatcher(abc.ABC):
    """Receives notifications; concrete impls deliver to Telegram.

    The dispatcher is responsible for downstream transport
    selection and rate limiting on the operator's behalf. The
    MAX watchdog itself only emits events.

    V1 contract: ``send`` accepts the row-level fields from
    :class:`NotificationOutbox`. Watchdogs convert their
    events into the same call so the outbox dispatcher and
    watchdog dispatcher share one interface.
    """

    @abc.abstractmethod
    async def send(
        self,
        *,
        event_type: str,
        chat_id: str,
        thread_id: str | None,
        payload: dict,
    ) -> None: ...

    async def dispatch(self, event: "WatchdogEvent") -> None:
        """Convenience adapter for callers that have a
        :class:`WatchdogEvent` instead of raw fields.

        The default implementation serialises the event into
        the ``send`` signature. Subclasses MAY override this
        if they want a custom rendering for watchdog events.
        """
        await self.send(
            event_type=event.kind,
            chat_id=event.snapshot.adapter,
            thread_id=None,
            payload={
                "adapter": event.snapshot.adapter,
                "kind": event.kind,
                "state": event.snapshot.state.value,
                "down_since": event.snapshot.down_since,
                "last_success_at": event.snapshot.last_success_at,
                "last_error_class": event.snapshot.last_error_class,
                "last_error_detail": event.snapshot.last_error_detail,
                "consecutive_failures": event.snapshot.consecutive_failures,
            },
        )


class LoggingNotificationDispatcher(NotificationDispatcher):
    """Offline dispatcher that logs to the standard logger.

    Used by tests and by environments where no Telegram is
    configured. Production wires a Telegram-aware dispatcher.
    """

    async def send(
        self,
        *,
        event_type: str,
        chat_id: str,
        thread_id: str | None,
        payload: dict,
    ) -> None:
        logger.warning(
            "notification event_type=%s chat_id=%s thread_id=%s payload=%s",
            event_type,
            chat_id,
            thread_id,
            payload,
        )


class _DispatcherSlot:
    """Mutable single-cell holder for a NotificationDispatcher.

    The runtime swaps the inner dispatcher on ``start()`` (logging →
    OutboxBacked). Watchdogs hold a :class:`_DispatcherSlot` rather
    than a concrete dispatcher, so they always dispatch through whatever
    is current — no stale-reference bug where the watchdog keeps
    logging because it captured the pre-start dispatcher.
    """

    __slots__ = ("current",)

    def __init__(self, current: NotificationDispatcher) -> None:
        self.current = current


# Clock protocol — tests inject a deterministic clock.
Clock = Callable[[], float]
