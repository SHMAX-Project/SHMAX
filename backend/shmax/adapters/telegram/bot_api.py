"""Telegram Bot API adapter on top of aiogram v3.

V1 complete implementation: every ABC method calls a real
aiogram entry point; network is delegated to ``aiogram.Bot``
which is replaceable via the ``bot_factory`` hook for tests.
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
from .config import ProxyConfig, TransportConfig, TransportMode


logger = logging.getLogger(__name__)


# Audit P1 #7: the bot sends with parse_mode=HTML, so ANY text of
# external origin (MAX messages, Telegram user text, pairing
# replies that echo user input) must be escaped before it reaches
# ``send_message`` — otherwise a message containing "<b>" or a
# stray "&" either breaks the send with a 400 or injects markup.
def _escape_html(text: str) -> str:
    import html as _html

    return _html.escape(text or "", quote=False)



_AIOGRAM_SYMBOLS: dict[str, Any] = {}


def _ensure_aiogram() -> dict[str, Any]:
    """Lazy import of aiogram. Returns a dict of symbols."""
    if _AIOGRAM_SYMBOLS:
        return _AIOGRAM_SYMBOLS
    try:
        from aiogram import Bot, Dispatcher
        from aiogram.client.default import DefaultBotProperties
        from aiogram.client.session.aiohttp import AiohttpSession
        from aiogram.client.telegram import TelegramAPIServer
        from aiogram.enums import ParseMode
    except ImportError as e:  # pragma: no cover - exercised at runtime
        raise RuntimeError(
            "aiogram is not installed; run `uv pip install aiogram`"
        ) from e
    _AIOGRAM_SYMBOLS.update(
        Bot=Bot,
        Dispatcher=Dispatcher,
        DefaultBotProperties=DefaultBotProperties,
        AiohttpSession=AiohttpSession,
        TelegramAPIServer=TelegramAPIServer,
        ParseMode=ParseMode,
    )
    return _AIOGRAM_SYMBOLS


class TelegramBotApiAdapter(TelegramAdapter):
    """Production Telegram adapter over aiogram v3."""

    def __init__(
        self,
        config: TransportConfig,
        *,
        bot_factory: Optional[Callable[[], Any]] = None,
        dispatcher_factory: Optional[Callable[[], Any]] = None,
        on_command: Optional[Callable[[str, dict], Any]] = None,
    ) -> None:
        if config.mode not in (
            TransportMode.AUTO,
            TransportMode.BOT_API_DIRECT,
            TransportMode.BOT_API_SOCKS5,
            TransportMode.BOT_API_HTTP,
        ):
            raise ValueError(
                f"TelegramBotApiAdapter does not support mode={config.mode}"
            )
        if not config.bot_token:
            raise ValueError("TransportConfig.bot_token is required for BOT_API")
        self._config = config
        self._bot_factory = bot_factory
        self._dispatcher_factory = dispatcher_factory
        self._bot: Any = None
        self._dispatcher: Any = None
        self._events: asyncio.Queue[TelegramEvent] = asyncio.Queue()
        self._started = False
        # The command handler is an optional async callable the
        # runtime installs to react to /start, /setname and
        # /shmax_connect without coupling the adapter to the DB.
        # The adapter ONLY routes private-chat commands through
        # the hook — group messages still flow to the bridge.
        self._on_command: Optional[Callable[[str, dict], Any]] = on_command
        # ``_polling_task`` holds the long-running
        # ``dispatcher.start_polling()`` task. There is exactly
        # one polling loop per adapter instance.
        self._polling_task: Optional[asyncio.Task[None]] = None
        self._stop_polling = asyncio.Event()

    def _make_bot(self) -> Any:
        if self._bot_factory is not None:
            return self._bot_factory()
        syms = _ensure_aiogram()
        # Aiogram 3.31 contract: HTTP/SOCKS proxies are wired
        # via the ``session=`` argument — a custom session
        # (``AiohttpSession``) carries the ``proxy=...`` tuple
        # that aiohttp consumes at request time. ``api=`` is a
        # separate concept (custom Telegram server) and is NOT
        # a proxy mechanism in this aiogram version.
        session: Any = None
        if self._config.proxy is not None:
            session = self._build_session(self._config.proxy)
        bot_kwargs: dict[str, Any] = {
            "token": self._config.bot_token,
            "default": syms["DefaultBotProperties"](parse_mode=syms["ParseMode"].HTML),
        }
        if session is not None:
            bot_kwargs["session"] = session
        # Test / appliance hook: point the Bot API at a custom
        # server (local mock for the E2E suite, or a reverse
        # proxy in restricted networks). Format must carry the
        # ``{token}`` and ``{method}`` placeholders. The API
        # server lives on the SESSION in aiogram 3 — ``Bot()``
        # has no ``api=`` constructor argument.
        api_base = os.environ.get("SHMAX_TELEGRAM_API_BASE", "")
        if api_base:
            server = syms["TelegramAPIServer"](
                base=api_base, file=f"{api_base}file/bot{{token}}/{{path}}"
            )
            if session is None:
                session = syms["AiohttpSession"]()
            session.api = server
        if session is not None and "session" not in bot_kwargs:
            bot_kwargs["session"] = session
        return syms["Bot"](**bot_kwargs)

    def _build_session(self, proxy: ProxyConfig) -> Any:
        """Build an ``AiohttpSession`` carrying the proxy.

        aiogram's ``AiohttpSession(proxy=...)`` accepts the URL
        string form (``http://user:pass@host:port`` or
        ``socks5://...``). SOCKS5 is delegated to aiohttp-socks
        which aiogram declares as an optional dependency.
        """
        syms = _ensure_aiogram()
        if proxy.scheme not in ("socks5", "http"):
            raise ValueError(
                f"Bot API proxy only supports socks5/http: {proxy.scheme}"
            )
        url = self._proxy_url(proxy)
        return syms["AiohttpSession"](proxy=url)

    @staticmethod
    def _proxy_url(proxy: ProxyConfig) -> str:
        url = proxy.scheme + "://"
        if proxy.username:
            url += proxy.username
            if proxy.password:
                url += ":" + proxy.password
            url += "@"
        url += f"{proxy.host}:{proxy.port}"
        return url

    def _make_dispatcher(self) -> Any:
        if self._dispatcher_factory is not None:
            return self._dispatcher_factory()
        syms = _ensure_aiogram()
        return syms["Dispatcher"]()

    @staticmethod
    def _from_aio_message(msg: Any) -> TelegramEvent:
        # Structural caption knowledge (surgical P1): the Bot API
        # exposes body text and media caption as DISTINCT fields and
        # they are mutually exclusive — when ``text`` is absent the
        # non-empty ``caption`` IS the media caption. Record that
        # relationship here, at the boundary where it is known, so
        # composition can suppress the duplicate structurally
        # instead of searching flattened text later.
        text_field = getattr(msg, "text", None)
        caption_field = getattr(msg, "caption", None)
        return TelegramEvent(
            update_id=str(msg.message_id),
            chat_id=str(msg.chat.id) if msg.chat else "",
            thread_id=str(msg.message_thread_id) if getattr(msg, "message_thread_id", None) else None,
            sender_id=str(msg.from_user.id) if msg.from_user else "",
            text=text_field or caption_field or "",
            raw=msg,
            metadata={
                "text_is_media_caption": (
                    text_field is None and caption_field is not None
                )
            },
        )

    @staticmethod
    def _to_normalized(msg: Any) -> NormalizedMessage:
        chat = msg.chat
        chat_kind = ChatKind.UNKNOWN
        if chat is not None:
            t = getattr(chat, "type", None)
            if t == "private":
                chat_kind = ChatKind.DIALOG
            elif t == "group" or t == "supergroup":
                chat_kind = ChatKind.GROUP
            elif t == "channel":
                chat_kind = ChatKind.CHANNEL
        attachments: list[Attachment] = []
        if getattr(msg, "photo", None):
            attachments.append(
                Attachment(
                    kind=AttachmentKind.PHOTO,
                    payload={
                        "file_id": msg.photo[-1].file_id,
                        "width": msg.photo[-1].width,
                        "height": msg.photo[-1].height,
                    },
                    caption=msg.caption,
                )
            )
        if getattr(msg, "document", None):
            attachments.append(
                Attachment(
                    kind=AttachmentKind.DOCUMENT,
                    payload={
                        "file_id": msg.document.file_id,
                        "file_name": msg.document.file_name,
                        "mime": msg.document.mime_type,
                    },
                    caption=msg.caption,
                )
            )
        if getattr(msg, "voice", None):
            attachments.append(
                Attachment(
                    kind=AttachmentKind.VOICE,
                    payload={"file_id": msg.voice.file_id, "duration": msg.voice.duration},
                )
            )
        if getattr(msg, "video", None):
            attachments.append(
                Attachment(
                    kind=AttachmentKind.VIDEO,
                    payload={
                        "file_id": msg.video.file_id,
                        "duration": msg.video.duration,
                    },
                    caption=msg.caption,
                )
            )
        if getattr(msg, "video_note", None):
            attachments.append(
                Attachment(
                    kind=AttachmentKind.VIDEO,
                    payload={
                        "file_id": msg.video_note.file_id,
                        "duration": msg.video_note.duration,
                        "video_note": True,
                    },
                )
            )
        if getattr(msg, "sticker", None):
            attachments.append(
                Attachment(
                    kind=AttachmentKind.STICKER,
                    payload={"file_id": msg.sticker.file_id, "emoji": getattr(msg.sticker, "emoji", None)},
                )
            )
        sender = None
        if msg.from_user is not None:
            sender = SenderRef(
                transport="telegram",
                external_id=str(msg.from_user.id),
                display_name=msg.from_user.full_name,
                handle=msg.from_user.username,
            )
        chat_ref = ChatRef(
            transport="telegram",
            external_id=str(chat.id) if chat else "",
            kind=chat_kind,
            title=chat.title if chat and chat.title else (chat.full_name if chat else None),
        )
        reply_to = None
        if getattr(msg, "reply_to_message", None):
            r = msg.reply_to_message
            reply_to = NormalizedMessage(
                source="telegram",
                chat=chat_ref,
                sender=None,
                external_id=str(r.message_id),
                text=r.text or "",
                timestamp=r.date.timestamp() if r.date else 0,
            )
        text = msg.text or msg.caption or ""
        return NormalizedMessage(
            source="telegram",
            chat=chat_ref,
            sender=sender,
            external_id=str(msg.message_id),
            text=text,
            timestamp=int(msg.date.timestamp()) if msg.date else 0,
            reply_to=reply_to,
            attachments=tuple(attachments),
        )

    async def connect(self) -> None:
        if self._started:
            return
        self._bot = self._make_bot()
        self._dispatcher = self._make_dispatcher()
        handle = self

        @self._dispatcher.message()
        async def _on_message(message: Any) -> None:
            # Slash commands are routed to the runtime command
            # handler — in PRIVATE chats (/start, /setname) and,
            # critically, in GROUPS: /shmax_connect CODE is the
            # group-pairing primitive and MUST reach the handler
            # (audit P0 #2 — the previous private-only gate meant
            # the wizard's pairing command was silently dropped).
            # Non-command messages keep flowing through to the
            # bridge engine so TG→MAX traffic is unaffected.
            chat = getattr(message, "chat", None)
            observer = getattr(handle._on_command, "message_listener", None)
            sender = getattr(message, "from_user", None)
            if observer and chat and sender and not sender.is_bot:
                try:
                    await observer(chat_id=str(chat.id), user_id=str(sender.id),
                                   username=sender.username, display_name=sender.full_name)
                except Exception:
                    logger.warning("Telegram message discovery failed")
            text = (getattr(message, "text", "") or "").strip()
            if (
                handle._on_command is not None
                and chat is not None
                and text.startswith("/")
            ):
                try:
                    chat_type = getattr(chat, "type", None)
                    thread_id = getattr(message, "message_thread_id", None)

                    async def _respond(
                        reply_text: str, _chat=chat, _thread=thread_id
                    ) -> None:
                        # Send the handler's reply straight back to
                        # the chat the command came from.
                        kwargs: dict[str, Any] = {
                            "chat_id": int(_chat.id),
                            "text": reply_text,
                        }
                        if _thread is not None:
                            kwargs["message_thread_id"] = int(_thread)
                        await handle._bot.send_message(**kwargs)

                    payload = {
                        "user_id": str(getattr(message.from_user, "id", "")),
                        "display_name": getattr(
                            message.from_user, "full_name", None
                        )
                        or getattr(message.from_user, "first_name", None),
                        "username": getattr(message.from_user, "username", None),
                        "text": text,
                        "chat_kind": chat_type,
                        "chat_id": str(chat.id),
                        "chat_title": getattr(chat, "title", None),
                        "thread_id": thread_id,
                        "respond": _respond,
                    }
                    await handle._on_command(text.split()[0], payload)
                    # Never queue command messages — they are not
                    # bridge traffic.
                    return
                except Exception:  # noqa: BLE001
                    logger.exception("command handler raised")
            await handle._events.put(self._from_aio_message(message))

        # Member-sync: live membership updates (Bot API
        # chat_member) — join/leave/kick/ban/unban update the SHMAX
        # Users page without the member ever messaging the bot.
        @self._dispatcher.chat_member()
        async def _on_chat_member(event: Any) -> None:
            listener = getattr(handle._on_command, "membership_listener", None)
            if listener is None:
                return
            try:
                old = getattr(event, "old_chat_member", None)
                new = getattr(event, "new_chat_member", None)
                user = getattr(new, "user", None) or getattr(
                    old, "user", None
                )
                if user is None or getattr(user, "is_bot", False):
                    return
                status = str(getattr(new, "status", "") or "")
                await listener(
                    chat_id=str(getattr(event, "chat").id),
                    user_id=str(user.id),
                    username=getattr(user, "username", None),
                    display_name=(
                        (getattr(user, "first_name", "") or "")
                        + (
                            f" {getattr(user, 'last_name')}"
                            if getattr(user, "last_name", None)
                            else ""
                        )
                    )
                    or None,
                    status=status,
                    is_member=bool(getattr(new, "is_member", False)),
                )
            except Exception:  # noqa: BLE001
                logger.exception("chat_member handler failed")

        # Validate credentials before starting the polling
        # task so we fail fast on a bad token.
        await self._bot.get_me()

        # Start the long-running polling task. ``start_polling``
        # blocks until the bot is stopped; we run it as a
        # background task that the runtime supervises. There is
        # exactly one polling loop per adapter instance.
        self._stop_polling.clear()
        self._polling_task = asyncio.create_task(
            self._run_polling(), name="shmax.telegram.bot_api.polling"
        )
        self._started = True

    async def _run_polling(self) -> None:
        """Drive ``dispatcher.start_polling`` until shutdown.

        We set ``handle_signals=False`` so the polling task does
        not register a SIGINT/SIGTERM handler — the runtime's
        lifecycle owns shutdown.
        """
        assert self._bot is not None and self._dispatcher is not None
        try:
            # Member-sync: chat_member updates drive live
            # membership changes (join/leave/kick/ban/unban) — a
            # newly joined user appears without sending a message.
            await self._dispatcher.start_polling(
                self._bot,
                handle_signals=False,
                close_bot_session=False,
                allowed_updates=["message", "chat_member", "my_chat_member"],
            )
        except asyncio.CancelledError:
            # Stop path: dispatcher.start_polling returns
            # cleanly when ``stop_polling()`` is called.
            raise
        except Exception:
            logger.exception("Bot API polling crashed")
            raise

    async def disconnect(self) -> None:
        if not self._started:
            return
        # Stop the polling loop first; ``dispatcher.stop_polling()``
        # cancels the in-flight long-poll so the
        # ``start_polling()`` coroutine inside ``_polling_task``
        # returns. We then await the task.
        try:
            if self._dispatcher is not None:
                # ``stop_polling()`` was added in aiogram 3.x.
                stop_polling = getattr(self._dispatcher, "stop_polling", None)
                if callable(stop_polling):
                    try:
                        await stop_polling()
                    except Exception:
                        logger.exception("dispatcher.stop_polling() failed")
        finally:
            self._stop_polling.set()
            if self._polling_task is not None:
                self._polling_task.cancel()
                try:
                    await self._polling_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                self._polling_task = None
        # Close the underlying aiohttp session.
        try:
            if self._bot is not None:
                session = getattr(self._bot, "session", None)
                if session is not None:
                    close = getattr(session, "close", None)
                    if callable(close):
                        await close()
        except Exception:  # pragma: no cover - best-effort
            logger.exception("error closing aiogram session")
        self._bot = None
        self._dispatcher = None
        self._started = False

    async def authenticate(self) -> None:
        await self.connect()

    async def get_bot_info(self) -> TelegramBotInfo:
        if not self._started or self._bot is None:
            raise RuntimeError("get_bot_info called before connect()")
        me = await self._bot.get_me()
        return TelegramBotInfo(
            bot_id=str(me.id),
            username=me.username or "",
            can_join_groups=bool(getattr(me, "can_join_groups", True)),
        )

    async def listen_events(self) -> AsyncIterator[TelegramEvent]:
        if not self._started:
            raise RuntimeError("listen_events called before connect()")
        while True:
            ev = await self._events.get()
            yield ev

    async def create_topic(self, chat_id: str, title: str) -> str:
        if not self._started or self._bot is None:
            raise RuntimeError("create_topic called before connect()")
        thread = await self._bot.create_forum_topic(chat_id=int(chat_id), name=title)
        return str(thread.message_thread_id)

    async def send_message(
        self,
        chat_id: str,
        text: str,
        thread_id: str | None = None,
    ) -> DeliveryResult:
        if not self._started or self._bot is None:
            raise RuntimeError("send_message called before connect()")
        kwargs: dict[str, Any] = {"chat_id": int(chat_id), "text": _escape_html(text)}
        if thread_id is not None:
            kwargs["message_thread_id"] = int(thread_id)
        msg = await self._bot.send_message(**kwargs)
        return DeliveryResult(
            external_chat_id=str(msg.chat.id) if msg.chat else str(chat_id),
            external_message_id=str(msg.message_id),
            thread_id=str(msg.message_thread_id) if getattr(msg, "message_thread_id", None) else thread_id,
        )

    async def send_media(
        self,
        chat_id: str,
        media_ref: str,
        thread_id: str | None = None,
        kind: str = "document",
        caption: str | None = None,
        reply_to: ReplyRef | None = None,
    ) -> DeliveryResult:
        if not self._started or self._bot is None:
            raise RuntimeError("send_media called before connect()")
        if ":" not in media_ref:
            raise ValueError("media_ref must be 'kind:/abs/path'")
        kind_str, _, path = media_ref.partition(":")
        kind_str = (kind_str or kind).strip().lower()
        from aiogram.types import FSInputFile

        kwargs: dict[str, Any] = {"chat_id": int(chat_id)}
        if thread_id is not None:
            kwargs["message_thread_id"] = int(thread_id)
        if reply_to:
            kwargs["reply_to_message_id"] = int(reply_to.message_external_id)
        # P1 #3: native caption — HTML-escaped like every other
        # externally supplied text.
        if caption:
            kwargs["caption"] = _escape_html(caption)
        file = FSInputFile(path)
        try:
            if kind_str == "photo":
                msg = await self._bot.send_photo(photo=file, **kwargs)
            elif kind_str == "sticker":
                kwargs.pop("caption", None)
                msg = await self._bot.send_sticker(sticker=file, **kwargs)
            elif kind_str == "file":
                msg = await self._bot.send_document(document=file, **kwargs)
            elif kind_str == "video":
                msg = await self._bot.send_video(video=file, **kwargs)
            elif kind_str == "voice":
                msg = await self._bot.send_voice(voice=file, **kwargs)
            elif kind_str in ("video_note", "videonote"):
                # aiogram's send_video_note takes duration in seconds.
                dur_path, _, dur_str = path.partition(":")
                dur_ms = int(dur_str) if dur_str else None
                if dur_ms is None:
                    raise ValueError(
                        "video_note requires explicit duration in ms, "
                        "e.g. 'video_note:/abs/clip.mp4:4000'"
                    )
                msg = await self._bot.send_video_note(
                    video_note=FSInputFile(dur_path), duration=int(dur_ms / 1000), **kwargs
                )
            else:
                raise ValueError(f"unknown media kind: {kind_str!r}")
        except Exception as exc:
            from ...media.diagnostics import MediaFailure
            logger.warning("media direction=max_to_telegram category=%s operation=telegram.attachment_send exception=%s reason=request_failed outcome=MEDIA_FAILED", kind_str, type(exc).__name__)
            raise MediaFailure("telegram.attachment_send", "request_failed") from None
        return DeliveryResult(
            external_chat_id=str(msg.chat.id) if msg.chat else str(chat_id),
            external_message_id=str(msg.message_id),
            thread_id=str(msg.message_thread_id) if getattr(msg, "message_thread_id", None) else thread_id,
        )

    async def send_reply(
        self,
        chat_id: str,
        reply_to: ReplyRef,
        text: str,
        thread_id: str | None = None,
    ) -> DeliveryResult:
        if not self._started or self._bot is None:
            raise RuntimeError("send_reply called before connect()")
        kwargs: dict[str, Any] = {
            "chat_id": int(chat_id),
            "text": _escape_html(text),
            "reply_to_message_id": int(reply_to.message_external_id),
        }
        if thread_id is not None:
            kwargs["message_thread_id"] = int(thread_id)
        msg = await self._bot.send_message(**kwargs)
        return DeliveryResult(
            external_chat_id=str(msg.chat.id) if msg.chat else str(chat_id),
            external_message_id=str(msg.message_id),
            thread_id=str(msg.message_thread_id) if getattr(msg, "message_thread_id", None) else thread_id or reply_to.thread_id,
        )

    async def probe(self) -> "TelegramBotInfo":
        """getMe against a throwaway bot instance — no dispatcher,
        no polling task, no shared state touched (audit P1 #1).
        Returns the verified bot identity."""
        bot = self._make_bot()
        try:
            me = await bot.get_me()
            return TelegramBotInfo(
                bot_id=str(me.id),
                username=me.username or "",
                can_join_groups=bool(getattr(me, "can_join_groups", True)),
                can_read_all_group_messages=getattr(me, "can_read_all_group_messages", None),
            )
        finally:
            try:
                session = getattr(bot, "session", None)
                if session is not None:
                    close = getattr(session, "close", None)
                    if callable(close):
                        await close()
            except Exception:  # noqa: BLE001
                logger.exception("probe: closing throwaway session failed")

    def is_real(self) -> bool:
        # The Bot API adapter IS the real Telegram transport. The
        # missing override made install_telegram() refuse the
        # production adapter ("non-real") and the listener
        # supervisor never start — getMe succeeded while no
        # polling ever ran.
        return True

    async def health(self) -> dict:
        if not self._started:
            return {
                "connected": False,
                "authenticated": False,
                "bot_id": None,
                "polling": False,
                "transport": "bot_api",
            }
        # Audit P0 #2: a successful getMe says NOTHING about the
        # receive path. The polling task is the actual update
        # consumer — if it is missing, finished or died with an
        # exception, the transport is NOT healthy even though the
        # API is reachable. ``listen_events()`` idling on an empty
        # queue must never be mistaken for a live listener.
        polling_ok = (
            self._polling_task is not None
            and not self._polling_task.done()
        )
        polling_error: str | None = None
        if self._polling_task is not None and self._polling_task.done():
            if self._polling_task.cancelled():
                # P1 #1: exception() on a cancelled task RAISES
                # CancelledError — inspect safely instead.
                polling_error = "polling task cancelled"
            else:
                exc = self._polling_task.exception()
                if exc is not None:
                    polling_error = f"{type(exc).__name__}: {exc}"[:200]
                else:
                    polling_error = "polling task finished"
        try:
            me = await self._bot.get_me()
            return {
                "connected": True,
                "authenticated": True,
                "bot_id": str(me.id),
                "polling": polling_ok,
                "degraded": not polling_ok,
                "polling_error": polling_error,
                "transport": "bot_api",
            }
        except Exception:
            return {
                "connected": False,
                "authenticated": False,
                "bot_id": None,
                "polling": polling_ok,
                "transport": "bot_api",
            }

    async def download_attachment(self, payload: dict, dest) -> int:
        from pathlib import Path
        from ...media.diagnostics import MediaFailure
        if not self._started or self._bot is None:
            raise MediaFailure("telegram.getFile", "bot_not_connected")
        ref = payload.get("file_id")
        if not isinstance(ref, str) or not ref or len(ref) > 1024:
            raise MediaFailure("telegram.metadata", "invalid_file_id")
        try:
            info = await self._bot.get_file(ref)
        except Exception as exc:
            raise MediaFailure("telegram.getFile", "request_failed") from exc
        limit = 20 * 1024 * 1024
        if getattr(info, "file_size", 0) and info.file_size > limit:
            raise MediaFailure("telegram.download", "file_too_large")
        file_path = getattr(info, "file_path", None)
        from pathlib import PurePosixPath
        if not isinstance(file_path, str) or not file_path or ".." in PurePosixPath(file_path).parts or file_path.startswith("/") or ":" in file_path:
            raise MediaFailure("telegram.getFile", "invalid_file_path")
        path = Path(dest)
        # Stream through the SAME aiogram session as getFile/polling. Never
        # construct a separate direct client when a proxy/Xray is configured.
        written = 0
        try:
            session = self._bot.session
            url = session.api.file_url(self._config.bot_token, file_path)
            async with asyncio.timeout(35):
                with path.open("wb") as out:
                    async for chunk in session.stream_content(url=url, timeout=30, chunk_size=65536, raise_for_status=True):
                        written += len(chunk)
                        if written > limit:
                            raise MediaFailure("telegram.download", "file_too_large")
                        out.write(chunk)
            if not written:
                raise MediaFailure("telegram.validation", "empty_download")
        except MediaFailure:
            path.unlink(missing_ok=True)
            raise
        except Exception as exc:
            path.unlink(missing_ok=True)
            raise MediaFailure("telegram.download", "transport_failed") from exc
        return written

    async def download_to(self, ref: str, dest) -> int:
        return await self.download_attachment({"file_id": ref}, dest)

    # ------------------------------------------------------------------
    # Audit P0 #3 — real Telegram API validation
    # ------------------------------------------------------------------
    async def get_chat_summary(self, chat_id: str) -> dict:
        """Live ``getChat`` — the authoritative chat facts."""
        if not self._started or self._bot is None:
            raise RuntimeError("get_chat_summary called before connect()")
        chat = await self._bot.get_chat(int(chat_id))
        return {
            "id": str(chat.id),
            "type": getattr(chat, "type", None),
            "title": getattr(chat, "title", None)
            or getattr(chat, "full_name", None),
            "is_forum": bool(getattr(chat, "is_forum", False)),
        }

    async def get_bot_permissions(self, chat_id: str) -> dict:
        """Live ``getChatMember(bot)`` — the authoritative rights.

        aiogram maps member kinds to distinct classes:
        ``ChatMemberMember`` / ``ChatMemberAdministrator`` /
        ``ChatMemberRestricted`` / ``ChatMemberOwner``. The rights
        attributes exist per class, so we read them defensively
        (``getattr`` with sensible defaults: a plain member CAN
        send; an administrator can send and — only if flagged —
        manage topics).
        """
        if not self._started or self._bot is None:
            raise RuntimeError("get_bot_permissions called before connect()")
        me = await self._bot.get_me()
        member = await self._bot.get_chat_member(int(chat_id), me.id)
        status = getattr(member, "status", "unknown")
        can_send = bool(getattr(member, "can_send_messages", True))
        can_manage_topics = bool(getattr(member, "can_manage_topics", False))
        is_member = status in ("member", "administrator", "creator")
        if status == "administrator":
            can_send = bool(getattr(member, "can_send_messages", True))
        return {
            "status": status,
            "can_send_messages": can_send,
            "can_manage_topics": can_manage_topics,
            "is_member": is_member,
        }


__all__ = ["TelegramBotApiAdapter"]
