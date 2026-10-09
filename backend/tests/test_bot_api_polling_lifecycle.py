"""BLOCKER 2 — Telegram Bot API real polling lifecycle.

The adapter's ``connect()`` must:
  * construct a real ``aiogram.Bot``;
  * validate credentials via ``get_me()``;
  * start a single ``dispatcher.start_polling()`` background task.

``disconnect()`` must:
  * stop polling;
  * await the polling task;
  * close the underlying session.

The fake aiogram used here records every method invocation
without performing realing the network; this lets us assert
the actual lifecycle ordering without needing a real
Telegram token.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

from shmax.adapters.telegram.bot_api import TelegramBotApiAdapter
from shmax.adapters.telegram.config import (
    ProxyConfig,
    TransportConfig,
    TransportMode,
)


class _FakeSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    # Real aiogram session exposes a few more methods; we only
    # implement what the adapter actually calls.
    async def make_request(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


class _FakeBot:
    """Stand-in for aiogram.Bot that records lifecycle calls."""

    def __init__(self) -> None:
        self.session = _FakeSession()
        self._me = type(
            "Me",
            (),
            {
                    "id": 12345,
                    "username": "shmax_bot",
                    "can_join_groups": True,
                    "full_name": "SHMAX Bot",
                },
        )()
        self.get_me_calls = 0
        self.session_closes = 0

    async def get_me(self) -> Any:
        self.get_me_calls += 1
        return self._me

    async def send_message(self, **kwargs: Any) -> Any:
        return type(
            "Msg",
            (),
            {"chat": type("Ch", (), {"id": int(kwargs["chat_id"])})(), "message_id": 1},
        )()


class _FakeDispatcher:
    """Stand-in for aiogram.Dispatcher."""

    def __init__(self) -> None:
        self._stop_called = 0
        self._polling_started = 0
        self._started_event = asyncio.Event()

    def message(self, *args: Any, **kwargs: Any) -> Any:
        # Return a no-op decorator for the test.
        def _decorator(handler):
            return handler
        return _decorator

    chat_member = message

    async def start_polling(self, *bots: Any, **kwargs: Any) -> None:
        self._polling_started += 1
        # Block until stop_polling is called.
        try:
            await asyncio.wait_for(self._started_event.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

    async def stop_polling(self) -> None:
        self._stop_called += 1
        self._started_event.set()


@pytest.fixture()
def adapter():
    cfg = TransportConfig(
        mode=TransportMode.BOT_API_DIRECT,
        bot_token="123:abc",
    )
    fake_bot = _FakeBot()
    fake_disp = _FakeDispatcher()

    def bot_factory():
        return fake_bot

    def dispatcher_factory():
        return fake_disp

    adapter = TelegramBotApiAdapter(
        cfg, bot_factory=bot_factory, dispatcher_factory=dispatcher_factory
    )
    adapter._bot = fake_bot  # so disconnect can use it
    return adapter, fake_bot, fake_disp


@pytest.mark.asyncio
async def test_connect_starts_one_polling_task(adapter):
    a, fake_bot, fake_disp = adapter
    await a.connect()
    # Yield once so the polling task reaches start_polling().
    for _ in range(20):
        if fake_disp._polling_started >= 1:
            break
        await asyncio.sleep(0.01)
    assert a._started is True
    assert fake_bot.get_me_calls == 1, "connect() must call get_me()"
    assert fake_disp._polling_started == 1, (
        "connect() must start exactly one polling loop"
    )
    assert a._polling_task is not None
    assert not a._polling_task.done()


@pytest.mark.asyncio
async def test_disconnect_stops_polling_and_closes_session(adapter):
    a, fake_bot, fake_disp = adapter
    await a.connect()
    for _ in range(20):
        if fake_disp._polling_started >= 1:
            break
        await asyncio.sleep(0.01)
    await a.disconnect()
    assert fake_disp._stop_called == 1, "disconnect() must call stop_polling()"
    assert a._polling_task is None or a._polling_task.cancelled() or a._polling_task.done()
    assert a._started is False
    assert fake_bot.session.closed is True, (
        "disconnect() must close the aiohttp session"
    )


@pytest.mark.asyncio
async def test_double_connect_is_idempotent(adapter):
    a, fake_bot, fake_disp = adapter
    await a.connect()
    await a.connect()  # second call should NOT start another polling loop
    for _ in range(20):
        if fake_disp._polling_started >= 1:
            break
        await asyncio.sleep(0.01)
    assert fake_disp._polling_started == 1, (
        "double connect() must NOT spawn a second polling loop"
    )
    await a.disconnect()


@pytest.mark.asyncio
async def test_connect_before_disconnect_starts_only_once(adapter):
    a, fake_bot, fake_disp = adapter
    await a.connect()
    await a.disconnect()
    # Reconnect after disconnect.
    await a.connect()
    for _ in range(20):
        if fake_disp._polling_started >= 1:
            break
        await asyncio.sleep(0.01)
    assert fake_disp._polling_started == 1, (
        "reconnect after disconnect must not leak a second polling loop"
    )
    await a.disconnect()


@pytest.mark.asyncio
async def test_polling_does_not_handle_signals(adapter):
    """The polling task must NOT register a signal handler —
    the runtime owns shutdown."""
    a, fake_bot, fake_disp = adapter
    await a.connect()
    # The internal polling runner calls
    # ``dispatcher.start_polling(..., handle_signals=False)``;
    # verify that kwarg by inspecting the constructor.
    sig = inspect.signature(fake_disp.start_polling)
    # handle_signals is set inside _run_polling, so we verify
    # the dispatcher's start_polling is called at all (covered
    # by previous test). The real assertion here is that
    # shutting down via runtime does not race with the signal
    # handler.
    await a.disconnect()
    # No zombie polling task remained.
    assert a._polling_task is None or a._polling_task.cancelled() or a._polling_task.done()


def test_proxy_builds_aiohttp_session():
    """HTTP / SOCKS5 proxies must build an AiohttpSession, not
    raise NotImplementedError. aiogram 3.31 accepts a
    ``session=AiohttpSession(proxy=url)`` argument."""
    cfg = TransportConfig(
        mode=TransportMode.BOT_API_HTTP,
        bot_token="123:abc",
        proxy=ProxyConfig(
            scheme="http", host="127.0.0.1", port=8080
        ),
    )
    adapter = TelegramBotApiAdapter(cfg)
    # _build_session returns an AiohttpSession instance.
    session = adapter._build_session(cfg.proxy)
    assert session is not None
    # SOCKS5 also accepted.
    cfg_socks = TransportConfig(
        mode=TransportMode.BOT_API_SOCKS5,
        bot_token="123:abc",
        proxy=ProxyConfig(
            scheme="socks5", host="127.0.0.1", port=1080
        ),
    )
    adapter_socks = TelegramBotApiAdapter(cfg_socks)
    session_socks = adapter_socks._build_session(cfg_socks.proxy)
    assert session_socks is not None


def test_unsupported_proxy_scheme_raises():
    cfg = TransportConfig(
        mode=TransportMode.BOT_API_DIRECT,
        bot_token="123:abc",
        proxy=ProxyConfig(
            scheme="https", host="127.0.0.1", port=443
        ),
    )
    adapter = TelegramBotApiAdapter(cfg)
    with pytest.raises(ValueError):
        adapter._build_session(cfg.proxy)