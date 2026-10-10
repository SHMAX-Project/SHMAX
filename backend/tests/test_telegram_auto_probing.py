"""BLOCKER 6 — AUTO transport actually probes candidates.

The TelegramTransportManager must:
  * try each candidate (connect + health);
  * on failure disconnect/cleanup and move on;
  * never leave two listeners alive at the same time;
  * bound the total time per attempt (no infinite loop);
  * raise if every candidate fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shmax.adapters.telegram.config import (
    ProxyConfig,
    TransportConfig,
    TransportMode,
)
from shmax.adapters.telegram.manager import TelegramTransportManager


class _StubAdapter:
    """Stand-in for TelegramAdapter that records lifecycle events."""

    instances = []  # class-level: every constructed instance

    def __init__(
        self,
        name: str,
        *,
        connect_raises: Exception | None = None,
    ) -> None:
        self.name = name
        self._connect_raises = connect_raises
        self.connected = False
        self.disconnected = False
        self.probed = False
        _StubAdapter.instances.append(self)

    async def connect(self) -> None:
        if self._connect_raises is not None:
            raise self._connect_raises
        self.connected = True

    async def probe(self) -> None:
        # P1 #1: selection probes WITHOUT connecting — a connect
        # here would start a second getUpdates consumer.
        if self._connect_raises is not None:
            raise self._connect_raises
        self.probed = True

    async def disconnect(self) -> None:
        self.disconnected = True


@pytest.fixture(autouse=True)
def _reset_stubs():
    _StubAdapter.instances.clear()
    yield
    _StubAdapter.instances.clear()


def _patch_constructors(monkeypatch, factory):
    """Swap TelegramBotApiAdapter / TelegramMtProtoAdapter with
    factory(name, **kwargs)."""
    from shmax.adapters.telegram import manager

    monkeypatch.setattr(manager, "TelegramBotApiAdapter", factory)
    monkeypatch.setattr(manager, "TelegramMtProtoAdapter", factory)


def _stub_factory(name: str, *, connect_raises: Exception | None = None):
    """Build a stub via a keyword-free constructor (the manager
    doesn't pass kwargs)."""
    return _StubAdapter(name, connect_raises=connect_raises)


@pytest.mark.asyncio
async def test_first_succeeds_returns_it(monkeypatch):
    cfg = TransportConfig(
        mode=TransportMode.AUTO,
        bot_token="123:abc",
    )

    def factory(config):
        return _stub_factory("bot_api_direct")

    _patch_constructors(monkeypatch, factory)

    manager = TelegramTransportManager(cfg)
    adapter = await manager.select_probed()
    assert adapter.name == "bot_api_direct"
    # P1 #1: selected via probe() — unconnected, polling starts
    # only later in install_telegram's listener loop.
    assert adapter.probed is True
    assert adapter.connected is False
    assert adapter.disconnected is False  # we keep this one
    # Only one adapter was constructed.
    assert len(_StubAdapter.instances) == 1


@pytest.mark.asyncio
async def test_first_fails_second_msecond_returns_it(monkeypatch):
    cfg = TransportConfig(
        mode=TransportMode.AUTO,
        bot_token="123:abc",
        proxy=ProxyConfig(scheme="socks5", host="127.0.0.1", port=10808),
    )
    call_count = {"n": 0}

    def factory(config):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _stub_factory("first", connect_raises=RuntimeError("nope"))
        return _stub_factory("second")

    _patch_constructors(monkeypatch, factory)

    manager = TelegramTransportManager(cfg)
    adapter = await manager.select_probed()
    assert adapter.name == "second"
    # P1 #1: probing NEVER connects — a connect would start a
    # second getUpdates consumer next to the live listener. The
    # first candidate's probe FAILED (that is why it moved on);
    # neither candidate was ever connected.
    assert _StubAdapter.instances[0].connected is False
    assert _StubAdapter.instances[1].connected is False
    assert _StubAdapter.instances[1].probed is True
    assert _StubAdapter.instances[0].disconnected is False
    assert _StubAdapter.instances[1].disconnected is False


@pytest.mark.asyncio
async def test_all_candidates_fail_raises(monkeypatch):
    cfg = TransportConfig(
        mode=TransportMode.AUTO,
        bot_token="123:abc",
    )

    def factory(config):
        return _stub_factory("always_fail", connect_raises=RuntimeError("nope"))

    _patch_constructors(monkeypatch, factory)

    manager = TelegramTransportManager(cfg)
    with pytest.raises(RuntimeError, match="AUTO ladder exhausted"):
        await manager.select_probed()
    # P1 #1: failed candidates were only probed — never connected,
    # so nothing to disconnect (no polling consumer ever existed).
    for inst in _StubAdapter.instances:
        assert inst.connected is False
        assert inst.disconnected is False


@pytest.mark.asyncio
async def test_explicit_override_skips_ladder(monkeypatch):
    cfg = TransportConfig(
        mode=TransportMode.AUTO,
        bot_token="123:abc",
        proxy=ProxyConfig(scheme="http", host="127.0.0.1", port=10808),
    )

    def factory(config):
        return _stub_factory("bot_api_http")

    _patch_constructors(monkeypatch, factory)

    manager = TelegramTransportManager(cfg)
    adapter = await manager.select_probed(
        override_mode=TransportMode.BOT_API_HTTP
    )
    assert adapter.name == "bot_api_http"


@pytest.mark.asyncio
async def test_connect_timeout_is_bounded(monkeypatch):
    """A candidate that hangs on connect must not block forever."""
    import asyncio

    cfg = TransportConfig(
        mode=TransportMode.AUTO,
        bot_token="123:abc",
    )

    class Hangs:
        async def connect(self):
            await asyncio.sleep(60)

        async def disconnect(self):
            pass

    def factory(config):
        return Hangs()

    _patch_constructors(monkeypatch, factory)

    manager = TelegramTransportManager(cfg)
    # 100ms is enough to surface the timeout, then we move on.
    with pytest.raises(RuntimeError, match="AUTO ladder exhausted"):
        await manager.select_probed(connect_timeout=0.1)


@pytest.mark.asyncio
async def test_explicit_mode_skips_ladder(monkeypatch):
    """When ``mode`` is not AUTO, only that one mode is tried."""
    cfg = TransportConfig(
        mode=TransportMode.MTPROTO_DIRECT,
        api_id=12345,
        api_hash="hash",
        bot_token="123:abc",
    )

    def factory(config):
        return _stub_factory("mtproto_direct")

    _patch_constructors(monkeypatch, factory)

    manager = TelegramTransportManager(cfg)
    adapter = await manager.select_probed()
    assert adapter.name == "mtproto_direct"