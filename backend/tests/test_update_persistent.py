"""Persistent update transactions + version validation."""

from __future__ import annotations

import pytest
import pytest_asyncio

from shmax.database import Database, DatabaseSettings
from shmax.database.migrations import run_migrations_sync
from shmax.host.server import _validate_version
from shmax.update.persistent import UpdateTransactionRepository
from shmax.update.state_machine import UpdateStage
from shmax.database.models import UpdateTransaction
from shmax.config import RuntimeMode


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
async def test_start_records_initial_row(db):
    async with db.session() as s:
        repo = UpdateTransactionRepository(s)
        rec = await repo.start(
            from_version="1.0.0", to_version="1.1.0", old_schema="0003_admin_sessions"
        )
    assert rec.state == UpdateStage.CHECK.value
    assert rec.from_version == "1.0.0"
    assert rec.to_version == "1.1.0"
    assert rec.old_schema == "0003_admin_sessions"


@pytest.mark.asyncio
async def test_advance_persists_each_stage(db):
    async with db.session() as s:
        repo = UpdateTransactionRepository(s)
        rec = await repo.start(from_version="1.0.0", to_version="1.1.0")
    async with db.session() as s:
        repo = UpdateTransactionRepository(s)
        rec = await repo.advance(
            rec.transaction_id, new_state=UpdateStage.PRECHECK
        )
        assert rec.state == UpdateStage.PRECHECK.value
    async with db.session() as s:
        repo = UpdateTransactionRepository(s)
        rec = await repo.advance(
            rec.transaction_id,
            new_state=UpdateStage.BACKUP,
            backup_id="backup-123",
        )
        assert rec.state == UpdateStage.BACKUP.value
        assert rec.backup_id == "backup-123"
    async with db.session() as s:
        repo = UpdateTransactionRepository(s)
        rec = await repo.advance(
            rec.transaction_id,
            new_state=UpdateStage.MIGRATE,
            new_schema="0004_update_transactions",
        )
        assert rec.new_schema == "0004_update_transactions"


@pytest.mark.asyncio
async def test_fail_records_error(db):
    async with db.session() as s:
        repo = UpdateTransactionRepository(s)
        rec = await repo.start(from_version="1.0.0", to_version="1.1.0")
    async with db.session() as s:
        repo = UpdateTransactionRepository(s)
        rec = await repo.fail(rec.transaction_id, error="healthcheck timeout")
    assert rec.state == UpdateStage.FAILED.value
    assert "healthcheck timeout" in rec.error


@pytest.mark.asyncio
async def test_complete_moves_to_done(db):
    async with db.session() as s:
        repo = UpdateTransactionRepository(s)
        rec = await repo.start(from_version="1.0.0", to_version="1.1.0")
    async with db.session() as s:
        repo = UpdateTransactionRepository(s)
        rec = await repo.complete(rec.transaction_id)
    assert rec.state == UpdateStage.DONE.value


@pytest.mark.asyncio
async def test_latest_returns_most_recent(db):
    async with db.session() as s:
        repo = UpdateTransactionRepository(s)
        rec1 = await repo.start(from_version="1.0.0", to_version="1.0.1")
        rec2 = await repo.start(from_version="1.0.1", to_version="1.1.0")
    async with db.session() as s:
        # Bump rec1 to an earlier started_at so the ordering
        # is unambiguous even when both rows were created
        # within the same second.
        from sqlalchemy import select as _sa
        stmt = _sa(UpdateTransaction).where(
            UpdateTransaction.transaction_id == rec1.transaction_id
        )
        row = (await s.execute(stmt)).scalar_one()
        row.started_at = row.started_at - 100
        await s.commit()
    async with db.session() as s:
        repo = UpdateTransactionRepository(s)
        latest = await repo.latest()
    assert latest is not None
    assert latest.transaction_id == rec2.transaction_id


def test_version_validator_accepts_semver():
    assert _validate_version("1.2.3") == "1.2.3"
    assert _validate_version("0.0.0") == "0.0.0"
    assert _validate_version("1.0.0-dev.1") == "1.0.0-dev.1"
    assert _validate_version("1.0.0-beta+build.42") == "1.0.0-beta+build.42"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "v1.0.0",  # leading v not allowed
        "../../etc/passwd",  # traversal
        "1.2.3; rm -rf",  # shell metacharacter
        "1.2.3 4",  # space
        "1.2",  # incomplete
        "1.2.3.4",  # too many segments
        "abc",
    ],
)
def test_version_validator_rejects_dangerous_input(bad: str):
    with pytest.raises(ValueError):
        _validate_version(bad)
