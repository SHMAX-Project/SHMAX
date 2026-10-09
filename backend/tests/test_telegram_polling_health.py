"""P0 #2 — Telegram health must reflect the real polling task.

A dead polling task behind a successful getMe previously reported
HEALTHY. Pinned here:

* health() reports polling=False / degraded when the polling task
  is done or crashed;
* integration evidence becomes DEGRADED (never HEALTHY);
* the wizard Test listener_alive check FAILS in that state.
"""

from __future__ import annotations

import asyncio

import pytest

from shmax.adapters.telegram.bot_api import TelegramBotApiAdapter
from shmax.adapters.telegram.config import TransportConfig, TransportMode
from shmax.integration_status import evidence_state


def _started_adapter_with_dead_polling(monkeypatch) -> TelegramBotApiAdapter:
    """A started adapter whose getMe succeeds but whose polling
    task has finished (crashed)."""

    class _Me:
        id = 424242
        username = "bot"
        is_bot = True

    class _Bot:
        async def get_me(self):
            return _Me()

    adapter = TelegramBotApiAdapter(
        TransportConfig(mode=TransportMode.BOT_API_DIRECT, bot_token="1:x")
    )
    adapter._bot = _Bot()
    adapter._started = True

    async def _crashed():
        raise RuntimeError("polling loop exploded")

    adapter._polling_task = asyncio.get_running_loop().create_task(_crashed())
    return adapter


@pytest.mark.asyncio
async def test_health_reports_dead_polling_unhealthy(monkeypatch):
    adapter = _started_adapter_with_dead_polling(monkeypatch)
    # Let the crashed task finish.
    await asyncio.sleep(0.05)
    health = await adapter.health()
    assert health["connected"] is True
    assert health["authenticated"] is True
    assert health["bot_id"] is not None
    # The receive path is DEAD — health must say so.
    assert health["polling"] is False
    assert health.get("degraded") is True
    assert health.get("polling_error")


@pytest.mark.asyncio
async def test_dead_polling_is_never_healthy_evidence(monkeypatch):
    adapter = _started_adapter_with_dead_polling(monkeypatch)
    await asyncio.sleep(0.05)
    health = await adapter.health()
    now = 0.0
    probe = {"adapter": adapter, "at": now, "error": False, "verified": True, "health": health}
    snap = {"state": type("S", (), {"value": "healthy"})()}
    state = evidence_state(True, adapter, probe, snap, now)
    assert state == "DEGRADED", "dead polling must degrade the transport"


@pytest.mark.asyncio
async def test_wizard_listener_alive_fails_on_dead_polling(monkeypatch):
    """The wizard Test step's listener check must FAIL when the
    adapter's polling task is dead even if the supervisor runs."""

    adapter = _started_adapter_with_dead_polling(monkeypatch)
    await asyncio.sleep(0.05)

    class _Sup:
        _task = None

    sup = _Sup()

    class _Loop:
        def call_later(self, *a, **k):
            pass

    # The wizard check reads the supervisor task and the adapter
    # health; emulate the check the endpoint performs.
    supervisor_alive = True
    tg_health = await adapter.health()
    tg_listener_alive = supervisor_alive and tg_health.get("polling") is not False
    assert tg_listener_alive is False


# ----------------------------------------------------------------------
# P1 #1 (surgical) — cancelled polling + watchdog
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cancelled_polling_health_is_structured_and_safe():
    """A CANCELLED polling task: health() returns a structured
    unhealthy result and NO CancelledError escapes."""

    class _Me:
        id = 424242
        username = "bot"
        is_bot = True

    class _Bot:
        async def get_me(self):
            return _Me()

    adapter = TelegramBotApiAdapter(
        TransportConfig(mode=TransportMode.BOT_API_DIRECT, bot_token="1:x")
    )
    adapter._bot = _Bot()
    adapter._started = True

    async def _hang():
        await asyncio.sleep(3600)

    task = asyncio.get_running_loop().create_task(_hang())
    adapter._polling_task = task
    await asyncio.sleep(0.01)
    task.cancel()
    with __import__("contextlib").suppress(asyncio.CancelledError):
        await task

    health = await adapter.health()  # must not raise
    assert health["connected"] is True
    assert health["polling"] is False
    assert "cancelled" in (health.get("polling_error") or "")


@pytest.mark.asyncio
async def test_watchdog_treats_dead_polling_as_failure():
    """The Telegram watchdog requires connected+authenticated+polling;
    a dead polling task drives a watchdog failure snapshot."""
    from shmax.watchdog.base import AdapterHealthState
    from shmax.watchdog.telegram_watchdog import TelegramWatchdog

    class _Store:
        def __init__(self):
            self.snapshots = {}

        def load(self, name):
            return self.snapshots.get(name)

        def save(self, snap):
            self.snapshots[snap.adapter] = snap

    class _Adapter:
        def is_real(self):
            return True

        async def health(self):
            return {
                "connected": True,
                "authenticated": True,
                "bot_id": "424242",
                "polling": False,  # dead receive path
                "polling_error": "polling task finished",
                "transport": "bot_api",
            }

    store = _Store()
    wd = TelegramWatchdog(adapter=_Adapter(), store=store, dispatcher=None)
    snap = await wd.tick()
    assert snap.state in (AdapterHealthState.DEGRADED, AdapterHealthState.DOWN), (
        f"dead polling must be a watchdog failure, got {snap.state}"
    )
