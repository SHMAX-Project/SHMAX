"""MAX adapter protocol.

Pure structural typing — no runtime dependency on PyMax.

V1 contract: every send_* returns a
:class:`shmax.domain.models.DeliveryResult` with the
destination message id so the bridge can write the
persistent mapping by real delivery.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncIterator

from ...domain.models import DeliveryResult, ReplyRef


@dataclass(frozen=True)
class MaxProfile:
    """Public profile of the connected MAX account."""

    user_id: str
    display_name: str
    handle: str | None = None


@dataclass(frozen=True)
class MaxChat:
    chat_id: str
    title: str | None
    kind: str  # "dm" | "group" | "channel" — concrete strings, not enum


@dataclass(frozen=True)
class MaxEvent:
    """Incoming event from MAX."""

    event_id: str
    chat_id: str
    sender_id: str
    text: str
    raw: Any = None
    reply_to: Any | None = None
    attachments: tuple[Any, ...] = ()
    timestamp: int = 0
    metadata: dict[str, Any] | None = None


class MaxAdapter(ABC):
    """Future-facing contract for MAX.

    Methods are async because real adapters are network-bound.
    """

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def authenticate(self) -> None: ...

    @abstractmethod
    async def get_profile(self) -> MaxProfile: ...

    @abstractmethod
    async def get_chats(self) -> list[MaxChat]: ...

    @abstractmethod
    def listen_events(self) -> AsyncIterator[MaxEvent]: ...

    @abstractmethod
    async def send_text(
        self,
        chat_id: str,
        text: str,
    ) -> DeliveryResult: ...

    @abstractmethod
    async def send_media(
        self,
        chat_id: str,
        media_ref: str,
        kind: str = "document",
        caption: str | None = None,
    ) -> DeliveryResult: ...

    @abstractmethod
    async def send_reply(
        self,
        chat_id: str,
        reply_to: ReplyRef,
        text: str,
    ) -> DeliveryResult: ...

    @abstractmethod
    async def health(self) -> bool: ...

    async def download_to(self, ref: str, dest) -> int:
        """Download referenced media (URL / file id) into ``dest``;
        returns bytes written. P0 #11 hook used by the media
        pipeline."""
        raise NotImplementedError(
            f"{type(self).__name__} does not implement download_to"
        )

    async def download_attachment(self, payload: dict, dest) -> int:
        """Download a structured attachment payload into ``dest``;
        returns bytes written. Id-based transports (PyMax FILE /
        VIDEO) need the payload coordinates, not just a URL."""
        raise NotImplementedError(
            f"{type(self).__name__} does not implement download_attachment"
        )

    async def prepare_sms(self, *, phone: str) -> None:
        """Hook used by the auth state machine to ask the
        transport to send an SMS code.

        The default implementation calls :py:meth:`connect`,
        which is what the PyMax adapter does internally. Fake
        adapters override to a no-op so the auth flow works
        against an in-memory fake.
        """
        await self.connect()

    def is_real(self) -> bool:
        """Return True if this adapter talks to the real service.

        Production code uses this flag to reject fake adapters
        early. The base default is False so any unclassified
        subclass is treated as suspect; PyMaxMaxAdapter overrides
        to True.
        """
        return False


__all__ = ["MaxAdapter", "MaxProfile", "MaxChat", "MaxEvent"]
