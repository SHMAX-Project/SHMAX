"""Surgical P0 — FIRST bot verify must use the SAVED network.

Regression for SOCKS5 (real local SOCKS server), plus HTTP and Xray
variants through the same structural path:

network saved (no runtime adapter)
→ first /api/telegram/verify
→ getMe goes THROUGH the saved transport (proxy used)
→ zero direct Telegram requests (direct connector disabled in-test).

Deterministic: the fake SOCKS server answers the CONNECT handshake
itself with a canned getMe response — verify can only succeed if the
request traversed the proxy.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest
from fastapi.testclient import TestClient

from shmax.api.app import create_app
from shmax.config import RuntimeMode, Settings

_GETME_JSON = json.dumps({
    "ok": True,
    "result": {
        "id": 424242, "is_bot": True,
        "first_name": "Bot", "username": "saved_net_bot",
        "can_join_groups": True,
    },
})


class _FakeSocks:
    """Minimal SOCKS5 (no-auth) server that answers CONNECT itself
    with a canned HTTP response — proving the traffic traversed it."""

    def __init__(self, port: int):
        self.port = port
        self.handshakes = 0
        self._server = None
        self._thread = None

    def start(self) -> None:
        import socket

        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", self.port))
        srv.listen(8)
        self._server = srv

        def serve():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                threading.Thread(
                    target=self._handle, args=(conn,), daemon=True
                ).start()

        self._thread = threading.Thread(target=serve, daemon=True)
        self._thread.start()

    def _handle(self, conn):
        try:
            data = conn.recv(1024)
            print("SOCKS-TRACE greet:", data.hex()[:40], flush=True)
            if not data or data[0] != 5:
                conn.close()
                return
            conn.sendall(b"\x05\x00")
            req = conn.recv(1024)
            print("SOCKS-TRACE req:", req.hex()[:64], len(req), flush=True)
            if len(req) < 10 or req[1] != 1:
                conn.close()
                return
            self.handshakes += 1
            conn.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
            # Wait for the client's HTTP request BEFORE responding —
            # writing early races aiohttp_socks' stream switch and
            # the client drops the buffered early bytes.
            conn.settimeout(3)
            try:
                conn.recv(65536)
            except OSError:
                pass
            body = _GETME_JSON.encode()
            conn.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n".encode()
                + b"Connection: close\r\n\r\n" + body
            )
            # Give the client time to read before closing — an
            # immediate close races aiohttp's response parse.
            import time as _time

            _time.sleep(0.5)
            try:
                conn.settimeout(0.3)
                conn.recv(4096)
            except OSError:
                pass
        except OSError:
            pass
        finally:
            conn.close()

    def stop(self):
        if self._server:
            self._server.close()


@pytest.fixture()
def client(data_dir, monkeypatch):
    monkeypatch.setenv(
        "SHMAX_TELEGRAM_REACHABILITY_URL", "http://127.0.0.1:59999"
    )
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


def _plain_http_base(monkeypatch):
    """Point the Bot API base at plain HTTP: the fake SOCKS answers
    CONNECT with a canned plain-HTTP response, so aiogram must NOT
    attempt TLS through the proxy."""
    monkeypatch.setenv("SHMAX_TELEGRAM_API_BASE", "http://telegram.internal/{token}/{method}")


def _save_network(client, mode="bot_api_socks5", **proxy):
    resp = client.post(
        "/api/telegram/transport",
        json={"mode": mode, "skip_bot_probe": True, **proxy},
    )
    assert resp.status_code in (200, 502)  # reachability may fail; saved anyway
    # Confirm the SAVED MODE is exactly what was requested and NO
    # runtime adapter was installed (the P0 precondition).
    facts = client.get("/api/telegram/transport").json()
    assert facts["mode"] == mode, f"saved mode must be {mode}"
    runtime = client.app.state.runtime
    assert runtime.telegram_adapter.is_real() is False, (
        "precondition broken: runtime adapter exists before first verify"
    )


class _FakeHttpProxy:
    """Minimal HTTP proxy: accepts the absolute-URI request aiogram's
    AiohttpSession sends for proxy=http://... and answers with a
    canned getMe — proving the traffic traversed the configured
    HTTP proxy."""

    def __init__(self, port: int):
        self.port = port
        self.requests = 0
        self._server = None

    def start(self) -> None:
        import socket

        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", self.port))
        srv.listen(8)
        self._server = srv

        def serve():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                threading.Thread(
                    target=self._handle, args=(conn,), daemon=True
                ).start()

        threading.Thread(target=serve, daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(5)
            # Keep-alive loop: answer EVERY request on this
            # connection — closing after the first races aiohttp's
            # connection reuse (ServerDisconnectedError).
            while True:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    data += chunk
                # Count EVERY request: python-socks' HTTP proxy
                # path sends CONNECT first, then the tunneled
                # GET — either proves the traffic traversed the
                # configured proxy.
                self.requests += 1
                body = _GETME_JSON.encode()
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\n".encode()
                    + b"Connection: keep-alive\r\n\r\n" + body
                )
        except OSError:
            pass
        finally:
            conn.close()

    def stop(self):
        if self._server:
            self._server.close()


def _run_fake_socks(port: int) -> _FakeSocks:
    socks = _FakeSocks(port)
    socks.start()
    return socks


def _forbid_polling(monkeypatch):
    """Tripwires covering the WHOLE verify handler (including every
    post-probe action): connect(), select_probed() and
    install_telegram() must NEVER run during verify — any call
    fails the test with an AssertionError."""
    from shmax.adapters.telegram.bot_api import TelegramBotApiAdapter
    from shmax.adapters.telegram.manager import TelegramTransportManager
    from shmax.runtime import AppRuntime

    async def _tripwire(*a, **k):
        raise AssertionError(
            "verify must not call connect()/select_probed()/install_telegram()"
        )

    monkeypatch.setattr(TelegramBotApiAdapter, "connect", _tripwire, raising=False)
    monkeypatch.setattr(
        TelegramTransportManager, "select_probed", _tripwire, raising=False
    )
    monkeypatch.setattr(AppRuntime, "install_telegram", _tripwire, raising=False)


def _assert_no_listener(client):
    """After verify returns, still no persistent listener exists."""
    runtime = client.app.state.runtime
    assert runtime.telegram_manager is None, (
        "verify installed a telegram manager (listener activation)"
    )
    assert runtime.telegram_adapter.is_real() is False, (
        "verify installed a live adapter (polling listener)"
    )


def test_first_verify_socks5_uses_saved_network(client, monkeypatch):
    """SOCKS5: network saved → no runtime adapter → first verify →
    getMe THROUGH the proxy (fake SOCKS handshake observed), zero
    direct requests (direct connector disabled)."""
    socks = _run_fake_socks(10850)
    _plain_http_base(monkeypatch)
    try:
        _save_network(
            client,
            proxy_host="127.0.0.1", proxy_port=10850,
            proxy_username="u", proxy_password="p",
        )

        # Disable DIRECT connections: any direct Telegram attempt
        # fails with this marker; the proxy path (ProxyConnector)
        # is untouched.
        import aiohttp

        class _NoDirect(aiohttp.TCPConnector):
            def __init__(self, *a, **kw):
                raise RuntimeError("DIRECT-DISABLED-BY-TEST")

        monkeypatch.setattr(aiohttp, "TCPConnector", _NoDirect, raising=False)
        _forbid_polling(monkeypatch)

        resp = client.post("/api/telegram/verify", json={"token": "123:x"})
        print("SOCKS-TRACE verify:", resp.status_code, resp.text[:160], flush=True)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["verified"] is True
        assert body["username"] == "saved_net_bot"
        # The SOCKS handshake proves the request traversed the proxy.
        assert socks.handshakes >= 1, "getMe did not traverse the proxy"
        _assert_no_listener(client)
    finally:
        socks.stop()


def test_first_verify_http_uses_saved_network(client, monkeypatch):
    """HTTP mode REALLY persisted (bot_api_http): the first verify
    getMe traverses the configured HTTP proxy (fake proxy serves the
    canned response), zero direct requests, verify SUCCEEDS and no
    listener is activated."""
    http_proxy = _FakeHttpProxy(10851)
    http_proxy.start()
    try:
        _save_network(client, mode="bot_api_http",
                      proxy_host="127.0.0.1", proxy_port=10851)
        _plain_http_base(monkeypatch)

        import aiohttp

        class _NoDirect(aiohttp.TCPConnector):
            def __init__(self, *a, **kw):
                raise RuntimeError("DIRECT-DISABLED-BY-TEST")

        monkeypatch.setattr(aiohttp, "TCPConnector", _NoDirect, raising=False)
        _forbid_polling(monkeypatch)

        resp = client.post("/api/telegram/verify", json={"token": "123:x"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["verified"] is True
        assert body["username"] == "saved_net_bot"
        assert http_proxy.requests >= 1, (
            "getMe did not traverse the configured HTTP proxy"
        )
        _assert_no_listener(client)

        # Mode is STILL HTTP after the successful verify — no change.
        facts = client.get("/api/telegram/transport").json()
        assert facts["mode"] == "bot_api_http"
        assert facts["proxy_host"] == "127.0.0.1"
        assert facts["proxy_port"] == 10851
    finally:
        http_proxy.stop()


def test_first_verify_xray_uses_saved_network(client, tmp_path, monkeypatch):
    """Xray-managed network: the first verify ensures the local Xray
    (stub) is running from the cached node, then getMe traverses the
    local SOCKS."""
    import pathlib

    stub = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "e2e" / "stub-xray.py"
    if not stub.exists():
        pytest.skip("stub-xray not found")
    monkeypatch.setenv("SHMAX_E2E_FORWARD", "127.0.0.1:59999")

    socks = _run_fake_socks(10852)  # plays the local Xray SOCKS role
    _plain_http_base(monkeypatch)
    # Re-point the APP-OWNED manager at the stub binary (it was
    # created during lifespan before this env existed).
    client.app.state.xray_manager.binary_path = str(stub)
    try:
        # Save an Xray-managed network: transport socks5 @ local socks
        # + xray-managed marker in the SecretStore.
        from shmax.xray import state as xstate
        from shmax.xray.subscription import parse_subscription

        secret_store = client.app.state.secrets
        link = (
            "vless://u@x.example.com:443?type=tcp&security=tls"
            "&sni=x.com#N"
        )
        parsed = parse_subscription(link)
        xstate.store_subscription(secret_store, "http://x/sub", parsed.raw_links)
        node = parsed.nodes[0]
        xstate.store_selection(
            secret_store, node.node_id, parsed.raw_links[node.node_id]
        )
        _save_network(
            client,
            proxy_host="127.0.0.1", proxy_port=10852,
        )

        import aiohttp

        class _NoDirect(aiohttp.TCPConnector):
            def __init__(self, *a, **kw):
                raise RuntimeError("DIRECT-DISABLED-BY-TEST")

        monkeypatch.setattr(aiohttp, "TCPConnector", _NoDirect, raising=False)
        _forbid_polling(monkeypatch)

        resp = client.post("/api/telegram/verify", json={"token": "123:x"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["verified"] is True
        assert socks.handshakes >= 1, "getMe did not traverse the local SOCKS"
        _assert_no_listener(client)
    finally:
        socks.stop()
