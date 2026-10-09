"""Update state machine tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import pytest

from shmax.update.state_machine import (
    UpdateContext,
    UpdateStage,
    UpdateStateError,
    UpdateTransaction,
    VersionInfo,
)


@dataclass
class _Recorder:
    events: list[str] = field(default_factory=list)
    fail_at: str | None = None
    hc_ok: bool = True


async def _recording_context(rec: _Recorder) -> UpdateContext:
    async def _noop(): return None
    async def _hc(): return rec.hc_ok
    async def _fetch(): 
        rec.events.append("fetch")
        return VersionInfo("0.1.0", "0.2.0")
    async def _backup():
        rec.events.append("backup")
        return "/tmp/x.shmax"
    async def _pull():
        rec.events.append("pull")
        if rec.fail_at == "pull":
            raise RuntimeError("pull failed")
    async def _stop():
        rec.events.append("stop")
        if rec.fail_at == "stop":
            raise RuntimeError("stop failed")
    async def _start():
        rec.events.append("start")
        if rec.fail_at == "start":
            raise RuntimeError("start failed")
    async def _mig():
        rec.events.append("migrate")
        if rec.fail_at == "migrate":
            raise RuntimeError("migrate failed")
    async def _rb():
        rec.events.append("rollback")

    return UpdateContext(
        fetch_version=_fetch,
        precheck=_noop,
        create_backup=_backup,
        fetch_version_payload=_pull,
        stop_app=_stop,
        switch_version=_noop,
        run_migrations=_mig,
        start_app=_start,
        healthcheck=_hc,
        rollback=_rb,
    )


@pytest.mark.asyncio
async def test_happy_path():
    rec = _Recorder()
    ctx = await _recording_context(rec)
    tx = UpdateTransaction(ctx)
    info = await tx.run()
    assert info.available_version == "0.2.0"
    assert tx.current == UpdateStage.DONE
    assert rec.events == ["fetch", "backup", "pull", "stop", "migrate", "start"]


@pytest.mark.asyncio
async def test_no_op_when_versions_match():
    rec = _Recorder()
    ctx = await _recording_context(rec)

    async def _no_op_fetch():
        rec.events.append("fetch")
        return VersionInfo("0.1.0", "0.1.0")
    ctx.fetch_version = _no_op_fetch
    tx = UpdateTransaction(ctx)
    await tx.run()
    assert rec.events == ["fetch"]


@pytest.mark.asyncio
async def test_pull_failure_triggers_rollback():
    rec = _Recorder(fail_at="pull")
    ctx = await _recording_context(rec)
    tx = UpdateTransaction(ctx)
    with pytest.raises(RuntimeError):
        await tx.run()
    assert tx.current == UpdateStage.FAILED
    assert "rollback" in rec.events


@pytest.mark.asyncio
async def test_healthcheck_failure_triggers_rollback():
    rec = _Recorder()
    rec.hc_ok = False
    ctx = await _recording_context(rec)
    tx = UpdateTransaction(ctx)
    with pytest.raises(UpdateStateError):
        await tx.run()
    assert tx.current == UpdateStage.FAILED
    assert "rollback" in rec.events


async def _async(value):
    return value
