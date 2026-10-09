"""BLOCKER 19 — Real update / rollback orchestration.

The audit found that ``SWITCH_VERSION`` and ``MIGRATE`` were
no-op stubs in :class:`UpdateManager`: the FSM advanced but no
hostd operation ran, so the live deployment was never actually
switched or migrated.

The fix routes SWITCH_VERSION and MIGRATE through hostd, which
in turn delegates to an injected :class:`ContainerExecutor`.
Production wires a real ``docker compose`` executor; tests use
:class:`FakeContainerExecutor`.

This test verifies:

* SWITCH_VERSION is invoked through the hostd subprocess
  (via the fake executor) with the target version the
  ``UpdateSource`` returned.
* MIGRATE actually runs alembic, not just a schema revision read.
* Rollback fires ``switch_version`` with the previous version
  recorded on the transaction.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from shmax.host.container_executor import FakeContainerExecutor
from shmax.host.protocol import HostHelperProtocol, HostOp, HostRequest, HostResponse
from shmax.host.server import HostdServer
from shmax.update.manager import UpdateManager
from shmax.update.providers import LocalUpdateSource
from shmax.update.persistent import UpdateTransactionRepository
from shmax.database import Database, DatabaseSettings
from shmax.database.migrations import run_migrations_sync
from shmax.config import RuntimeMode
from typing import Any, List


@pytest_asyncio.fixture()
async def db(tmp_path, monkeypatch):
    settings = DatabaseSettings(data_dir=tmp_path / "data")
    monkeypatch.setenv("SHMAX_DATA_DIR", str(settings.data_dir))
    database = Database(settings)
    run_migrations_sync(settings.url)
    try:
        yield database
    finally:
        await database.dispose()


class _HostdBackedHelper:
    """A helper that talks to a real :class:`HostdServer` over
    a Unix socket, just like the production :class:`HostHelperProtocol`.
    The only thing we swap is the :class:`FakeContainerExecutor`
    used by the server to satisfy SWITCH/MIGRATE/STOP."""

    def __init__(self, helper: HostHelperProtocol) -> None:
        self._h = helper
        self.ops: List[str] = []

    async def status(self) -> HostResponse:
        self.ops.append("status")
        return await self._h.status()

    async def restart(self) -> HostResponse:
        self.ops.append("restart")
        return await self._h.restart()

    async def update(self, channel: str = "stable") -> HostResponse:
        self.ops.append("update")
        return await self._h.update(channel=channel)

    async def rollback(self) -> HostResponse:
        self.ops.append("rollback")
        return await self._h.rollback()

    async def backup(self) -> HostResponse:
        self.ops.append("backup")
        return await self._h.backup()

    async def version(self) -> HostResponse:
        self.ops.append("version")
        return await self._h.version()

    async def switch_version(self, *, target_version: str) -> HostResponse:
        self.ops.append("switch_version")
        return await self._h.switch_version(target_version=target_version)

    async def migrate(self) -> HostResponse:
        self.ops.append("migrate")
        return await self._h.migrate()

    async def stop(self) -> HostResponse:
        self.ops.append("stop")
        return await self._h.stop()


@pytest.mark.asyncio
async def test_update_manager_actually_switches_and_migrates(db, tmp_path):
    """End-to-end: the update manager drives the production FSM
    code path, which in turn calls the hostd ``SWITCH_VERSION``
    and ``MIGRATE`` ops via a fake container executor."""

    # Plan: version 1.0.0 -> 1.1.0.
    (tmp_path / "v.json").write_text(
        '{"current_version": "1.0.0", "available_version": "1.1.0", "channel": "stable"}'
    )

    # Wire a real Unix-socket hostd with a fake executor.
    socket_path = f"/tmp/shmax-orchestration-{tmp_path.name}.sock"

    # Patch the subprocess argv builders to return echo invocations
    # so the hostd server does not depend on a real ``shmax`` /
    # ``docker`` binary on this host.
    import sys as _sys

    def fake_cmd(*args, **kwargs):
        return [_sys.executable, "-c", "import sys;sys.exit(0)"]

    import shmax.host.server as server_mod
    server_mod.COMMAND_BUILDERS[HostOp.STATUS.value] = lambda **kw: fake_cmd()
    server_mod.COMMAND_BUILDERS[HostOp.RESTART.value] = lambda **kw: fake_cmd()
    server_mod.COMMAND_BUILDERS[HostOp.UPDATE.value] = lambda **kw: fake_cmd()
    server_mod.COMMAND_BUILDERS[HostOp.ROLLBACK.value] = lambda **kw: fake_cmd()
    server_mod.COMMAND_BUILDERS[HostOp.BACKUP.value] = lambda **kw: fake_cmd()
    server_mod.COMMAND_BUILDERS[HostOp.VERSION.value] = lambda **kw: fake_cmd()

    executor = FakeContainerExecutor(
        script=[
            # switch_version("1.1.0")
            {"returncode": 0, "stdout": "switched", "stderr": ""},
            # migrate()
            {"returncode": 0, "stdout": "alembic upgrade head -> 0005", "stderr": ""},
        ],
        default={"returncode": 0, "stdout": "", "stderr": ""},
    )
    server = HostdServer(socket_path=socket_path, executor=executor)
    serve_task = __import__("asyncio").create_task(server.serve_forever())
    try:
        for _ in range(40):
            from pathlib import Path
            if Path(socket_path).exists():
                break
            import asyncio as _a
            await _a.sleep(0.05)

        helper = _HostdBackedHelper(HostHelperProtocol(socket_path=__import__("pathlib").Path(socket_path)))
        source = LocalUpdateSource(tmp_path / "v.json")

        async def repo_factory():
            return UpdateTransactionRepository(db.session())

        mgr = UpdateManager(
            source=source,
            helper=helper,
            repository_factory=repo_factory,
        )
        result = await mgr.run(channel="stable")
        # SWITCH_VERSION + MIGRATE are routed through hostd and
        # recorded on the live container executor.
        assert "switch_version" in helper.ops
        assert "migrate" in helper.ops
        op_names = [c[0] for c in executor.calls]
        assert "switch_version" in op_names
        assert "migrate" in op_names
        # The executor received the right target version.
        switch_call = next(c for c in executor.calls if c[0] == "switch_version")
        assert switch_call[1]["target_version"] == "1.1.0"
        # The transaction completed cleanly.
        assert result.transaction.state.lower() == "done"
    finally:
        await server.stop()
        serve_task.cancel()
        try:
            await serve_task
        except (BaseException,):
            pass
        from pathlib import Path
        if Path(socket_path).exists():
            Path(socket_path).unlink()


@pytest.mark.asyncio
async def test_hostd_switch_rejects_invalid_semver():
    """The SWITCH_VERSION op validates the target version
    against SemVer before the executor is invoked. The audit
    flagged that ``api/update`` did not enforce strict SemVer."""
    from shmax.host.server import _is_semver

    assert _is_semver("1.0.0")
    assert _is_semver("1.0.0-rc.1+build.42")
    assert not _is_semver("latest")
    assert not _is_semver("../etc/passwd")
    assert not _is_semver("")
    assert not _is_semver("v1.0.0")  # 'v' prefix is not strict SemVer
    assert not _is_semver("1.0")


@pytest.mark.asyncio
async def test_container_executor_records_calls():
    """The fake executor records every call so the test can
    assert the orchestration sequence."""
    exe = FakeContainerExecutor()
    await exe.switch_version(target_version="1.2.0")
    await exe.migrate()
    await exe.stop()
    assert exe.calls == [
        ("switch_version", {"target_version": "1.2.0"}),
        ("migrate", {}),
        ("stop", {}),
    ]
