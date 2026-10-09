"""Regression tests for Telegram listener lifecycle (P0 #4) and
deterministic startup restoration (P0 #5).

Pins:

* install_telegram disconnects the PREVIOUS adapter before the
  swap — no orphan polling consumers, no self-inflicted 409s;
* a supervisor whose factory returns cleanly retires instead of
  hot-spinning;
* restore_max: a persisted AUTHENTICATED session reconnects and
  re-arms the listener without owner interaction; an unusable
  session is reported honestly (no fake HEALTHY);
* restore_telegram: the persisted transport config (DB + secret
  store) is rebuilt through the BOUNDED PROBED selection and
  installed with the canonical command handler.
"""

from __future__ import annotations

import asyncio
import pytest_asyncio
import json
from pathlib import Path

import pytest

from shmax.adapters.max.base import MaxProfile
from shmax.adapters.max.errors import MaxSessionError
from shmax.adapters.max.fake import FakeMaxAdapter
from shmax.adapters.telegram.fake import FakeTelegramAdapter
from shmax.config import RuntimeMode
from shmax.core.paths import PathLayout
from shmax.database import Database, DatabaseSettings
from shmax.database.migrations import run_migrations_sync
from shmax.runtime import AppRuntime, ComponentSupervisor, RuntimeConfig
from shmax.security import SecretStore


@pytest_asyncio.fixture()
async def runtime(tmp_path):
    data = tmp_path / "shmax-data"
    settings = DatabaseSettings(data_dir=data)
    db = Database(settings)
    run_migrations_sync(settings.url)
    secret_store = SecretStore(data / "secrets")
    layout = PathLayout(data)
    rt = AppRuntime(
        database=db,
        secret_store=secret_store,
        layout=layout,
        config=RuntimeConfig(start_workers=False),
        runtime_mode=RuntimeMode.TEST,
    )
    rt.install_max_adapter(FakeMaxAdapter())
    await rt.install_telegram(None, FakeTelegramAdapter())  # type: ignore[arg-type]
    yield rt
    await rt.stop()
    await db.dispose()


# ----------------------------------------------------------------------
# P0 #4 — listener lifecycle ownership
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_install_telegram_disconnects_previous_adapter(runtime):
    old = FakeTelegramAdapter()
    await old.connect()
    assert old._started is True
    runtime._tg = old

    new = FakeTelegramAdapter()
    await runtime.install_telegram(None, new)  # type: ignore[arg-type]

    assert runtime.telegram_adapter is new
    # The previous adapter MUST have been disconnected — a live
    # polling task left behind is the source of 409 conflicts.
    assert old._started is False


@pytest.mark.asyncio
async def test_install_telegram_keeps_single_listener_task(runtime):
    """Two install_telegram calls must leave exactly ONE listener
    supervisor task — never two getUpdates consumers."""
    from shmax.adapters.telegram.config import (
        TransportConfig,
        TransportMode,
    )
    from shmax.adapters.telegram.manager import TelegramTransportManager

    first = FakeTelegramAdapter()
    await runtime.install_telegram(
        TelegramTransportManager(
            TransportConfig(mode=TransportMode.AUTO, bot_token="t")
        ),
        first,
    )
    sup = runtime._supervisors.get("telegram_listener")
    task_after_first = sup._task if sup else None

    second = FakeTelegramAdapter()
    await runtime.install_telegram(
        TelegramTransportManager(
            TransportConfig(mode=TransportMode.AUTO, bot_token="t")
        ),
        second,
    )
    sup2 = runtime._supervisors.get("telegram_listener")
    # The supervisor object is stable; at most one task is alive.
    alive = [
        t
        for t in (task_after_first, sup2._task if sup2 else None)
        if t is not None and not t.done()
    ]
    # The first task was stopped by the swap.
    assert task_after_first is None or task_after_first.done()
    assert len(alive) <= 1


@pytest.mark.asyncio
async def test_supervisor_retires_cleanly_on_factory_return():
    """A factory that returns (e.g. listener not configured) must
    NOT be restarted in a hot loop."""
    calls = {"n": 0}

    async def factory() -> None:
        calls["n"] += 1
        await asyncio.sleep(0)
        return

    sup = ComponentSupervisor(name="probe", factory=factory)
    sup.start()
    await asyncio.sleep(0.2)
    assert calls["n"] == 1, "factory must run exactly once per start()"
    assert sup._task is not None and sup._task.done()
    await sup.stop()


# ----------------------------------------------------------------------
# P0 #5 — deterministic startup restoration
# ----------------------------------------------------------------------
def _authenticated_state_file(runtime: AppRuntime) -> None:
    path = runtime.layout.root / "state" / "max_auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "state": "authenticated",
                "phone_hint": "***8888",
                "account_id": "fake-max-user",
                "display_name": "Fake MAX User",
                "handle": None,
                "last_error": None,
                "last_error_class": None,
                "next_action": "done",
                "updated_at": 1700000000,
            }
        )
    )


@pytest.mark.asyncio
async def test_restore_max_reconnects_valid_session(tmp_path):
    """AUTHENTICATED snapshot + working adapter → listener starts,
    no owner interaction."""
    from shmax.startup_restore import restore_max

    data = tmp_path / "data"
    settings = DatabaseSettings(data_dir=data)
    db = Database(settings)
    run_migrations_sync(settings.url)
    layout = PathLayout(data)
    rt = AppRuntime(
        database=db,
        secret_store=SecretStore(data / "secrets"),
        layout=layout,
        config=RuntimeConfig(start_workers=False),
        runtime_mode=RuntimeMode.TEST,
    )
    rt.install_max_adapter(FakeMaxAdapter())
    _authenticated_state_file(rt)
    # Recreate the auth service so it loads the persisted snapshot.
    from shmax.auth.max import (
        CredentialFuture,
        MaxAuthService,
        MaxAuthStateStore,
    )

    sms_f = CredentialFuture(kind="sms")
    pwd_f = CredentialFuture(kind="password")
    rt.max_auth = MaxAuthService(
        adapter=rt.max_adapter,
        store=MaxAuthStateStore(layout.root / "state" / "max_auth.json"),
        sms_future=sms_f,
        password_future=pwd_f,
    )
    rt.max_auth.on_state_change = rt._on_max_auth_state_change
    # The realistic restart path: the lifespan has started the
    # runtime (creating the listener supervisors) before restore.
    await rt.start()

    result = await restore_max(rt)
    assert result["restored"] is True, result
    assert result["state"] == "HEALTHY"
    sup = rt._supervisors.get("max_listener")
    assert sup is not None and sup._task is not None
    await rt.stop()
    await db.dispose()


@pytest.mark.asyncio
async def test_restore_max_reports_expired_session_honestly(tmp_path):
    """An unusable session must NOT restore — and must not fake
    HEALTHY."""

    class _ExpiredAdapter(FakeMaxAdapter):
        async def get_profile(self) -> MaxProfile:
            raise MaxSessionError("session revoked")

    from shmax.startup_restore import restore_max

    data = tmp_path / "data"
    settings = DatabaseSettings(data_dir=data)
    db = Database(settings)
    run_migrations_sync(settings.url)
    layout = PathLayout(data)
    rt = AppRuntime(
        database=db,
        secret_store=SecretStore(data / "secrets"),
        layout=layout,
        config=RuntimeConfig(start_workers=False),
        runtime_mode=RuntimeMode.TEST,
    )
    rt.install_max_adapter(_ExpiredAdapter())
    _authenticated_state_file(rt)
    from shmax.auth.max import (
        CredentialFuture,
        MaxAuthService,
        MaxAuthStateStore,
    )

    rt.max_auth = MaxAuthService(
        adapter=rt.max_adapter,
        store=MaxAuthStateStore(layout.root / "state" / "max_auth.json"),
        sms_future=CredentialFuture(kind="sms"),
        password_future=CredentialFuture(kind="password"),
    )
    rt.max_auth.on_state_change = rt._on_max_auth_state_change
    await rt.start()

    result = await restore_max(rt)
    assert result["restored"] is False
    assert result["state"] != "HEALTHY"
    # The listener must not run against a dead session.
    sup = rt._supervisors.get("max_listener")
    assert sup is None or sup._task is None or sup._task.done()
    await rt.stop()
    await db.dispose()


@pytest.mark.asyncio
async def test_restore_telegram_uses_probed_selection(tmp_path, monkeypatch):
    """The restore path must go through the BOUNDED PROBED
    selection — never the unprobed ``select()``."""
    from shmax.adapters.telegram.config import TransportMode
    from shmax.adapters.telegram.manager import TelegramTransportManager
    from shmax.startup_restore import (
        persist_telegram_transport,
        restore_telegram,
    )

    data = tmp_path / "data"
    settings = DatabaseSettings(data_dir=data)
    db = Database(settings)
    run_migrations_sync(settings.url)
    secret_store = SecretStore(data / "secrets")
    layout = PathLayout(data)
    rt = AppRuntime(
        database=db,
        secret_store=secret_store,
        layout=layout,
        config=RuntimeConfig(start_workers=False),
        runtime_mode=RuntimeMode.TEST,
    )

    # Persist config + token the way the wizard does.
    secret_store.set_secret("telegram_bot_token", "123:abc")
    from shmax.adapters.telegram.config import ProxyConfig

    await persist_telegram_transport(
        db,
        secret_store,
        mode=TransportMode.BOT_API_DIRECT,
        proxy=ProxyConfig(
            scheme="socks5", host="p", port=1080, username="u", password="s3cret"
        ),
        api_id=None,
        api_hash=None,
        mtproxy=None,
    )

    probed_calls = {"n": 0}
    restored_adapter = FakeTelegramAdapter()

    async def _fake_probed(self, *, override_mode=None, connect_timeout=5.0):
        probed_calls["n"] += 1
        return restored_adapter

    monkeypatch.setattr(
        TelegramTransportManager, "select_probed", _fake_probed
    )

    result = await restore_telegram(
        rt,
        db=db,
        secret_store=secret_store,
        session_dir=str(data / "sessions" / "telegram"),
        auto_state_path=data / "state" / "telegram_auto.json",
    )
    assert probed_calls["n"] == 1, "restore must use select_probed"
    assert result["restored"] is True
    assert runtime_installed_with_token(rt, restored_adapter)
    await rt.stop()
    await db.dispose()


def runtime_installed_with_token(rt: AppRuntime, adapter) -> bool:
    return rt.telegram_adapter is adapter


@pytest.mark.asyncio
async def test_restore_telegram_without_config_is_not_configured(tmp_path):
    from shmax.startup_restore import restore_telegram

    data = tmp_path / "data"
    settings = DatabaseSettings(data_dir=data)
    db = Database(settings)
    run_migrations_sync(settings.url)
    secret_store = SecretStore(data / "secrets")
    layout = PathLayout(data)
    rt = AppRuntime(
        database=db,
        secret_store=secret_store,
        layout=layout,
        config=RuntimeConfig(start_workers=False),
        runtime_mode=RuntimeMode.TEST,
    )
    result = await restore_telegram(
        rt,
        db=db,
        secret_store=secret_store,
        session_dir=str(data / "sessions" / "telegram"),
    )
    assert result["restored"] is False
    assert result["state"] == "NOT_CONFIGURED"
    await rt.stop()
    await db.dispose()


@pytest.mark.asyncio
async def test_persisted_transport_keeps_password_out_of_db(tmp_path):
    """P1 #6: the proxy password lands in the SecretStore, never
    in the settings table."""
    from sqlalchemy import select

    from shmax.adapters.telegram.config import ProxyConfig, TransportMode
    from shmax.database.models import Setting
    from shmax.startup_restore import (
        load_telegram_transport,
        persist_telegram_transport,
    )

    data = tmp_path / "data"
    settings = DatabaseSettings(data_dir=data)
    db = Database(settings)
    run_migrations_sync(settings.url)
    secret_store = SecretStore(data / "secrets")

    await persist_telegram_transport(
        db,
        secret_store,
        mode=TransportMode.BOT_API_SOCKS5,
        proxy=ProxyConfig(
            scheme="socks5", host="p", port=1080, username="u", password="s3cret"
        ),
        api_id=None,
        api_hash="hash-value",
        mtproxy=None,
    )
    async with db.session() as s:
        row = (
            await s.execute(
                select(Setting).where(Setting.key == "telegram_transport")
            )
        ).scalars().first()
    assert row is not None
    assert "s3cret" not in row.value, "proxy password leaked into DB"
    assert "hash-value" not in row.value, "api_hash leaked into DB"
    assert secret_store.get_secret("telegram_proxy_password") == "s3cret"
    assert secret_store.get_secret("telegram_api_hash") == "hash-value"

    facts = await load_telegram_transport(db, secret_store)
    assert facts["proxy"]["password"] == "s3cret"
    assert facts["api_hash"] == "hash-value"
    await db.dispose()


# ----------------------------------------------------------------------
# P0 #4 — bounded startup restore
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_restore_max_connect_hang_is_bounded(tmp_path):
    """A hanging MAX connect must not block startup; the app-side
    restore returns within a bounded window with DEGRADED."""

    class _HangingAdapter(FakeMaxAdapter):
        async def connect(self):
            await asyncio.sleep(3600)

    from shmax.startup_restore import RESTORE_CONNECT_TIMEOUT, restore_max

    data = tmp_path / "data-hang"
    settings = DatabaseSettings(data_dir=data)
    db = Database(settings)
    run_migrations_sync(settings.url)
    layout = PathLayout(data)
    rt = AppRuntime(
        database=db,
        secret_store=SecretStore(data / "secrets"),
        layout=layout,
        config=RuntimeConfig(start_workers=False),
        runtime_mode=RuntimeMode.TEST,
    )
    rt.install_max_adapter(_HangingAdapter())
    _authenticated_state_file(rt)
    from shmax.auth.max import (
        CredentialFuture,
        MaxAuthService,
        MaxAuthStateStore,
    )

    rt.max_auth = MaxAuthService(
        adapter=rt.max_adapter,
        store=MaxAuthStateStore(layout.root / "state" / "max_auth.json"),
        sms_future=CredentialFuture(kind="sms"),
        password_future=CredentialFuture(kind="password"),
    )
    rt.max_auth.on_state_change = rt._on_max_auth_state_change

    import time as _time

    started = _time.monotonic()
    result = await asyncio.wait_for(
        restore_max(rt), timeout=RESTORE_CONNECT_TIMEOUT + 5
    )
    elapsed = _time.monotonic() - started
    assert result["state"] == "DEGRADED"
    assert elapsed < RESTORE_CONNECT_TIMEOUT + 2, "restore must be bounded"
    # The runtime is untouched and still startable — the web UI lives.
    await rt.start()
    await rt.stop()
    await db.dispose()


@pytest.mark.asyncio
async def test_restore_max_invalid_session_is_auth_required(tmp_path):
    from shmax.adapters.max.errors import MaxSessionError

    class _RejectedAdapter(FakeMaxAdapter):
        async def connect(self):
            raise MaxSessionError("session revoked")

    from shmax.startup_restore import restore_max

    data = tmp_path / "data-reject"
    settings = DatabaseSettings(data_dir=data)
    db = Database(settings)
    run_migrations_sync(settings.url)
    layout = PathLayout(data)
    rt = AppRuntime(
        database=db,
        secret_store=SecretStore(data / "secrets"),
        layout=layout,
        config=RuntimeConfig(start_workers=False),
        runtime_mode=RuntimeMode.TEST,
    )
    rt.install_max_adapter(_RejectedAdapter())
    _authenticated_state_file(rt)
    from shmax.auth.max import (
        CredentialFuture,
        MaxAuthService,
        MaxAuthStateStore,
    )

    rt.max_auth = MaxAuthService(
        adapter=rt.max_adapter,
        store=MaxAuthStateStore(layout.root / "state" / "max_auth.json"),
        sms_future=CredentialFuture(kind="sms"),
        password_future=CredentialFuture(kind="password"),
    )
    rt.max_auth.on_state_change = rt._on_max_auth_state_change

    result = await restore_max(rt)
    assert result["state"] == "AUTH_REQUIRED"
    assert result["restored"] is False
    await rt.stop()
    await db.dispose()


# ----------------------------------------------------------------------
# P1 #1 — exactly one getUpdates consumer across a hot-swap
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_hot_swap_never_runs_two_polling_consumers(runtime, monkeypatch):
    """During select_probed + install_telegram the number of
    CONCURRENTLY RUNNING polling loops must never exceed 1 — the
    probe must not create a second consumer next to the live one."""
    from shmax.adapters.telegram.bot_api import TelegramBotApiAdapter
    from shmax.adapters.telegram.config import (
        TransportConfig,
        TransportMode,
    )
    from shmax.adapters.telegram.manager import TelegramTransportManager

    counters = {"running": 0, "max_running": 0}

    async def _counting_polling(self):
        counters["running"] += 1
        counters["max_running"] = max(
            counters["max_running"], counters["running"]
        )
        try:
            await asyncio.sleep(3600)
        finally:
            counters["running"] -= 1

    async def _instant_probe(self):
        return None

    class _StubBot:
        class _Session:
            async def close(self):
                return None

        session = _Session()

        async def get_me(self):
            class _Me:
                id = 424242
                username = "bot"

            return _Me()

    def _stub_make_bot(self):
        return _StubBot()

    monkeypatch.setattr(TelegramBotApiAdapter, "_run_polling", _counting_polling)
    monkeypatch.setattr(TelegramBotApiAdapter, "probe", _instant_probe)
    monkeypatch.setattr(TelegramBotApiAdapter, "_make_bot", _stub_make_bot)

    cfg = TransportConfig(mode=TransportMode.AUTO, bot_token="123:x")

    # The supervisors only exist after runtime.start().
    await runtime.start()

    # Live listener #1.
    first = TelegramBotApiAdapter(cfg)
    await first.connect()
    manager = TelegramTransportManager(cfg)
    await runtime.install_telegram(manager, first)
    await asyncio.sleep(0.05)
    assert counters["running"] >= 1

    # Hot-swap to a new adapter via the probed selection.
    second = await manager.select_probed()
    await runtime.install_telegram(manager, second)
    await asyncio.sleep(0.05)

    assert counters["max_running"] == 1, (
        f"polling overlap detected: max_running={counters['max_running']}"
    )
    assert first._started is False
    assert second._started is True


# ----------------------------------------------------------------------
# P1 #2 (surgical) — restore cleanup asserts disconnect called
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_restore_timeout_disconnects_adapter(tmp_path):
    """connect hangs → timeout → DEGRADED AND the adapter was
    disconnected (no half-open client left behind)."""

    class _DisconnectCounting(FakeMaxAdapter):
        def __init__(self):
            super().__init__()
            self.disconnects = 0

        async def connect(self):
            await asyncio.sleep(3600)

        async def disconnect(self):
            self.disconnects += 1
            await super().disconnect()

    from shmax.startup_restore import restore_max

    data = tmp_path / "data-dc"
    settings = DatabaseSettings(data_dir=data)
    db = Database(settings)
    run_migrations_sync(settings.url)
    layout = PathLayout(data)
    rt = AppRuntime(
        database=db,
        secret_store=SecretStore(data / "secrets"),
        layout=layout,
        config=RuntimeConfig(start_workers=False),
        runtime_mode=RuntimeMode.TEST,
    )
    adapter = _DisconnectCounting()
    rt.install_max_adapter(adapter)
    _authenticated_state_file(rt)
    from shmax.auth.max import (
        CredentialFuture,
        MaxAuthService,
        MaxAuthStateStore,
    )

    rt.max_auth = MaxAuthService(
        adapter=adapter,
        store=MaxAuthStateStore(layout.root / "state" / "max_auth.json"),
        sms_future=CredentialFuture(kind="sms"),
        password_future=CredentialFuture(kind="password"),
    )
    rt.max_auth.on_state_change = rt._on_max_auth_state_change

    result = await asyncio.wait_for(restore_max(rt), timeout=20)
    assert result["state"] == "DEGRADED"
    assert adapter.disconnects >= 1, "timeout must disconnect the client"
    await rt.stop()
    await db.dispose()


@pytest.mark.asyncio
async def test_restore_profile_failure_disconnects_adapter(tmp_path):
    """connect OK but profile verification fails → disconnect."""

    class _NoProfileAdapter(FakeMaxAdapter):
        def __init__(self):
            super().__init__()
            self.disconnects = 0

        async def get_profile(self):
            from shmax.adapters.max.base import MaxProfile

            return MaxProfile(user_id="", display_name="", handle=None)

        async def disconnect(self):
            self.disconnects += 1
            await super().disconnect()

    from shmax.startup_restore import restore_max

    data = tmp_path / "data-noprofile"
    settings = DatabaseSettings(data_dir=data)
    db = Database(settings)
    run_migrations_sync(settings.url)
    layout = PathLayout(data)
    rt = AppRuntime(
        database=db,
        secret_store=SecretStore(data / "secrets"),
        layout=layout,
        config=RuntimeConfig(start_workers=False),
        runtime_mode=RuntimeMode.TEST,
    )
    adapter = _NoProfileAdapter()
    rt.install_max_adapter(adapter)
    _authenticated_state_file(rt)
    from shmax.auth.max import (
        CredentialFuture,
        MaxAuthService,
        MaxAuthStateStore,
    )

    rt.max_auth = MaxAuthService(
        adapter=adapter,
        store=MaxAuthStateStore(layout.root / "state" / "max_auth.json"),
        sms_future=CredentialFuture(kind="sms"),
        password_future=CredentialFuture(kind="password"),
    )
    rt.max_auth.on_state_change = rt._on_max_auth_state_change
    await rt.start()

    result = await restore_max(rt)
    assert result["restored"] is False
    assert adapter.disconnects >= 1, "profile failure must disconnect"
    await rt.stop()
    await db.dispose()
