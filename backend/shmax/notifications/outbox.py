"""Persistent notification outbox.

ACK-based semantics:

* ``enqueue`` writes a row to the SQLite ``notification_outbox``
  table. The row is the durable copy.
* ``drain_pending`` picks ready rows, dispatches them via
  the injected :class:`NotificationDispatcher`, and marks
  the row ``delivered`` only after the dispatcher returns
  success.
* Rows in ``delivered`` are kept for audit; the operator
  can purge via :func:`purge_delivered`.

Coalescing:

* ``coalesce_key`` identifies the conceptual event (e.g.
  ``max_down_reminder``). When a new row is enqueued with
  a coalesce_key that already has a non-delivered row,
  the existing row is REPLACED in-place — only the latest
  state is kept. This is what prevents a Telegram outage
  from producing one reminder per hour that then all fire
  when Telegram comes back: the most recent state replaces
  the older ones.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

from .dispatcher import NotificationDispatcher


logger = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS notification_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    coalesce_key TEXT,
    destination_chat_id TEXT NOT NULL,
    thread_id TEXT,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | delivered | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    delivered_at INTEGER,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS ix_outbox_status_next
    ON notification_outbox(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS ix_outbox_coalesce
    ON notification_outbox(coalesce_key) WHERE coalesce_key IS NOT NULL;
"""


class NotificationOutbox:
    """Persistent outbox of notifications to deliver."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # Each call to sqlite3.connect() opens a fresh handle;
        # we keep one for the lifetime of the runtime so
        # queries are cheap.
        loop = asyncio.get_running_loop()
        self._conn = await loop.run_in_executor(None, self._open_sync)

    def _open_sync(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._db_path), isolation_level=None, check_same_thread=False
        )
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    async def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    async def enqueue(
        self,
        *,
        event_type: str,
        destination_chat_id: str,
        payload: dict[str, Any],
        thread_id: Optional[str] = None,
        coalesce_key: Optional[str] = None,
        delay_seconds: float = 0.0,
    ) -> int:
        """Insert a notification; coalesce by key.

        Returns the row id. If ``coalesce_key`` matches an
        existing non-delivered row, the existing row is
        replaced in-place.
        """
        loop = asyncio.get_running_loop()
        async with self._lock:
            return await loop.run_in_executor(
                None,
                self._enqueue_sync,
                event_type,
                destination_chat_id,
                payload,
                thread_id,
                coalesce_key,
                delay_seconds,
            )

    def _enqueue_sync(
        self,
        event_type: str,
        destination_chat_id: str,
        payload: dict,
        thread_id: Optional[str],
        coalesce_key: Optional[str],
        delay_seconds: float,
    ) -> int:
        assert self._conn is not None
        now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
        next_attempt = now + int(delay_seconds)
        payload_json = json.dumps(payload, separators=(",", ":"))
        if coalesce_key is not None:
            existing = self._conn.execute(
                "SELECT id FROM notification_outbox "
                "WHERE coalesce_key = ? AND status != 'delivered' LIMIT 1",
                (coalesce_key,),
            ).fetchone()
            if existing is not None:
                self._conn.execute(
                    "UPDATE notification_outbox SET "
                    "event_type = ?, destination_chat_id = ?, thread_id = ?, "
                    "payload_json = ?, status = 'pending', attempts = 0, "
                    "next_attempt_at = ?, created_at = ?, delivered_at = NULL, "
                    "last_error = NULL "
                    "WHERE id = ?",
                    (
                        event_type,
                        destination_chat_id,
                        thread_id,
                        payload_json,
                        next_attempt,
                        now,
                        existing[0],
                    ),
                )
                return int(existing[0])
        cur = self._conn.execute(
            "INSERT INTO notification_outbox "
            "(event_type, coalesce_key, destination_chat_id, thread_id, "
            " payload_json, status, attempts, next_attempt_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?)",
            (
                event_type,
                coalesce_key,
                destination_chat_id,
                thread_id,
                payload_json,
                next_attempt,
                now,
            ),
        )
        return int(cur.lastrowid)

    async def drain_pending(self, dispatcher: NotificationDispatcher) -> int:
        """Dispatch every ready row.

        Returns the number of rows that transitioned to
        ``delivered`` during this pass.
        """
        loop = asyncio.get_running_loop()
        async with self._lock:
            rows = await loop.run_in_executor(None, self._fetch_ready_sync)
        delivered = 0
        for row in rows:
            (rid, event_type, chat_id, thread_id, payload_json, attempts) = row
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError:
                await self._mark_failed(rid, attempts + 1, "bad payload json")
                continue
            try:
                await dispatcher.send(
                    event_type=event_type,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    payload=payload,
                )
            except Exception as e:  # noqa: BLE001
                await self._mark_failed(rid, attempts + 1, repr(e)[:512])
                continue
            await self._mark_delivered(rid)
            delivered += 1
        return delivered

    def _fetch_ready_sync(self) -> list[tuple]:
        assert self._conn is not None
        now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
        return list(
            self._conn.execute(
                "SELECT id, event_type, destination_chat_id, thread_id, "
                "payload_json, attempts FROM notification_outbox "
                "WHERE status = 'pending' AND next_attempt_at <= ? "
                "ORDER BY next_attempt_at ASC LIMIT 32",
                (now,),
            )
        )

    async def _mark_delivered(self, row_id: int) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            await loop.run_in_executor(None, self._mark_delivered_sync, row_id)

    def _mark_delivered_sync(self, row_id: int) -> None:
        assert self._conn is not None
        now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
        self._conn.execute(
            "UPDATE notification_outbox "
            "SET status='delivered', delivered_at = ? "
            "WHERE id = ?",
            (now, row_id),
        )

    async def _mark_failed(self, row_id: int, attempts: int, last_error: str) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            await loop.run_in_executor(
                None, self._mark_failed_sync, row_id, attempts, last_error
            )

    def _mark_failed_sync(self, row_id: int, attempts: int, last_error: str) -> None:
        assert self._conn is not None
        # Backoff: 60s base, doubled per attempt, capped at 30min.
        delay = min(1800, 60 * (2 ** min(attempts, 8)))
        next_attempt = int(_dt.datetime.now(_dt.timezone.utc).timestamp()) + delay
        self._conn.execute(
            "UPDATE notification_outbox "
            "SET attempts = ?, last_error = ?, next_attempt_at = ? "
            "WHERE id = ?",
            (attempts, last_error, next_attempt, row_id),
        )

    async def purge_delivered(self, *, max_age_seconds: int = 7 * 24 * 3600) -> int:
        loop = asyncio.get_running_loop()
        async with self._lock:
            return await loop.run_in_executor(
                None, self._purge_delivered_sync, max_age_seconds
            )

    def _purge_delivered_sync(self, max_age_seconds: int) -> int:
        assert self._conn is not None
        cutoff = int(_dt.datetime.now(_dt.timezone.utc).timestamp()) - max_age_seconds
        cur = self._conn.execute(
            "DELETE FROM notification_outbox WHERE status = 'delivered' "
            "AND delivered_at IS NOT NULL AND delivered_at < ?",
            (cutoff,),
        )
        return cur.rowcount or 0

    async def list_pending(self) -> list[dict]:
        loop = asyncio.get_running_loop()
        async with self._lock:
            return await loop.run_in_executor(None, self._list_pending_sync)

    def _list_pending_sync(self) -> list[dict]:
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT id, event_type, coalesce_key, destination_chat_id, "
            "thread_id, status, attempts, next_attempt_at, created_at, "
            "last_error FROM notification_outbox "
            "WHERE status != 'delivered' "
            "ORDER BY next_attempt_at ASC LIMIT 64"
        ).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "id": r[0],
                    "event_type": r[1],
                    "coalesce_key": r[2],
                    "destination_chat_id": r[3],
                    "thread_id": r[4],
                    "status": r[5],
                    "attempts": r[6],
                    "next_attempt_at": r[7],
                    "created_at": r[8],
                    "last_error": r[9],
                }
            )
        return out


__all__ = ["NotificationOutbox"]
