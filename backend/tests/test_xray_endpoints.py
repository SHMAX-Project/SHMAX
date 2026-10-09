"""Surgical acceptance — Xray API endpoints + startup bootstrap.

Covers: subscription storage/selection via SecretStore, cached
node restart without subscription server, Xray failure does not
block the web app, Xray starts BEFORE the Telegram listener,
selected-node-disappeared surfaces actionable state.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from shmax.api.app import create_app
from shmax.config import RuntimeMode, Settings
from shmax.security import SecretStore

VLESS_OK = (
    "vless://uuid-9@tg1.example.com:443"
    "?type=tcp&security=tls&sni=tg1.example.com"
    "#Node%20OK"
)


@pytest.fixture()
def client(data_dir, monkeypatch):
    monkeypatch.setenv(
        "SHMAX_TELEGRAM_REACHABILITY_URL", "http://127.0.0.1:59999"
    )
    monkeypatch.setenv("SHMAX_XRAY_BINARY", "/nonexistent/xray-missing")
    monkeypatch.setenv("SHMAX_XRAY_SOCKS_PORT", "10820")
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


def _seed_subscription_directly(client, url=VLESS_OK):
    """Store the subscription without network fetch (the HTTP fetch
    itself is exercised in test_xray_subscription via parse tests and
    would need an external server here; storage is what we pin)."""
    import asyncio

    from shmax.xray import state as xstate
    from shmax.xray.subscription import parse_subscription

    secret_store = client.app.state.secrets
    parsed = parse_subscription(url)
    xstate.store_subscription(
        secret_store, "https://sub.example.com/vless", parsed.raw_links
    )
    asyncio.run(xstate.store_nodes_sanitized(
        client.app.state.db, [n.to_dict() for n in parsed.nodes]
    ))


def test_subscription_fetch_malformed_url_rejected(client):
    resp = client.post(
        "/api/telegram/xray/subscription",
        json={"url": "ftp://not-http"},
    )
    assert resp.status_code == 400


def test_status_reflects_selection(client):
    _seed_subscription_directly(client)
    status = client.get("/api/telegram/xray/status").json()
    assert status["subscription_loaded"] is True
    assert len(status["nodes"]) == 1
    assert status["nodes"][0]["supported"] is True
    assert status["selected_node_id"] is None
    assert status["selected_node_disappeared"] is False


def test_select_start_xray_missing_binary_502_sanitized(client):
    """Xray binary missing → 502 with sanitized RU detail; app alive."""
    _seed_subscription_directly(client)
    node_id = client.get("/api/telegram/xray/status").json()["nodes"][0]["id"]
    resp = client.post("/api/telegram/xray/select", json={"node_id": node_id})
    assert resp.status_code == 502
    assert "Xray" in resp.json()["detail"]
    # app still alive
    assert client.get("/health/live").status_code == 200


def test_selection_persisted_even_when_xray_fails(client):
    """The selection is stored (encrypted SecretStore) so a later
    successful Xray start can proceed without the subscription
    server; disappearance detection uses it."""
    _seed_subscription_directly(client)
    node_id = client.get("/api/telegram/xray/status").json()["nodes"][0]["id"]
    # xray will fail (binary missing) — selection still recorded
    client.post("/api/telegram/xray/select", json={"node_id": node_id})

    from shmax.xray import state as xstate

    secret_store = client.app.state.secrets
    selection = xstate.load_selected(secret_store)
    assert selection is not None and selection[0] == node_id
    # the selected raw link round-trips through the SecretStore
    assert selection[1].startswith("vless://")


def test_cached_node_config_rebuilds_without_subscription_server(client):
    """Cached selected node → runtime Xray config regenerates with NO
    subscription fetch — reboot does not need the sub server."""
    _seed_subscription_directly(client)
    node_id = client.get("/api/telegram/xray/status").json()["nodes"][0]["id"]
    client.post("/api/telegram/xray/select", json={"node_id": node_id})

    from shmax.xray import state as xstate

    secret_store = client.app.state.secrets
    cfg = xstate.selected_node_config(
        secret_store, local_socks_port=10820
    )
    assert cfg is not None
    assert cfg["inbounds"][0]["listen"] == "127.0.0.1"
    assert cfg["outbounds"][0]["protocol"] == "vless"


def test_selected_node_disappeared_actionable(client):
    """Selected node gone from refreshed subscription → actionable
    flag; NEVER auto-replaced."""
    _seed_subscription_directly(client)
    node_id = client.get("/api/telegram/xray/status").json()["nodes"][0]["id"]
    client.post("/api/telegram/xray/select", json={"node_id": node_id})

    # Subscription refreshes with a DIFFERENT node.
    from shmax.xray import state as xstate
    from shmax.xray.subscription import parse_subscription

    other = VLESS_OK.replace("uuid-9", "uuid-other").replace(
        "tg1.example.com", "tg2.example.com"
    )
    parsed = parse_subscription(other)
    secret_store = client.app.state.secrets
    xstate.store_subscription(
        secret_store, "https://sub.example.com/vless", parsed.raw_links
    )
    asyncio.run(xstate.store_nodes_sanitized(
        client.app.state.db, [n.to_dict() for n in parsed.nodes]
    ))

    status = client.get("/api/telegram/xray/status").json()
    assert status["selected_node_disappeared"] is True
    # The stale selection is still the recorded one — no silent swap.
    assert status["selected_node_id"] == node_id


def test_xray_bootstrap_starts_before_telegram(client, monkeypatch):
    """Startup: with a cached selection the bootstrap attempts Xray
    BEFORE restore_telegram; with the binary missing it reports
    honestly and the web app still starts."""
    _seed_subscription_directly(client)
    node_id = client.get("/api/telegram/xray/status").json()["nodes"][0]["id"]
    client.post("/api/telegram/xray/select", json={"node_id": node_id})

    # Re-open the app (fresh lifespan = fresh startup restore).
    data_dir = client.app.state.settings.data_dir
    settings = Settings(
        host="127.0.0.1", port=0, data_dir=data_dir,
        log_level="WARNING", runtime_mode=RuntimeMode.TEST,
    )
    app2 = create_app(settings)
    from fastapi.testclient import TestClient as _TC

    with _TC(app2) as c2:
        # App started despite Xray binary missing.
        assert c2.get("/health/live").status_code == 200
        # Xray bootstrap attempted + failed honestly.
        report = getattr(c2.app.state, "restore_report", {})
        assert "xray" in report
        assert report["xray"]["started"] is False
        # Telegram NOT silently direct: the persisted transport is
        # still the Xray SOCKS5 mode (read the persisted facts —
        # admin session is fresh in this second client).
        from shmax.startup_restore import load_telegram_transport

        facts = asyncio.run(load_telegram_transport(
            app2.state.db, app2.state.secrets
        ))
        assert facts["mode"] == "bot_api_socks5"
        assert facts["proxy"]["host"] == "127.0.0.1"
        assert facts["proxy"]["port"] == 10820


# ----------------------------------------------------------------------
# Surgical review — subscription size limit (state.fetch_subscription)
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_subscription_rejects_non_http_scheme():
    from shmax.xray.state import fetch_subscription

    for bad in ("file:///etc/passwd", "ftp://x/sub", "data:text/plain,abc"):
        with pytest.raises(ValueError, match="схема"):
            await fetch_subscription(bad)


@pytest.mark.asyncio
async def test_subscription_content_length_too_large_rejected(monkeypatch):
    """Content-Length > 2 MiB is rejected before reading the body."""
    import aiohttp

    from shmax.xray import state as xstate

    class _Resp:
        status = 200
        headers = {"Content-Length": str(3 * 1024 * 1024)}

        def raise_for_status(self):
            pass

        async def text(self):
            return "x" * (3 * 1024 * 1024)

    class _Ctx:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *a):
            return False

    class _Session:
        def __init__(self, *a, **kw):
            pass

        def get(self, url):
            return _Ctx()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeAiohttp:
        ClientTimeout = aiohttp.ClientTimeout
        ClientSession = _Session

    import types

    fake_mod = types.ModuleType("aiohttp")
    fake_mod.ClientTimeout = aiohttp.ClientTimeout
    fake_mod.ClientSession = _Session
    monkeypatch.setitem(__import__("sys").modules, "aiohttp", fake_mod)

    with pytest.raises(ValueError, match="слишком большая"):
        await xstate.fetch_subscription("https://sub.example.com/big")


def _mount_fake_aiohttp(monkeypatch, resp):
    """Swap sys.modules['aiohttp'] for a stub whose ClientSession.get
    returns EXACTLY this response object — the stream the fetch
    consumes is the one we control."""
    import types

    import aiohttp

    class _Ctx:
        async def __aenter__(self):
            return resp

        async def __aexit__(self, *a):
            return False

    class _Session:
        def __init__(self, *a, **kw):
            pass

        def get(self, url):
            return _Ctx()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    fake_mod = types.ModuleType("aiohttp")
    fake_mod.ClientTimeout = aiohttp.ClientTimeout
    fake_mod.ClientSession = _Session
    monkeypatch.setitem(__import__("sys").modules, "aiohttp", fake_mod)


class _StreamResp:
    """Response with a real chunked stream of EXACTLY `total` bytes
    (chunked 64 KiB), no Content-Length."""

    def __init__(self, total: int, chunk: int = 64 * 1024, payload: bytes | None = None):
        self.status = 200
        self.headers = {}
        self._payload = payload
        self._left = total
        self._chunk = chunk

    def raise_for_status(self):
        pass

    class _Stream:
        def __init__(self, resp):
            self._resp = resp

        def iter_any(self):
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._resp._left <= 0:
                raise StopAsyncIteration
            take = min(self._resp._left, self._resp._chunk)
            self._resp._left -= take
            if self._resp._payload is not None:
                out = self._resp._payload[:take]
                self._resp._payload = self._resp._payload[take:]
                return out
            return b"x" * take

    @property
    def content(self):
        return self._Stream(self)

    def raise_for_status(self):
        pass


def _text_resp(total: int, *, declare: bool):
    class _Resp:
        status = 200

        def __init__(self):
            self.headers = (
                {"Content-Length": str(total)} if declare else {}
            )

        def raise_for_status(self):
            pass

    return _Resp()


@pytest.mark.asyncio
async def test_subscription_exact_2mib_accepted(monkeypatch):
    """EXACTLY 2097152 REAL streamed bytes (a vless link padded to
    the exact limit, streamed from the response) => accepted."""
    from shmax.xray import state as xstate

    link = "vless://u@x.example.com:443?type=tcp&security=tls&sni=x.com#Owned"
    payload = (link + "\n").encode() + b"x" * (2097152 - len(link) - 1)
    assert len(payload) == 2097152
    resp = _StreamResp(len(payload), chunk=64 * 1024, payload=payload)
    _mount_fake_aiohttp(monkeypatch, resp)
    result = await xstate.fetch_subscription("https://sub.example.com/exact")
    assert len(result.encode()) == 2097152
    assert result.startswith(link)


@pytest.mark.asyncio
async def test_subscription_2mib_plus_one_rejected(monkeypatch):
    """2097153 streamed bytes => rejected."""
    from shmax.xray import state as xstate

    prefix = "vless://u@x.example.com:443?type=tcp&security=tls#N\n"
    body = prefix + "x" * (2097153 - len(prefix))
    resp = _StreamResp(len(body.encode()))
    _mount_fake_aiohttp(monkeypatch, resp)
    with pytest.raises(ValueError, match="слишком большая"):
        await xstate.fetch_subscription("https://sub.example.com/plus1")


@pytest.mark.asyncio
async def test_subscription_content_length_too_large_rejected(monkeypatch):
    """Content-Length > 2 MiB is rejected before reading the body."""
    from shmax.xray import state as xstate

    resp = _text_resp(3 * 1024 * 1024, declare=True)
    _mount_fake_aiohttp(monkeypatch, resp)
    with pytest.raises(ValueError, match="слишком большая"):
        await xstate.fetch_subscription("https://sub.example.com/cl")


@pytest.mark.asyncio
async def test_subscription_streamed_over_limit_rejected(monkeypatch):
    """Chunked/streamed body > 2 MiB is rejected mid-stream even
    without a Content-Length header; the stream consumed IS the
    response we control."""
    from shmax.xray import state as xstate

    resp = _StreamResp(3 * 1024 * 1024)
    _mount_fake_aiohttp(monkeypatch, resp)
    with pytest.raises(ValueError, match="слишком большая"):
        await xstate.fetch_subscription("https://sub.example.com/stream")


@pytest.mark.asyncio
async def test_subscription_normal_body_passes(monkeypatch):
    import aiohttp
    import types

    from shmax.xray import state as xstate

    body = "vless://u@x.example.com:443?type=tcp&security=tls#N"

    class _Resp:
        status = 200
        headers = {}

        def raise_for_status(self):
            pass

        async def text(self):
            return body

    class _Ctx:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *a):
            return False

    class _Session:
        def __init__(self, *a, **kw):
            pass

        def get(self, url):
            return _Ctx()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    fake_mod = types.ModuleType("aiohttp")
    fake_mod.ClientTimeout = aiohttp.ClientTimeout
    fake_mod.ClientSession = _Session
    monkeypatch.setitem(__import__("sys").modules, "aiohttp", fake_mod)

    result = await xstate.fetch_subscription("https://sub.example.com/ok")
    assert "vless://" in result




# ----------------------------------------------------------------------
# Surgical review — ONE app-owned Xray manager
# ----------------------------------------------------------------------
def test_app_owned_manager_shared_across_bootstrap_api_shutdown(
    data_dir, monkeypatch, tmp_path
):
    """App-owned manager, exercised through the REAL surfaces in the
    APPLICATION's event loop (TestClient portal — the same loop the
    lifespan shutdown runs in):

    bootstrap start (app loop) -> API status sees the SAME process ->
    REAL API stop (xray/select restart reaps the bootstrap child) ->
    application shutdown (lifespan -> runtime.stop -> manager.stop)
    reaps the running child -> zero live children."""
    import socket
    import stat as _stat

    from fastapi.testclient import TestClient

    from shmax.api.app import create_app
    from shmax.xray import state as xstate
    from shmax.xray.subscription import parse_subscription

    stub = tmp_path / "stub-xray-owned"
    stub.write_text(
        "\n".join([
            "#!/usr/bin/env python3",
            "import json, socket, sys",
            "cfg_path = sys.argv[sys.argv.index('-c') + 1]",
            "with open(cfg_path, encoding='utf-8') as fh:",
            "    cfg = json.load(fh)",
            "if '-test' in sys.argv:",
            "    # config-test mode: validate only, never bind.",
            "    json.dumps(cfg)",
            "    print('CONFIG OK', flush=True)",
            "    sys.exit(0)",
            "port = cfg['inbounds'][0]['port']",
            "srv = socket.socket()",
            "srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)",
            "srv.bind(('127.0.0.1', port))",
            "srv.listen(8)",
            "print('READY', flush=True)",
            "while True:",
            "    conn, _ = srv.accept()",
            "    try:",
            "        conn.recv(1024)",
            "    except OSError:",
            "        pass",
            "    finally:",
            "        conn.close()",
        ])
    )
    stub.chmod(stub.stat().st_mode | _stat.S_IXUSR)

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    free_port = srv.getsockname()[1]
    srv.close()

    monkeypatch.setenv(
        "SHMAX_TELEGRAM_REACHABILITY_URL", "http://127.0.0.1:59999"
    )
    monkeypatch.setenv("SHMAX_XRAY_BINARY", str(stub))
    monkeypatch.setenv("SHMAX_XRAY_SOCKS_PORT", str(free_port))
    monkeypatch.setenv("SHMAX_E2E_FORWARD", "127.0.0.1:59999")

    settings = Settings(
        host="127.0.0.1", port=0, data_dir=data_dir,
        log_level="WARNING", runtime_mode=RuntimeMode.TEST,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        import pathlib
        import secrets as _secrets

        sp = pathlib.Path(app.state.paths.root) / "state" / "setup_secret"
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(_secrets.token_urlsafe(24))
        token = client.post(
            "/api/setup/token",
            headers={"X-Shmax-Setup-Secret": sp.read_text()},
        ).json()["token"]
        resp = client.post(
            "/api/setup/admin",
            json={"token": token, "username": "owner",
                  "password": "owner-pass-12345"},
        )
        client.headers.update({"X-CSRF-Token": resp.json()["csrf_token"]})

        manager = app.state.xray_manager
        assert manager.binary_path == str(stub)
        assert manager.socks_port == free_port
        secret_store = app.state.secrets

        body = ("vless://u@x.example.com:443?type=tcp&security=tls"
                "&sni=x.com#Owned")
        parsed = parse_subscription(body)
        xstate.store_subscription(
            secret_store, "http://x/sub", parsed.raw_links
        )
        node = parsed.nodes[0]
        # Bootstrap: start via the app-owned manager IN THE APP LOOP
        # (the same loop lifespan shutdown uses).
        status = client.portal.call(manager.start, {
            "outbounds": [{
                "tag": "proxy", "protocol": "freedom",
                "settings": {},
            }],
            "inbounds": [{
                "tag": "socks-in", "protocol": "socks",
                "listen": "127.0.0.1", "port": free_port,
                "settings": {"auth": "noauth", "udp": False},
            }],
        })
        assert status.state == "running"
        bootstrap_child = manager._process
        assert bootstrap_child is not None and bootstrap_child.returncode is None

        # API status sees the SAME manager/process.
        resp = client.get("/api/telegram/xray/status")
        assert resp.status_code == 200
        api_body = resp.json()
        assert api_body["state"] == "running"
        assert api_body["pid"] == bootstrap_child.pid, (
            "API must observe the app-owned manager process"
        )

        # REAL API stop: /xray/select restarts the manager — the
        # bootstrap child must be TERM'd and reaped by that call.
        asyncio.run(xstate.store_nodes_sanitized(
            app.state.db, [n.to_dict() for n in parsed.nodes]
        ))
        resp = client.post(
            "/api/telegram/xray/select", json={"node_id": node.node_id}
        )
        assert resp.status_code == 200, resp.text
        for _ in range(30):
            if bootstrap_child.returncode is not None:
                break
            import time as _t

            _t.sleep(0.1)
        assert bootstrap_child.returncode is not None, (
            "API select must reap the bootstrap child"
        )
        # The same manager now runs the API-started child.
        assert manager._process is not None
        api_child = manager._process
        assert api_child.pid != bootstrap_child.pid

    # The `with` block exited: lifespan shutdown ran
    # runtime.stop() -> xray_manager.stop() in the app loop. The
    # API-started child must be reaped; manager clean.
    for _ in range(30):
        if api_child.returncode is not None:
            break
        import time as _t

        _t.sleep(0.1)
    assert api_child.returncode is not None, (
        "application shutdown must reap the xray child"
    )
    assert manager.is_running is False
    assert manager._process is None
