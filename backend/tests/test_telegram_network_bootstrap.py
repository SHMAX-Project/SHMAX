"""Surgical acceptance — Telegram network bootstrap.

Pins:

* the Network step can be saved + tested WITHOUT a bot token
  (reachability only — no getMe, no adapter, no polling consumer);
* a failing transport returns 502 with a sanitized detail —
  NO direct fallback after SOCKS/HTTP is selected;
* when a token IS present the adapter installs through the
  selected transport (getMe through it).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from shmax.api.app import create_app
from shmax.config import RuntimeMode, Settings


@pytest.fixture()
def client(data_dir, monkeypatch):
    """Booted app with Telegram reachability pointed at a controlled
    URL (SHMAX_TELEGRAM_REACHABILITY_URL)."""
    monkeypatch.setenv(
        "SHMAX_TELEGRAM_REACHABILITY_URL", "http://127.0.0.1:59999"
    )  # nothing listens — direct must FAIL
    settings = Settings(
        host="127.0.0.1", port=0, data_dir=data_dir,
        log_level="WARNING", runtime_mode=RuntimeMode.TEST,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        import pathlib
        import secrets as _secrets

        sp = pathlib.Path(app.state.paths.root) / "state" / "setup_secret"
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(_secrets.token_urlsafe(24))
        token = c.post(
            "/api/setup/token",
            headers={"X-Shmax-Setup-Secret": sp.read_text()},
        ).json()["token"]
        resp = c.post(
            "/api/setup/admin",
            json={"token": token, "username": "owner", "password": "owner-pass-12345"},
        )
        c.headers.update({"X-CSRF-Token": resp.json()["csrf_token"]})
        yield c


def _transport(client, **fields):
    body = {"mode": "bot_api_socks5", "skip_bot_probe": True, **fields}
    return client.post("/api/telegram/transport", json=body)


def test_socks5_saved_without_bot_token(client):
    """Network step: SOCKS5 proxy saved + reachability tested with NO
    bot token. Unreachable proxy (nothing listens) => 502 sanitized,
    NO direct fallback: the mode stays bot_api_socks5."""
    resp = _transport(
        client,
        proxy_host="127.0.0.1", proxy_port=59998,
        proxy_username="u", proxy_password="p",
    )
    assert resp.status_code == 502, resp.text
    body = resp.json()
    assert body["reachable"] is False
    assert body["mode"] == "bot_api_socks5", (
        "SOCKS5 failure must NOT fall back to direct"
    )
    # sanitized detail: credentials never leak into the response
    assert "proxy_password" not in body
    facts = client.get("/api/telegram/transport").json()
    assert facts["mode"] == "bot_api_socks5"
    assert facts["proxy_host"] == "127.0.0.1"
    assert facts["proxy_has_password"] is True
    assert "p" not in (facts["proxy_username"] or "")


def test_transport_get_never_returns_password(client):
    _transport(
        client,
        proxy_host="proxy.example.com", proxy_port=1080,
        proxy_username="alice", proxy_password="sekrit",
    )
    got = client.get("/api/telegram/transport").json()
    assert got["proxy_host"] == "proxy.example.com"
    assert got["proxy_port"] == 1080
    assert got["proxy_username"] == "alice"
    assert got["proxy_has_password"] is True
    blob = str(got)
    assert "sekrit" not in blob, "password must never leave the SecretStore"


def test_network_save_does_not_create_polling_consumer(client, data_dir):
    """The exact-one-poller invariant: network bootstrap without a
    token must not install an adapter or start a polling task."""
    _transport(
        client,
        proxy_host="127.0.0.1", proxy_port=59998,
    )
    runtime = client.app.state.runtime
    sup = runtime._supervisors.get("telegram_listener")
    task = getattr(sup, "_task", None)
    # Never a live poller after a tokenless network save.
    assert task is None or task.done()
    # And the runtime adapter is NOT a real polling Bot API adapter.
    adapter = runtime.telegram_adapter
    real_polling = (
        adapter.is_real()
        and getattr(adapter, "_polling_task", None) is not None
        and not adapter._polling_task.done()
    )
    assert not real_polling, "tokenless save must not start a poller"


def test_direct_mode_saved_without_token(client):
    """Direct mode can be saved without a bot token; the
    reachability probe fails against the controlled dead URL — the
    answer is sanitized and honest."""
    resp = client.post(
        "/api/telegram/transport",
        json={"mode": "bot_api_direct", "skip_bot_probe": True},
    )
    assert resp.status_code == 502
    body = resp.json()
    assert body["reachable"] is False
    assert "напрямую" in body["detail"].lower() or "недоступен" in body["detail"].lower()


def test_getme_uses_selected_transport_when_token_present(client, monkeypatch):
    """With a token present, transport setup goes through the probed
    selection (getMe THROUGH the transport). Unreachable proxy =>
    transport setup fails with 400 — no silent direct fallback."""
    client.app.state.secrets.set_secret("telegram_bot_token", "123:x")
    resp = _transport(
        client,
        proxy_host="127.0.0.1", proxy_port=59998,
    )
    assert resp.status_code == 400, (
        f"getMe through the dead proxy must fail: {resp.text}"
    )
    assert "transport setup failed" in resp.json()["detail"]
