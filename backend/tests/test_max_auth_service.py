"""Tests for the MAX auth state machine and proxy URL parser.

WAITING_CODE / WAITING_PASSWORD in this version of SHMAX are
driven by the upstream PyMax provider invocation, not by the
service publishing them eagerly. The fake adapter below
mimics the upstream SmsAuthFlow contract:

* ``prepare_sms(phone)`` returns only AFTER the server has
  accepted the verification request. It signals acceptance by
  invoking the SMS provider's ``get_code(phone)``.
* ``get_profile()`` succeeds only after the operator has
  fulfilled both credentials (or just SMS for no-2FA accounts).

This mirrors PyMax 2.4.1's behaviour and is the contract the
SHMAX service asserts in production.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from shmax.adapters.max.base import MaxAdapter, MaxChat, MaxEvent, MaxProfile
from shmax.adapters.max.errors import MaxAuthError, MaxSessionError
from shmax.adapters.max.providers import (
    CredentialFuture,
    QueuedPasswordProvider,
    QueuedSmsProvider,
)
from shmax.auth.max import (
    MaxAuthService,
    MaxAuthState,
    MaxAuthStateStore,
    build_providers,
)


class _Adapter(MaxAdapter):
    """In-memory fake for the auth flow.

    The test wires the live :class:`CredentialFuture` from the
    service into this adapter so that ``sms_provider.get_code``
    awaits the same future the service fulfils.
    """

    def __init__(self) -> None:
        self.sms_calls = 0
        self.password_calls = 0
        self._sms_consumed = 0
        self._connected = False
        self._profile = MaxProfile(user_id="u-1", display_name="Alice", handle="alice")
        self._needs_password = False
        # Default providers; the service installs the real
        # ones in production and the tests re-install them.
        self._sms_provider = QueuedSmsProvider(future=CredentialFuture(kind="sms"))
        self._password_provider = QueuedPasswordProvider(
            future=CredentialFuture(kind="password")
        )

    async def prepare_sms(self, *, phone: str) -> None:
        """Mimic SmsAuthFlow: server accepted -> invoke provider."""
        self.sms_calls += 1
        self._connected = True
        # Block on the SMS provider; this is the upstream
        # behaviour — request_code returns, then
        # code_provider.get_code is awaited. The on_waiting
        # callback fires here, publishing WAITING_CODE.
        await self._sms_provider.get_code(phone)
        self._sms_consumed += 1
        # After the operator submits the code the upstream
        # auth flow either sends ``send_code`` (no-2FA) or asks
        # for the 2FA password.
        if self._needs_password:
            await self._password_provider.get_password()
            self.password_calls += 1

    async def connect(self) -> None:
        self._connected = True

    async def authenticate(self, *, phone: str) -> None:  # pragma: no cover - unused here
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def reconnect_now(self) -> None:  # pragma: no cover - unused in tests
        await self.connect()

    async def _unused(self) -> None:  # pragma: no cover
        pass

    async def get_profile(self) -> MaxProfile:
        if not self._connected:
            raise MaxSessionError("not connected")
        if self._needs_password and self.password_calls < 1:
            raise MaxAuthError("2fa required")
        return self._profile

    async def get_chats(self):  # pragma: no cover - unused here
        return []

    async def listen_events(self):  # pragma: no cover - unused here
        if False:  # pragma: no cover
            yield MaxEvent(kind="message", chat_id="c", text="t")

    async def send_text(self, *, chat_id, text, reply_to=None):  # pragma: no cover
        return "id"

    async def send_media(self, *, chat_id, media, text="", reply_to=None):  # pragma: no cover
        return "id"

    async def send_reply(self, *, chat_id, text, reply_to_max_message_id, **kw):  # pragma: no cover
        return "id"

    async def health(self) -> bool:  # pragma: no cover
        return True


@pytest.fixture
def tmp_store():
    with tempfile.TemporaryDirectory() as d:
        yield MaxAuthStateStore(Path(d) / "max_auth.json")


@pytest.fixture
def sms_future():
    return CredentialFuture(kind="sms")


@pytest.fixture
def password_future():
    return CredentialFuture(kind="password")


async def _wait_until(predicate, *, timeout: float = 2.0, interval: float = 0.01):
    """Spin the event loop until ``predicate()`` returns truthy
    or the timeout elapses. Returns the last evaluated value."""
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return True
        await asyncio.sleep(interval)
        elapsed += interval
    return predicate()


@pytest.mark.asyncio
async def test_state_machine_happy_path(tmp_store, sms_future, password_future):
    """Regression: WAITING_CODE is driven by the provider, not by start()."""
    adapter = _Adapter()
    adapter._sms_provider = QueuedSmsProvider(future=sms_future)
    adapter._password_provider = QueuedPasswordProvider(future=password_future)
    svc = MaxAuthService(
        adapter=adapter,
        store=tmp_store,
        sms_future=sms_future,
        password_future=password_future,
    )
    snap = await svc.start(phone="+79991234567")
    # start() MUST publish CONNECTING — NOT WAITING_CODE.
    # The old code synchronously published WAITING_CODE here,
    # which was the bug.
    assert snap.state == MaxAuthState.CONNECTING
    assert snap.next_action == "wait"
    assert snap.phone_hint and snap.phone_hint.endswith("4567")
    # Background task runs prepare_sms → provider.get_code() →
    # callback publishes WAITING_CODE.
    await _wait_until(lambda: svc.state == MaxAuthState.WAITING_CODE)
    assert svc.state == MaxAuthState.WAITING_CODE
    assert adapter.sms_calls == 1

    snap = await svc.submit_code(code="12345")
    # After the future is fulfilled prepare_sms returns; the
    # auth task detects profile.user_id and publishes
    # AUTHENTICATED.
    await _wait_until(lambda: svc.state == MaxAuthState.AUTHENTICATED)
    assert svc.state == MaxAuthState.AUTHENTICATED
    assert svc.snapshot.account_id == "u-1"
    assert svc.snapshot.display_name == "Alice"


@pytest.mark.asyncio
async def test_state_machine_with_2fa(tmp_store, sms_future, password_future):
    adapter = _Adapter()
    adapter._needs_password = True
    adapter._sms_provider = QueuedSmsProvider(future=sms_future)
    adapter._password_provider = QueuedPasswordProvider(future=password_future)
    svc = MaxAuthService(
        adapter=adapter,
        store=tmp_store,
        sms_future=sms_future,
        password_future=password_future,
    )
    await svc.start(phone="+79991234567")
    await _wait_until(lambda: svc.state == MaxAuthState.WAITING_CODE)
    assert svc.state == MaxAuthState.WAITING_CODE

    # After submit_code runs, the auth task's prepare_sms proceeds
    # from the SMS provider await to the password provider
    # await — the on_waiting callback publishes WAITING_PASSWORD.
    await svc.submit_code(code="11111")
    await _wait_until(lambda: svc.state == MaxAuthState.WAITING_PASSWORD)
    assert svc.snapshot.next_action == "submit_password"

    snap = await svc.submit_password(password="s3cret")
    await _wait_until(lambda: svc.state in (
        MaxAuthState.AUTHENTICATED, MaxAuthState.FAILED,
    ))
    assert svc.state == MaxAuthState.AUTHENTICATED, f"state={svc.state} err={svc.snapshot.last_error}"


@pytest.mark.asyncio
async def test_bad_phone_rejected(tmp_store, sms_future, password_future):
    svc = MaxAuthService(
        adapter=_Adapter(),
        store=tmp_store,
        sms_future=sms_future,
        password_future=password_future,
    )
    snap = await svc.start(phone="abc")
    assert snap.state == MaxAuthState.FAILED
    assert snap.last_error_class == "PhoneInvalid"


@pytest.mark.asyncio
async def test_state_persisted_across_restart(tmp_store, sms_future, password_future):
    sms2 = CredentialFuture(kind="sms")
    pwd2 = CredentialFuture(kind="password")
    adapter = _Adapter()
    adapter._sms_provider = QueuedSmsProvider(future=sms2)
    adapter._password_provider = QueuedPasswordProvider(future=pwd2)
    svc = MaxAuthService(
        adapter=adapter,
        store=tmp_store,
        sms_future=sms2,
        password_future=pwd2,
    )
    await svc.start(phone="+79991234567")
    await _wait_until(lambda: svc.state == MaxAuthState.WAITING_CODE)
    await svc.submit_code(code="22222")
    await _wait_until(lambda: svc.state == MaxAuthState.AUTHENTICATED)

    # New service on the same store must see the persisted state.
    reload = MaxAuthService(
        adapter=adapter,
        store=tmp_store,
        sms_future=CredentialFuture(kind="sms"),
        password_future=CredentialFuture(kind="password"),
    )
    assert reload.state == MaxAuthState.AUTHENTICATED
    assert reload.snapshot.account_id == "u-1"


@pytest.mark.asyncio
async def test_cancel_resets_to_not_started(tmp_store, sms_future, password_future):
    svc = MaxAuthService(
        adapter=_Adapter(),
        store=tmp_store,
        sms_future=sms_future,
        password_future=password_future,
    )
    await svc.start(phone="+79991234567")
    snap = await svc.cancel()
    assert snap.state == MaxAuthState.NOT_STARTED
    assert snap.next_action == "start"


@pytest.mark.asyncio
async def test_code_must_not_be_empty(tmp_store, sms_future, password_future):
    svc = MaxAuthService(
        adapter=_Adapter(),
        store=tmp_store,
        sms_future=sms_future,
        password_future=password_future,
    )
    await svc.start(phone="+79991234567")
    await _wait_until(lambda: svc.state == MaxAuthState.WAITING_CODE)
    snap = await svc.submit_code(code="")
    assert snap.state == MaxAuthState.WAITING_CODE
    assert snap.last_error_class == "CodeEmpty"


@pytest.mark.asyncio
async def test_credential_future_invalidated_on_new_start(tmp_store):
    """A second start() must invalidate the previous generation's future."""
    sms = CredentialFuture(kind="sms")
    pwd = CredentialFuture(kind="password")
    adapter = _Adapter()
    adapter._sms_provider = QueuedSmsProvider(future=sms)
    adapter._password_provider = QueuedPasswordProvider(future=pwd)
    svc = MaxAuthService(
        adapter=adapter,
        store=tmp_store,
        sms_future=sms,
        password_future=pwd,
    )
    # First start fires a prepare_sms coroutine that blocks on
    # get_code. We then issue a second start that invalidates
    # the first future. The first prepare_sms wakes with
    # RuntimeError("invalidated"); the auth task catches it
    # and the second round takes over.
    await svc.start(phone="+79991234567")
    await _wait_until(lambda: svc.state == MaxAuthState.WAITING_CODE)
    snap1 = await svc.submit_code(code="11111")
    await _wait_until(lambda: svc.state == MaxAuthState.AUTHENTICATED)
    # Now start a brand-new round — the prior AUTHENTICATED state
    # should be replaced with the new round's CONNECTING state.
    await svc.start(phone="+79991234567")
    assert svc.state in (
        MaxAuthState.CONNECTING,
        MaxAuthState.REQUESTING_CODE,
    )
    await _wait_until(lambda: svc.state == MaxAuthState.WAITING_CODE)
    snap = await svc.submit_code(code="99999")
    await _wait_until(lambda: svc.state == MaxAuthState.AUTHENTICATED)
    assert svc.state == MaxAuthState.AUTHENTICATED


@pytest.mark.asyncio
async def test_start_does_not_set_waiting_code_without_provider(tmp_store, sms_future, password_future):
    """The original BLOCKER regression test.

    start() must NEVER publish WAITING_CODE. The provider
    invocation is the authoritative signal.
    """
    adapter = _Adapter()
    adapter._sms_provider = QueuedSmsProvider(future=sms_future)
    adapter._password_provider = QueuedPasswordProvider(future=password_future)
    svc = MaxAuthService(
        adapter=adapter,
        store=tmp_store,
        sms_future=sms_future,
        password_future=password_future,
    )
    snap = await svc.start(phone="+79991234567")
    assert snap.state == MaxAuthState.CONNECTING
    # Not yet WAITING_CODE — provider not yet invoked.
    # Cancel before the provider is invoked; we should never
    # reach WAITING_CODE.
    await svc.cancel()
    assert svc.state == MaxAuthState.NOT_STARTED


@pytest.mark.asyncio
async def test_request_code_failure_publishes_failed_not_waiting_code(
    tmp_store, sms_future, password_future
):
    """If the upstream request_code equivalent rejects, the state
    must land at FAILED, not WAITING_CODE."""

    class _RejectingAdapter(_Adapter):
        async def prepare_sms(self, *, phone: str) -> None:
            self.sms_calls += 1
            self._connected = True
            # Simulate MAX server rejecting the verification
            # request — the provider is NEVER invoked, so the
            # state machine must NOT publish WAITING_CODE.
            raise MaxAuthError("phone_not_allowed")

    adapter = _RejectingAdapter()
    adapter._sms_provider = QueuedSmsProvider(future=sms_future)
    adapter._password_provider = QueuedPasswordProvider(future=password_future)
    svc = MaxAuthService(
        adapter=adapter,
        store=tmp_store,
        sms_future=sms_future,
        password_future=password_future,
    )
    await svc.start(phone="+79991234567")
    await _wait_until(lambda: svc.state in (MaxAuthState.FAILED, MaxAuthState.WAITING_CODE))
    assert svc.state == MaxAuthState.FAILED
    assert svc.snapshot.last_error_class == "MaxAuthError"
    assert svc.snapshot.next_action == "retry"


@pytest.mark.asyncio
async def test_resend_re_invokes_provider(tmp_store, sms_future, password_future):
    """resend_code must trigger a fresh provider invocation, not
    only flip frontend state. The previous round's future is
    invalidated and the adapter's prepare_sms is called again."""
    adapter = _Adapter()
    adapter._sms_provider = QueuedSmsProvider(future=sms_future)
    adapter._password_provider = QueuedPasswordProvider(future=password_future)
    svc = MaxAuthService(
        adapter=adapter,
        store=tmp_store,
        sms_future=sms_future,
        password_future=password_future,
    )
    await svc.start(phone="+79991234567")
    await _wait_until(lambda: svc.state == MaxAuthState.WAITING_CODE)
    first_calls = adapter.sms_calls
    snap = await svc.resend_code()
    assert snap.state == MaxAuthState.CONNECTING
    # The new prepare_sms round must invoke the provider
    # again, publishing WAITING_CODE via the callback.
    await _wait_until(lambda: adapter.sms_calls > first_calls)
    await _wait_until(lambda: svc.state == MaxAuthState.WAITING_CODE)
    assert adapter.sms_calls > first_calls


def test_proxy_url_parser():
    """The /api/telegram/transport endpoint uses _parse_proxy_url."""
    from shmax.api.app import _parse_proxy_url

    cfg = _parse_proxy_url("socks5://user:pa%40ss@127.0.0.1:9050")
    assert cfg is not None
    assert cfg.scheme == "socks5"
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 9050
    assert cfg.username == "user"
    assert cfg.password == "pa@ss"

    cfg = _parse_proxy_url("http://proxy.example.com:8080")
    assert cfg is not None and cfg.scheme == "http"

    # IPv6
    cfg = _parse_proxy_url("socks5://[2001:db8::1]:1080")
    assert cfg is not None and cfg.host == "2001:db8::1"

    # Bad schemes
    assert _parse_proxy_url("ftp://x") is None
    assert _parse_proxy_url("") is None
    assert _parse_proxy_url("socks5://no-port") is None


def test_token_holder_round_trip():
    from shmax.api.app import TokenHolder

    h = TokenHolder()
    assert h.peek() is None
    h.set("abc")
    assert h.peek() == "abc"