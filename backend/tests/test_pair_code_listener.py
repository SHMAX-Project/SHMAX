"""Final repair #3 — /api/telegram/group/pair-code must return a
pairing code ONLY when the persistent Telegram listener is
CONFIRMED alive (bounded wait), never on a failed start; a retry
after a failed start must work and must keep EXACTLY ONE poller.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from shmax.api.app import create_app, PAIR_LISTENER_READY_TIMEOUT
from shmax.config import RuntimeMode, Settings


class _FakeListenerAdapter:
    """Adapter double with explicit connect() behavior.

    connect_ok=False → connect raises (persistent start failure).
    connect_ok=True  → connect sets _started and spawns a live
    polling task (exactly what the runtime health check requires).
    """

    def __init__(self, registry: list, connect_ok: bool):
        self._registry = registry
        self._connect_ok = connect_ok
        self._started = False
        self._polling_task: asyncio.Task | None = None
        self.on_command = None
        registry.append(self)

    def is_real(self) -> bool:
        return True

    def set_on_command(self, handler) -> None:
        self.on_command = handler

    async def connect(self) -> None:
        if not self._connect_ok:
            raise ConnectionError("persistent connect failed (simulated)")
        if self._started:
            return
        self._started = True
        self._polling_task = asyncio.create_task(
            asyncio.sleep(3600), name="fake.telegram.polling"
        )

    async def listen_events(self):
        # A real listener blocks receiving updates; the runtime
        # loop stays inside this async generator.
        while True:
            await asyncio.sleep(3600)
            yield None

    async def disconnect(self) -> None:
        self._started = False
        if self._polling_task is not None:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._polling_task = None

    async def health(self) -> dict:
        return {"state": "HEALTHY" if self._started else "DOWN"}

    def live_pollers(self) -> int:
        return sum(
            1
            for a in self._registry
            if a._polling_task is not None and not a._polling_task.done()
        )


def _patch_select_probed(monkeypatch, queue: list["_FakeListenerAdapter"]):
    """select_probed returns the next scripted adapter."""
    from shmax.adapters.telegram.manager import TelegramTransportManager

    async def _fake(self, *a, **k):
        if not queue:
            raise AssertionError("scripted adapter queue exhausted")
        return queue.pop(0)

    monkeypatch.setattr(
        TelegramTransportManager, "select_probed", _fake, raising=True
    )


@pytest.fixture()
def client(data_dir, monkeypatch):
    # Shorten the bounded wait — failures must be detected fast.
    monkeypatch.setattr(
        "shmax.api.app.PAIR_LISTENER_READY_TIMEOUT", 1.5, raising=True
    )
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
            json={"token": token, "username": "owner",
                  "password": "owner-pass-12345"},
        )
        c.headers.update({"X-CSRF-Token": resp.json()["csrf_token"]})
        # The operator's bot token is verified/known before the
        # pairing step (the listener activation path requires it).
        c.app.state.secrets.set_secret("telegram_bot_token", "123:x")
        yield c


def _pair_code(client):
    return client.post("/api/telegram/group/pair-code")


def test_pair_code_fails_when_connect_fails(client, monkeypatch):
    """Regression A: probe succeeds, persistent connect fails →
    NOT 200, no usable pairing code, pollers == 0."""
    registry: list[_FakeListenerAdapter] = []
    adapters = [_FakeListenerAdapter(registry, connect_ok=False)]
    _patch_select_probed(monkeypatch, adapters)

    resp = _pair_code(client)
    assert resp.status_code == 502, resp.text
    assert "listener" in resp.json()["detail"].lower()
    assert "code" not in resp.json(), "no pairing code may be issued"
    # Rollback ran: zero live pollers.
    assert registry[0].live_pollers() == 0
    sup = client.app.state.runtime._supervisors.get("telegram_listener")
    assert sup is None or sup._task is None or sup._task.done()


def test_pair_code_retry_after_failed_start(client, monkeypatch):
    """Regression B: first persistent connect fails, second
    attempt succeeds → exactly one poller, code returned, the
    command handler is wired (so /shmax_connect can be received)."""
    registry: list[_FakeListenerAdapter] = []
    adapters = [
        _FakeListenerAdapter(registry, connect_ok=False),
        _FakeListenerAdapter(registry, connect_ok=True),
    ]
    _patch_select_probed(monkeypatch, adapters)

    first = _pair_code(client)
    assert first.status_code == 502

    second = _pair_code(client)
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["code"], "a usable pairing code must be returned"
    # EXACTLY one live poller across all attempts.
    assert registry[1].live_pollers() == 1
    # The /shmax_connect handler is wired onto the LIVE adapter.
    assert registry[1].on_command is not None


def test_pair_code_repeat_with_healthy_listener_no_second_poller(
    client, monkeypatch
):
    """Regression C: listener already healthy → repeat pair-code →
    still exactly one poller, code returned again."""
    registry: list[_FakeListenerAdapter] = []
    healthy = _FakeListenerAdapter(registry, connect_ok=True)
    _patch_select_probed(monkeypatch, [healthy])

    first = _pair_code(client)
    assert first.status_code == 200, first.text
    code_one = first.json()["code"]
    poller_task = healthy._polling_task

    # A scripted adapter for a second install must NOT be consumed:
    # the healthy manager is reused (no reinstall path).
    second = _pair_code(client)
    assert second.status_code == 200, second.text
    assert second.json()["code"]
    assert healthy._polling_task is poller_task, (
        "healthy listener must not be restarted"
    )
    assert healthy.live_pollers() == 1
