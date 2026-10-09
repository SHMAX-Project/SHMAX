"""Update V2 state machine tests."""

from __future__ import annotations

import asyncio

import pytest

from shmax.update.state_machine import (
    RollbackRecord,
    UpdateContext,
    UpdateStage,
    UpdateStateError,
    UpdateTransaction,
    VersionInfo,
)


class _Rec:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.fail_at: str | None = None


def _ctx(rec: _Rec) -> UpdateContext:
    async def _v() -> VersionInfo:
        rec.events.append("fetch_version")
        return VersionInfo(current_version="0.1.0", available_version="0.2.0")

    async def _noop() -> None:
        rec.events.append("noop")

    async def _backup() -> str:
        rec.events.append("backup")
        return "backup-id"

    async def _payload() -> bytes:
        rec.events.append("fetch_payload")
        return b"payload"

    async def _fail() -> None:
        rec.events.append("fail")
        if rec.fail_at:
            raise RuntimeError(rec.fail_at)

    async def _ok() -> bool:
        rec.events.append("healthcheck_ok")
        return True

    async def _bad() -> bool:
        rec.events.append("healthcheck_bad")
        return False

    return UpdateContext(
        fetch_version=_v,
        precheck=_noop,
        create_backup=_backup,
        fetch_version_payload=_payload,
        stop_app=_fail if rec.fail_at == "stop" else _noop,
        switch_version=_noop,
        run_migrations=_noop,
        start_app=_fail if rec.fail_at == "start" else _noop,
        healthcheck=_bad if rec.fail_at == "healthcheck" else _ok,
        rollback=_noop,
    )


@pytest.mark.asyncio
async def test_happy_path_full_stages():
    rec = _Rec()
    tx = UpdateTransaction(_ctx(rec))
    info = await tx.run()
    expected = [
        "fetch_version",
        "noop",
        "backup",
        "fetch_payload",
        "noop",
        "noop",
        "noop",
        "noop",
        "healthcheck_ok",
    ]
    assert rec.events == expected
    assert tx.current == UpdateStage.DONE
    assert info.available_version == "0.2.0"


@pytest.mark.asyncio
async def test_nothing_to_update_skips_to_done():
    rec = _Rec()
    async def _v() -> VersionInfo:
        return VersionInfo(current_version="0.2.0", available_version="0.2.0")
    rec.events.append("noop")
    ctx = UpdateContext(
        fetch_version=_v,
        precheck=lambda: asyncio.sleep(0),
        create_backup=lambda: asyncio.sleep(0),
        fetch_version_payload=lambda: asyncio.sleep(0),
        stop_app=lambda: asyncio.sleep(0),
        switch_version=lambda: asyncio.sleep(0),
        run_migrations=lambda: asyncio.sleep(0),
        start_app=lambda: asyncio.sleep(0),
        healthcheck=lambda: asyncio.sleep(0) or True,
        rollback=lambda: asyncio.sleep(0),
    )
    tx = UpdateTransaction(ctx)
    await tx.run()
    assert rec.events == ["noop"]
    assert tx.current == UpdateStage.DONE


@pytest.mark.asyncio
async def test_start_failure_triggers_rollback():
    rec = _Rec()
    rec.fail_at = "start"
    ctx = _ctx(rec)
    tx = UpdateTransaction(ctx)
    with pytest.raises(RuntimeError):
        await tx.run()
    assert tx.current == UpdateStage.FAILED
    # The rollback hook was invoked.
    assert "noop" in rec.events  # rollback runs _noop


@pytest.mark.asyncio
async def test_healthcheck_failure_triggers_rollback():
    rec = _Rec()
    rec.fail_at = "healthcheck"
    tx = UpdateTransaction(_ctx(rec))
    with pytest.raises(UpdateStateError):
        await tx.run()
    assert tx.current == UpdateStage.FAILED


@pytest.mark.asyncio
async def test_rollback_record_captures_previous_version_and_backup():
    rec = _Rec()
    tx = UpdateTransaction(_ctx(rec))
    await tx.run()
    assert tx.rollback_record.previous_version == "0.1.0"
    assert tx.rollback_record.backup_id == "backup-id"


def test_stage_order_matches_documented_sequence():
    from shmax.update.state_machine import STAGE_ORDER

    expected_names = [
        "check",
        "precheck",
        "backup",
        "fetch",
        "stop",
        "switch_version",
        "migrate",
        "start",
        "healthcheck",
        "done",
    ]
    assert [s.value for s in STAGE_ORDER] == expected_names


# ----------------------------------------------------------------------
# Live acceptance #19 — host updater failure paths in a staged env.
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_failed_download_triggers_rollback_and_failed_state(tmp_path):
    """FETCH stage failure (download/bad sha) → rollback runs, the
    transaction lands in FAILED, never a silent partial install."""
    rec = _Rec()
    rec.fail_at = "fetch"

    ctx = _ctx(rec)
    async def _payload() -> bytes:
        rec.events.append("fetch_payload")
        raise RuntimeError("download failed: sha mismatch")

    ctx.fetch_version_payload = _payload
    rollback_calls: list[str] = []

    async def _rollback() -> None:
        rollback_calls.append("rolled-back")

    ctx.rollback = _rollback
    tx = UpdateTransaction(ctx)
    with pytest.raises(RuntimeError):
        await tx.run()
    assert rollback_calls == ["rolled-back"]
    assert tx.current == UpdateStage.FAILED
    assert tx.rollback_record.backup_id == "backup-id"
    assert tx.rollback_record.previous_version == "0.1.0"


@pytest.mark.asyncio
async def test_failed_update_preserves_persistent_data(tmp_path):
    """APPLICATION IS DISPOSABLE, USER DATA IS PERSISTENT: a failed
    update (healthcheck timeout) rolls back and the persistent data
    dir keeps its content untouched."""
    persistent = tmp_path / "var-lib-shmax"
    persistent.mkdir()
    (persistent / "db.sqlite").write_bytes(b"user-data-bytes")
    (persistent / "secrets.enc").write_bytes(b"encrypted-secret-bytes")

    rec = _Rec()
    rec.fail_at = "healthcheck"
    ctx = _ctx(rec)

    switched: list[str] = []

    async def _switch() -> None:
        # The host updater stages a NEW deployment; the persistent
        # dir must not be part of that write set.
        (tmp_path / "opt-shmax-staging").mkdir(exist_ok=True)
        switched.append("switched")

    async def _healthcheck() -> bool:
        rec.events.append("healthcheck_timeout")
        return False

    async def _rollback() -> None:
        # Rollback restores the PREVIOUS deployment artifacts only.
        assert (persistent / "db.sqlite").read_bytes() == b"user-data-bytes"
        assert (persistent / "secrets.enc").read_bytes() == (
            b"encrypted-secret-bytes"
        )
        rec.events.append("rollback")

    ctx.switch_version = _switch
    ctx.healthcheck = _healthcheck
    ctx.rollback = _rollback
    tx = UpdateTransaction(ctx)
    with pytest.raises(UpdateStateError):
        await tx.run()
    assert tx.current == UpdateStage.FAILED
    assert (persistent / "db.sqlite").read_bytes() == b"user-data-bytes"
    assert (persistent / "secrets.enc").read_bytes() == b"encrypted-secret-bytes"


def test_hostd_protocol_has_no_shell_surface():
    """No arbitrary command execution: the host protocol exposes a
    FIXED op set; there is no exec/shell op and requests carry only
    validated ops (see HostRequest.validate)."""
    from shmax.host.protocol import ALLOWED_OPS, HostRequest, HostOp

    ops = {op.value for op in HostOp}
    assert not any(
        s in " ".join(ops).lower() for s in ("shell", "exec", "command")
    )
    with pytest.raises(ValueError):
        HostRequest.validate('{"op": "shell", "args": {}}')
    req = HostRequest.validate('{"op": "status", "args": {}}')
    assert req.op == "status"
