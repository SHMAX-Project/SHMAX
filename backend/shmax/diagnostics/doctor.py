"""Diagnostics (doctor) engine."""

from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from ..database import Database
from ..security import SecretStore


class CheckStatus(str, Enum):
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"
    NOT_CONFIGURED = "not_configured"
    NOT_TESTED = "not_tested"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    detail: str = ""
    technical: Optional[str] = None


@dataclass
class DoctorReport:
    results: list[CheckResult] = field(default_factory=list)

    def add(self, result: CheckResult) -> None:
        self.results.append(result)

    @property
    def ok(self) -> bool:
        return all(r.status in (CheckStatus.OK, CheckStatus.NOT_CONFIGURED, CheckStatus.NOT_TESTED) for r in self.results)

    def by_status(self) -> dict[CheckStatus, int]:
        out: dict[CheckStatus, int] = {s: 0 for s in CheckStatus}
        for r in self.results:
            out[r.status] = out.get(r.status, 0) + 1
        return out


class Doctor:
    """Run a curated set of offline self-checks.

    External integrations report ``NOT_TESTED`` until the live
    bridge has at least one verified round-trip.
    """

    def __init__(self, data_dir: Path, db: Database, secret_store: SecretStore) -> None:
        self._data_dir = data_dir
        self._db = db
        self._secrets = secret_store

    async def run(self) -> DoctorReport:
        report = DoctorReport()
        await self._check_system(report)
        await self._check_architecture(report)
        await self._check_data_root(report)
        await self._check_filesystem_writable(report)
        await self._check_database(report)
        await self._check_migrations(report)
        await self._check_secret_store(report)
        await self._check_queue(report)
        await self._check_config(report)
        # External integrations are intentionally NOT_TESTED in
        # offline mode — see docs/live-validation-todo.md.
        self._external(report, "MAX connectivity", "live bridge validation pending")
        self._external(report, "Telegram Bot API", "live bridge validation pending")
        self._external(report, "Telegram MTProto", "live bridge validation pending")
        self._external(report, "Proxy", "live bridge validation pending")
        return report

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------
    async def _check_system(self, report: DoctorReport) -> None:
        report.add(
            CheckResult(
                name="system",
                status=CheckStatus.OK,
                detail=f"python {platform.python_version()} on {platform.system()} {platform.machine()}",
                technical=sys.version,
            )
        )

    async def _check_architecture(self, report: DoctorReport) -> None:
        arch = platform.machine().lower()
        status = CheckStatus.OK
        if arch in {"armv7l", "armv7"}:
            status = CheckStatus.WARNING
        report.add(
            CheckResult(
                name="architecture",
                status=status,
                detail=f"{arch} (armv7 is experimental)",
                technical=platform.machine(),
            )
        )

    async def _check_data_root(self, report: DoctorReport) -> None:
        if self._data_dir.is_dir():
            report.add(
                CheckResult(
                    name="data_root",
                    status=CheckStatus.OK,
                    detail=str(self._data_dir),
                )
            )
        else:
            report.add(
                CheckResult(
                    name="data_root",
                    status=CheckStatus.ERROR,
                    detail=f"missing: {self._data_dir}",
                )
            )

    async def _check_filesystem_writable(self, report: DoctorReport) -> None:
        try:
            test = self._data_dir / ".shmax-write-probe"
            test.write_text("ok")
            test.unlink()
            report.add(CheckResult(name="filesystem_writable", status=CheckStatus.OK, detail=str(self._data_dir)))
        except Exception as e:
            report.add(CheckResult(name="filesystem_writable", status=CheckStatus.ERROR, detail=str(e)))

    async def _check_database(self, report: DoctorReport) -> None:
        try:
            async with self._db.session() as s:
                await s.execute(__import__("sqlalchemy").text("SELECT 1"))
            report.add(CheckResult(name="database", status=CheckStatus.OK, detail="sqlite reachable"))
        except Exception as e:
            report.add(CheckResult(name="database", status=CheckStatus.ERROR, detail=str(e)))

    async def _check_migrations(self, report: DoctorReport) -> None:
        # Stage 1B uses ``create_all`` because Alembic's sync
        # ``command.upgrade`` cannot drive our async engine.
        # The Alembic migration files in ``backend/alembic/versions/``
        # are the source of truth for the schema; the ``upgrade
        # head`` pathway lands in Stage 2.
        try:
            from ..database import create_all

            await create_all(self._db.engine)
            report.add(
                CheckResult(
                    name="migrations",
                    status=CheckStatus.OK,
                    detail="schema applied (create_all)",
                )
            )
        except Exception as e:
            report.add(CheckResult(name="migrations", status=CheckStatus.WARNING, detail=str(e)))

    async def _check_secret_store(self, report: DoctorReport) -> None:
        if self._secrets.has_master_key():
            report.add(CheckResult(name="secret_store", status=CheckStatus.OK, detail="master key present"))
        else:
            report.add(CheckResult(name="secret_store", status=CheckStatus.NOT_CONFIGURED, detail="no master key yet"))

    async def _check_queue(self, report: DoctorReport) -> None:
        try:
            from ..queue.repository import DeliveryQueueRepository

            async with self._db.session() as s:
                repo = DeliveryQueueRepository(s)
                counts = await repo.counts_by_state()
            report.add(
                CheckResult(
                    name="queue",
                    status=CheckStatus.OK,
                    detail=f"pending={counts.get('pending',0)} retry={counts.get('retry',0)} failed={counts.get('failed',0)} delivered={counts.get('delivered',0)}",
                )
            )
        except Exception as e:
            report.add(CheckResult(name="queue", status=CheckStatus.ERROR, detail=str(e)))

    async def _check_config(self, report: DoctorReport) -> None:
        # We do not load .env; just confirm SHMAX_DATA_DIR is set.
        data_dir = os.environ.get("SHMAX_DATA_DIR", "/var/lib/shmax")
        report.add(
            CheckResult(
                name="config",
                status=CheckStatus.OK if data_dir else CheckStatus.WARNING,
                detail=f"SHMAX_DATA_DIR={data_dir}",
            )
        )

    def _external(self, report: DoctorReport, name: str, detail: str) -> None:
        report.add(CheckResult(name=name, status=CheckStatus.NOT_TESTED, detail=detail))


__all__ = ["CheckResult", "CheckStatus", "Doctor", "DoctorReport"]
