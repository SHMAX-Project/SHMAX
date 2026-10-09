"""Notification dispatchers.

The watchdog emits :class:`WatchdogEvent`; this module turns
them into Telegram messages. ``BufferingNotificationDispatcher``
queues events locally so they survive a Telegram outage and
can be flushed when Telegram recovers.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..adapters.telegram.base import TelegramAdapter
from ..watchdog.base import (
    AdapterHealthState,
    LoggingNotificationDispatcher,
    NotificationDispatcher,
    WatchdogEvent,
)


logger = logging.getLogger(__name__)


def format_event(event: WatchdogEvent) -> str:
    """Render a watchdog event as a short Russian operator notice."""
    snap = event.snapshot
    last_ok = snap.last_success_at or "—"
    downtime = "—"
    if snap.down_since is not None:
        delta = max(0, int(datetime.now(timezone.utc).timestamp()) - snap.down_since)
        downtime = _fmt_duration(delta)
    title = {
        "down": "🔴 " + _adapter_name(event.adapter) + " недоступен.",
        "recovered": "🟢 " + _adapter_name(event.adapter) + " снова работает.",
        "hourly_reminder": "🔴 " + _adapter_name(event.adapter) + " всё ещё недоступен.",
        "degraded": "⚠️ " + _adapter_name(event.adapter) + " работает с перебоями.",
    }.get(event.kind, event.kind)
    body = {
        "down": (
            "SHMAX потерял связь и не смог восстановить её автоматически.\n"
            f"Последний успешный контакт: {_fmt_ts(snap.last_success_at)}\n"
            f"Простой: {downtime}\n"
            f"Ошибка: {snap.last_error_class or '—'}"
        ),
        "recovered": (
            "Связь восстановлена.\n"
            f"Простой составил: {downtime}"
        ),
        "hourly_reminder": (
            f"Простой: {downtime}\n"
            f"Последний успешный контакт: {_fmt_ts(snap.last_success_at)}"
        ),
        "degraded": (
            f"Ошибка: {snap.last_error_class or '—'}\n"
            f"Подряд: {snap.consecutive_failures}"
        ),
    }.get(event.kind, "")
    return f"{title}\n\n{body}".strip()


def _adapter_name(name: str) -> str:
    return {"max": "MAX", "telegram": "Telegram"}.get(name, name.upper())


def _fmt_ts(value: Optional[int]) -> str:
    if value is None:
        return "—"
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(value)


def _fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h} ч {m} мин"
    if m > 0:
        return f"{m} мин {s} сек"
    return f"{s} сек"


class BufferingNotificationDispatcher(NotificationDispatcher):
    """Buffer events on disk and flush when asked.

    When Telegram is DOWN, watchdog events accumulate here so
    they are not lost. The buffer is JSONL: one event per line.
    """

    def __init__(self, path: Path, inner: NotificationDispatcher) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._inner = inner
        self._lock = asyncio.Lock()

    async def send(
        self,
        *,
        event_type: str,
        chat_id: str,
        thread_id: str | None,
        payload: dict,
    ) -> None:
        async with self._lock:
            with self._path.open("a") as fh:
                fh.write(
                    json.dumps(
                        {
                            "event_type": event_type,
                            "chat_id": chat_id,
                            "thread_id": thread_id,
                            "payload": payload,
                            "ts": int(datetime.now(timezone.utc).timestamp()),
                        }
                    )
                    + "\n"
                )
            await self._inner.send(
                event_type=event_type,
                chat_id=chat_id,
                thread_id=thread_id,
                payload=payload,
            )

    async def flush(self, telegram: TelegramAdapter) -> int:
        """Replay buffered events to Telegram. Returns count sent.

        No-op if no buffer file exists.
        """
        if not self._path.exists():
            return 0
        async with self._lock:
            text = self._path.read_text()
            self._path.unlink(missing_ok=True)
        events = [json.loads(line) for line in text.splitlines() if line.strip()]
        sent = 0
        for raw in events:
            try:
                await telegram.send_message(
                    chat_id=raw.get("chat_id", "self"),
                    text=f"[replay] {raw.get('event_type', '')}: {raw.get('payload', {}).get('last_error_class', '')}",
                )
                sent += 1
            except Exception:  # noqa: BLE001
                logger.exception("failed to flush buffered notification")
        return sent

    def buffered_count(self) -> int:
        if not self._path.exists():
            return 0
        return sum(1 for line in self._path.read_text().splitlines() if line.strip())


class TelegramNotificationDispatcher(NotificationDispatcher):
    """Sends watchdog events to a pre-configured Telegram chat/thread."""

    def __init__(
        self,
        adapter: TelegramAdapter,
        *,
        chat_id: str,
        thread_id: Optional[str] = None,
    ) -> None:
        self._adapter = adapter
        self._chat_id = chat_id
        self._thread_id = thread_id

    async def send(
        self,
        *,
        event_type: str,
        chat_id: str,
        thread_id: str | None,
        payload: dict,
    ) -> None:
        # Render via format_event when the payload looks like a
        # watchdog snapshot; otherwise send a compact one-liner.
        text = self._render_text(event_type=event_type, payload=payload)
        # Exceptions are NOT swallowed — the outbox sender records
        # them and applies backoff. The audit flagged silent
        # failures as a source of false-confidence delivery.
        await self._adapter.send_message(
            chat_id=self._chat_id,
            text=text,
            thread_id=thread_id or self._thread_id,
        )

    def _render_text(self, *, event_type: str, payload: dict) -> str:
        # Minimal schema mapping. Watchdogs use
        # ``event_type`` of ``down | recovered | hourly_reminder |
        # degraded``. Anything else is rendered as a header line.
        if event_type in ("down", "recovered", "hourly_reminder", "degraded"):
            adapter = payload.get("adapter", "max")
            kind_ru = {
                "max": "MAX",
                "telegram": "Telegram",
            }.get(adapter, adapter.upper())
            if event_type == "down":
                return (
                    f"🔴 {kind_ru} недоступен.\n\n"
                    f"SHMAX потерял связь и не смог восстановить её автоматически.\n"
                    f"Ошибка: {payload.get('last_error_class', '—')}"
                )
            if event_type == "recovered":
                return f"🟢 {kind_ru} снова работает.\n\nСвязь восстановлена."
            if event_type == "hourly_reminder":
                return (
                    f"🔴 {kind_ru} всё ещё недоступен.\n\n"
                    f"Простой продолжается.\n"
                    f"Ошибка: {payload.get('last_error_class', '—')}"
                )
            return (
                f"⚠️ {kind_ru} работает с перебоями.\n\n"
                f"Подряд: {payload.get('consecutive_failures', '—')}"
            )
        return f"[{event_type}] {payload}"


def build_notification_dispatcher(
    *,
    telegram: Optional[TelegramAdapter],
    chat_id: Optional[str],
    thread_id: Optional[str],
    buffer_path: Path,
) -> NotificationDispatcher:
    """Factory used by the API layer.

    Returns a :class:`BufferingNotificationDispatcher` wrapping
    either a :class:`TelegramNotificationDispatcher` (when
    Telegram is configured) or a :class:`LoggingNotificationDispatcher`.
    """
    if telegram is not None and chat_id is not None:
        inner = TelegramNotificationDispatcher(telegram, chat_id=chat_id, thread_id=thread_id)
    else:
        inner = LoggingNotificationDispatcher()
    return BufferingNotificationDispatcher(buffer_path, inner)


__all__ = [
    "BufferingNotificationDispatcher",
    "TelegramNotificationDispatcher",
    "build_notification_dispatcher",
    "format_event",
]
