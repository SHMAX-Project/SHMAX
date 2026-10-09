"""Audit P0 #10: Test step must actively verify the runtime.

Exercises ``_run_wizard_test_probes`` end-to-end against a real
runtime backed by the real database + Fake adapters (network
stub, not Fake-named).
"""

from __future__ import annotations

from pathlib import Path
import tempfile

import pytest
import pytest_asyncio

from shmax.adapters.max.fake import FakeMaxAdapter
from shmax.adapters.telegram.fake import FakeTelegramAdapter
from shmax.database import Database, DatabaseSettings
from shmax.database.migrations import run_migrations_sync
from shmax.database.models import (
    AdminUser,
    MaxAccount,
    Route,
    TelegramAccount,
    TelegramTarget,
    TelegramUser,
)
from shmax.security import SecretStore
from shmax.config import RuntimeMode
from shmax.runtime import AppRuntime, RuntimeConfig
from shmax.core.paths import PathLayout


@pytest_asyncio.fixture
async def runtime(tmp_path):
    data_dir = Path(tmp_path) / "shmax"
    settings = DatabaseSettings(data_dir=data_dir)
    run_migrations_sync(settings.url)
    db = Database(settings)
    layout = PathLayout(data_dir)
    secret_store = SecretStore(data_dir / "secrets")
    rt = AppRuntime(
        database=db,
        secret_store=secret_store,
        layout=layout,
        config=RuntimeConfig(
            watchdog_interval_seconds=0.1,
            queue_lease_seconds=5.0,
            worker_poll_seconds=0.1,
            outbox_poll_seconds=0.1,
            max_concurrent_workers=1,
            start_workers=False,
        ),
        runtime_mode=RuntimeMode.TEST,
    )
    rt.install_max_adapter(FakeMaxAdapter())
    await rt.install_telegram(None, FakeTelegramAdapter())  # type: ignore[arg-type]
    yield rt
    try:
        await rt.stop()
    except Exception:  # noqa: BLE001
        pass
    await db.dispose()


@pytest.mark.asyncio
async def test_test_endpoint_returns_structured_checks(runtime):
    from shmax.api.app import _run_wizard_test_probes

    report = await _run_wizard_test_probes(runtime)
    assert "ok" in report
    assert "checks" in report
    assert "summary" in report
    # The active probe must include every required check id.
    ids = {c["id"] for c in report["checks"]}
    for required in (
        "max.authenticated",
        "max.chats",
        "telegram.get_me",
        "telegram.listener_alive",
        "telegram.target_paired",
        "routes.enabled",
        "users.approved",
        "bridge.workers_alive",
        "bridge.queue_operational",
    ):
        assert required in ids, f"missing probe: {required}"


@pytest.mark.asyncio
async def test_test_endpoint_routes_pass_when_seeded(runtime):
    """When MAX is configured, an admin exists, and a route is
    seeded, the probe must reflect that — the audit requires
    ACTIVE verification, not stored-only."""
    from shmax.api.app import _run_wizard_test_probes

    async with runtime.db.session() as s:  # type: ignore[attr-defined]
        s.add(MaxAccount(label="primary", status="connected"))
        s.add(TelegramAccount(label="primary", status="connected"))
        s.add(AdminUser(username="admin", password_hash="x" * 16))
        # P0 #12: the probe requires VALIDATED target facts.
        import datetime as _dtmod

        s.add(TelegramTarget(
            account_id=1, chat_id="tg-1", title="test", is_forum=False,
            chat_type="supergroup", bot_status="administrator",
            missing_permissions="[]",
            validated_at=_dtmod.datetime.now(_dtmod.timezone.utc),
            is_active=True,
        ))
        s.add(TelegramUser(user_id="u1", role="operator", approved=True))
        await s.commit()
        s.add(Route(
            max_account_id=1,
            max_chat_id="m-1",
            telegram_account_id=1,
            telegram_chat_id="tg-1",
            enabled=True,
        ))
        await s.commit()

    report = await _run_wizard_test_probes(runtime)
    by_id = {c["id"]: c for c in report["checks"]}
    assert by_id["routes.enabled"]["ok"] is True
    assert by_id["routes.enabled"]["detail"] == "1 route(s) to the paired group"
    assert by_id["users.approved"]["ok"] is True
    assert by_id["telegram.target_paired"]["ok"] is True


@pytest.mark.asyncio
async def test_test_endpoint_failure_links_to_correct_setup_step(runtime):
    """Each failed check must carry a non-empty ``fix_step`` that
    points at the wizard step that resolves the failure."""
    from shmax.api.app import _run_wizard_test_probes

    report = await _run_wizard_test_probes(runtime)
    # With the bare fixture (no MAX/TG configured), most checks
    # fail and must carry a fix_step.
    failed = [c for c in report["checks"] if not c["ok"]]
    assert failed
    for c in failed:
        assert c["fix_step"], f"missing fix_step on {c['id']}"


@pytest.mark.asyncio
async def test_test_endpoint_queue_counts(runtime):
    """The queue check must reach the database and report counts,
    not crash with a string-only detail."""
    from shmax.api.app import _run_wizard_test_probes

    report = await _run_wizard_test_probes(runtime)
    queue = next(c for c in report["checks"] if c["id"] == "bridge.queue_operational")
    assert queue["ok"] is True
    assert "pending=" in queue["detail"]