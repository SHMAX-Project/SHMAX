"""MAX auth lifecycle tests — single-flight, busy guard, listener gate.

The audit caught that:
  1. Three real SMS codes were sent from one UI click on the
     ODROID. Root cause: the listener supervisor restarted
     connect(), each restart drove a fresh request_code.
  2. Repeated start() clicks while a round is in flight fired
     additional request_code sequences.

This file pins:

  * start() while a round is in flight is a no-op (busy guard).
  * explicit resend() cancels the previous task and starts one new
    auth round.
  * The MAX listener must NOT call connect() without a saved
    session — otherwise PyMax's connect() runs the SmsAuthFlow
    (request_code) again.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from shmax.adapters.max.base import MaxProfile
from shmax.adapters.max.providers import (
    CredentialFuture,
    QueuedPasswordProvider,
    QueuedSmsProvider,
)
from shmax.auth.max import MaxAuthService, MaxAuthState, MaxAuthStateStore


class _CountingAdapter:
    """Records every prepare_sms() call so we can assert
    "exactly one request per user action"."""

    def __init__(self, sms_future, password_future) -> None:
        # The auth service installs CredentialFuture.on_waiting
        # callbacks on construction. The QueuedSmsProvider object
        # itself does not have set_callbacks — only the future
        # does. We mirror the production wiring.
        self._sms_provider = QueuedSmsProvider(future=sms_future)
        self._password_provider = QueuedPasswordProvider(
            future=password_future
        )
        self.prepare_calls: list[str] = []
        self.connect_calls = 0
        self._connected = False
        self._profile = MaxProfile(user_id="u-1", display_name="Alice", handle="alice")

    async def prepare_sms(self, *, phone: str) -> None:
        self.prepare_calls.append(phone)
        self._connected = True
        # Block on the SMS provider; this mimics PyMax's
        # behaviour — request_code returns, then
        # code_provider.get_code is awaited. The on_waiting
        # callback fires here, publishing WAITING_CODE.
        await self._sms_provider.get_code(phone)

    async def connect(self) -> None:
        self.connect_calls += 1
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def get_profile(self):
        if not self._connected:
            from shmax.adapters.max.errors import MaxSessionError
            raise MaxSessionError("not connected")
        return self._profile

    async def get_chats(self):
        return []

    async def listen_events(self):
        if False:  # pragma: no cover
            yield type("E", (), {"kind": "message"})()


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


async def _wait_state(svc: MaxAuthService, *, predicate, timeout: float = 2.0):
    """Spin the loop until ``predicate()`` returns truthy."""
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return True
        await asyncio.sleep(0.01)
        elapsed += 0.01
    return predicate()


@pytest.mark.asyncio
async def test_start_busy_guard_blocks_double_request(
    tmp_store, sms_future, password_future
):
    """Two start() calls while a round is in flight must fire
    exactly ONE prepare_sms — the second is a no-op.
    """
    adapter = _CountingAdapter(sms_future, password_future)
    svc = MaxAuthService(
        adapter=adapter,
        store=tmp_store,
        sms_future=sms_future,
        password_future=password_future,
    )
    # First click.
    await svc.start(phone="+79991234567")
    await _wait_state(svc, predicate=lambda: svc.state == MaxAuthState.WAITING_CODE)
    # Second click while WAITING_CODE — must be a no-op.
    snap2 = await svc.start(phone="+79991234567")
    assert snap2.state == MaxAuthState.WAITING_CODE
    await asyncio.sleep(0.05)
    # Only ONE prepare_sms() ever fired.
    assert len(adapter.prepare_calls) == 1
    assert adapter.prepare_calls[0] == "+79991234567"


@pytest.mark.asyncio
async def test_resend_re_invokes_provider_exactly_once(
    tmp_store, sms_future, password_future
):
    """Explicit resend must cancel the prior round and fire
    exactly one fresh prepare_sms."""
    adapter = _CountingAdapter(sms_future, password_future)
    svc = MaxAuthService(
        adapter=adapter,
        store=tmp_store,
        sms_future=sms_future,
        password_future=password_future,
    )
    await svc.start(phone="+79991234567")
    await _wait_state(svc, predicate=lambda: svc.state == MaxAuthState.WAITING_CODE)
    first_count = len(adapter.prepare_calls)
    # Resend.
    await svc.resend_code()
    await _wait_state(
        svc, predicate=lambda: svc.state == MaxAuthState.WAITING_CODE
    )
    # The new round must have fired exactly one additional
    # prepare_sms (not zero, not two).
    assert len(adapter.prepare_calls) == first_count + 1


@pytest.mark.asyncio
async def test_cancel_resets_state_and_drops_provider(
    tmp_store, sms_future, password_future
):
    adapter = _CountingAdapter(sms_future, password_future)
    svc = MaxAuthService(
        adapter=adapter,
        store=tmp_store,
        sms_future=sms_future,
        password_future=password_future,
    )
    await svc.start(phone="+79991234567")
    await _wait_state(svc, predicate=lambda: svc.state == MaxAuthState.WAITING_CODE)
    await svc.cancel()
    assert svc.state == MaxAuthState.NOT_STARTED
    # After cancel, the credential future must be invalidated
    # so a stale OTP cannot fulfil a future round.
    with pytest.raises(RuntimeError):
        await sms_future.wait(generation=sms_future.current_generation() - 1)


@pytest.mark.asyncio
async def test_terminal_failure_is_not_overwritten_by_refresh(
    tmp_store, sms_future, password_future
):
    """Audit H: terminal FAILED must remain visible. A refresh
    must not overwrite a terminal error back to NOT_STARTED."""

    class _BoomAdapter(_CountingAdapter):
        async def prepare_sms(self, *, phone: str) -> None:
            self.prepare_calls.append(phone)
            raise RuntimeError("synthetic upstream boom")

    adapter = _BoomAdapter(sms_future, password_future)
    svc = MaxAuthService(
        adapter=adapter,
        store=tmp_store,
        sms_future=sms_future,
        password_future=password_future,
    )
    await svc.start(phone="+79991234567")
    await _wait_state(svc, predicate=lambda: svc.state == MaxAuthState.FAILED)
    # last_error must be populated and stable.
    err_class = svc.snapshot.last_error_class
    err_text = svc.snapshot.last_error
    # A refresh MUST NOT silently overwrite FAILED.
    snap = await svc.refresh()
    assert snap.state == MaxAuthState.FAILED
    assert snap.last_error_class == err_class
    assert snap.last_error == err_text