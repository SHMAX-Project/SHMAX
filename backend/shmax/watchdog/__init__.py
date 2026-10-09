"""Watchdog subsystem.

Both transport adapters (MAX and Telegram) get a watchdog that:

* probes the adapter on a configurable interval,
* transitions through HEALTHY → DEGRADED → DOWN,
* persists state across restarts (file-backed JSON),
* emits notification events on transitions,
* runs a self-heal sequence (probe → reconnect → session reload
  → restart worker → probe) before giving up.

The watchdog is intentionally clock-injectable so tests can
simulate hours in milliseconds.
"""

from .base import (
    AdapterHealthState,
    NotificationDispatcher,
    WatchdogConfig,
    WatchdogEvent,
    WatchdogSnapshot,
    WatchdogStateStore,
)
from .max_watchdog import MaxWatchdog
from .telegram_watchdog import TelegramWatchdog

__all__ = [
    "AdapterHealthState",
    "MaxWatchdog",
    "NotificationDispatcher",
    "TelegramWatchdog",
    "WatchdogConfig",
    "WatchdogEvent",
    "WatchdogSnapshot",
    "WatchdogStateStore",
]
