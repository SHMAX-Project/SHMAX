"""SQLAlchemy 2.x async engine factory.

The engine is wired to a single SQLite file under
``$SHMAX_DATA_DIR/db/shmax.db``. We always enable WAL mode,
foreign keys, and a busy timeout so the bridge survives
concurrent reads from the FastAPI thread and the queue worker.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


@dataclass(frozen=True)
class DatabaseSettings:
    """Database configuration resolved from environment."""

    data_dir: Path

    @property
    def url(self) -> str:
        db_path = self.data_dir / "db" / "shmax.db"
        # Use POSIX-style path; aiosqlite handles it on both Linux/macOS.
        return f"sqlite+aiosqlite:///{db_path}"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "db" / "shmax.db"


def build_engine(settings: DatabaseSettings) -> AsyncEngine:
    """Build the async engine and install SQLite pragmas."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "db").mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(
        settings.url,
        future=True,
        echo=False,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection, _):
        # SQLite requires these per-connection.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON;")
        cursor.execute("PRAGMA journal_mode = WAL;")
        cursor.execute("PRAGMA busy_timeout = 5000;")
        cursor.execute("PRAGMA synchronous = NORMAL;")
        cursor.close()

    return engine


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        engine,
        expire_on_commit=False,
        autoflush=False,
    )


class Database:
    """Holder for engine + session factory.

    Use as a singleton per process; dispose on shutdown.
    """

    def __init__(self, settings: DatabaseSettings) -> None:
        self.settings = settings
        self.engine: AsyncEngine = build_engine(settings)
        self.session_factory: async_sessionmaker[AsyncSession] = (
            build_session_factory(self.engine)
        )

    async def dispose(self) -> None:
        await self.engine.dispose()

    def session(self) -> AsyncSession:
        return self.session_factory()
