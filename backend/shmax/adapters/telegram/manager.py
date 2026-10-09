"""Telegram transport manager with persistent AUTO state.

The AUTO ladder (Stage 1B):

    1. Bot API direct
    2. Bot API SOCKS5 (if configured)
    3. Bot API HTTP (if configured)
    4. MTProto direct (if api_id/api_hash configured)
    5. MTProto SOCKS (if configured)
    6. MTProxy (if configured)

We do NOT cycle endlessly between modes. Once a transport
succeeds we cache the choice for the session; only after a
DOWN → HEALTHY transition do we re-evaluate.

The manager also exposes ``record_success`` /
``record_failure`` so the watchdog can update the persisted
state without re-deriving the AUTO ladder every probe.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from .base import TelegramAdapter
from .bot_api import TelegramBotApiAdapter
from .config import (
    MTProxyConfig,
    ProxyConfig,
    TransportConfig,
    TransportMode,
)
from .mtproto import TelegramMtProtoAdapter


_AUTO_LADDER: tuple[TransportMode, ...] = (
    TransportMode.BOT_API_DIRECT,
    TransportMode.BOT_API_SOCKS5,
    TransportMode.BOT_API_HTTP,
    TransportMode.MTPROTO_DIRECT,
    TransportMode.MTPROTO_SOCKS,
    TransportMode.MTPROTO_PROXY,
)


@dataclass
class TelegramAutoState:
    active_transport: Optional[str] = None
    last_success_at: Optional[int] = None
    last_failure_at: Optional[int] = None
    last_failure_class: Optional[str] = None
    attempts: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TelegramAutoState":
        return cls(
            active_transport=data.get("active_transport"),
            last_success_at=data.get("last_success_at"),
            last_failure_at=data.get("last_failure_at"),
            last_failure_class=data.get("last_failure_class"),
            attempts=int(data.get("attempts", 0)),
        )


class TelegramAutoStateStore:
    """File-backed AUTO state."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> TelegramAutoState:
        if not self._path.exists():
            return TelegramAutoState()
        try:
            return TelegramAutoState.from_dict(json.loads(self._path.read_text()))
        except (OSError, json.JSONDecodeError):
            return TelegramAutoState()

    def save(self, state: TelegramAutoState) -> None:
        self._path.write_text(json.dumps(state.to_dict(), indent=2, sort_keys=True))


class TelegramTransportManager:
    def __init__(self, config: TransportConfig, state_store: TelegramAutoStateStore | None = None) -> None:
        self._config = config
        self._store = state_store

    @property
    def config(self) -> TransportConfig:
        return self._config

    @property
    def state(self) -> TelegramAutoState:
        return self._store.load() if self._store else TelegramAutoState()

    def select(self, *, override_mode: Optional[TransportMode] = None) -> TelegramAdapter:
        mode = override_mode or self._config.mode
        if mode == TransportMode.AUTO:
            mode = self._auto_pick()
        cfg = self._build_for_mode(mode)
        if mode in (
            TransportMode.BOT_API_DIRECT,
            TransportMode.BOT_API_SOCKS5,
            TransportMode.BOT_API_HTTP,
        ):
            return TelegramBotApiAdapter(cfg)
        if mode in (
            TransportMode.MTPROTO_DIRECT,
            TransportMode.MTPROTO_SOCKS,
            TransportMode.MTPROTO_PROXY,
        ):
            return TelegramMtProtoAdapter(cfg)
        raise ValueError(f"unsupported transport mode: {mode}")

    async def select_probed(
        self,
        *,
        override_mode: Optional[TransportMode] = None,
        connect_timeout: float = 5.0,
    ) -> "TelegramAdapter":
        """AUTO ladder that actually probes each candidate.

        Audit P1 #1: candidates are verified via :meth:`probe` —
        a getMe WITHOUT starting the receive loop. The previous
        implementation called ``connect()``, which started a full
        polling consumer for the candidate WHILE the old adapter's
        listener was still running — two concurrent getUpdates
        consumers and Telegram 409 conflicts.

        The returned adapter is UNCONNECTED; the runtime's
        ``install_telegram`` stops the old listener, disconnects
        the old adapter and only then starts exactly one polling
        loop for the new adapter.

        ``connect_timeout`` caps how long we wait on any single
        candidate. Raises ``RuntimeError`` if every candidate
        fails.
        """
        import asyncio as _asyncio

        if override_mode is not None:
            ladder = [override_mode]
        elif self._config.mode == TransportMode.AUTO:
            ladder = self.auto_iter()
        else:
            ladder = [self._config.mode]

        last_exc: Optional[BaseException] = None
        adapter: Any = None
        for mode in ladder:
            try:
                cfg = self._build_for_mode(mode)
                if mode in (
                    TransportMode.BOT_API_DIRECT,
                    TransportMode.BOT_API_SOCKS5,
                    TransportMode.BOT_API_HTTP,
                ):
                    adapter = TelegramBotApiAdapter(cfg)
                elif mode in (
                    TransportMode.MTPROTO_DIRECT,
                    TransportMode.MTPROTO_SOCKS,
                    TransportMode.MTPROTO_PROXY,
                ):
                    adapter = TelegramMtProtoAdapter(cfg)
                else:
                    continue
                await _asyncio.wait_for(adapter.probe(), timeout=connect_timeout)
                await self.record_success(mode)
                return adapter
            except (_asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
                last_exc = e
                await self.record_failure(mode, e)
                adapter = None
                continue
        raise RuntimeError(
            f"AUTO ladder exhausted; last failure: {last_exc!r}"
        )

    def auto_iter(self) -> "list[TransportMode]":
        """Concrete modes the AUTO ladder would try, in order."""
        return [m for m in _AUTO_LADDER if self._mode_available(m)]

    async def record_success(self, mode: TransportMode) -> None:
        if not self._store:
            return
        st = self.state
        st.active_transport = mode.value
        st.last_success_at = _now()
        st.last_failure_at = None
        st.last_failure_class = None
        self._store.save(st)

    async def record_failure(self, mode: TransportMode, exc: BaseException) -> None:
        if not self._store:
            return
        st = self.state
        st.last_failure_at = _now()
        st.last_failure_class = type(exc).__name__
        st.attempts += 1
        self._store.save(st)

    def _build_for_mode(self, mode: TransportMode) -> TransportConfig:
        return TransportConfig(
            mode=mode,
            bot_token=self._config.bot_token,
            api_id=self._config.api_id,
            api_hash=self._config.api_hash,
            proxy=self._proxy_for_mode(mode),
            mtproxy=self._mtproxy_for_mode(mode),
            session_dir=self._config.session_dir,
        )

    def _proxy_for_mode(self, mode: TransportMode) -> Optional[ProxyConfig]:
        if mode == TransportMode.BOT_API_SOCKS5 and self._config.proxy and self._config.proxy.scheme == "socks5":
            return self._config.proxy
        if mode == TransportMode.BOT_API_HTTP and self._config.proxy and self._config.proxy.scheme == "http":
            return self._config.proxy
        if mode == TransportMode.MTPROTO_SOCKS and self._config.proxy and self._config.proxy.scheme == "socks5":
            return self._config.proxy
        return None

    def _mtproxy_for_mode(self, mode: TransportMode) -> Optional[MTProxyConfig]:
        if mode == TransportMode.MTPROTO_PROXY and self._config.mtproxy is not None:
            return self._config.mtproxy
        return None

    def _auto_pick(self) -> TransportMode:
        st = self.state if self._store else TelegramAutoState()
        # Honour the previously successful mode if it still works.
        if st.active_transport is not None:
            try:
                prev = TransportMode(st.active_transport)
            except ValueError:
                prev = None
            if prev is not None and self._mode_available(prev):
                return prev
        for mode in _AUTO_LADDER:
            if self._mode_available(mode):
                return mode
        return TransportMode.BOT_API_DIRECT

    def _mode_available(self, mode: TransportMode) -> bool:
        if mode in (
            TransportMode.BOT_API_DIRECT,
            TransportMode.BOT_API_SOCKS5,
            TransportMode.BOT_API_HTTP,
        ):
            return bool(self._config.bot_token)
        if mode in (
            TransportMode.MTPROTO_DIRECT,
            TransportMode.MTPROTO_SOCKS,
            TransportMode.MTPROTO_PROXY,
        ):
            # MTProto works for both bot and user accounts:
            # bots need api_id/api_hash/bot_token, users only
            # need api_id/api_hash. We allow the fallback path
            # whenever credentials are present, so the AUTO
            # ladder has somewhere to land if the bot token
            # is missing.
            return bool(self._config.api_id and self._config.api_hash)
        return False


def _now() -> int:
    import time

    return int(time.time())


__all__ = [
    "TelegramAutoState",
    "TelegramAutoStateStore",
    "TelegramTransportManager",
]
