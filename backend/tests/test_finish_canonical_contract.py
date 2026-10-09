"""P0 #2 (surgical review) — the canonical backend Finish contract.

Without ``acknowledge_unconfigured``:

* a blocked "approved" user must NOT satisfy Users readiness;
* a read_only "approved" user must NOT satisfy Users readiness;
* a forum route without a thread id must NOT satisfy Chats
  readiness;
* a failed mandatory Test must reject completion;
* revoked Telegram rights must flip the target invalid and reject
  Finish (see also test_validation_facts_persistence.py).

Every negative case must produce 409 on POST /api/setup/complete
even when called directly, bypassing the UI.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from shmax.api.app import create_app
from shmax.config import RuntimeMode, Settings


def _make_app(data_dir):
    settings = Settings(
        host="127.0.0.1",
        port=0,
        data_dir=data_dir,
        log_level="WARNING",
        runtime_mode=RuntimeMode.TEST,
    )
    return create_app(settings)


@pytest.fixture()
def booted(data_dir):
    """Booted app + authenticated admin client. The seeding runs in
    each test via :func:`seed_baseline` (asyncio.run, sync test
    bodies — same pattern as test_setup_complete_enforcement)."""
    from shmax.database import Database, DatabaseSettings as _DbSettings
    from shmax.database.migrations import run_migrations_sync

    run_migrations_sync(_DbSettings(data_dir=data_dir).url)
    app = _make_app(data_dir)
    with TestClient(app) as client:
        import pathlib
        import secrets as _secrets

        secret_path = pathlib.Path(app.state.paths.root) / "state" / "setup_secret"
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        secret_path.write_text(_secrets.token_urlsafe(24))
        token = client.post(
            "/api/setup/token",
            headers={"X-Shmax-Setup-Secret": secret_path.read_text()},
        ).json()["token"]
        resp = client.post(
            "/api/setup/admin",
            json={"token": token, "username": "owner", "password": "owner-pass-12345"},
        )
        client.headers.update({"X-CSRF-Token": resp.json()["csrf_token"]})
        yield client


def seed_baseline(client) -> None:
    """Seed the 'healthy' baseline: MAX authed, TG healthy fake,
    validated target, valid forum route, usable user, Test passed."""
    import asyncio

    from shmax.adapters.telegram.fake import FakeTelegramAdapter
    from shmax.adapters.telegram.config import (
        TransportConfig,
        TransportMode,
    )
    from shmax.adapters.telegram.manager import TelegramTransportManager
    from shmax.database.models import TelegramAccount
    from shmax.pairing.repository import PairingCodesRepository
    from shmax.api.app import _build_telegram_command_handler
    from shmax.routing.repository import RoutesRepository
    from shmax.users.repository import TelegramUsersRepository

    runtime = client.app.state.runtime
    runtime.secrets.set_secret("telegram_bot_token", "123:x")

    async def _seed():
        async with runtime.db.session() as s:
            s.add(
                TelegramAccount(
                    label="primary", bot_id="424242",
                    bot_username="b", status="verified",
                )
            )
            await s.commit()
        adapter = FakeTelegramAdapter()
        await adapter.connect()
        manager = TelegramTransportManager(
            TransportConfig(mode=TransportMode.AUTO, bot_token="123:x")
        )
        await runtime.install_telegram(manager, adapter)
        code = await PairingCodesRepository(runtime.db.session).issue()
        handler = _build_telegram_command_handler(
            users=TelegramUsersRepository(runtime.db),
            pairing_codes=PairingCodesRepository(runtime.db.session),
            secret_store=runtime.secrets,
            db=runtime.db,
            adapter_getter=lambda: runtime.telegram_adapter,
        )

        async def noop(_t):
            return None

        await handler(
            "/shmax_connect",
            {
                "user_id": "777000", "display_name": "Owner", "username": None,
                "chat_kind": "supergroup", "chat_id": "-1001234567890",
                "chat_title": "G", "thread_id": None,
                "text": f"/shmax_connect {code.code}", "respond": noop,
            },
        )
        await TelegramUsersRepository(runtime.db).set_approval(
            user_id="777000", approved=True, role="operator"
        )

        # Deterministic account rows (the AUTHENTICATED hook's
        # MaxAccount upsert is scheduled as a background task).
        from shmax.database.models import MaxAccount

        async with runtime.db.session() as s:
            for model, label in ((MaxAccount, "primary"), (TelegramAccount, "primary")):
                row = (await s.execute(
                    __import__("sqlalchemy").select(model).limit(1)
                )).scalars().first()
                if row is None:
                    s.add(model(label=label))
            await s.commit()
            max_id = (await s.execute(
                __import__("sqlalchemy").select(MaxAccount.id).order_by(MaxAccount.id.asc()).limit(1)
            )).scalar_one()
            tg_id = (await s.execute(
                __import__("sqlalchemy").select(TelegramAccount.id).order_by(TelegramAccount.id.asc()).limit(1)
            )).scalar_one()
            await RoutesRepository(s).upsert(
                max_account_id=max_id, max_chat_id="max-group-1",
                telegram_account_id=tg_id, telegram_chat_id="-1001234567890",
                telegram_thread_id="900", enabled=True,
            )
            await s.commit()

        await runtime.max_auth.start(phone="+79991234567")
        for _ in range(200):
            if runtime.max_auth.state.value == "waiting_code":
                break
            await asyncio.sleep(0.02)
        await runtime.max_auth.submit_code(code="123456")
        for _ in range(200):
            if runtime.max_auth.state.value == "authenticated":
                break
            await asyncio.sleep(0.02)
        await runtime.probe_integration("max")
        await runtime.probe_integration("telegram")

    asyncio.run(_seed())
    assert client.get("/api/wizard/test").json()["ok"] is True


def test_baseline_is_ready(booted):
    client = booted
    seed_baseline(client)
    resp = client.post("/api/setup/complete")
    assert resp.status_code == 200, resp.text


def test_blocked_approved_user_rejects_finish(booted):
    client = booted
    seed_baseline(client)
    runtime = client.app.state.runtime
    client.post("/api/setup/complete")  # complete, then break users

    import asyncio

    from shmax.users.repository import TelegramUsersRepository

    asyncio.run(
        TelegramUsersRepository(runtime.db).set_blocked(
            user_id="777000", blocked=True
        )
    )
    readiness = client.get("/api/wizard/readiness").json()
    assert readiness["users"]["ready"] is False
    resp = client.post("/api/setup/complete")
    assert resp.status_code == 409
    steps = {m["step"] for m in resp.json()["missing"]}
    assert "users" in steps


def test_read_only_approved_user_rejects_finish(booted):
    client = booted
    seed_baseline(client)
    runtime = client.app.state.runtime
    client.post("/api/setup/complete")

    import asyncio

    from shmax.users.repository import TelegramUsersRepository

    asyncio.run(
        TelegramUsersRepository(runtime.db).set_approval(
            user_id="777000", approved=True, role="read_only"
        )
    )
    readiness = client.get("/api/wizard/readiness").json()
    assert readiness["users"]["ready"] is False
    resp = client.post("/api/setup/complete")
    assert resp.status_code == 409


def test_forum_route_without_thread_id_rejects_finish(booted):
    client = booted
    seed_baseline(client)
    runtime = client.app.state.runtime
    client.post("/api/setup/complete")

    import asyncio

    from sqlalchemy import update

    from shmax.database.models import Route

    async def break_route():
        async with runtime.db.session() as s:
            await s.execute(
                update(Route).values(telegram_thread_id=None)
            )
            await s.commit()

    asyncio.run(break_route())
    readiness = client.get("/api/wizard/readiness").json()
    assert readiness["chats"]["ready"] is False
    resp = client.post("/api/setup/complete")
    assert resp.status_code == 409
    assert any(m["step"] == "chats" for m in resp.json()["missing"])


def test_failed_test_rejects_finish(booted):
    client = booted
    seed_baseline(client)
    runtime = client.app.state.runtime
    client.post("/api/setup/complete")

    # Break the route so the next mandatory Test FAILS (persisted).
    import asyncio

    from sqlalchemy import update

    from shmax.database.models import Route

    async def break_route():
        async with runtime.db.session() as s:
            await s.execute(
                update(Route).values(telegram_thread_id=None)
            )
            await s.commit()

    asyncio.run(break_route())
    report = client.get("/api/wizard/test").json()
    assert report["ok"] is False  # persisted as the latest result

    # Restore the route — static readiness is fine again, but the
    # latest PERSISTED Test result is still a failure: completion
    # must be rejected until the operator re-runs the Test.
    asyncio.run(_restore(runtime))

    resp = client.post("/api/setup/complete")
    assert resp.status_code == 409
    # The static steps are fine — the persisted Test result is the
    # blocking gate, surfaced in finish_reason.
    assert "Test FAILED" in resp.json().get("finish_reason", "")

    # Re-run the Test → fresh PASS → completion accepted.
    assert client.get("/api/wizard/test").json()["ok"] is True
    resp = client.post("/api/setup/complete")
    assert resp.status_code == 200


async def _restore(runtime):
    from sqlalchemy import update

    from shmax.database.models import Route

    async with runtime.db.session() as s:
        await s.execute(update(Route).values(telegram_thread_id="900"))
        await s.commit()


# ----------------------------------------------------------------------
# Surgical P0 — Finish performs FRESH live revalidation
# ----------------------------------------------------------------------
def _real_mutable_adapter():
    from shmax.adapters.telegram.fake import FakeTelegramAdapter

    """A REAL-flagged adapter whose group facts stay mutable so
    permissions can be revoked between calls."""

    class _RealMutable(FakeTelegramAdapter):
        def is_real(self):
            return True

        def __init__(self):
            super().__init__()
            self.summary = {
                "id": "-1001234567890", "type": "supergroup",
                "title": "G", "is_forum": True,
            }
            self.perms = {
                "status": "administrator", "can_send_messages": True,
                "can_manage_topics": True, "is_member": True,
            }

        async def get_chat_summary(self, chat_id: str) -> dict:
            return dict(self.summary)

        async def get_bot_permissions(self, chat_id: str) -> dict:
            return dict(self.perms)

    return _RealMutable()


def test_finish_revalidates_fresh_without_new_test_run(booted):
    """Test PASS → revoke can_manage_topics → do NOT rerun Test →
    POST /api/setup/complete => 409 (the reproduced defect: it used
    to return 200 trusting the stale successful Test)."""
    client = booted
    seed_baseline(client)
    runtime = client.app.state.runtime

    # Swap the installed fake for a REAL-flagged mutable stub —
    # same production adapter surface, controllable rights.
    import asyncio

    from shmax.adapters.telegram.config import (
        TransportConfig,
        TransportMode,
    )
    from shmax.adapters.telegram.manager import TelegramTransportManager

    stub = _real_mutable_adapter()

    async def install():
        await stub.connect()
        manager = TelegramTransportManager(
            TransportConfig(mode=TransportMode.AUTO, bot_token="123:x")
        )
        await runtime.install_telegram(manager, stub)

    # Run the install ON THE APP'S event loop (the TestClient
    # portal schedules the coroutine natively) — a supervisor task
    # created on a foreign loop dies as soon as that loop closes.
    client.portal.start_task_soon(install)

    # Give the restarted listener task a moment to enter its loop.
    import time as _time

    _time.sleep(0.3)
    # Fresh baseline: Test passes with healthy rights.
    report = client.get("/api/wizard/test").json()
    assert report["ok"] is True, json.dumps(
        [c for c in report["checks"] if not c["ok"]]
    )
    assert client.post("/api/setup/complete").status_code == 200

    # REVOKE manage_topics. NO new Test run.
    stub.perms["can_manage_topics"] = False

    resp = client.post("/api/setup/complete")
    assert resp.status_code == 409, (
        f"Finish must not trust the stale successful Test: {resp.text}"
    )
    body = resp.json()
    joined = json.dumps(body, ensure_ascii=False)
    assert "управлять темами" in joined, (
        "the exact revoked right must be surfaced: " + joined
    )

    # The invalid facts were persisted by the Finish-time validation.
    import asyncio as _asyncio
    from sqlalchemy import select

    from shmax.database.models import TelegramTarget

    async def load_target():
        async with runtime.db.session() as s:
            row = (
                await s.execute(select(TelegramTarget))
            ).scalars().first()
            return row

    target = _asyncio.run(load_target())
    assert target is not None and target.validated_at is None, (
        "revoked rights must persist invalid validation facts"
    )
    assert "управлять темами" in (target.missing_permissions or "")

    # Restore rights → a fresh Finish succeeds again (a fully valid
    # live configuration can still Finish).
    stub.perms["can_manage_topics"] = True
    resp = client.post("/api/setup/complete")
    assert resp.status_code == 200


# ----------------------------------------------------------------------
# Surgical P1 #2b — PyMax caption delivered via the real SDK shape
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pymax_send_media_passes_caption_as_sdk_text():
    """maxapi-python 2.4.1 send_message(chat_id, text=..., attachments=[...])
    carries the media caption in ``text``. The production adapter must
    pass the caption there — never silently discard it."""
    from unittest.mock import AsyncMock, MagicMock

    import pytest as _pytest

    pymax = _pytest.importorskip("pymax")
    from shmax.adapters.max import pymax_adapter as _pa

    _pa._ensure_pymax()
    from shmax.adapters.max.pymax_adapter import PyMaxMaxAdapter

    adapter = PyMaxMaxAdapter.__new__(PyMaxMaxAdapter)  # skip __init__
    adapter._handle = MagicMock()
    adapter._handle.started = True
    sent = {}

    async def _send(**kwargs):
        sent.update(kwargs)

        class _Msg:
            id = 4242

        return _Msg()

    adapter._handle.client.send_message = AsyncMock(side_effect=_send)

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        img = Path(d) / "x.jpg"
        img.write_bytes(b"JPEG")
        await adapter.send_media(
            chat_id="555",
            media_ref=f"photo:{img}",
            kind="photo",
            caption="holiday pic",
        )
    assert sent.get("chat_id") == 555
    assert sent.get("text") == "holiday pic", (
        "caption must ride the SDK text field alongside attachments"
    )
    assert sent.get("attachments"), "attachment must be present"


@pytest.mark.asyncio
async def test_pymax_send_media_without_caption_sends_no_text():
    from unittest.mock import AsyncMock, MagicMock

    import pytest as _pytest

    pymax = _pytest.importorskip("pymax")
    from shmax.adapters.max import pymax_adapter as _pa

    _pa._ensure_pymax()
    from shmax.adapters.max.pymax_adapter import PyMaxMaxAdapter

    adapter = PyMaxMaxAdapter.__new__(PyMaxMaxAdapter)
    adapter._handle = MagicMock()
    adapter._handle.started = True
    sent = {}

    async def _send(**kwargs):
        sent.update(kwargs)

        class _Msg:
            id = 43

        return _Msg()

    adapter._handle.client.send_message = AsyncMock(side_effect=_send)

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        img = Path(d) / "y.jpg"
        img.write_bytes(b"JPEG")
        await adapter.send_media(
            chat_id="556", media_ref=f"photo:{img}", kind="photo"
        )
    assert sent.get("text") is None
