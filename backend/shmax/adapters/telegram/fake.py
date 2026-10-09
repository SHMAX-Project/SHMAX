"""In-memory fake :class:`TelegramAdapter` for offline tests."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Optional

from ...domain.enums import ChatKind
from ...domain.models import ChatRef, DeliveryResult, NormalizedMessage, ReplyRef, SenderRef
from .base import TelegramAdapter, TelegramBotInfo, TelegramEvent


class FakeTelegramAdapter(TelegramAdapter):
    """Deterministic in-memory Telegram adapter for tests."""

    def __init__(
        self,
        *,
        bot_info: TelegramBotInfo | None = None,
        connected: bool = True,
    ) -> None:
        self._bot_info = bot_info or TelegramBotInfo(
            bot_id="fake-tg-bot",
            username="shmax_test_bot",
            can_join_groups=True,
        )
        self._connected = connected
        self._events: asyncio.Queue[TelegramEvent] = asyncio.Queue()
        self.sent_messages: list[dict] = []
        self._started = False
        self._seq = 0
        # Audit P0 #3: configurable chat facts / bot rights so the
        # pairing validation path is fully testable offline.
        # Defaults describe a healthy supergroup with topics on.
        self.chat_summary: dict = {
            "id": "-1001234567890",
            "type": "supergroup",
            "title": "SHMAX Test Group",
            "is_forum": True,
        }
        self.bot_permissions: dict = {
            "status": "administrator",
            "can_send_messages": True,
            "can_manage_topics": True,
            "is_member": True,
        }

    def _next_id(self) -> str:
        self._seq += 1
        return f"fake-msg-{self._seq}"

    async def inject_event(self, event: TelegramEvent) -> None:
        await self._events.put(event)

    def set_connected(self, value: bool) -> None:
        self._connected = value

    async def connect(self) -> None:
        self._started = True

    async def disconnect(self) -> None:
        self._started = False

    async def authenticate(self) -> None:
        self._started = True

    async def get_bot_info(self) -> TelegramBotInfo:
        return self._bot_info

    async def download_to(self, ref: str, dest) -> int:
        """Deterministic fake download: writes fixed bytes."""
        from pathlib import Path as _Path

        _Path(dest).write_bytes(b"fake-telegram-media-bytes")
        return len(b"fake-telegram-media-bytes")

    async def get_chat_summary(self, chat_id: str) -> dict:
        info = dict(self.chat_summary)
        info["id"] = str(chat_id)
        return info

    async def get_bot_permissions(self, chat_id: str) -> dict:
        return dict(self.bot_permissions)

    async def listen_events(self) -> AsyncIterator[TelegramEvent]:
        if not self._started:
            raise RuntimeError("connect() first")
        while True:
            ev = await self._events.get()
            yield ev

    async def create_topic(self, chat_id: str, title: str) -> str:
        return "fake-topic-1"

    async def send_message(
        self, chat_id: str, text: str, thread_id: Optional[str] = None
    ) -> DeliveryResult:
        if getattr(self, "fail_send", False):
            remaining = getattr(self, "fail_count", 0) - 1
            if remaining >= 0:
                self.fail_count = remaining
                if remaining == 0:
                    self.fail_send = False
                raise RuntimeError("fake telegram adapter: simulated failure")
        msg_id = self._next_id()
        self.sent_messages.append(
            {
                "kind": "message",
                "chat_id": chat_id,
                "thread_id": thread_id,
                "text": text,
                "message_id": msg_id,
            }
        )
        return DeliveryResult(
            external_chat_id=chat_id,
            external_message_id=msg_id,
            thread_id=thread_id,
            timestamp=int(time.time()),
        )

    async def send_media(
        self,
        chat_id: str,
        media_ref: str,
        thread_id: Optional[str] = None,
        kind: str = "document",
        caption: Optional[str] = None,
    ) -> DeliveryResult:
        msg_id = self._next_id()
        self.sent_messages.append(
            {
                "kind": "media",
                "chat_id": chat_id,
                "thread_id": thread_id,
                "media_ref": media_ref,
                "media_kind": kind,
                "caption": caption,
                "message_id": msg_id,
            }
        )
        return DeliveryResult(
            external_chat_id=chat_id,
            external_message_id=msg_id,
            thread_id=thread_id,
        )

    async def send_reply(
        self,
        chat_id: str,
        reply_to: ReplyRef,
        text: str,
        thread_id: Optional[str] = None,
    ) -> DeliveryResult:
        msg_id = self._next_id()
        self.sent_messages.append(
            {
                "kind": "reply",
                "chat_id": chat_id,
                "thread_id": thread_id or reply_to.thread_id,
                "reply_to_message_id": reply_to.message_external_id,
                "reply_to_chat_id": reply_to.chat_external_id,
                "text": text,
                "message_id": msg_id,
            }
        )
        return DeliveryResult(
            external_chat_id=chat_id,
            external_message_id=msg_id,
            thread_id=thread_id or reply_to.thread_id,
        )

    async def health(self) -> dict:
        return {
            "connected": self._connected and self._started,
            "authenticated": self._started,
            "bot_id": self._bot_info.bot_id if self._started else None,
            "transport": "fake",
        }


__all__ = ["FakeTelegramAdapter"]
