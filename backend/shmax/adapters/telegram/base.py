"""Telegram adapter protocol.

Pure structural typing — no runtime dependency on aiogram/Telethon.

V1 contract: every send_* returns a
:class:`shmax.domain.models.DeliveryResult` with the
destination message id so the bridge can write the
persistent mapping by real delivery. ``send_reply`` accepts
a :class:`shmax.domain.models.ReplyRef` so the adapter
preserves forum-topic thread ids end-to-end.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from ...domain.models import DeliveryResult, ReplyRef


@dataclass(frozen=True)
class TelegramBotInfo:
    bot_id: str
    username: str
    can_join_groups: bool
    can_read_all_group_messages: bool | None = None


@dataclass(frozen=True)
class TelegramEvent:
    """Incoming update from Telegram."""

    update_id: str
    chat_id: str
    thread_id: str | None  # forum topic id, if any
    sender_id: str
    text: str
    raw: Any = None
    # Optional rich fields the adapter may populate.
    reply_to: Any | None = None
    attachments: tuple[Any, ...] = ()
    timestamp: int = 0
    chat: Any | None = None
    metadata: dict[str, Any] | None = None


class TelegramAdapter(ABC):
    """Future-facing contract for Telegram."""

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def get_bot_info(self) -> TelegramBotInfo: ...

    @abstractmethod
    def listen_events(self) -> AsyncIterator[TelegramEvent]: ...

    @abstractmethod
    async def create_topic(self, chat_id: str, title: str) -> str: ...

    @abstractmethod
    async def send_message(
        self,
        chat_id: str,
        text: str,
        thread_id: str | None = None,
    ) -> DeliveryResult: ...

    @abstractmethod
    async def send_media(
        self,
        chat_id: str,
        media_ref: str,
        thread_id: str | None = None,
        kind: str = "document",
    ) -> DeliveryResult: ...

    @abstractmethod
    async def send_reply(
        self,
        chat_id: str,
        reply_to: ReplyRef,
        text: str,
        thread_id: str | None = None,
    ) -> DeliveryResult: ...

    @abstractmethod
    async def health(self) -> bool: ...

    def is_real(self) -> bool:
        """Return True if this adapter talks to the real Telegram service.

        Used by the production runtime to refuse fake adapters.
        Concrete subclasses override; the base default is False so
        that an unclassified subclass is treated as suspect.
        """
        return False

    # ------------------------------------------------------------------
    # Optional capabilities
    # ------------------------------------------------------------------
    def set_on_command(self, handler: "Callable[[str, dict], Any] | None") -> None:
        """Install the slash-command handler (default storage)."""
        setattr(self, "_on_command", handler)

    async def download_to(self, ref: str, dest) -> int:
        """Download the referenced media (file id / URL) into
        ``dest``; returns bytes written. P0 #11 hook used by the
        media pipeline."""
        raise NotImplementedError(
            f"{type(self).__name__} does not implement download_to"
        )

    async def probe(self) -> None:
        """Credential / reachability probe WITHOUT starting the
        receive loop (audit P1 #1). A full ``connect()`` starts a
        long-lived polling consumer; probing must never produce a
        second getUpdates consumer next to the live listener."""
        raise NotImplementedError(
            f"{type(self).__name__} does not implement probe"
        )

    async def get_chat_summary(self, chat_id: str) -> dict:
        """Live getChat — the authoritative chat facts.

        Returns ``{"id", "type", "title", "is_forum"}`` where
        ``type`` is the RAW Telegram chat type string
        (``"supergroup"``, ``"group"``, ``"channel"``,
        ``"private"``). Audit P0 #3: never trust a forum flag
        that did not come from this call.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement get_chat_summary"
        )

    async def get_bot_permissions(self, chat_id: str) -> dict:
        """Live getChatMember(bot) — the authoritative rights.

        Returns ``{"status", "can_send_messages", "can_manage_topics",
        "is_member"}``. Audit P0 #3: SHMAX must verify the rights it
        actually needs before declaring a group paired.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement get_bot_permissions"
        )


class NotConfiguredTelegramAdapter(TelegramAdapter):
    """Production stand-in until the operator installs a real adapter.

    Every method raises :class:`TelegramNotConfiguredError` so the
    API layer can render ``NOT_CONFIGURED`` instead of silently
    pretending the bridge works. The runtime swaps this out for a
    :class:`TelegramBotApiAdapter` after ``install_telegram`` is
    called.
    """

    async def connect(self) -> None:
        raise TelegramNotConfiguredError("telegram adapter not configured")

    async def disconnect(self) -> None:
        return None

    async def get_bot_info(self) -> TelegramBotInfo:
        raise TelegramNotConfiguredError("telegram adapter not configured")

    async def listen_events(self):  # type: ignore[override]
        if False:  # pragma: no cover
            yield None
        raise TelegramNotConfiguredError("telegram adapter not configured")

    async def create_topic(self, chat_id: str, title: str) -> str:
        raise TelegramNotConfiguredError("telegram adapter not configured")

    async def send_message(
        self, chat_id: str, text: str, thread_id: str | None = None
    ):
        raise TelegramNotConfiguredError("telegram adapter not configured")

    async def send_media(
        self,
        chat_id: str,
        media_ref: str,
        thread_id: str | None = None,
        kind: str = "document",
    ):
        raise TelegramNotConfiguredError("telegram adapter not configured")

    async def send_reply(
        self,
        chat_id: str,
        reply_to: ReplyRef,
        text: str,
        thread_id: str | None = None,
    ):
        raise TelegramNotConfiguredError("telegram adapter not configured")

    async def health(self) -> bool:
        return False

    def is_real(self) -> bool:
        return False


class TelegramNotConfiguredError(RuntimeError):
    """Raised by :class:`NotConfiguredTelegramAdapter`.

    The API layer translates this into a 503 with a clear
    ``NOT_CONFIGURED`` payload so the wizard can render the
    next-step guidance instead of a generic error.
    """


__all__ = [
    "NotConfiguredTelegramAdapter",
    "TelegramAdapter",
    "TelegramBotInfo",
    "TelegramEvent",
    "TelegramNotConfiguredError",
]
