"""Production clean-state tests.

These tests exercise the SAME factory path Docker uses:

* :py:func:`shmax.api.app.create_app` with a real :py:class:`Settings`
  instance built from environment variables, including
  ``SHMAX_RUNTIME_MODE=production``.

They assert the post-startup state of the runtime. A previous pass
shipped with :py:class:`FakeMaxAdapter` as the production default,
which surfaced "Fake MAX User" / "fake-max-user" on a clean install.
These tests refuse to regress.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture()
def production_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build a production settings object exactly the way the
    Docker runtime builds it: ``SHMAX_DATA_DIR`` pointing at an
    empty per-test directory, ``SHMAX_RUNTIME_MODE=production``,
    no extra env vars that would mask the test.
    """
    monkeypatch.setenv("SHMAX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHMAX_RUNTIME_MODE", "production")
    monkeypatch.setenv("SHMAX_HOST", "127.0.0.1")
    monkeypatch.setenv("SHMAX_PORT", "0")
    monkeypatch.setenv("SHMAX_LOG_LEVEL", "WARNING")
    yield tmp_path


def test_production_refuses_fakes(production_env: Path):
    """The runtime cannot ship with a Fake adapter in production."""
    from fastapi.testclient import TestClient

    from shmax.api.app import create_app
    from shmax.config import Settings

    settings = Settings.from_env()
    assert settings.runtime_mode.value == "production"
    app = create_app(settings)
    with TestClient(app) as client:
        runtime = client.app.state.runtime
        # Both adapter slots must not be fakes.
        assert runtime._max.__class__.__name__ != "FakeMaxAdapter", (
            "Production runtime leaked FakeMaxAdapter. A clean install "
            "must surface the real PyMax-backed adapter (or fail closed)."
        )
        assert runtime._tg.__class__.__name__ != "FakeTelegramAdapter", (
            "Production runtime leaked FakeTelegramAdapter. Telegram must "
            "expose a NOT_CONFIGURED sentinel until the operator installs "
            "the real Bot API / MTProto adapter."
        )


def test_production_max_auth_state_is_not_started(production_env: Path):
    """A fresh production install must NOT show an authenticated MAX
    session. ``max_auth.refresh()`` returns ``not_started`` so the
    wizard renders the phone form."""
    import asyncio
    from fastapi.testclient import TestClient

    from shmax.api.app import create_app
    from shmax.config import Settings

    settings = Settings.from_env()
    app = create_app(settings)
    with TestClient(app) as client:
        runtime = client.app.state.runtime

        async def get_state():
            return await runtime.max_auth.refresh()

        snap = asyncio.run(get_state())
    # The hard failure case for the original bug.
    assert snap.state.value == "not_started", (
        f"Production runtime reported MAX state={snap.state.value}; "
        "this is the 'Fake MAX User' leak. A fresh install must be "
        "NOT_STARTED until the operator submits a real phone."
    )
    assert snap.account_id is None
    assert snap.display_name is None
    assert snap.phone_hint is None
    assert snap.next_action == "start"


def test_production_get_max_chats_returns_empty_not_fake(production_env: Path):
    """``GET /api/max/chats`` on a fresh install must NOT pretend
    to be authenticated. The real PyMax-backed adapter either
    raises (no session) or returns an empty list — never the
    Fake adapter's contents.
    """
    # Hit /api/max/chats via a real loopback HTTP client so the
    # localhost check in /api/setup/* passes. TestClient's default
    # ``request.client.host`` is "testclient", not a loopback IP.
    import threading
    import time
    import socket
    import urllib.request
    import urllib.error
    import json

    from shmax.api.app import create_app
    from shmax.config import Settings

    settings = Settings.from_env()
    app = create_app(settings)

    # Bind uvicorn on a free 127.0.0.1 port.
    sock_ = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock_.bind(("127.0.0.1", 0))
    port = sock_.getsockname()[1]
    sock_.close()

    import uvicorn
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # Wait for ready.
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    base = f"http://127.0.0.1:{port}"
    try:
        tok_resp = json.loads(
            urllib.request.urlopen(base + "/api/setup/token", data=b"{}", timeout=5).read()
        )
        assert "token" in tok_resp, f"setup/token: {tok_resp!r}"
        # setup/admin
        body = json.dumps({
            "username": "admin",
            "password": "shmaxAdminPass!2026",
            "token": tok_resp["token"],
        }).encode()
        urllib.request.urlopen(urllib.request.Request(
            base + "/api/setup/admin", data=body,
            headers={"Content-Type": "application/json"},
        ), timeout=5).read()
        # login -> capture cookie
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        login_body = json.dumps({"username": "admin", "password": "shmaxAdminPass!2026"}).encode()
        opener.open(urllib.request.Request(
            base + "/api/auth/login",
            data=login_body,
            headers={"Content-Type": "application/json"},
        ), timeout=5)
        chats_resp = opener.open(urllib.request.Request(base + "/api/max/chats"), timeout=5).read()
        body = json.loads(chats_resp)
    finally:
        server.should_exit = True
        thread.join(timeout=3)

    assert body.get("configured") is False, (
        "Production /api/max/chats reports configured=True on a clean "
        "install. The runtime silently auto-AUTHENTICATED MAX."
    )
    for chat in body.get("chats", []):
        title = (chat.get("title") or "").lower()
        chat_id = (chat.get("chat_id") or "").lower()
        assert "fake" not in title, f"fake title leaked: {chat!r}"
        assert "fake" not in chat_id, f"fake id leaked: {chat!r}"


def test_production_default_telegram_adapter_is_not_configured(production_env: Path):
    """Until the operator installs a real Telegram adapter, the
    default adapter must be a sentinel that surfaces
    ``NOT_CONFIGURED`` rather than a fake or a half-installed
    Bot API adapter."""
    from fastapi.testclient import TestClient

    from shmax.adapters.telegram.base import (
        NotConfiguredTelegramAdapter,
    )
    from shmax.api.app import create_app
    from shmax.config import Settings

    settings = Settings.from_env()
    app = create_app(settings)
    with TestClient(app) as client:
        runtime = client.app.state.runtime
        assert isinstance(runtime._tg, NotConfiguredTelegramAdapter), (
            f"Expected NotConfiguredTelegramAdapter on a fresh install, "
            f"got {type(runtime._tg).__name__}"
        )


def test_production_refuses_to_start_with_fake_factory(monkeypatch, tmp_path):
    """A test that explicitly asks the production runtime to
    install a Fake adapter must FAIL with a clear error, not
    silently substitute the fake."""
    from fastapi.testclient import TestClient

    from shmax.adapters.max.fake import FakeMaxAdapter
    from shmax.api.app import create_app
    from shmax.config import RuntimeMode, Settings

    monkeypatch.setenv("SHMAX_DATA_DIR", str(tmp_path))
    settings = Settings.from_env()
    assert settings.runtime_mode.value == "production"
    app = create_app(settings)
    with TestClient(app) as client:
        runtime = client.app.state.runtime
        with pytest.raises(RuntimeError, match="production"):
            # The runtime constructor's hard invariant must reject
            # any subsequent attempt to install a Fake.
            runtime.install_max_adapter(FakeMaxAdapter())


def test_settings_production_default(monkeypatch):
    """``Settings.from_env()`` defaults to PRODUCTION unless the
    operator sets ``SHMAX_RUNTIME_MODE`` explicitly. This is the
    fail-closed default: any future refactor that forgets to
    pass ``runtime_mode`` lands in production and is caught by
    the test suite."""
    monkeypatch.delenv("SHMAX_RUNTIME_MODE", raising=False)
    monkeypatch.setenv("SHMAX_DATA_DIR", "/tmp/shmax-test-default")
    from shmax.config import RuntimeMode, Settings
    s = Settings.from_env()
    assert s.runtime_mode == RuntimeMode.PRODUCTION