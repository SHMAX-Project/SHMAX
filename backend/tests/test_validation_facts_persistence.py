"""NEW (surgical review) — Test validation facts must be PERSISTED.

Pins:

* a healthy group pair → validated facts in the DB;
* a later revoked-permission live check → Test FAILS **and** the DB
  target becomes invalid (missing_permissions persisted,
  validated_at cleared);
* readiness/Finish consume that authoritative invalid state.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shmax.database import Database, DatabaseSettings
from shmax.database.migrations import run_migrations_sync
from shmax.database.models import Base, TelegramAccount, TelegramTarget


class _LiveStub:
    """Real-adapter-shaped stub with MUTABLE facts (permissions can
    be revoked between calls)."""

    def is_real(self):
        return True

    def __init__(self):
        self.summary = {
            "id": "-1001234567890", "type": "supergroup",
            "title": "Group", "is_forum": True,
        }
        self.perms = {
            "status": "administrator", "can_send_messages": True,
            "can_manage_topics": True, "is_member": True,
        }

    async def get_chat_summary(self, chat_id: str) -> dict:
        return dict(self.summary)

    async def get_bot_permissions(self, chat_id: str) -> dict:
        return dict(self.perms)

    async def health(self):
        return {
            "connected": True, "authenticated": True, "bot_id": "424242",
            "polling": True, "transport": "bot_api",
        }


class _FakeRuntime:
    def __init__(self, db, adapter):
        self.db = db
        self._tg = adapter
        self.telegram_adapter = adapter
        self._max = None
        self._supervisors = {"telegram_listener": None}
        self._worker_tasks = [type("T", (), {"done": lambda self: False})()]

    async def integration_snapshot(self):
        return {}


@pytest_asyncio.fixture
async def env(tmp_path):
    settings = DatabaseSettings(data_dir=tmp_path)
    run_migrations_sync(settings.url)
    db = Database(settings)

    async with db.session() as s:
        s.add(TelegramAccount(label="primary", bot_id="424242", status="verified"))
        await s.commit()
    yield db
    await db.dispose()


async def _seed_target(db):
    import datetime as dt

    async with db.session() as s:
        s.add(
            TelegramTarget(
                account_id=1, chat_id="-1001234567890", title="Group",
                is_forum=True, chat_type="supergroup",
                bot_status="administrator", missing_permissions="[]",
                validated_at=dt.datetime.now(dt.timezone.utc),
                cached_at=dt.datetime.now(dt.timezone.utc),
                is_active=True,
            )
        )
        await s.commit()


async def _load_target(db):
    async with db.session() as s:
        row = (await s.execute(select(TelegramTarget))).scalars().first()
        return {
            "validated_at": row.validated_at,
            "missing_permissions": row.missing_permissions,
        }


def test_healthy_pair_persists_validated(env):
    from shmax.api.app import _run_wizard_test_probes

    stub = _LiveStub()
    runtime = _FakeRuntime(env, stub)
    asyncio = __import__("asyncio")
    asyncio.run(_seed_target(env))
    report = asyncio.run(_run_wizard_test_probes(runtime))
    check = {c["id"]: c for c in report["checks"]}["telegram.target_paired"]
    assert check["ok"] is True
    facts = asyncio.run(_load_target(env))
    assert facts["validated_at"] is not None
    assert (facts["missing_permissions"] or "[]") == "[]"


def test_revoked_rights_persist_invalid_state_and_reject_finish(env):
    """Revoke manage_topics → Test fails AND the DB target is
    explicitly invalidated (not left with stale positive facts)."""
    from shmax.api.app import _run_wizard_test_probes

    stub = _LiveStub()
    runtime = _FakeRuntime(env, stub)
    asyncio = __import__("asyncio")
    asyncio.run(_seed_target(env))

    # Pass once (fresh positive facts).
    asyncio.run(_run_wizard_test_probes(runtime))

    # Revoke the topics right.
    stub.perms["can_manage_topics"] = False
    report = asyncio.run(_run_wizard_test_probes(runtime))
    check = {c["id"]: c for c in report["checks"]}["telegram.target_paired"]
    assert check["ok"] is False

    facts = asyncio.run(_load_target(env))
    # The invalid state IS persisted — validated_at cleared, the
    # missing permission recorded.
    assert facts["validated_at"] is None, (
        "stale positive validated_at must be cleared on revoked rights"
    )
    assert "управлять темами" in (facts["missing_permissions"] or "")

    # Readiness consumes the authoritative invalid state.
    from shmax.wizard_readiness import wizard_readiness

    class _SnapRuntime(_FakeRuntime):
        async def integration_snapshot(self):
            return {
                "max": {"state": "HEALTHY"},
                "telegram": {"state": "HEALTHY"},
                "bridge": {"state": "HEALTHY"},
            }

    readiness = asyncio.run(wizard_readiness(_SnapRuntime(env, stub)))
    # readiness maps step -> StepStatus dataclass.
    tg_group = readiness["telegram_group"]
    assert tg_group.ready is False
    assert "управлять темами" in tg_group.reason


def test_failed_live_check_persists_invalid_too(env):
    """A getChat exception must ALSO invalidate the stored facts —
    without a live confirmation we do not know the group is valid."""
    from shmax.api.app import _run_wizard_test_probes

    class _Broken(_LiveStub):
        async def get_chat_summary(self, chat_id: str) -> dict:
            raise RuntimeError("bot was kicked")

    stub = _Broken()
    runtime = _FakeRuntime(env, stub)
    asyncio = __import__("asyncio")
    asyncio.run(_seed_target(env))
    report = asyncio.run(_run_wizard_test_probes(runtime))
    check = {c["id"]: c for c in report["checks"]}["telegram.target_paired"]
    assert check["ok"] is False
    facts = asyncio.run(_load_target(env))
    assert facts["validated_at"] is None
    assert "live validation failed" in (facts["missing_permissions"] or "")
