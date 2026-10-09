"""PyMax-backed implementation of :class:`MaxAdapter`.

Stage 1 deliverable. The Core layer talks only to
``MaxAdapter``; the rest of SHMAX must not import PyMax.

Design notes
============

Lifecycle
---------
PyMax exposes two entry points:

* ``Client.start()``  — long-running, blocks until disconnect,
  performs automatic reconnect.
* ``Client.connect()`` — one-shot: connects, logs in (or restores
  the saved session), fires ``on_start``, then returns. The
  connection stays open after it returns; the caller is expected
  to drive the event loop itself.

We use ``connect()`` because it gives us a clean handshake
point: after it returns we know ``client.me`` and
``client.is_connected`` are populated, and we can let the
caller iterate ``listen_events()`` at their own pace.

Events
------
PyMax delivers events via decorators registered **before**
``connect()``. We register handlers that translate each
PyMax message into our ``MaxEvent`` dataclass and push it
into an ``asyncio.Queue``. ``listen_events()`` is a thin
``async iterator`` over that queue.

Session persistence
-------------------
``work_dir`` and ``session_name`` are the only knobs PyMax
exposes for where its SQLite session lives. We pass
``session_dir`` as ``work_dir`` and a fixed ``session_name``
so the session always lands in the persistent
``$SHMAX_DATA_DIR/sessions/max/`` tree. The probe verifies
the file exists across restarts.

Secret handling
---------------
The phone, SMS code and 2FA password are read through the
``InteractiveSmsProvider`` / ``InteractivePasswordProvider``
helpers, which prompt the user directly. The adapter itself
never logs, prints, or persists any of those values.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .base import MaxAdapter, MaxChat, MaxEvent, MaxProfile
from .config import PyMaxAdapterConfig
from ...domain.models import DeliveryResult, ReplyRef
from .errors import (
    MaxAuthError,
    MaxConnectionError,
    MaxError,
    MaxSessionError,
    MaxTransportError,
    MaxUnsupportedError,
)
from .providers import (
    InteractivePasswordProvider,
    InteractiveSmsProvider,
)

logger = logging.getLogger(__name__)

# PyMax imports are deferred so the rest of SHMAX keeps working
# even on a host that does not yet have ``maxapi-python`` installed.
_PyMaxClient: Any = None
_PyMaxExtraConfig: Any = None
_PyMaxMessage: Any = None
_PyMaxFile: Any = None
_PyMaxPhoto: Any = None
_PyMaxVideo: Any = None
_PyMaxVoice: Any = None
_PyMaxVideoNote: Any = None
_PyMaxApiError: Any = None
_PyMaxPyMaxError: Any = None
_PyMaxDeviceType: Any = None


def _ensure_pymax() -> None:
    """Import PyMax symbols on first use."""
    global _PyMaxClient, _PyMaxExtraConfig, _PyMaxMessage
    global _PyMaxFile, _PyMaxPhoto, _PyMaxVideo, _PyMaxVoice, _PyMaxVideoNote
    global _PyMaxApiError, _PyMaxPyMaxError, _PyMaxDeviceType
    if _PyMaxClient is not None:
        return
    try:
        from pymax import (
            Client as _C,
            ExtraConfig as _E,
            File as _F,
            Message as _M,
            Photo as _P,
            Video as _V,
            VideoNote as _VN,
            Voice as _VO,
        )
        from pymax import ApiError as _AE
        from pymax import PyMaxError as _PE
        from pymax.api.session.enums import DeviceType as _DT
    except ImportError as e:  # pragma: no cover - exercised at runtime
        raise MaxUnsupportedError(
            "maxapi-python is not installed. Run `uv pip install maxapi-python`."
        ) from e
    _PyMaxClient = _C
    _PyMaxExtraConfig = _E
    _PyMaxMessage = _M
    _PyMaxFile = _F
    _PyMaxPhoto = _P
    _PyMaxVideo = _V
    _PyMaxVoice = _VO
    _PyMaxVideoNote = _VN
    _PyMaxApiError = _AE
    _PyMaxPyMaxError = _PE
    _PyMaxDeviceType = _DT


def _device_type_to_enum(name: str) -> Any:
    """Map SHMAX config strings to PyMax DeviceType enum members.

    Falls back to ANDROID if PyMax exposes a different enum value
    name in a future release — the contract is the literal
    'android' / 'ios' / 'web' / 'desktop' string.
    """
    _ensure_pymax()
    mapping = {
        "android": getattr(_PyMaxDeviceType, "ANDROID", None),
        "ios": getattr(_PyMaxDeviceType, "IOS", None),
        "web": getattr(_PyMaxDeviceType, "WEB", None),
        "desktop": getattr(_PyMaxDeviceType, "DESKTOP", None),
    }
    value = mapping.get(name)
    if value is None:  # pragma: no cover - defensive
        return _PyMaxDeviceType.ANDROID
    return value


def _classify_pymax_error(exc: BaseException) -> Exception:
    """Translate a PyMax exception into our typed hierarchy."""
    _ensure_pymax()
    if isinstance(exc, _PyMaxApiError):
        return MaxTransportError(str(exc))
    if isinstance(exc, _PyMaxPyMaxError):
        # Base class for everything PyMax raises; treat as transport
        # by default and let the adapter narrow further on context.
        return MaxTransportError(str(exc))
    if isinstance(exc, (ConnectionError, EOFError, OSError, TimeoutError)):
        return MaxConnectionError(str(exc))
    if isinstance(exc, FileNotFoundError):
        return MaxSessionError(str(exc))
    return exc


@dataclass
class _PyMaxHandle:
    """Bag of internal references to the live PyMax client."""

    client: Any
    events: "asyncio.Queue[MaxEvent]"
    disconnect_event: "asyncio.Event" = field(default_factory=asyncio.Event)
    started: bool = False


class PyMaxMaxAdapter(MaxAdapter):
    """Concrete :class:`MaxAdapter` on top of ``maxapi-python``."""

    supports_media_filename = True

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def __init__(
        self,
        config: PyMaxAdapterConfig,
        sms_provider: Optional[InteractiveSmsProvider] = None,
        password_provider: Optional[InteractivePasswordProvider] = None,
        qr_handler: Optional[Any] = None,
    ) -> None:
        _ensure_pymax()
        self._config = config
        self._sms_provider = sms_provider or InteractiveSmsProvider()
        self._password_provider = password_provider or InteractivePasswordProvider()
        self._qr_handler = qr_handler
        self._handle: _PyMaxHandle | None = None
        # Paths are validated eagerly so a misconfigured SHMAX_DATA_DIR
        # surfaces immediately, not on first login.
        config.session_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env_or_default(cls, *, session_dir: Optional[str] = None) -> "PyMaxMaxAdapter":
        """Build the production adapter from environment variables.

        Raises if PyMax is not importable; callers in production
        mode must surface that as a startup failure rather than
        silently fall back to a fake.
        """
        cfg = PyMaxAdapterConfig.from_env()
        if session_dir:
            cfg = PyMaxAdapterConfig(
                phone=cfg.phone,
                session_dir=Path(session_dir).expanduser().resolve(),
                session_name=cfg.session_name,
                app_version=cfg.app_version,
                reconnect=cfg.reconnect,
                log_level=cfg.log_level,
            )
        return cls(
            config=cfg,
            sms_provider=InteractiveSmsProvider(),
            password_provider=InteractivePasswordProvider(),
        )

    def is_real(self) -> bool:
        """Return True — this is the production PyMax-backed adapter."""
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _build_client(self) -> Any:
        cfg = self._config
        extra = _PyMaxExtraConfig(
            reconnect=cfg.reconnect,
            log_level=cfg.log_level,
            device_type=_device_type_to_enum(cfg.device_type),
        )
        # ``_handle`` must exist before handler registration so
        # handlers can push into the asyncio queue without an
        # ``is None`` guard.
        handle = _PyMaxHandle(
            client=None,  # type: ignore[arg-type]
            events=asyncio.Queue(),
        )
        self._handle = handle
        client = _PyMaxClient(
            phone=cfg.phone or "",
            work_dir=str(cfg.session_dir),
            session_name=cfg.session_name,
            extra_config=extra,
            sms_code_provider=self._sms_provider,
            password_provider=self._password_provider,
            app_version=cfg.app_version,
        )
        handle.client = client
        self._register_handlers(client)
        return client

    def _register_handlers(self, client: Any) -> None:
        handle = self._handle
        assert handle is not None  # set right before this call

        @client.on_message()
        async def _on_msg(msg: Any, _client: Any) -> None:
            handle.events.put_nowait(self._from_pymax_message(msg))

        @client.on_message_edit()
        async def _on_edit(msg: Any, _client: Any) -> None:
            handle.events.put_nowait(self._from_pymax_message(msg))

        @client.on_disconnect()
        async def _on_disconnect(exc: Any, _reconnect: bool, _delay: float) -> None:
            handle.disconnect_event.set()
            # We do not push a synthetic event; callers can poll
            # ``health().connected`` instead. A disconnect during a
            # long-running ``listen_events()`` simply stops the
            # iterator.

    # ------------------------------------------------------------------
    # Translation PyMax <-> our domain
    # ------------------------------------------------------------------
    @staticmethod
    def _from_pymax_message(msg: Any) -> MaxEvent:
        return MaxEvent(
            event_id=str(msg.id),
            chat_id=str(msg.chat_id) if msg.chat_id is not None else "",
            sender_id=str(msg.sender) if msg.sender is not None else "",
            text=msg.text or "",
            raw=msg,
        )

    @staticmethod
    def _from_pymax_chat(chat: Any) -> MaxChat:
        kind = getattr(chat, "type", None)
        kind_str = str(getattr(kind, "value", kind) or "unknown")
        title = getattr(chat, "title", None)
        return MaxChat(
            chat_id=str(chat.id),
            title=title,
            kind=kind_str.lower(),
        )

    @staticmethod
    def _from_pymax_profile(profile: Any, client: Any) -> MaxProfile:
        me = getattr(client, "me", None)
        contact = getattr(me, "contact", None)
        if contact is None and profile is not None:
            contact = getattr(profile, "contact", None)
        if contact is None:
            # PyMax populates ``client.me`` after login. If it is
            # still missing (e.g. probing before authentication),
            # return a minimal profile so the call does not raise;
            # the caller can detect the missing fields.
            return MaxProfile(user_id="", display_name="(unknown)", handle=None)
        names = getattr(contact, "names", None) or []
        display = ""
        if names:
            n = names[0]
            first = getattr(n, "first_name", "") or ""
            last = getattr(n, "last_name", "") or ""
            display = (first + " " + last).strip()
        user_id = str(getattr(contact, "id", "") or "")
        handle = getattr(contact, "link", None)
        return MaxProfile(
            user_id=user_id,
            display_name=display or "(unnamed)",
            handle=str(handle) if handle is not None else None,
        )

    # ------------------------------------------------------------------
    # MaxAdapter interface
    # ------------------------------------------------------------------
    async def prepare_sms(self, *, phone: str) -> None:
        """Prepare a fresh SMS request, wiring the phone into
        the PyMax client before the handshake.

        Overrides the base default. Sets the config phone so
        the SmsAuthFlow actually has a phone to submit;
        PyMax's upstream SmsAuthFlow raises
        ``RuntimeError("Phone is required for SMS authentication")``
        otherwise.
        """
        if phone:
            self._config = PyMaxAdapterConfig(
                phone=phone,
                session_dir=self._config.session_dir,
                session_name=self._config.session_name,
                app_version=self._config.app_version,
                reconnect=self._config.reconnect,
                log_level=self._config.log_level,
            )
        await super().prepare_sms(phone=phone)

    async def connect(self) -> None:
        if self._handle is not None and self._handle.started:
            return
        client = self._build_client()
        try:
            await client.connect()
        except BaseException as e:
            translated = _classify_pymax_error(e)
            # ``MaxAuthError`` from a provider failure gets translated
            # by the provider path; here we only catch transport / session.
            if isinstance(translated, MaxError):
                raise translated from e
            raise
        self._handle.started = True

    async def download_to(self, ref: str, dest) -> int:
        """Fetch a MAX media URL into ``dest``."""
        from pathlib import Path as _Path

        if not ref or "://" not in str(ref):
            raise ValueError("MAX media reference must be an HTTP(S) URL")
        return await self._download_url(str(ref), _Path(dest))

    async def download_attachment(self, payload: dict, dest) -> int:
        """Download a PyMax 2.4.1 attachment payload into ``dest``.

        Payloads produced by the normalizer carry either a direct
        ``url`` (photo ``base_url``, audio ``url``) or the source
        message coordinates plus ``file_id`` / ``video_id`` for
        ``Client.get_file_by_id`` / ``Client.get_video_by_id``
        (the URL is only resolvable through those API calls).
        """
        from pathlib import Path as _Path

        dest = _Path(dest)
        url = payload.get("url")
        if url and "://" in str(url):
            return await self._download_url(str(url), dest)
        kind = str(payload.get("pymax_type", "")).upper()
        chat_id = payload.get("chat_id")
        message_id = payload.get("message_id")
        if not chat_id or not message_id:
            raise ValueError("attachment payload lacks chat/message coordinates")
        if "FILE" in kind and payload.get("file_id") is not None:
            req = await self._handle.client.get_file_by_id(
                int(chat_id), message_id, int(payload["file_id"])
            )
            url = getattr(req, "url", None)
        elif "VIDEO" in kind and payload.get("video_id") is not None:
            req = await self._handle.client.get_video_by_id(
                int(chat_id), message_id, int(payload["video_id"])
            )
            url = getattr(req, "url", None)
        elif "AUDIO" in kind and payload.get("token"):
            # Audio tokens resolve via the same file endpoint family;
            # without a direct URL we cannot fetch offline.
            url = None
        if not url or "://" not in str(url):
            raise ValueError(f"no downloadable URL for {kind or 'attachment'}")
        return await self._download_url(str(url), dest)

    async def _download_url(self, url: str, dest) -> int:
        import aiohttp
        from urllib.parse import urlsplit
        from ...media.diagnostics import MediaFailure
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        trusted = ("max.ru", "oneme.ru", "okcdn.ru", "mycdn.me", "vkcdn.ru", "userapi.com")
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443) or not any(host == domain or host.endswith("." + domain) for domain in trusted):
            raise MediaFailure("max.download", "untrusted_media_url")
        written = 0
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=35)) as http:
                async with http.get(url, allow_redirects=False) as resp:
                    if resp.status != 200:
                        raise MediaFailure("max.download", "http_response_failed")
                    with dest.open("wb") as fh:
                        async for chunk in resp.content.iter_chunked(65536):
                            written += len(chunk)
                            if written > 20 * 1024 * 1024:
                                raise MediaFailure("max.download", "file_too_large")
                            fh.write(chunk)
            if not written:
                raise MediaFailure("max.validation", "empty_download")
        except MediaFailure:
            dest.unlink(missing_ok=True)
            raise
        except Exception as exc:
            dest.unlink(missing_ok=True)
            raise MediaFailure("max.download", "transport_failed") from exc
        return written

    async def disconnect(self) -> None:
        if self._handle is None:
            return
        client = self._handle.client
        try:
            await client.close()
        finally:
            self._handle = None

    async def authenticate(self) -> None:
        if self._handle is None:
            raise MaxSessionError("authenticate() called before connect()")
        # PyMax's ``relogin()`` resets the current session and runs
        # the configured auth flow again. It is the closest thing to
        # an explicit re-auth entry point.
        try:
            await self._handle.client.relogin()
        except BaseException as e:
            translated = _classify_pymax_error(e)
            if isinstance(translated, MaxError):
                raise translated from e
            raise

    async def get_profile(self) -> MaxProfile:
        if self._handle is None or not self._handle.started:
            raise MaxSessionError("get_profile() called before connect()")
        client = self._handle.client
        # ``client.me`` is populated by the login handshake.
        me = getattr(client, "me", None)
        if me is None:
            raise MaxSessionError("Profile not yet available — connect() did not finish")
        return self._from_pymax_profile(me, client)

    async def get_chats(self) -> list[MaxChat]:
        if self._handle is None or not self._handle.started:
            raise MaxSessionError("get_chats() called before connect()")
        try:
            chats = await self._handle.client.fetch_chats()
        except BaseException as e:
            translated = _classify_pymax_error(e)
            if isinstance(translated, MaxError):
                raise translated from e
            raise
        return [self._from_pymax_chat(c) for c in chats]

    async def listen_events(self) -> AsyncIterator[MaxEvent]:
        if self._handle is None or not self._handle.started:
            raise MaxSessionError("listen_events() called before connect()")
        events = self._handle.events
        disconnect = self._handle.disconnect_event
        while True:
            if disconnect.is_set() and events.empty():
                return
            get_task = asyncio.create_task(events.get())
            disc_task = asyncio.create_task(disconnect.wait())
            done, _ = await asyncio.wait(
                {get_task, disc_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if get_task in done:
                disc_task.cancel()
                yield get_task.result()
            else:
                get_task.cancel()
                return

    async def send_text(self, chat_id: str, text: str) -> DeliveryResult:
        return await self._send_impl(chat_id, text, reply_to=None)

    async def send_reply(
        self,
        chat_id: str,
        reply_to: ReplyRef,
        text: str,
    ) -> DeliveryResult:
        return await self._send_impl(chat_id, text, reply_to=reply_to.message_external_id)

    async def _send_impl(
        self,
        chat_id: str,
        text: str,
        reply_to: str | None,
    ) -> DeliveryResult:
        if self._handle is None or not self._handle.started:
            raise MaxSessionError("send_text/send_reply called before connect()")
        try:
            result = await self._handle.client.send_message(
                chat_id=int(chat_id),
                text=text,
                reply_to=int(reply_to) if reply_to is not None else None,
            )
            # PyMax returns the message payload; extract id.
            message_id = ""
            if isinstance(result, dict):
                message_id = str(result.get("id") or result.get("message_id") or "")
            else:
                message_id = str(getattr(result, "id", "") or "")
            return DeliveryResult(
                external_chat_id=str(chat_id),
                external_message_id=message_id or "",
            )
        except BaseException as e:
            translated = _classify_pymax_error(e)
            if isinstance(translated, MaxError):
                raise translated from e
            raise

    async def send_media(
        self,
        chat_id: str,
        media_ref: str,
        kind: str = "document",
        caption: str | None = None,
        reply_to: ReplyRef | None = None,
        filename: str | None = None,
    ) -> DeliveryResult:
        """Send a single file referenced by ``media_ref``.

        ``media_ref`` is one of:
          * ``photo:/absolute/path``
          * ``file:/absolute/path``
          * ``video:/absolute/path``
          * ``voice:/absolute/path``
          * ``video_note:/absolute/path[:duration_ms]``
        """
        if ":" not in media_ref:
            raise MaxUnsupportedError(
                "media_ref must look like 'kind:/path' (e.g. 'photo:/abs/img.jpg')"
            )
        kind_str, _, raw = media_ref.partition(":")
        kind_str = kind_str.strip().lower()
        if kind_str in {"document", "doc"}:kind_str="file"
        path_part, _, dur_str = raw.partition(":")
        path = Path(path_part).expanduser()
        if not path.is_file():
            raise MaxUnsupportedError(f"media file not found: {path}")
        if self._handle is None or not self._handle.started:
            raise MaxSessionError("send_media called before connect()")
        from .photo_upload import install,phase,DeliveryConfirmationUncertain
        install(self._handle.client)
        phase_token=phase.set("idle")
        try:
            if kind_str == "photo":
                attachment = _PyMaxPhoto(path=str(path))
            elif kind_str == "file":
                attachment = _PyMaxFile(path=str(path),name=filename or path.name)
            elif kind_str == "video":
                attachment = _PyMaxVideo(path=str(path))
            elif kind_str == "voice":
                attachment = _PyMaxVoice(path=str(path))
            elif kind_str in ("video_note", "videonote"):
                dur_ms = int(dur_str) if dur_str else None
                if dur_ms is None:
                    raise MaxUnsupportedError(
                        "video_note requires explicit duration in ms, "
                        "e.g. 'video_note:/abs/clip.mp4:4000'"
                    )
                attachment = _PyMaxVideoNote(path=str(path), duration=dur_ms)
            else:
                raise MaxUnsupportedError(f"unknown media kind: {kind_str!r}")
            # Caption delivery (surgical review): maxapi-python
            # 2.4.1 send_message accepts ``text`` alongside
            # ``attachments`` — the text IS the media caption in
            # the MAX client. Never silently discard it.
            result = await self._handle.client.send_message(
                chat_id=int(chat_id),
                text=(caption or "").strip() or None,
                attachments=[attachment],
                **({"reply_to": int(reply_to.message_external_id)} if reply_to else {}),
            )
            message_id = ""
            if isinstance(result, dict):
                message_id = str(result.get("id") or result.get("message_id") or "")
            else:
                message_id = str(getattr(result, "id", "") or "")
            return DeliveryResult(
                external_chat_id=str(chat_id),
                external_message_id=message_id or "",
            )
        except BaseException as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            frame = e.__traceback__
            upload = False
            while frame is not None:
                upload = upload or "upload" in frame.tb_frame.f_code.co_name
                frame = frame.tb_next
            operation = "max.upload" if upload else "max.attachment_send"
            logger.warning("media direction=telegram_to_max category=%s operation=%s exception=%s reason=request_failed correlation=%s outcome=MEDIA_FAILED",
                           kind_str, operation, type(e).__name__, __import__("hashlib").sha256(str(chat_id).encode()).hexdigest()[:12])
            if phase.get()=="uploaded":
                raise DeliveryConfirmationUncertain("delivery_confirmation_uncertain_review_before_retry") from None
            translated = _classify_pymax_error(e)
            safe_type = type(translated) if isinstance(translated, MaxError) else MaxTransportError
            raise safe_type("MAX media upload/send failed") from None
        finally:
            phase.reset(phase_token)

    async def health(self) -> dict:
        """Internal-state snapshot. Never returns secrets."""
        if self._handle is None or not self._handle.started:
            return {
                "connected": False,
                "authenticated": False,
                "account_id": None,
                "session_available": self._session_file_exists(),
            }
        client = self._handle.client
        me = getattr(client, "me", None)
        contact = getattr(me, "contact", None) if me else None
        account_id = str(getattr(contact, "id", "")) if contact else None
        is_connected = bool(getattr(client, "is_connected", False))
        return {
            "connected": is_connected,
            "authenticated": account_id is not None,
            "account_id": account_id,
            "session_available": self._session_file_exists(),
        }

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------
    def _session_file_exists(self) -> bool:
        path = self._config.session_dir / self._config.session_name
        return path.is_file()

    @property
    def session_path(self) -> Path:
        return self._config.session_dir / self._config.session_name

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"PyMaxMaxAdapter(session_dir={self._config.session_dir!s}, "
            f"session_name={self._config.session_name!r})"
        )


__all__ = ["PyMaxMaxAdapter"]
