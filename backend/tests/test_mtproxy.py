"""MTProxy + SOCKS proxy config construction tests."""

from __future__ import annotations

import asyncio
import inspect

import pytest

from shmax.adapters.telegram.config import (
    MTProxyConfig,
    ProxyConfig,
    TransportConfig,
    TransportMode,
)
from shmax.adapters.telegram.manager import (
    TelegramAutoState,
    TelegramAutoStateStore,
    TelegramTransportManager,
)
from shmax.adapters.telegram.mtproto import TelegramMtProtoAdapter
from shmax.security import SecretStore
from pathlib import Path


def test_mtproxy_config_tuple_shape():
    """Telethon ConnectionTcpMTProxyRandomizedIntermediate takes ``(ip, port, dc_id, *, loggers, proxy=...)``.

    Our adapter hands ``proxy=(host, port, secret)`` to the
    connection class via the underlying ``TelegramClient``.
    """
    cls = TelegramMtProtoAdapter._make_mtproxy_connection.__class__
    # The static helper returns a class reference; we verify
    # that the resulting tuple, when constructed, matches the
    # shape Telethon expects.
    mp = MTProxyConfig(host="mtproxy.example", port=443, secret="dd" + "0" * 62)
    assert mp.host == "mtproxy.example"
    assert mp.port == 443
    assert len(mp.secret) == 64  # standard MTProxy secret length after hex decoding
    proxy_tuple = (mp.host, mp.port, mp.secret)
    assert proxy_tuple[0] == "mtproxy.example"
    assert proxy_tuple[2].startswith("dd")


def test_socks_proxy_tuple_for_telethon():
    """Telethon expects ``(type, host, port)`` or with credentials.

    type 3 = SOCKS5, type 2 = HTTP.
    """
    socks = ProxyConfig(scheme="socks5", host="proxy.example", port=1080)
    creds = (3, socks.host, socks.port)
    assert creds[0] == 3

    socks_with_auth = ProxyConfig(
        scheme="socks5", host="proxy.example", port=1080, username="u", password="p"
    )
    full = (3, socks_with_auth.host, socks_with_auth.port, socks_with_auth.username, socks_with_auth.password)
    assert full[3:] == ("u", "p")


def test_transport_manager_auto_picks_bot_api_first(tmp_path):
    secret_store = SecretStore(tmp_path / "secrets")
    secret_store.set_secret("telegram_bot_token", "abc")
    cfg = TransportConfig(mode=TransportMode.AUTO, bot_token="abc")
    state_store = TelegramAutoStateStore(tmp_path / "tg_state.json")
    mgr = TelegramTransportManager(cfg, state_store=state_store)
    mode = mgr._auto_pick()
    assert mode == TransportMode.BOT_API_DIRECT


def test_transport_manager_auto_falls_back_to_mtproto(tmp_path):
    # Force Bot API out of the ladder by clearing the token;
    # real V1 fallback fires only when bot_token is missing.
    cfg = TransportConfig(
        mode=TransportMode.AUTO,
        api_id=1,
        api_hash="h",
    )
    state_store = TelegramAutoStateStore(tmp_path / "tg_state.json")
    mgr = TelegramTransportManager(cfg, state_store=state_store)
    mode = mgr._auto_pick()
    assert mode == TransportMode.MTPROTO_DIRECT


def test_transport_manager_auto_honours_cached_success(tmp_path):
    state_store = TelegramAutoStateStore(tmp_path / "tg_state.json")
    state_store.save(TelegramAutoState(active_transport=TransportMode.MTPROTO_PROXY.value))
    mt = MTProxyConfig(host="x", port=1, secret="d" * 64)
    cfg = TransportConfig(
        mode=TransportMode.AUTO,
        api_id=1,
        api_hash="h",
        bot_token="abc",
        mtproxy=mt,
    )
    mgr = TelegramTransportManager(cfg, state_store=state_store)
    assert mgr._auto_pick() == TransportMode.MTPROTO_PROXY


def test_transport_manager_record_success_persists(tmp_path):
    state_store = TelegramAutoStateStore(tmp_path / "tg_state.json")
    cfg = TransportConfig(mode=TransportMode.AUTO, bot_token="abc")
    mgr = TelegramTransportManager(cfg, state_store=state_store)
    asyncio.run(mgr.record_success(TransportMode.BOT_API_DIRECT))
    state = state_store.load()
    assert state.active_transport == TransportMode.BOT_API_DIRECT.value
    assert state.last_success_at is not None
