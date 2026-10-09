"""Production update manager.

Bridges the pure :class:`UpdateTransaction` state machine to
the persistent :class:`UpdateTransactionRepository` and the
real ``shmax-hostd`` daemon. Every stage transition is
persisted so an interrupted update can be inspected after a
restart.

The manager is in charge of:

1. ``UpdateContext`` — wiring FSM hooks to the hostd-backed
   implementation. Each hook calls the daemon over the Unix
   socket (or the test double) and translates the response
   into an exception / return value the FSM can act on.
2. Persisting the FSM state to ``update_transactions`` at
   each stage transition.
3. Marking the transaction ``FAILED`` if the FSM catches an
   exception; the manager also forwards ``healthcheck``
   failures with the hostd's exact diagnostic.

For unit tests an ``UpdateSource`` and ``InProcessHostHelper``
suffice; for production the operator passes the live
:class:`HostHelperProtocol` (which talks to ``/run/shmax/hostd.sock``).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from ..host.protocol import HostHelperProtocol
from .persistent import UpdateTransactionRecord, UpdateTransactionRepository
from .providers import UpdateSource
from .state_machine import (
    RollbackRecord,
    STAGE_ORDER,
    UpdateContext,
    UpdateStage,
    UpdateStateError,
    UpdateTransaction,
    VersionInfo,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UpdateManagerResult:
    version: VersionInfo
    transaction: UpdateTransactionRecord


class UpdateManager:
    def __init__(
        self,
        *,
        source: UpdateSource,
        helper: HostHelperProtocol,
        repository_factory: Callable[[], Awaitable[UpdateTransactionRepository]],
        schema_reader: Callable[[], str] = lambda: "",
    ) -> None:
        self._source = source
        self._helper = helper
        self._repository_factory = repository_factory
        self._schema_reader = schema_reader

    async def run(self, channel: str = "stable") -> UpdateManagerResult:
        version = await self._source.fetch()
        repo = await self._repository_factory()
        rec = await repo.start(
            from_version=version.current_version,
            to_version=version.available_version,
            old_schema=self._schema_reader(),
        )
        context = self._build_context(rec.transaction_id, version, repo)
        txn = UpdateTransaction(context=context, channel=channel)
        try:
            await txn.run()
        except Exception as e:
            await self._fail(rec.transaction_id, repo, repr(e))
            raise
        rec = await repo.complete(rec.transaction_id)
        return UpdateManagerResult(version=version, transaction=rec)

    async def _fail(
        self,
        transaction_id: str,
        repo: UpdateTransactionRepository,
        error: str,
    ) -> None:
        try:
            await repo.fail(transaction_id, error=error)
        except Exception:  # noqa: BLE001
            logger.exception(
                "could not persist FAILED for %s", transaction_id
            )

    def _build_context(
        self,
        transaction_id: str,
        version: VersionInfo,
        repo: UpdateTransactionRepository,
    ) -> UpdateContext:
        async def advance(stage: UpdateStage) -> None:
            await repo.advance(transaction_id, new_state=stage)

        async def fetch_version() -> VersionInfo:
            await advance(UpdateStage.CHECK)
            return version

        async def precheck() -> None:
            await advance(UpdateStage.PRECHECK)
            r = await self._helper.version()
            if not r.ok:
                raise UpdateStateError(
                    f"hostd precheck failed: {r.detail or 'unknown'}"
                )

        async def create_backup() -> str:
            await advance(UpdateStage.BACKUP)
            r = await self._helper.backup()
            if not r.ok:
                raise UpdateStateError(
                    f"hostd backup failed: {r.detail or 'unknown'}"
                )
            return str(r.data.get("backup_id") or "")

        async def fetch_version_payload() -> None:
            await advance(UpdateStage.FETCH)
            r = await self._helper.update(channel=version.channel)
            if not r.ok:
                raise UpdateStateError(
                    f"hostd update failed: {r.detail or 'unknown'}"
                )

        async def stop_app() -> None:
            await advance(UpdateStage.STOP)
            r = await self._helper.restart()
            if not r.ok:
                raise UpdateStateError(
                    f"hostd restart failed: {r.detail or 'unknown'}"
                )

        async def switch_version() -> None:
            await advance(UpdateStage.SWITCH_VERSION)
            # The audit flagged the previous no-op implementation.
            # We must actually flip the image reference via the
            # hostd ``SWITCH_VERSION`` op so the next ``up`` picks
            # up the new tag.
            r = await self._helper.switch_version(
                target_version=version.available_version
            )
            if not r.ok:
                raise UpdateStateError(
                    f"hostd switch_version failed: {r.detail or 'unknown'}"
                )

        async def run_migrations() -> None:
            await advance(UpdateStage.MIGRATE)
            # Production path: alembic upgrade head runs against
            # the live database via the hostd ``MIGRATE`` op. The
            # previous implementation only read the schema revision
            # — that did not actually migrate anything.
            r = await self._helper.migrate()
            if not r.ok:
                raise UpdateStateError(
                    f"hostd migrate failed: {r.detail or 'unknown'}"
                )
            new_schema = self._schema_reader()
            await repo.advance(
                transaction_id,
                new_state=UpdateStage.MIGRATE,
                new_schema=new_schema,
            )

        async def start_app() -> None:
            await advance(UpdateStage.START)
            r = await self._helper.restart()
            if not r.ok:
                raise UpdateStateError(
                    f"hostd start failed: {r.detail or 'unknown'}"
                )

        async def healthcheck() -> bool:
            await advance(UpdateStage.HEALTHCHECK)
            r = await self._helper.status()
            return r.ok

        async def rollback() -> None:
            await advance(UpdateStage.ROLLBACK)
            r = await self._helper.rollback()
            if not r.ok:
                raise UpdateStateError(
                    f"hostd rollback failed: {r.detail or 'unknown'}"
                )

        return UpdateContext(
            fetch_version=fetch_version,
            precheck=precheck,
            create_backup=create_backup,
            fetch_version_payload=fetch_version_payload,
            stop_app=stop_app,
            switch_version=switch_version,
            run_migrations=run_migrations,
            start_app=start_app,
            healthcheck=healthcheck,
            rollback=rollback,
        )


__all__ = [
    "UpdateManager",
    "UpdateManagerResult",
]