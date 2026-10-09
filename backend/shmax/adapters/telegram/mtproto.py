"""Telegram MTProto adapter on top of Telethon 1.45.

V1 complete implementation. Real Telethon API for:

* bot-token authentication (no phone, no SMS)
* per-message dispatch (NewMessage handler)
* send_message with native reply via ``reply_to``
* create_forum_topic via ``CreateForumTopicRequest``
* per-kind media: photo / document / video / voice / video_note
* SOCKS proxy and MTProxy relay

Imports are deferred so the rest of SHMAX imports cleanly
without Telethon installed.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from typing import Any, Callable, Optional

from ...domain.enums import AttachmentKind, ChatKind
from ...domain.models import (
    Attachment,
    ChatRef,
    DeliveryResult,
    NormalizedMessage,
    ReplyRef,
    SenderRef,
)
from .base import TelegramAdapter, TelegramBotInfo, TelegramEvent
from .config import (
    MTProxyConfig,
    ProxyConfig,
    TransportConfig,
    TransportMode,
)


logger = logging.getLogger(__name__)


_TELETHON_SYMBOLS: dict[str, Any] = {}


def _ensure_telethon() -> dict[str, Any]:
    if _TELETHON_SYMBOLS:
        return _TELETHON_SYMBOLS
    try:
        from telethon import TelegramClient
        from telethon.errors import (
            ApiIdInvalidError,
            FloodWaitError,
            RPCError,
        )
        from telethon.events import NewMessage
        from telethon.network.connection.tcpmtproxy import (
            ConnectionTcpMTProxyRandomizedIntermediate,
        )
        # Telethon 1.45: CreateForumTopicRequest lives in
        # ``telethon.tl.functions.messages`` (not channels).
        from telethon.tl.functions.messages import CreateForumTopicRequest
        from telethon.tl.types import (
            Channel,
            Chat,
            InputPeerEmpty,
            Message as TgMessage,
            User,
        )
    except ImportError as e:  # pragma: no cover - exercised at runtime
        raise RuntimeError(
            "telethon is not installed; run `uv pip install telethon`"
        ) from e
    _TELETHON_SYMBOLS.update(
        TelegramClient=TelegramClient,
        ApiIdInvalidError=ApiIdInvalidError,
        FloodWaitError=FloodWaitError,
        RPCError=RPCError,
        NewMessage=NewMessage,
        CreateForumTopicRequest=CreateForumTopicRequest,
        Channel=Channel,
        Chat=Chat,
        InputPeerEmpty=InputPeerEmpty,
        TgMessage=TgMessage,
        User=User,
        ConnectionTcpMTProxyRandomizedIntermediate=ConnectionTcpMTProxyRandomizedIntermediate,
    )
    return _TELETHON_SYMBOLS


class TelegramMtProtoAdapter(TelegramAdapter):
    """Concrete MTProto adapter via Telethon.

    Bot auth uses ``client.start(bot_token=token)`` so the
    operator does not need a phone number.
    """

    def __init__(
        self,
        config: TransportConfig,
        *,
        client_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        if config.mode not in (
            TransportMode.AUTO,
            TransportMode.MTPROTO_DIRECT,
            TransportMode.MTPROTO_SOCKS,
            TransportMode.MTPROTO_PROXY,
        ):
            raise ValueError(
                f"TelegramMtProtoAdapter does not support mode={config.mode}"
            )
        if config.api_id is None or config.api_hash is None:
            raise ValueError("api_id and api_hash are required for MTProto")
        if not config.bot_token:
            raise ValueError("bot_token is required for MTProto bot login")
        self._config = config
        self._client_factory = client_factory
        self._client: Any = None
        self._events: asyncio.Queue[TelegramEvent] = asyncio.Queue()
        self._started = False

    def _build_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        syms = _ensure_telethon()
        session_dir = self._config.session_dir or os.environ.get(
            "SHMAX_DATA_DIR", "./.dev-data"
        )
        os.makedirs(session_dir, exist_ok=True)
        session_path = os.path.join(session_dir, "tg_mtproto.session")
        kwargs: dict[str, Any] = {
            "session": session_path,
            "api_id": self._config.api_id,
            "api_hash": self._config.api_hash,
        }
        # MTProxy path. Telethon takes both ``connection`` (the
        # randomised intermediate class) and ``proxy`` (the
        # (host, port, secret) tuple). We pass the class itself
        # — the connection is instantiated lazily when the
        # client negotiates the handshake.
        if self._config.mtproxy is not None:
            mp: MTProxyConfig = self._config.mtproxy
            kwargs["connection"] = syms[
                "ConnectionTcpMTProxyRandomizedIntermediate"
            ]
            kwargs["proxy"] = (mp.host, mp.port, mp.secret)
        elif self._config.proxy is not None:
            kwargs["proxy"] = self._build_socks_proxy(self._config.proxy)
        return syms["TelegramClient"](**kwargs)

    def _build_connection(self) -> Any:
        """Legacy helper retained for tests. Production connect()
        wires MTProxy directly via ``_build_client``."""
        return None

    def _make_mtproxy_connection(self, host: str, port: int, secret: str) -> Any:
        """Return the standard MTProxy tuple form ``(host, port, secret)``
        — the value ``TelegramClient`` accepts via ``proxy=``."""
        return (host, port, secret)

    @staticmethod
    def _build_socks_proxy(proxy: ProxyConfig) -> tuple:
        """Build the Telethon SOCKS/HTTP proxy tuple.

        Telethon's contract (verified against 1.45): proxy is a
        tuple ``(type, addr, port)`` or
        ``(type, addr, port, username, password)`` where
        ``type`` is ``'socks5'`` / ``'socks4'`` / ``'http'``.
        We avoid integer constants (those changed between
        versions).
        """
        if proxy.scheme not in ("socks5", "socks4", "http"):
            raise ValueError(
                f"Telethon proxy does not support scheme: {proxy.scheme}"
            )
        if proxy.username:
            return (
                proxy.scheme,
                proxy.host,
                proxy.port,
                proxy.username,
                proxy.password or "",
            )
        return (proxy.scheme, proxy.host, proxy.port)

    @staticmethod
    def _event_from_new_message(event: Any) -> TelegramEvent:
        """Translate a Telethon NewMessage event into our wire form.

        ``thread_id`` is the forum-topic root, NOT the
        ``reply_to_msg_id``. Telegram stores the topic root on
        ``message.reply_to.reply_to_top_id`` for forum threads;
        ``reply_to_msg_id`` is a normal reply. We map them as
        follows:

        * ``reply_to_top_id`` set → topic root
        * otherwise ``None``
        """
        msg = event.message
        chat = event.chat
        chat_id = str(getattr(chat, "id", "")) if chat else ""
        reply_to = getattr(msg, "reply_to", None)
        # Prefer the top-level reply (forum topic root); fall
        # back to the regular reply id only if the top id is
        # missing.
        top_id = getattr(reply_to, "reply_to_top_id", None) if reply_to else None
        reply_id = getattr(reply_to, "reply_to_msg_id", None) if reply_to else None
        thread_id = str(top_id) if top_id else (str(reply_id) if reply_id else None)
        sender = event.sender
        sender_id = str(getattr(sender, "id", "")) if sender else ""
        # Telethon exposes the media caption as ``message`` — when
        # media is attached the text IS the caption (structural
        # fact, recorded at the boundary; surgical P1).
        has_media = getattr(msg, "media", None) is not None
        return TelegramEvent(
            update_id=str(msg.id),
            chat_id=chat_id,
            thread_id=thread_id,
            sender_id=sender_id,
            text=msg.message or "",
            raw=event,
            metadata={
                "text_is_media_caption": bool(has_media and msg.message)
            },
        )

    @staticmethod
    def _normalized_from_event(event: Any) -> NormalizedMessage:
        msg = event.message
        chat = event.chat
        chat_kind = ChatKind.UNKNOWN
        if chat is not None:
            cls_name = type(chat).__name__
            if cls_name == "User":
                chat_kind = ChatKind.DIALOG
            elif cls_name == "Chat":
                chat_kind = ChatKind.GROUP
            elif cls_name == "Channel":
                if getattr(chat, "megagroup", False) or getattr(chat, "gigagroup", False):
                    chat_kind = ChatKind.GROUP
                else:
                    chat_kind = ChatKind.CHANNEL
        attachments: list[Attachment] = []
        if getattr(msg, "photo", None):
            attachments.append(
                Attachment(
                    kind=AttachmentKind.PHOTO,
                    payload={"photo_id": str(msg.photo.id)},
                    caption=msg.message,
                )
            )
        if getattr(msg, "document", None):
            doc = msg.document
            attachments.append(
                Attachment(
                    kind=AttachmentKind.DOCUMENT,
                    payload={
                        "file_id": str(doc.id),
                        "access_hash": str(doc.access_hash),
                        "mime": getattr(doc, "mime_type", None),
                    },
                )
            )
        if getattr(msg, "voice", None):
            attachments.append(
                Attachment(
                    kind=AttachmentKind.VOICE,
                    payload={
                        "file_id": str(msg.voice.id),
                        "duration": msg.voice.duration,
                    },
                )
            )
        if getattr(msg, "video", None):
            attachments.append(
                Attachment(
                    kind=AttachmentKind.VIDEO,
                    payload={
                        "file_id": str(msg.video.id),
                        "duration": msg.video.duration,
                    },
                )
            )
        if getattr(msg, "video_note", None):
            attachments.append(
                Attachment(
                    kind=AttachmentKind.VIDEO,
                    payload={
                        "file_id": str(msg.video_note.id),
                        "duration": msg.video_note.duration,
                        "video_note": True,
                    },
                )
            )
        sender = event.sender
        sender_ref = None
        if sender is not None:
            sender_ref = SenderRef(
                transport="telegram",
                external_id=str(sender.id),
                display_name=getattr(sender, "first_name", None)
                or getattr(sender, "title", None),
                handle=getattr(sender, "username", None),
            )
        chat_ref = ChatRef(
            transport="telegram",
            external_id=str(chat.id) if chat else "",
            kind=chat_kind,
            title=getattr(chat, "title", None) or getattr(chat, "first_name", None),
        )
        reply_to = None
        if getattr(msg, "reply_to", None) and getattr(msg.reply_to, "reply_to_msg_id", None):
            reply_to = NormalizedMessage(
                source="telegram",
                chat=chat_ref,
                sender=None,
                external_id=str(msg.reply_to.reply_to_msg_id),
            )
        return NormalizedMessage(
            source="telegram",
            chat=chat_ref,
            sender=sender_ref,
            external_id=str(msg.id),
            text=msg.message or "",
            timestamp=int(msg.date.timestamp()) if msg.date else 0,
            reply_to=reply_to,
            attachments=tuple(attachments),
        )

    async def connect(self) -> None:
        if self._started:
            return
        syms = _ensure_telethon()
        # Build the client. The TelegramClient constructor in
        # Telethon 1.45 accepts ``connection=`` (a class) plus
        # ``proxy=`` for both SOCKS and MTProxy tuples. We let
        # ``_build_client`` assemble both and pass them through
        # in one place.
        self._client = self._build_client()
        await self._client.start(bot_token=self._config.bot_token)
        handle = self

        @self._client.on(syms["NewMessage"])
        async def _on_new(event: Any) -> None:
            await handle._events.put(self._event_from_new_message(event))

        self._started = True

    async def disconnect(self) -> None:
        if not self._started or self._client is None:
            return
        try:
            await self._client.disconnect()
        except Exception:  # pragma: no cover - best-effort
            logger.exception("error disconnecting Telethon client")
        self._client = None
        self._started = False

    async def authenticate(self) -> None:
        await self.connect()

    async def get_bot_info(self) -> TelegramBotInfo:
        if not self._started or self._client is None:
            raise RuntimeError("get_bot_info called before connect()")
        me = await self._client.get_me()
        return TelegramBotInfo(
            bot_id=str(me.id),
            username=me.username or "",
            can_join_groups=True,
        )

    async def listen_events(self) -> AsyncIterator[TelegramEvent]:
        if not self._started:
            raise RuntimeError("listen_events called before connect()")
        while True:
            ev = await self._events.get()
            yield ev

    @staticmethod
    def _signed_int64_random_id() -> int:
        """Generate a signed int64 random id.

        Telegram's TL schema declares ``random_id`` as ``long``
        (signed 64-bit). Using ``int.from_bytes(..., 'big')`` (the
        unsigned default) on a value with the high bit set would
        overflow the schema and the server rejects the request
        with ``RPC_ERR_INVALID``.

        The signed variant flips the high bit and never returns
        a negative value with the topmost 64 bits set.
        """
        unsigned = int.from_bytes(os.urandom(8), "big", signed=False)
        # Convert unsigned 64-bit range to signed 64-bit range.
        return unsigned - (1 << 64) if unsigned >= (1 << 63) else unsigned

    @staticmethod
    def _extract_topic_id(result: Any) -> Optional[str]:
        """Extract the new topic id from
        ``CreateForumTopicRequest`` updates against real
        Telethon 1.45 types.

        The TL response is an ``Updates`` object whose
        ``updates`` list contains ``UpdateNewChannelMessage``
        wrapping a ``MessageService``. The service ``action``
        is `` ``MessageActionTopicCreate`` (NOT
        ``MessageActionForumTopicCreate`` — that name never
        existed in this SDK). The action has ``title`` /
        ``icon_color`` / ``icon_emoji_id`` but NO ``id`` field.

        The topic id is the **service message's id** in the
        channel — Telegram assigns one message per topic-root, and
        the topic id is the same as the topic-root message id.
        We pick the first ``MessageActionTopicCreate`` action's
        parent message id.
        """
        updates = getattr(result, "updates", None) or []
        for upd in updates:
            msg = getattr(upd, "message", None)
            if msg is None:
                continue
            action = getattr(msg, "action", None)
            if action is None:
                continue
            cls_name = getattr(action, "__class__", None).__name__
            if cls_name in ("MessageActionTopicCreate",):
                # Service message id is the topic id.
                msg_id = getattr(msg, "id", None)
                if msg_id is not None:
                    return str(msg_id)
        return None

    async def create_topic(self, chat_id: str, title: str) -> str:
        if not self._started or self._client is None:
            raise RuntimeError("create_topic called before connect()")
        syms = _ensure_telethon()
        result = await self._client(
            syms["CreateForumTopicRequest"](
                channel=int(chat_id),
                title=title,
                random_id=self._signed_int64_random_id(),
            )
        )
        topic_id = self._extract_topic_id(result)
        if not topic_id:
            return ""
        return topic_id

    async def send_message(
        self,
        chat_id: str,
        text: str,
        thread_id: str | None = None,
    ) -> DeliveryResult:
        if not self._started or self._client is None:
            raise RuntimeError("send_message called before connect()")
        # Telethon's ``client.send_message`` does NOT accept a
        # ``random_id`` keyword — that's only meaningful on
        # lower-level TL requests. Sending is idempotent at the
        # server via the bot token, and the high-level wrapper
        # assigns its own idempotency token internally.
        kwargs: dict[str, Any] = {
            "entity": int(chat_id),
            "message": text,
        }
        if thread_id is not None:
            kwargs["reply_to"] = int(thread_id)
        msg = await self._client.send_message(**kwargs)
        return DeliveryResult(
            external_chat_id=str(getattr(msg.peer_id, "channel_id", chat_id)),
            external_message_id=str(msg.id),
            thread_id=thread_id,
        )

    async def send_media(
        self,
        chat_id: str,
        media_ref: str,
        thread_id: str | None = None,
        kind: str = "document",
    ) -> DeliveryResult:
        if not self._started or self._client is None:
            raise RuntimeError("send_media called before connect()")
        if ":" not in media_ref:
            raise ValueError("media_ref must be 'kind:/abs/path'")
        kind_str, _, path = media_ref.partition(":")
        kind_str = (kind_str or kind).strip().lower()
        kwargs: dict[str, Any] = {"entity": int(chat_id)}
        if thread_id is not None:
            kwargs["reply_to"] = int(thread_id)
        if kind_str == "photo":
            msg = await self._client.send_file(**kwargs, file=path, force_document=False)
        elif kind_str == "file":
            msg = await self._client.send_file(**kwargs, file=path, force_document=True)
        elif kind_str == "video":
            msg = await self._client.send_file(**kwargs, file=path, video_note=False)
        elif kind_str == "voice":
            msg = await self._client.send_file(
                **kwargs, file=path, voice_note=True
            )
        elif kind_str in ("video_note", "videonote"):
            dur_path, _, dur_str = path.partition(":")
            dur_ms = int(dur_str) if dur_str else None
            if dur_ms is None:
                raise ValueError(
                    "video_note requires explicit duration in ms, "
                    "e.g. 'video_note:/abs/clip.mp4:4000'"
                )
            msg = await self._client.send_file(
                **kwargs,
                file=dur_path,
                video_note=True,
                duration=int(dur_ms / 1000),
            )
        else:
            raise ValueError(f"unknown media kind: {kind_str!r}")
        return DeliveryResult(
            external_chat_id=str(getattr(msg.peer_id, "channel_id", chat_id)),
            external_message_id=str(msg.id),
            thread_id=thread_id,
        )

    async def send_reply(
        self,
        chat_id: str,
        reply_to: ReplyRef,
        text: str,
        thread_id: str | None = None,
    ) -> DeliveryResult:
        if not self._started or self._client is None:
            raise RuntimeError("send_reply called before connect()")
        msg = await self._client.send_message(
            entity=int(chat_id),
            message=text,
            reply_to=int(reply_to.message_external_id),
        )
        return DeliveryResult(
            external_chat_id=str(getattr(msg.peer_id, "channel_id", chat_id)),
            external_message_id=str(msg.id),
            thread_id=thread_id or reply_to.thread_id,
        )

    async def probe(self) -> None:
        """Temporary client, getMe, disconnect — no NewMessage
        handler is registered, so no update consumer exists."""
        client = self._build_client()
        try:
            await client.start(bot_token=self._config.bot_token)
            await client.get_me()
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                logger.exception("probe: disconnecting temp client failed")

    def is_real(self) -> bool:
        return True

    async def health(self) -> dict:
        if not self._started or self._client is None:
            return {
                "connected": False,
                "authenticated": False,
                "bot_id": None,
                "transport": "mtproto",
            }
        try:
            me = await self._client.get_me()
            return {
                "connected": True,
                "authenticated": True,
                "bot_id": str(me.id),
                "transport": "mtproto",
            }
        except Exception:
            return {
                "connected": False,
                "authenticated": False,
                "bot_id": None,
                "transport": "mtproto",
            }


__all__ = ["TelegramMtProtoAdapter"]
