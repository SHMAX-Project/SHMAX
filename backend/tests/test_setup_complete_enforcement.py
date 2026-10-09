"""P0 #5 — POST /api/setup/complete enforces mandatory readiness.

A crafted API call must not be able to complete setup by bypassing
the UI. Without ``acknowledge_unconfigured`` every mandatory gate is
checked server-side; with it the intentional V1 override is
recorded.
"""

from __future__ import annotations

import pytest
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
def booted_client(data_dir):
    """A booted app with an authenticated admin session."""
    from shmax.database import Database, DatabaseSettings as _DbSettings
    from shmax.database.migrations import run_migrations_sync

    run_migrations_sync(_DbSettings(data_dir=data_dir).url)
    app = _make_app(data_dir)
    with TestClient(app) as client:
        # Bootstrap admin via the installer path (secret header).
        import pathlib

        secret_path = pathlib.Path(app.state.paths.root) / "state" / "setup_secret"
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        import secrets as _secrets

        secret_path.write_text(_secrets.token_urlsafe(24))
        token = client.post(
            "/api/setup/token",
            headers={"X-Shmax-Setup-Secret": secret_path.read_text()},
        ).json()["token"]
        resp = client.post(
            "/api/setup/admin",
            json={"token": token, "username": "owner", "password": "owner-pass-12345"},
        )
        assert resp.status_code == 200
        csrf = resp.json()["csrf_token"]
        client.headers.update({"X-CSRF-Token": csrf})
        yield client


def test_complete_rejected_when_nothing_is_configured(booted_client, data_dir):
    """Fresh install: no MAX, no Telegram, no target, no routes,
    no approved users — a direct API call MUST be rejected."""
    resp = booted_client.post("/api/setup/complete")
    assert resp.status_code == 409, resp.text
    body = resp.json()
    missing = body["missing"]
    steps = {m["step"] for m in missing}
    assert {"max", "telegram_bot", "telegram_group", "chats", "users"} <= steps
    # And setup is NOT complete.
    session = booted_client.get("/api/auth/session").json()
    assert session["setup_complete"] is False


def test_complete_with_ack_records_override(booted_client):
    resp = booted_client.post(
        "/api/setup/complete", json={"acknowledge_unconfigured": True}
    )
    assert resp.status_code == 200
    session = booted_client.get("/api/auth/session").json()
    assert session["setup_complete"] is True


def test_complete_accepted_when_full_readiness_met(booted_client, data_dir):
    """Seed EVERY mandatory gate through the real APIs / DB and
    verify the endpoint accepts without the override."""
    import datetime as dt
    import json

    from sqlalchemy import select

    runtime = booted_client.app.state.runtime
    from shmax.adapters.max.fake import FakeMaxAdapter
    from shmax.adapters.telegram.config import TransportConfig, TransportMode
    from shmax.adapters.telegram.fake import FakeTelegramAdapter
    from shmax.adapters.telegram.manager import TelegramTransportManager
    from shmax.database.models import MaxAccount, Route, TelegramAccount, TelegramTarget
    from shmax.routing.repository import RoutesRepository
    from shmax.users.repository import TelegramUsersRepository

    async def _seed():
        # MAX: authenticate through the real auth state machine.
        await runtime.max_auth.start(phone="+79991234567")
        for _ in range(100):
            if runtime.max_auth.state.value == "waiting_code":
                break
            await dt_await(0.02)
        await runtime.max_auth.submit_code(code="123456")
        for _ in range(100):
            if runtime.max_auth.state.value == "authenticated":
                break
            await dt_await(0.02)
        # Telegram: token + verified account row + installed adapter.
        runtime.secrets.set_secret("telegram_bot_token", "123:x")
        async with runtime.db.session() as s:
            s.add(
                TelegramAccount(
                    label="primary", bot_id="424242",
                    bot_username="shmax_test_bot", status="verified",
                )
            )
            await s.commit()
        adapter = FakeTelegramAdapter()
        await adapter.connect()  # the real listener connects; the fake needs it explicitly
        manager = TelegramTransportManager(
            TransportConfig(mode=TransportMode.AUTO, bot_token="123:x")
        )
        await runtime.install_telegram(manager, adapter)
        # Pair through the real handler so the target is validated.
        from shmax.pairing.repository import PairingCodesRepository
        from shmax.api.app import _build_telegram_command_handler

        code = await PairingCodesRepository(runtime.db.session).issue()
        handler = _build_telegram_command_handler(
            users=TelegramUsersRepository(runtime.db),
            pairing_codes=PairingCodesRepository(runtime.db.session),
            secret_store=runtime.secrets,
            db=runtime.db,
            adapter_getter=lambda: runtime.telegram_adapter,
        )
        await handler(
            "/shmax_connect",
            {
                "user_id": "777000",
                "display_name": "Owner",
                "username": "owner",
                "chat_kind": "supergroup",
                "chat_id": "-1001234567890",
                "chat_title": "G",
                "thread_id": None,
                "text": f"/shmax_connect {code.code}",
                "respond": lambda text: _noop(),
            },
        )
        # Approve the pairing user with a send-capable role.
        await TelegramUsersRepository(runtime.db).set_approval(
            user_id="777000", approved=True, role="operator"
        )
        # Route the paired chat (forum target → topic id required
        # by the surgical readiness contract).
        async with runtime.db.session() as s:
            await RoutesRepository(s).upsert(
                max_account_id=1,
                max_chat_id="max-group-1",
                telegram_account_id=1,
                telegram_chat_id="-1001234567890",
                telegram_thread_id="900",
                enabled=True,
            )
            await s.commit()

    async def dt_await(delay):
        import asyncio

        await asyncio.sleep(delay)

    async def _noop():
        return None

    import asyncio

    asyncio.run(_seed())
    # Probe so the snapshot carries fresh evidence.
    import asyncio

    asyncio.run(runtime.probe_integration("max"))
    asyncio.run(runtime.probe_integration("telegram"))

    # The mandatory Test must have RUN and PASSED (persisted).
    test_resp = booted_client.get("/api/wizard/test")
    assert test_resp.status_code == 200
    assert test_resp.json()["ok"] is True

    resp = booted_client.post("/api/setup/complete")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["setup_complete"] is True
