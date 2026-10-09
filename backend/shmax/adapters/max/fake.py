"""In-memory fake :class:`MaxAdapter` for offline tests."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Optional

from ...domain.enums import ChatKind
from ...domain.models import ChatRef, DeliveryResult, NormalizedMessage, ReplyRef, SenderRef
from .base import MaxAdapter, MaxChat, MaxEvent, MaxProfile


class FakeMaxAdapter(MaxAdapter):
    """Deterministic in-memory MAX adapter.

    The fake emulates the upstream PyMax SmsAuthFlow contract:
    a successful ``prepare_sms`` / ``connect`` runs through the
    SmsCodeProvider await path. Tests that wire the SHMAX
    MaxAuthService against this fake can therefore observe
    ``WAITING_CODE`` / ``WAITING_PASSWORD`` transitions the same
    way the production state machine expects them.
    """

    def __init__(
        self,
        *,
        profile: MaxProfile | None = None,
        chats: list[MaxChat] | None = None,
        connected: bool = True,
        auto_fulfill: bool = True,
        auto_password: bool = False,
    ) -> None:
        self._profile = profile or MaxProfile(
            user_id="fake-max-user",
            display_name="Fake MAX User",
            handle=None,
        )
        self._chats = chats or [
            MaxChat(chat_id="max-dialog-1", title=None, kind=ChatKind.DIALOG.value),
            MaxChat(chat_id="max-group-1", title="Работа", kind=ChatKind.GROUP.value),
        ]
        self._connected = connected
        self._events: asyncio.Queue[MaxEvent] = asyncio.Queue()
        self.sent_text: list[tuple[str, str]] = []
        self.sent_replies: list[tuple[str, str, str, Optional[str]]] = []
        self.sent_media: list[tuple[str, str, str]] = []
        self.sent_media_captions: list[dict] = []
        self._started = False
        self._seq = 0
        # Default providers. The auth service installs
        # CredentialFuture.on_waiting hooks on construction, so
        # the WAITING_CODE / WAITING_PASSWORD state transitions
        # fire the moment these providers are awaited.
        from .providers import (
            CredentialFuture,
            QueuedPasswordProvider,
            QueuedSmsProvider,
        )
        self._sms_provider = QueuedSmsProvider(
            future=CredentialFuture(kind="sms")
        )
        self._password_provider = QueuedPasswordProvider(
            future=CredentialFuture(kind="password")
        )
        # auto_fulfill=True lets a test that does not want to
        # drive the OTP round-trip still reach AUTHENTICATED.
        # When False the fake mimics PyMax exactly: it blocks
        # on the SmsCodeProvider until the test fulfils it.
        self._auto_fulfill = auto_fulfill
        self._auto_password = auto_password
        # E2E switches (env-gated, test mode only): SHMAX_E2E_OTP_DELAY
        # delays the WAITING_CODE transition so the Playwright suite
        # can reproduce the late-start OTP regression; SHMAX_E2E_VERIFY_DELAY
        # slows the VERIFYING_CODE phase. Both default to instant.
        import os as _os

        self._otp_delay = float(
            _os.environ.get("SHMAX_E2E_OTP_DELAY", "0") or 0
        )
        self._verify_delay = float(
            _os.environ.get("SHMAX_E2E_VERIFY_DELAY", "0") or 0
        )
        # SHMAX_E2E_2FA=1: MAX demands the 2FA password and the
        # password verification itself is slow (SHMAX_E2E_2FA_DELAY
        # seconds) — the surgical-review slow-2FA regression needs
        # an observable VERIFYING_PASSWORD phase.
        self._e2e_2fa = _os.environ.get("SHMAX_E2E_2FA", "") == "1"
        self._twofa_delay = float(
            _os.environ.get("SHMAX_E2E_2FA_DELAY", "0") or 0
        )
        if self._e2e_2fa:
            self._auto_password = True

    def _next_id(self) -> str:
        self._seq += 1
        return f"fake-max-msg-{self._seq}"

    async def inject_message(self, event: MaxEvent) -> None:
        await self._events.put(event)

    def set_connected(self, value: bool) -> None:
        self._connected = value

    async def connect(self) -> None:
        # A "connect" against an already-authenticated session is
        # idempotent — it just marks the adapter as live so
        # get_profile / get_chats / listen_events succeed. We do
        # NOT block on the SmsCodeProvider here; that path is the
        # fresh-auth flow handled by prepare_sms(phone) below.
        # Tests that want to observe the WAITING_CODE transition
        # call prepare_sms() explicitly.
        self._started = True
        if self._auto_fulfill:
            # Auto-fulfil any pending credential future so the
            # auth service can land in AUTHENTICATED without a
            # manual submit_code call. Best-effort: ignore the
            # return value because the future may not have been
            # requested yet.
            loop = asyncio.get_running_loop()
            fut = self._sms_provider.future._future
            if fut is not None and not fut.done():
                fut.set_result("auto-fulfill")
        if self._auto_password:
            loop = asyncio.get_running_loop()
            fut = self._password_provider.future._future
            if fut is not None and not fut.done():
                fut.set_result("auto-fulfill-pwd")

    async def prepare_sms(self, *, phone: str) -> None:
        # Mirror PyMax SmsAuthFlow: request_code returns, then
        # the auth flow awaits the SmsCodeProvider.
        self._started = True
        import asyncio as _asyncio

        if self._otp_delay > 0:
            # Delay BEFORE the provider await: the backend stays in
            # REQUESTING_CODE (in-flight) while the UI must already
            # be observing — the late-start regression needs the
            # transition itself to be observable.
            await _asyncio.sleep(self._otp_delay)
        await self._sms_provider.get_code(phone)
        if self._verify_delay > 0:
            await _asyncio.sleep(self._verify_delay)
        if self._auto_password:
            await self._password_provider.get_password()
            if self._e2e_2fa and self._twofa_delay > 0:
                # get_password returned = the operator submitted the
                # password (VERIFYING_PASSWORD published). The delay
                # keeps that state observable before AUTHENTICATED.
                await _asyncio.sleep(self._twofa_delay)

    async def download_to(self, ref: str, dest) -> int:
        from pathlib import Path as _Path

        _Path(dest).write_bytes(b"fake-max-media-bytes")
        return len(b"fake-max-media-bytes")

    async def download_attachment(self, payload: dict, dest) -> int:
        from pathlib import Path as _Path

        _Path(dest).write_bytes(b"fake-max-media-bytes")
        return len(b"fake-max-media-bytes")

    async def disconnect(self) -> None:
        self._started = False

    async def authenticate(self) -> None:
        self._started = True

    async def get_profile(self) -> MaxProfile:
        if not self._started:
            raise RuntimeError("connect() first")
        return self._profile

    async def get_chats(self) -> list[MaxChat]:
        if not self._started:
            raise RuntimeError("connect() first")
        return list(self._chats)

    async def listen_events(self) -> AsyncIterator[MaxEvent]:
        if not self._started:
            raise RuntimeError("connect() first")
        while True:
            ev = await self._events.get()
            yield ev

    async def send_text(self, chat_id: str, text: str) -> DeliveryResult:
        msg_id = self._next_id()
        self.sent_text.append((chat_id, text))
        return DeliveryResult(
            external_chat_id=chat_id,
            external_message_id=msg_id,
            timestamp=int(time.time()),
        )

    async def send_reply(
        self, chat_id: str, reply_to: ReplyRef, text: str
    ) -> DeliveryResult:
        msg_id = self._next_id()
        self.sent_replies.append((chat_id, reply_to.message_external_id, text, reply_to.thread_id))
        return DeliveryResult(
            external_chat_id=chat_id,
            external_message_id=msg_id,
            thread_id=reply_to.thread_id,
        )

    async def send_media(
        self,
        chat_id: str,
        media_ref: str,
        kind: str = "document",
        caption: Optional[str] = None,
    ) -> DeliveryResult:
        msg_id = self._next_id()
        self.sent_media.append((chat_id, media_ref, kind))
        self.sent_media_captions.append({"chat_id": chat_id, "caption": caption})
        return DeliveryResult(
            external_chat_id=chat_id,
            external_message_id=msg_id,
        )

    async def health(self) -> dict:
        return {
            "connected": self._connected and self._started,
            "authenticated": self._started,
            "account_id": self._profile.user_id if self._started else None,
            "session_available": True,
            "transport": "fake",
        }


__all__ = ["FakeMaxAdapter"]
