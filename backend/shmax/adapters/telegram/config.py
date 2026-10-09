"""Telegram transport configuration.

Settings are read from environment variables or constructed
explicitly. ``TelegramTransportManager`` consumes the resolved
``TransportConfig`` to pick the right adapter.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum


class TransportMode(str, Enum):
    """V1 transport modes."""

    AUTO = "auto"
    BOT_API_DIRECT = "bot_api_direct"
    BOT_API_SOCKS5 = "bot_api_socks5"
    BOT_API_HTTP = "bot_api_http"
    MTPROTO_DIRECT = "mtproto_direct"
    MTPROTO_SOCKS = "mtproto_socks"
    MTPROTO_PROXY = "mtproto_proxy"


@dataclass(frozen=True)
class ProxyConfig:
    """SOCKS5 / HTTP proxy descriptor."""

    scheme: str  # "socks5" | "http"
    host: str
    port: int
    username: str | None = None
    password: str | None = None  # in plain here; SecretStore holds it on disk


@dataclass(frozen=True)
class MTProxyConfig:
    """Telegram MTProxy descriptor.

    ``secret`` is the binary secret as a hex / ascii string from
    Telegram; the SecretStore is the only thing that should ever
    hold the plaintext.
    """

    host: str
    port: int
    secret: str


@dataclass(frozen=True)
class TransportConfig:
    mode: TransportMode
    bot_token: str | None = None
    api_id: int | None = None
    api_hash: str | None = None
    proxy: ProxyConfig | None = None
    mtproxy: MTProxyConfig | None = None
    session_dir: str | None = None  # for MTProto Telethon session files

    @classmethod
    def from_env(cls) -> "TransportConfig":
        mode = TransportMode(os.environ.get("SHMAX_TG_TRANSPORT", "auto"))
        return cls(
            mode=mode,
            bot_token=os.environ.get("SHMAX_TG_BOT_TOKEN"),
            api_id=int(os.environ["SHMAX_TG_API_ID"]) if os.environ.get("SHMAX_TG_API_ID") else None,
            api_hash=os.environ.get("SHMAX_TG_API_HASH"),
            session_dir=os.environ.get("SHMAX_TG_SESSION_DIR"),
        )
