"""BLOCKER 30 — Migration lock.

Two startup processes must NOT migrate the same SQLite database
concurrently. The fix uses a per-DB ``fcntl.flock`` advisory lock
placed next to the database file (``shmax.db.migration.lock``).

This test verifies:

* Two concurrent ``_acquire_migration_lock`` calls — the second
  raises ``RuntimeError`` instead of blocking the test.
* The lock is released after ``run_migrations_sync`` so a
  follow-up startup can proceed.
* The lock file is colocated with the database file.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from shmax.database.migrations import (
    _acquire_migration_lock,
    _migration_lock_path,
    _release_migration_lock,
    run_migrations_sync,
)


def test_lock_path_lives_next_to_db(tmp_path):
    db = tmp_path / "data" / "shmax.db"
    sync_url = f"sqlite:///{db}"
    lock = _migration_lock_path(sync_url)
    assert lock == db.with_suffix(db.suffix + ".migration.lock") or lock == db.parent / "shmax.db.migration.lock"
    # Path resolution varies by SQLAlchemy normalisation; accept
    # either absolute or relative variant.
    assert lock.name == "shmax.db.migration.lock"


def test_second_acquire_fails_fast(tmp_path):
    lock_path = tmp_path / "shmax.db.migration.lock"
    _acquire_migration_lock(lock_path)
    try:
        with pytest.raises(RuntimeError) as excinfo:
            _acquire_migration_lock(lock_path)
        assert "migration lock" in str(excinfo.value).lower()
    finally:
        _release_migration_lock(lock_path)


def test_release_allows_followup_acquire(tmp_path):
    lock_path = tmp_path / "shmax.db.migration.lock"
    _acquire_migration_lock(lock_path)
    _release_migration_lock(lock_path)
    # Second acquire should now succeed.
    _acquire_migration_lock(lock_path)
    _release_migration_lock(lock_path)


def test_run_migrations_uses_lock(tmp_path):
    """End-to-end: a successful migration releases the lock so
    a follow-up invocation does not raise."""
    db_path = tmp_path / "shmax-mig.db"
    sync_url = f"sqlite:///{db_path}"
    lock_path = _migration_lock_path(sync_url)
    # First migration acquires + releases cleanly.
    async_url = f"sqlite+aiosqlite:///{db_path}"
    run_migrations_sync(async_url)
    # Lock file must exist on disk (created by the first run).
    assert lock_path.is_file(), lock_path
    # Second migration acquires + releases cleanly.
    run_migrations_sync(async_url)


def test_lock_is_exclusive_across_processes(tmp_path):
    """A second process trying to acquire the same lock while the
    first is held must raise. We simulate by spawning a child
    that holds the lock for a short time, then assert the parent
    sees ``RuntimeError``."""
    import subprocess
    import sys

    lock_path = tmp_path / "outerlock.db.migration.lock"
    # Hold the lock in the parent process.
    _acquire_migration_lock(lock_path)
    try:
        # Run a child that tries to acquire the same lock.
        # Use a tiny script that imports the module and calls
        # ``_acquire_migration_lock``; we expect it to exit with
        # non-zero on RuntimeError.
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; "
                f"from shmax.database.migrations import _acquire_migration_lock; "
                f"_acquire_migration_lock(__import__('pathlib').Path('{lock_path}')); "
                "sys.exit(0)",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode != 0, result.stdout + result.stderr
    finally:
        _release_migration_lock(lock_path)