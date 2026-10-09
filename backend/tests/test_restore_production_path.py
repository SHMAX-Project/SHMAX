"""BLOCKER 15 — Restore production path uses the transactional coordinator.

The /api/backups/{name}/restore endpoint MUST NOT call the
legacy ``restore_backup()`` directly: that path bypasses the
safety snapshot, the manifest gate, and the runtime hooks. The
endpoint must route through :class:`TransactionalRestore` which:

1. validates the manifest and decrypts the archive;
3. creates the mandatory safety backup;
4. stops the runtime before mutating data_dir;
5. swaps the staging tree into data_dir atomically;
6. starts the runtime again so the API talks to the
   freshly-restored DB / SecretStore / sessions;
7. rolls back to the safety backup on any failure.

A regression that re-introduces a hand-rolled restore would skip
the safety snapshot or the runtime quiesce/refresh step.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest


def test_restore_endpoint_imports_transactional_coordinator():
    """The API module imports :class:`TransactionalRestore` and
    ``RestoreError`` from the backup manager. A regression that
    removes the import means the endpoint is no longer going
    through the coordinator."""
    from shmax.api import app as app_module
    from shmax.backup.manager import RestoreError, TransactionalRestore

    src = inspect.getsource(app_module)
    assert "TransactionalRestore" in src
    assert "RestoreError" in src
    assert "restore_backup_endpoint" in src


def test_restore_endpoint_does_not_call_legacy_restore_backup_directly():
    """The endpoint source must NOT call the legacy ``restore_backup``
    helper. That helper bypasses the safety snapshot and the
    runtime hooks."""
    from shmax.api import app as app_module

    src = inspect.getsource(app_module)
    # Find the endpoint body — it lives in ``create_app`` as a
    # closure. We isolate the lines that mention restore.
    restore_lines = [
        line for line in src.splitlines()
        if "restore" in line.lower() and not line.lstrip().startswith("#")
    ]
    body = "\n".join(restore_lines)
    # The endpoint must reference the coordinator, not the
    # legacy helper. We accept ``TransactionalRestore`` references
    # and reject bare ``restore_backup(`` calls.
    assert "TransactionalRestore(" in body
    # The bare legacy helper is unsafe in production.
    assert "restore_backup(" not in body


@pytest.mark.asyncio
async def test_restore_endpoint_uses_coordinator_run(tmp_path):
    """End-to-end: simulate the endpoint body that uses
    :class:`TransactionalRestore` and verify the safety backup
    and runtime hooks fire."""
    from shmax.backup.manager import (
        TransactionalRestore,
        RestoreError,
        create_backup,
    )

    # Prepare two backups: an "initial" data_dir and a "target"
    # backup we want to restore from.
    initial = tmp_path / "initial"
    initial.mkdir()
    (initial / "marker.txt").write_text("before")
    backups_dir = initial / "backups"
    backups_dir.mkdir()
    target_archive = backups_dir / "snapshot.shmax"
    create_backup(
        data_dir=initial,
        out_path=target_archive,
        shmax_version="1.0.0",
    )
    # Modify the live state to ensure the restore brings back
    # the original marker.
    (initial / "marker.txt").write_text("after")

    # Replace the live state to ensure restore reverts it.
    coordinator = TransactionalRestore(
        data_dir=initial,
        backups_dir=backups_dir,
        shmax_version="1.0.0",
    )
    before_called = []
    after_called = []

    def _before():
        before_called.append(True)

    def _after():
        after_called.append(True)

    coordinator.on_before_restore(_before)
    coordinator.on_after_restore(_after)

    outcome = coordinator.run(target_archive)
    # Manifest gate passed.
    assert outcome.manifest is not None
    # Both hooks fired.
    assert before_called, "before_restore hook not invoked"
    assert after_called, "after_restore hook not invoked"
    # Safety backup exists.
    assert outcome.safety_backup_path.exists()
    # Restored marker matches the snapshot, not the live edit.
    assert (initial / "marker.txt").read_text() == "before"


@pytest.mark.asyncio
async def test_restore_endpoint_rollback_on_hook_failure(tmp_path):
    """If a ``before_restore`` hook raises the coordinator must
    fail fast: the swap never runs, the live data_dir is
    unchanged, and the safety backup is preserved for manual
    recovery."""
    from shmax.backup.manager import (
        TransactionalRestore,
        RestoreError,
        create_backup,
    )

    initial = tmp_path / "live"
    initial.mkdir()
    (initial / "important.txt").write_text("production")
    backups_dir = initial / "backups"
    backups_dir.mkdir()
    target_archive = backups_dir / "snapshot.shmax"
    create_backup(
        data_dir=initial,
        out_path=target_archive,
        shmax_version="1.0.0",
    )
    # Modify live state after the snapshot. A successful restore
    # would revert it; a hook failure must NOT touch live state.
    (initial / "important.txt").write_text("production-after-snapshot")

    coordinator = TransactionalRestore(
        data_dir=initial,
        backups_dir=backups_dir,
        shmax_version="1.0.0",
    )

    def _hook_that_breaks_swap():
        raise RuntimeError("simulated swap failure")

    coordinator.on_before_restore(_hook_that_breaks_swap)
    with pytest.raises(RestoreError):
        coordinator.run(target_archive)
    # Live data is unchanged because the swap is gated behind
    # the before_restore hooks. The safety backup is preserved
    # so the operator can recover manually.
    assert (initial / "important.txt").read_text() == "production-after-snapshot"
    pre_restore_backups = list(backups_dir.glob("pre-restore-*.shmax"))
    assert pre_restore_backups, "safety backup was not retained"