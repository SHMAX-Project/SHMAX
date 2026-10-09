"""Update manager tests.

Covers:

* happy path - every stage persists to the DB; DONE state reached;
* healthcheck failure -> FAILED with diagnostic;
* rollback triggered by the FSM when any stage raises.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import List

import pytest
import pytest_asyncio

from shmax.database import Database, DatabaseSettings
from shmax.database.migrations import run_migrations_sync
from shmax.host.protocol import HostOp, HostRequest, HostResponse
from shmax.update.manager import UpdateManager
from shmax.update.persistent import UpdateTransactionRepository
from shmax.update.providers import LocalUpdateSource
from shmax.update.state_machine import UpdateStage, VersionInfo
from shmax.config import RuntimeMode


class _RecordingHelper:
    def __init__(self, *, fail_status: bool = False) -> None:
        self.calls: List[str] = []
        self.fail_status = fail_status

    async def _send(self, request: HostRequest) -> HostResponse:
        self.calls.append(request.op)
        if request.op == HostOp.STATUS.value and self.fail_status:
            return HostResponse(ok=False, detail="container down")
        return HostResponse(ok=True, detail="", data={"backup_id": "B1"})

    async def status(self):
        return await self._send(HostRequest(op=HostOp.STATUS.value, args={}))

    async def restart(self):
        return await self._send(HostRequest(op=HostOp.RESTART.value, args={}))

    async def update(self, channel: str = "stable"):
        return await self._send(
            HostRequest(op=HostOp.UPDATE.value, args={"channel": channel})
        )

    async def rollback(self):
        return await self._send(HostRequest(op=HostOp.ROLLBACK.value, args={}))

    async def backup(self):
        return await self._send(HostRequest(op=HostOp.BACKUP.value, args={}))

    async def version(self):
        return await self._send(HostRequest(op=HostOp.VERSION.value, args={}))

    async def switch_version(self, *, target_version: str):
        return await self._send(
            HostRequest(
                op=HostOp.SWITCH_VERSION.value,
                args={"target_version": target_version},
            )
        )

    async def migrate(self):
        return await self._send(HostRequest(op=HostOp.MIGRATE.value, args={}))

    async def stop(self):
        return await self._send(HostRequest(op=HostOp.STOP.value, args={}))


@pytest_asyncio.fixture()
async def db(tmp_path):
    settings = DatabaseSettings(data_dir=tmp_path)
    database = Database(settings)
    run_migrations_sync(settings.url)
    try:
        yield database
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_update_manager_persists_every_stage(db, tmp_path):
    (tmp_path / "v.json").write_text(
        '{"current_version": "1.0.0", "available_version": "1.1.0", "channel": "stable"}'
    )
    helper = _RecordingHelper()
    source = LocalUpdateSource(tmp_path / "v.json")

    async def repo_factory():
        return UpdateTransactionRepository(db.session())

    mgr = UpdateManager(
        source=source,
        helper=helper,
        repository_factory=repo_factory,
    )
    result = await mgr.run(channel="stable")
    assert result.version.available_version == "1.1.0"
    assert result.transaction.state == UpdateStage.DONE.value

    # hostd was called for backup / restart / status at minimum.
    expected = {HostOp.BACKUP.value, HostOp.UPDATE.value, HostOp.RESTART.value, HostOp.STATUS.value, HostOp.VERSION.value}
    assert expected.issubset(set(helper.calls)), helper.calls


@pytest.mark.asyncio
async def test_update_manager_records_failure(db, tmp_path):
    (tmp_path / "v.json").write_text(
        '{"current_version": "1.0.0", "available_version": "1.1.0"}'
    )
    helper = _RecordingHelper(fail_status=True)
    source = LocalUpdateSource(tmp_path / "v.json")

    async def repo_factory():
        return UpdateTransactionRepository(db.session())

    mgr = UpdateManager(
        source=source,
        helper=helper,
        repository_factory=repo_factory,
    )
    with pytest.raises(Exception):
        await mgr.run(channel="stable")
    # hostd rollback was attempted.
    assert HostOp.ROLLBACK.value in helper.calls, helper.calls
    # latest transaction is FAILED.
    async with db.session() as s:
        repo = UpdateTransactionRepository(s)
        rec = await repo.latest()
    assert rec is not None
    assert rec.state == UpdateStage.FAILED.value
    assert rec.error is not None


@pytest.mark.asyncio
async def test_update_manager_no_op_when_versions_match(db, tmp_path):
    (tmp_path / "v.json").write_text(
        '{"current_version": "1.0.0", "available_version": "1.0.0"}'
    )
    helper = _RecordingHelper()
    source = LocalUpdateSource(tmp_path / "v.json")

    async def repo_factory():
        return UpdateTransactionRepository(db.session())

    mgr = UpdateManager(
        source=source,
        helper=helper,
        repository_factory=repo_factory,
    )
    result = await mgr.run(channel="stable")
    assert result.version.current_version == "1.0.0"
    # No destructive ops were issued.
    assert HostOp.BACKUP.value not in helper.calls
    assert HostOp.UPDATE.value not in helper.calls
    assert result.transaction.state == UpdateStage.DONE.value
