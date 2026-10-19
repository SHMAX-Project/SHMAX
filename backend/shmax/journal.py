"""Private, bounded metadata journal. Producers never pass arbitrary text.

DELETE journaling deliberately avoids WAL growth caused by long-lived readers.
The database page cap reserves a second database's worth for rollback plus at least 64KiB (1MiB with the default budget).
Only this dedicated database is ever pruned. All disk work runs off the event loop.
"""

from __future__ import annotations
import asyncio
import contextvars
import datetime as dt
import hashlib
import re
import sqlite3
import time
import uuid
from pathlib import Path

correlation = contextvars.ContextVar("journal_correlation", default=None)
CATALOG = {
    "system.started": ("INFO", "SYSTEM", "SHMAX запущен"),
    "system.stopping": ("INFO", "SYSTEM", "SHMAX завершает работу"),
    "worker.started": ("INFO", "BRIDGE", "Обработчик запущен"),
    "worker.stopped": ("INFO", "BRIDGE", "Обработчик остановлен"),
    "worker.failed": ("ERROR", "BRIDGE", "Обработчик неожиданно завершился"),
    "database.migrated": (
        "SUCCESS",
        "SYSTEM",
        "Проверка миграций базы данных завершена",
    ),
    "admin.action": ("INFO", "SECURITY", "Действие администратора"),
    "config.changed": ("INFO", "SYSTEM", "Конфигурация изменена"),
    "update.success": ("SUCCESS", "UPDATE", "Обновление завершено"),
    "update.failed": ("ERROR", "UPDATE", "Обновление не выполнено"),
    "update.rollback": ("WARNING", "UPDATE", "Откат завершён"),
    "message.received": ("INFO", "BRIDGE", "Сообщение получено"),
    "message.normalized": ("INFO", "BRIDGE", "Сообщение нормализовано"),
    "message.queued": ("INFO", "DELIVERY", "Добавлено в очередь"),
    "message.duplicate": ("DEBUG", "BRIDGE", "Повторное событие пропущено"),
    "attachment.unsupported": ("WARNING", "DELIVERY", "Неподдерживаемое вложение"),
    "delivery.attempt": ("INFO", "DELIVERY", "Начата попытка доставки"),
    "delivery.success": ("SUCCESS", "DELIVERY", "Сообщение успешно доставлено"),
    "media.success": ("SUCCESS", "DELIVERY", "Медиа успешно доставлено"),
    "album.success": ("SUCCESS", "DELIVERY", "Альбом успешно доставлен"),
    "delivery.retry": ("WARNING", "DELIVERY", "Запланирована повторная доставка"),
    "delivery.temporary_failure": ("WARNING", "DELIVERY", "Временная ошибка доставки"),
    "delivery.failed": ("ERROR", "DELIVERY", "Доставка завершилась ошибкой"),
    "max.connected": ("SUCCESS", "MAX", "Соединение с MAX установлено"),
    "max.disconnected": ("WARNING", "MAX", "Соединение с сервером MAX потеряно"),
    "max.reconnect_attempt": ("INFO", "MAX", "Попытка восстановления MAX"),
    "max.reconnect_success": (
        "SUCCESS",
        "MAX",
        "Автоматическое переподключение MAX выполнено",
    ),
    "max.reconnect_failure": ("WARNING", "MAX", "MAX временно недоступен"),
    "max.auth_required": (
        "ERROR",
        "MAX",
        "Сессия MAX отклонена; требуется авторизация",
    ),
    "telegram.connected": ("SUCCESS", "TELEGRAM", "Соединение с Telegram установлено"),
    "telegram.disconnected": ("WARNING", "TELEGRAM", "Соединение с Telegram потеряно"),
    "telegram.polling_failure": (
        "WARNING",
        "TELEGRAM",
        "Ошибка получения обновлений Telegram",
    ),
    "telegram.recovery_attempt": (
        "INFO",
        "TELEGRAM",
        "Попытка восстановления Telegram",
    ),
    "telegram.recovery_success": (
        "SUCCESS",
        "TELEGRAM",
        "Соединение с Telegram восстановлено",
    ),
    "xray.selection": ("INFO", "XRAY", "Выбран узел Xray"),
    "xray.starting": ("INFO", "XRAY", "Xray запускается"),
    "xray.ready": ("SUCCESS", "XRAY", "Локальный прокси Xray готов"),
    "xray.transport_failure": ("WARNING", "XRAY", "Ошибка транспорта Xray"),
    "xray.switch_completed": ("SUCCESS", "XRAY", "Переключение Xray завершено"),
    "xray.switch_failed": ("ERROR", "XRAY", "Переключение Xray не завершено"),
}
SEVERITIES = {"DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR"}
CATEGORIES = {
    "SYSTEM",
    "MAX",
    "TELEGRAM",
    "XRAY",
    "BRIDGE",
    "DELIVERY",
    "SECURITY",
    "UPDATE",
}
ERRORS = {
    "TimeoutError",
    "ConnectionError",
    "ConnectionRefusedError",
    "OSError",
    "EOFError",
    "MaxAuthError",
    "MaxSessionError",
    "MaxTransportError",
    "MaxConnectionError",
    "MaxUnsupportedError",
    "TelegramBadRequest",
    "TelegramNetworkError",
    "TelegramUnauthorizedError",
    "ProxyTimeoutError",
    "InvalidPayload",
    "ListenerStopped",
    "TransportUnavailable",
    "TLSFailure",
    "DNSFailure",
    "UnknownError",
}


def message_correlation(source, chat, external):
    return hashlib.sha256(f"{source}:{chat}:{external}".encode()).hexdigest()[:24]


def iso(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat()


class JournalConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class EventJournal:
    def __init__(
        self,
        path: Path,
        *,
        retention_days=90,
        max_records=100_000,
        max_bytes=128 * 1024 * 1024,
        queue_size=1024,
        clock=time.time,
    ):
        self.path = Path(path)
        self.retention_days = retention_days
        self.max_records = max_records
        self.max_bytes = max_bytes
        self.reserve_bytes = max(65536, max_bytes // 128)
        self.clock = clock
        self.queue = asyncio.Queue(maxsize=queue_size)
        self.task = None
        self.dropped = 0
        self.failures = 0
        self._closed = False
        self._episodes = {}
        self._run_id = uuid.uuid4().hex

    def emit(
        self,
        event_type,
        *,
        component=None,
        correlation_id=None,
        direction=None,
        attempt=None,
        error_class=None,
        result=None,
        aggregate=False,
    ):
        # A closed vocabulary prevents secrets, message text and injected HTML from
        # entering SQLite at all. Unknown fields cannot be supplied by producers.
        if event_type not in CATALOG or self._closed:
            return
        if component not in {"max", "telegram", "xray", "bridge", "system", None}:
            component = None
        cid = correlation_id or correlation.get()
        if cid and not re.fullmatch(r"[a-f0-9]{16,64}|q-[0-9]{1,20}", str(cid)):
            cid = None
        if direction not in {"max_to_telegram", "telegram_to_max", None}:
            direction = None
        if error_class not in ERRORS:
            error_class = "UnknownError" if error_class else None
        if result not in {
            "success",
            "temporary",
            "permanent",
            "rolled_back",
            "degraded",
            None,
        }:
            result = None
        if attempt is not None:
            attempt = max(0, min(int(attempt), 1_000_000))
        # Recoveries split episodes without retaining one key per message.
        if (
            event_type.endswith(("success", "connected", "switch_completed"))
            and component
        ):
            self._episodes[component] = self._episodes.get(component, 0) + 1
        key = (
            f"{self._run_id}:{component}:{event_type}:{error_class}:{self._episodes.get(component,0)}"
            if aggregate
            else None
        )
        item = (
            self.clock(),
            event_type,
            component,
            cid,
            direction,
            attempt,
            error_class,
            result,
            key,
        )
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self.dropped += 1

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=1, factory=JournalConnection)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=1000")
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA auto_vacuum=INCREMENTAL")
        page = db.execute("PRAGMA page_size").fetchone()[0]
        # A rollback journal can contain at most the original database pages.
        pages = max(8, (self.max_bytes - self.reserve_bytes) // (2 * page))
        db.execute(f"PRAGMA max_page_count={pages}")
        return db

    def _init(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS events(
              id INTEGER PRIMARY KEY, first_at REAL NOT NULL, last_at REAL NOT NULL,
              severity TEXT NOT NULL, category TEXT NOT NULL, event_type TEXT NOT NULL,
              description TEXT NOT NULL, component TEXT, correlation_id TEXT,
              direction TEXT, attempt INTEGER, error_class TEXT, result TEXT,
              occurrences INTEGER NOT NULL DEFAULT 1, aggregate_key TEXT UNIQUE);
              CREATE INDEX IF NOT EXISTS event_time ON events(last_at DESC,id DESC);
              CREATE INDEX IF NOT EXISTS event_category ON events(category,last_at DESC,id DESC);
              CREATE INDEX IF NOT EXISTS event_severity ON events(severity,last_at DESC,id DESC);
              CREATE INDEX IF NOT EXISTS event_correlation ON events(correlation_id);
            """)
        self.path.chmod(0o600)

    async def start(self):
        try:
            await asyncio.to_thread(self._init)
        except Exception:
            self.failures += 1
        self.task = asyncio.create_task(self._run(), name="shmax.journal")

    def _prune(self, db):
        # Bounded transactions, oldest first. Never touches application tables.
        cutoff = self.clock() - self.retention_days * 86400
        size = self.path.stat().st_size
        budget_pressure = size > (self.max_bytes - self.reserve_bytes) * 0.45
        db.execute(
            "DELETE FROM events WHERE id IN (SELECT id FROM events WHERE last_at<? ORDER BY last_at,id LIMIT 1000)",
            (cutoff,),
        )
        count = db.execute("SELECT count(*) FROM events").fetchone()[0]
        limit = min(
            1000, max(0, count - self.max_records, 1000 if budget_pressure else 0)
        )
        if limit:
            db.execute(
                "DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY last_at,id LIMIT ?)",
                (limit,),
            )
        db.commit()
        db.execute("PRAGMA incremental_vacuum(256)").fetchall()

    def _write(self, batch):
        with self._connect() as db:
            if self.path.stat().st_size > (self.max_bytes - self.reserve_bytes) * 0.45:
                self._prune(db)
            for ts, typ, component, cid, direction, attempt, err, result, key in batch:
                sev, cat, desc = CATALOG[typ]
                db.execute(
                    """INSERT INTO events(first_at,last_at,severity,category,event_type,description,
                component,correlation_id,direction,attempt,error_class,result,aggregate_key)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(aggregate_key) DO UPDATE SET
                last_at=excluded.last_at,occurrences=events.occurrences+1,attempt=excluded.attempt""",
                    (
                        ts,
                        ts,
                        sev,
                        cat,
                        typ,
                        desc,
                        component,
                        cid,
                        direction,
                        attempt,
                        err,
                        result,
                        key,
                    ),
                )
            db.commit()
            self._prune(db)

    def _maintenance(self):
        with self._connect() as db:
            self._prune(db)

    async def _run(self):
        while True:
            try:
                item = await asyncio.wait_for(self.queue.get(), timeout=60)
            except asyncio.TimeoutError:
                try:
                    await asyncio.to_thread(self._maintenance)
                except Exception:
                    self.failures += 1
                continue
            if item is None:
                self.queue.task_done()
                return
            batch = [item]
            # A short batching window avoids a filesystem transaction per producer call.
            await asyncio.sleep(0.005)
            while len(batch) < 128 and not self.queue.empty():
                nxt = self.queue.get_nowait()
                if nxt is None:
                    self.queue.task_done()
                    break
                batch.append(nxt)
            try:
                await asyncio.to_thread(self._write, batch)
            except Exception:
                self.failures += 1
                self.dropped += len(batch)
            finally:
                for _ in batch:
                    self.queue.task_done()

    async def flush(self):
        await self.queue.join()

    async def close(self):
        self._closed = True
        if self.task:
            await self.flush()
            await self.queue.put(None)
            await self.task
            self.task = None

    def query(
        self,
        *,
        limit=50,
        before=None,
        severity=None,
        category=None,
        search="",
        since=None,
        until=None,
    ):
        limit = max(1, min(200, int(limit)))
        where = []
        args = []
        for col, val in [("severity", severity), ("category", category)]:
            if val:
                where.append(col + "=?")
                args.append(val)
        for col, op, val in [("last_at", ">=", since), ("last_at", "<=", until)]:
            if val is not None:
                where.append(col + op + "?")
                args.append(val)
        if before:
            ts, ident = before.split(":", 1)
            where.append("(last_at,id)<(?,?)")
            args.extend((float(ts), int(ident)))
        if search:
            search = (
                search[:128]
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            where.append(
                "(description LIKE ? ESCAPE '\\' OR event_type LIKE ? ESCAPE '\\' OR correlation_id LIKE ? ESCAPE '\\')"
            )
            args.extend(["%" + search + "%"] * 3)
        sql = (
            "SELECT * FROM events"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY last_at DESC,id DESC LIMIT ?"
        )
        with self._connect() as db:
            # Cancel excessive search work. Results never include arbitrary payloads.
            started = time.monotonic()
            db.set_progress_handler(lambda: time.monotonic() - started > 1, 1000)
            rows = [dict(r) for r in db.execute(sql, args + [limit + 1])]
        more = len(rows) > limit
        rows = rows[:limit]
        cursor = f"{rows[-1]['last_at']}:{rows[-1]['id']}" if more and rows else None
        for row in rows:
            row["timestamp"] = iso(row.pop("last_at"))
            row["first_timestamp"] = iso(row.pop("first_at"))
            row.pop("aggregate_key", None)
        return {"events": rows, "next_cursor": cursor, "storage": self.storage()}

    def storage(self):
        sizes = {}
        for suffix in ["", "-wal", "-shm", "-journal"]:
            file = self.path.parent / (self.path.name + suffix)
            try:
                stat = file.stat()
                sizes[suffix] = max(stat.st_size, getattr(stat, "st_blocks", 0) * 512)
            except FileNotFoundError:
                sizes[suffix] = 0
        return {
            "bytes": sum(sizes.values()),
            "files": sizes,
            "max_bytes": self.max_bytes,
            "max_records": self.max_records,
            "retention_days": self.retention_days,
            "dropped": self.dropped,
            "write_failures": self.failures,
            "queue_capacity": self.queue.maxsize,
        }
