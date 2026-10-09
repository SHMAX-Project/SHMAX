"""Telegram HTTPS reachability probe (network bootstrap).

Tests Telegram reachability through the SELECTED transport without
a bot token — the network step runs BEFORE the bot token exists.
No getMe, no polling consumer.

* direct  — plain HTTPS GET to the Telegram base;
* socks5  — through SOCKS5 (aiohttp-socks connector);
* http    — through HTTP CONNECT proxy.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_TELEGRAM_BASE = "https://api.telegram.org"


def _telegram_base() -> str:
    # Test/acceptance override; production default is the real API.
    return (os.environ.get("SHMAX_TELEGRAM_REACHABILITY_URL")
            or DEFAULT_TELEGRAM_BASE).rstrip("/")


@dataclass(frozen=True)
class ReachabilityResult:
    ok: bool
    detail: str
    http_status: int | None = None


async def probe_telegram(
    *,
    mode: str = "direct",
    proxy_host: str | None = None,
    proxy_port: int | None = None,
    proxy_username: str | None = None,
    proxy_password: str | None = None,
    timeout: float = 8.0,
) -> ReachabilityResult:
    """Probe Telegram HTTPS reachability through the given transport.

    Returns a sanitized result — no proxy credentials in the detail.
    """
    base = _telegram_base()
    check_url = f"{base}/"

    connector = None
    if mode in ("bot_api_socks5", "bot_api_http", "socks5", "http"):
        if not proxy_host or not proxy_port:
            return ReachabilityResult(False, "Прокси не настроен (хост/порт)")
        scheme = "socks5" if "socks" in mode else "http"
        from aiohttp_socks import ProxyConnector

        auth = ""
        if proxy_username:
            from urllib.parse import quote

            auth = quote(proxy_username)
            if proxy_password:
                auth += f":{quote(proxy_password)}"
            auth += "@"
        try:
            connector = ProxyConnector.from_url(
                f"{scheme}://{auth}{proxy_host}:{proxy_port}",
                rdns=True,
            )
        except Exception as e:  # noqa: BLE001
            return ReachabilityResult(
                False, f"Некорректный прокси: {type(e).__name__}"
            )

    try:
        async with aiohttp.ClientSession(
            connector=connector, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as session:
            async with session.get(check_url, allow_redirects=False) as resp:
                # Any HTTP response (even 302/404) proves Telegram is
                # reachable through the transport.
                return ReachabilityResult(
                    True,
                    "Telegram доступен",
                    http_status=resp.status,
                )
    except Exception as e:  # noqa: BLE001
        detail = type(e).__name__
        if mode != "direct":
            detail = f"Прокси недоступен ({detail})"
        else:
            detail = f"Telegram недоступен напрямую ({detail})"
        return ReachabilityResult(False, detail)
    finally:
        if connector is not None:
            # aiohttp closes the connector with the session; nothing
            # extra to release here.
            pass


def sanitize_proxy_url(url: str) -> str:
    """scheme://host:port — credentials stripped."""
    parts = urlsplit(url.strip())
    if not parts.hostname:
        return ""
    return f"{parts.scheme}://{parts.hostname}:{parts.port or 0}"


__all__ = ["probe_telegram", "ReachabilityResult", "sanitize_proxy_url"]
