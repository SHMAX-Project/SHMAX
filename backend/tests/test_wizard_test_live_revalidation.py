"""P1 #5 — the wizard Test step must LIVE-revalidate Telegram
rights, not trust stale stored validation facts.

Pairing-time validation (validated_at, missing_permissions=[])
can be stale: rights get revoked or Topics get disabled after
pairing. The Test probe must run getChat + getChatMember against
the real adapter and fail on revoked rights.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shmax.database.models import Base
from shmax.api.app import _run_wizard_test_probes


class _FakeRuntimeAdapter:
    """Real-adapter-shaped stub the probe treats as live."""

    def is_real(self):
        return True

    def __init__(self, *, summary: dict, perms: dict, fail: Exception | None = None):
        self._summary = summary
        self._perms = perms
        self._fail = fail
        self.calls = 0

    async def get_chat_summary(self, chat_id: str) -> dict:
        self.calls += 1
        if self._fail is not None:
            raise self._fail
        return dict(self._summary)

    async def get_bot_permissions(self, chat_id: str) -> dict:
        return dict(self._perms)

    async def health(self):
        return {
            "connected": True, "authenticated": True, "bot_id": "424242",
            "polling": True, "transport": "bot_api",
        }


class _FakeRuntime:
    """Minimal runtime surface for _run_wizard_test_probes."""

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
    from shmax.database import Database, DatabaseSettings as _DbSettings
    from shmax.database.migrations import run_migrations_sync

    settings = _DbSettings(data_dir=tmp_path)
    run_migrations_sync(settings.url)
    db = Database(settings)
    yield db
    await db.dispose()


async def _seed_target(db, *, validated=True, missing="[]"):
    from datetime import datetime, timezone

    from sqlalchemy import select

    from shmax.database.models import TelegramAccount, TelegramTarget

    async with db.session() as s:
        s.add(TelegramAccount(label="primary", bot_id="424242", status="verified"))
        s.add(
            TelegramTarget(
                account_id=1,
                chat_id="-1001234567890",
                title="Stale Group",
                is_forum=True,
                chat_type="supergroup",
                bot_status="administrator",
                missing_permissions=missing,
                validated_at=datetime.now(timezone.utc) if validated else None,
                is_active=True,
            )
        )
        await s.commit()


GOOD_SUMMARY = {
    "id": "-1001234567890", "type": "supergroup",
    "title": "Stale Group", "is_forum": True,
}
GOOD_PERMS = {
    "status": "administrator", "can_send_messages": True,
    "can_manage_topics": True, "is_member": True,
}


@pytest.mark.asyncio
async def test_stale_stored_falls_back_to_pass_without_live_adapter(env):
    """No live adapter (offline wiring): stored validated facts are
    the honest best available — probe passes on them."""
    await _seed_target(env)
    runtime = _FakeRuntime(env, None)
    report = await _run_wizard_test_probes(runtime)
    check = {c["id"]: c for c in report["checks"]}["telegram.target_paired"]
    assert check["ok"] is True


@pytest.mark.asyncio
async def test_revived_rights_detected_live(env):
    """Topics disabled AFTER pairing (stored facts still claim
    validated): the live probe must FAIL the check."""
    await _seed_target(env)
    stale_summary = dict(GOOD_SUMMARY, is_forum=False)
    stale_perms = dict(GOOD_PERMS, can_manage_topics=False)
    adapter = _FakeRuntimeAdapter(summary=stale_summary, perms=stale_perms)
    runtime = _FakeRuntime(env, adapter)
    report = await _run_wizard_test_probes(runtime)
    check = {c["id"]: c for c in report["checks"]}["telegram.target_paired"]
    assert check["ok"] is False
    assert "Темы" in check["detail"]
    assert adapter.calls == 1, "probe must run a LIVE getChat"


@pytest.mark.asyncio
async def test_still_healthy_rights_pass_live(env):
    await _seed_target(env)
    adapter = _FakeRuntimeAdapter(summary=GOOD_SUMMARY, perms=GOOD_PERMS)
    runtime = _FakeRuntime(env, adapter)
    report = await _run_wizard_test_probes(runtime)
    check = {c["id"]: c for c in report["checks"]}["telegram.target_paired"]
    assert check["ok"] is True
    assert "live" in check["detail"]

    # Stored facts were refreshed with the live result.
    from sqlalchemy import select

    from shmax.database.models import TelegramTarget
    async with env.session() as s:
        row = (await s.execute(select(TelegramTarget))).scalars().first()
    import datetime as dt
    assert row.validated_at is not None
    stored = row.validated_at if row.validated_at.tzinfo else row.validated_at.replace(tzinfo=dt.timezone.utc)
    assert stored > dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)


@pytest.mark.asyncio
async def test_live_validation_error_fails_check(env):
    await _seed_target(env)
    adapter = _FakeRuntimeAdapter(
        summary=GOOD_SUMMARY, perms=GOOD_PERMS,
        fail=RuntimeError("bot was kicked"),
    )
    runtime = _FakeRuntime(env, adapter)
    report = await _run_wizard_test_probes(runtime)
    check = {c["id"]: c for c in report["checks"]}["telegram.target_paired"]
    assert check["ok"] is False
    assert "live validation failed" in check["detail"]
