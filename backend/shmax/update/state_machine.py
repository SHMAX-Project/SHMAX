"""Update transaction state machine.

V1 stages:

    CHECK -> PRECHECK -> BACKUP -> FETCH -> STOP
        -> SWITCH_VERSION -> MIGRATE -> START
        -> HEALTHCHECK -> DONE

    On failure at any point: ROLLBACK -> FAILED.

The machine is pure — it owns no Docker socket. Hooks are
async callables the operator / installer wires in.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Optional


logger = logging.getLogger(__name__)


class UpdateStage(str, Enum):
    CHECK = "check"
    PRECHECK = "precheck"
    BACKUP = "backup"
    FETCH = "fetch"
    STOP = "stop"
    SWITCH_VERSION = "switch_version"
    MIGRATE = "migrate"
    START = "start"
    HEALTHCHECK = "healthcheck"
    DONE = "done"
    ROLLBACK = "rollback"
    FAILED = "failed"


STAGE_ORDER = [
    UpdateStage.CHECK,
    UpdateStage.PRECHECK,
    UpdateStage.BACKUP,
    UpdateStage.FETCH,
    UpdateStage.STOP,
    UpdateStage.SWITCH_VERSION,
    UpdateStage.MIGRATE,
    UpdateStage.START,
    UpdateStage.HEALTHCHECK,
    UpdateStage.DONE,
]


class UpdateStateError(Exception):
    pass


@dataclass(frozen=True)
class VersionInfo:
    current_version: str
    available_version: str
    channel: str = "stable"
    last_check_at: int = 0


@dataclass
class UpdateContext:
    """Side-effect hooks. All hooks are async."""

    fetch_version: Callable[[], Awaitable[VersionInfo]]
    precheck: Callable[[], Awaitable[None]]
    create_backup: Callable[[], Awaitable[str]]
    fetch_version_payload: Callable[[], Awaitable[bytes]]
    stop_app: Callable[[], Awaitable[None]]
    switch_version: Callable[[], Awaitable[None]]
    run_migrations: Callable[[], Awaitable[None]]
    start_app: Callable[[], Awaitable[None]]
    healthcheck: Callable[[], Awaitable[bool]]
    rollback: Callable[[], Awaitable[None]]


@dataclass
class RollbackRecord:
    previous_version: str = ""
    backup_id: str = ""
    schema_revision_before: str = ""
    schema_revision_after: str = ""


class UpdateTransaction:
    def __init__(
        self,
        context: UpdateContext,
        *,
        log: Optional[Callable[[str], None]] = None,
        channel: str = "stable",
    ) -> None:
        self._context = context
        self._log = log or (lambda msg: None)
        self._channel = channel
        self._current: Optional[UpdateStage] = None
        self.version: Optional[VersionInfo] = None
        self.rollback_record = RollbackRecord()

    @property
    def current(self) -> Optional[UpdateStage]:
        return self._current

    async def run(self) -> VersionInfo:
        try:
            await self._enter(UpdateStage.CHECK)
            self.version = await self._context.fetch_version()
            self._log(
                f"version: current={self.version.current_version} available={self.version.available_version}"
            )
            if self.version.current_version == self.version.available_version:
                self._log("nothing to update; skipping to DONE")
                self._current = UpdateStage.DONE
                return self.version
            await self._enter(UpdateStage.PRECHECK)
            await self._context.precheck()
            await self._enter(UpdateStage.BACKUP)
            backup_id = await self._context.create_backup()
            self.rollback_record.backup_id = backup_id
            self.rollback_record.previous_version = self.version.current_version
            self._log(f"backup {backup_id} created")
            await self._enter(UpdateStage.FETCH)
            await self._context.fetch_version_payload()
            await self._enter(UpdateStage.STOP)
            await self._context.stop_app()
            await self._enter(UpdateStage.SWITCH_VERSION)
            await self._context.switch_version()
            await self._enter(UpdateStage.MIGRATE)
            await self._context.run_migrations()
            await self._enter(UpdateStage.START)
            await self._context.start_app()
            await self._enter(UpdateStage.HEALTHCHECK)
            ok = await self._context.healthcheck()
            if not ok:
                raise UpdateStateError("healthcheck failed after start")
            self._current = UpdateStage.DONE
            return self.version
        except Exception as e:  # noqa: BLE001
            self._log(f"update failed at {self._current}: {e!r}; rolling back")
            try:
                await self._context.rollback()
            except Exception as rb:  # noqa: BLE001
                self._log(f"rollback failed: {rb!r}")
            self._current = UpdateStage.FAILED
            raise

    async def _enter(self, stage: UpdateStage) -> None:
        if stage not in STAGE_ORDER:
            raise UpdateStateError(f"not a forward stage: {stage}")
        if self._current is not None:
            idx = STAGE_ORDER.index(self._current) if self._current in STAGE_ORDER else -1
            target = STAGE_ORDER.index(stage)
            if target <= idx:
                raise UpdateStateError(
                    f"cannot transition {self._current} -> {stage}"
                )
        self._current = stage


__all__ = [
    "RollbackRecord",
    "STAGE_ORDER",
    "UpdateContext",
    "UpdateStage",
    "UpdateStateError",
    "UpdateTransaction",
    "VersionInfo",
]
