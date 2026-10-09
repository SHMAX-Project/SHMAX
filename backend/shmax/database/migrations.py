"""Alembic migrations wrapper.

Production uses the **sync** SQLite engine for migration because
``alembic.command.upgrade`` is synchronous and cannot drive an
async engine directly. The application runtime keeps the
async engine for the bridge; the sync engine is created on
demand for migration only.

``create_all`` is the **test-only** fast-path. The runtime
must use ``run_migrations``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.engine import Connection, create_engine

from .models import Base


HERE = Path(__file__).resolve()
BACKEND_ROOT = HERE.parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
ALEMBIC_DIR = BACKEND_ROOT / "alembic"


def _sync_url(async_url: str) -> str:
    """Convert ``sqlite+aiosqlite:///path`` to ``sqlite:///path``."""
    return async_url.replace("sqlite+aiosqlite://", "sqlite://", 1)


def _make_alembic_config(db_url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI) if ALEMBIC_INI.exists() else None)
    if not cfg.config_file_name:
        cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


async def current_revision(engine: AsyncEngine) -> Optional[str]:
    async with engine.connect() as conn:
        ctx = MigrationContext(await conn.run_sync(lambda c: c))
        return ctx.get_current_revision()


def run_migrations_sync(db_url: str) -> str:
    """Run ``alembic upgrade head`` against the given async URL.

    Internally we translate to a sync SQLite URL, run Alembic,
    and return the resulting revision id.

    BLOCKER 30 — a process / file lock around the migration
    prevents two startup processes from migrating the same
    SQLite file concurrently. The lock lives next to the DB
    file (``<db>.migration.lock``) and uses ``fcntl.flock``
    semantics: ``LOCK_EX`` (exclusive) with ``LOCK_NB`` so the
    second contender fails fast instead of blocking the
    startup. The holder proceeds with the migration and
    releases the lock on exit.
    """
    sync_url = _sync_url(db_url)
    cfg = _make_alembic_config(sync_url)
    lock_path = _migration_lock_path(sync_url)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    _acquire_migration_lock(lock_path)
    try:
        command.upgrade(cfg, "head")
    finally:
        _release_migration_lock(lock_path)
    # Alembic stamps the version into alembic_version. We re-read
    # via a sync engine to get the canonical revision string.
    sync_engine = create_engine(sync_url, future=True)
    try:
        with sync_engine.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).fetchone()
            return str(row[0]) if row else "head"
    finally:
        sync_engine.dispose()


async def create_all(engine: AsyncEngine) -> None:
    """Create every table from the SQLAlchemy metadata.

    Test-only path. Production startup uses ``run_migrations``.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


__all__ = ["create_all", "current_revision", "run_migrations_sync"]


# ----------------------------------------------------------------------
# Migration lock — BLOCKER 30
# ----------------------------------------------------------------------
import contextlib
import fcntl
import os
import time
from pathlib import Path as _Path


@contextlib.contextmanager
def _migration_lock(lock_path: "_Path"):
    """Acquire an exclusive fcntl lock on ``lock_path``.

    The lock is released either via the context manager exit or
    automatically when the process dies (POSIX advisory locks
    are tied to file descriptors, not the file on disk).
    Concurrent processes waiting on the lock fail fast instead
    of queueing — the second startup is expected to retry
    after the holder exits.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            os.close(fd)
            raise RuntimeError(
                f"another process holds the migration lock on {lock_path}"
            ) from e
        try:
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _migration_lock_path(sync_url: str) -> _Path:
    """Compute the lock path for the given sync SQLite URL.

    ``sqlite:///./foo.db`` -> ``./foo.db.migration.lock``
    ``sqlite:////var/lib/shmax/db/shmax.db`` -> ``/var/lib/shmax/db/shmax.db.migration.lock``
    """
    # Strip the scheme.
    if sync_url.startswith("sqlite:///"):
        path = sync_url[len("sqlite:///"):]
    elif sync_url.startswith("sqlite://"):
        path = sync_url[len("sqlite://"):]
    else:
        return _Path(sync_url + ".migration.lock")
    return _Path(path + ".migration.lock")


def _acquire_migration_lock(lock_path: _Path) -> None:
    """Open + acquire the lock and store the FD on the module so
    we can release it later. We don't use a context manager
    because Alembic may spawn subprocesses that need the lock
    released only after they finish."""
    global _LOCK_FD
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as e:
        os.close(fd)
        raise RuntimeError(
            f"another process holds the migration lock on {lock_path}"
        ) from e
    _LOCK_FD = (fd, lock_path)


def _release_migration_lock(lock_path: _Path) -> None:
    global _LOCK_FD
    if _LOCK_FD is None:
        return
    fd, stored = _LOCK_FD
    if stored != lock_path:
        # Defensive: do not release an unrelated lock.
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass
    _LOCK_FD = None


_LOCK_FD = None  # type: ignore[var-annotated]
