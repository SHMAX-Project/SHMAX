"""Regression tests for the MAX auth P1 hardening.

Covers the Codex findings:

* P1 #1 — the connect/handshake phase before the credential
  provider is invoked must be bounded. A hanging adapter must
  surface as FAILED (ConnectTimeout), never an eternal
  "Запрашиваем код у MAX…". The bound must NOT kill a round
  that legitimately reached WAITING_CODE.
* P1 #2 — a stale provider callback / invalid secondary request
  must not mutate an already active auth attempt.
* P1 #5 — user-facing / persisted validation errors must not
  contain the raw phone number.
"""

from __future__ import annotations

import asyncio
import json
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
from shmax.util.phone import PhoneNormalisationError, parse as parse_phone


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
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return True
        await asyncio.sleep(0.01)
        elapsed += 0.01
    return predicate()


class _HangingConnectAdapter:
    """Mimics a MAX endpoint that never completes the handshake."""

    def __init__(self, sms_future, password_future) -> None:
        self._sms_provider = QueuedSmsProvider(future=sms_future)
        self._password_provider = QueuedPasswordProvider(
            future=password_future
        )
        self.connected = False

    async def prepare_sms(self, *, phone: str) -> None:
        await asyncio.sleep(3600)

    async def connect(self) -> None:
        await asyncio.sleep(3600)

    async def disconnect(self) -> None:
        self.connected = False

    async def get_profile(self):
        return MaxProfile(user_id="", display_name="", handle=None)

    async def get_chats(self):
        return []

    async def listen_events(self):
        if False:  # pragma: no cover
            yield type("E", (), {})()


class _SlowThenWaitingAdapter:
    """Slow handshake (2s) that then reaches the provider wait —
    the operator-input phase must survive the connect timeout."""

    def __init__(self, sms_future, password_future) -> None:
        self._sms_provider = QueuedSmsProvider(future=sms_future)
        self._password_provider = QueuedPasswordProvider(
            future=password_future
        )

    async def prepare_sms(self, *, phone: str) -> None:
        await asyncio.sleep(2.0)
        await self._sms_provider.get_code(phone)

    async def connect(self) -> None:
        await asyncio.sleep(3600)

    async def disconnect(self) -> None:
        pass

    async def get_profile(self):
        return MaxProfile(user_id="", display_name="", handle=None)

    async def get_chats(self):
        return []

    async def listen_events(self):
        if False:  # pragma: no cover
            yield type("E", (), {})()


def _make_service(store, sms_future, password_future, adapter) -> MaxAuthService:
    return MaxAuthService(
        adapter=adapter,
        store=store,
        sms_future=sms_future,
        password_future=password_future,
    )


@pytest.mark.asyncio
async def test_hanging_connect_fails_bounded(
    tmp_store, sms_future, password_future, monkeypatch
):
    """P1 #1: a handshake that never completes must publish
    FAILED / ConnectTimeout within a bounded window."""
    adapter = _HangingConnectAdapter(sms_future, password_future)
    svc = _make_service(tmp_store, sms_future, password_future, adapter)
    monkeypatch.setattr(svc, "_CONNECT_TIMEOUT", 1.5)
    await svc.start(phone="+79991234567")
    assert svc.state == MaxAuthState.CONNECTING
    ok = await _wait_state(
        svc,
        predicate=lambda: svc.state == MaxAuthState.FAILED
        and svc.snapshot.last_error_class == "ConnectTimeout",
        timeout=5.0,
    )
    assert ok, f"expected FAILED/ConnectTimeout, got {svc.state}"
    # The failure must also be persisted (restart shows the same state).
    assert tmp_store.load().last_error_class == "ConnectTimeout"
    # Clean up the hanging task.
    await svc.cancel()


@pytest.mark.asyncio
async def test_provider_phase_not_killed_by_connect_timeout(
    tmp_store, sms_future, password_future, monkeypatch
):
    """P1 #1: after the provider fires (WAITING_CODE) the round
    must keep waiting for the operator — the connect bound only
    covers the pre-provider phase. We shrink the bound to prove
    the ordering."""
    adapter = _SlowThenWaitingAdapter(sms_future, password_future)
    svc = _make_service(tmp_store, sms_future, password_future, adapter)
    monkeypatch.setattr(svc, "_CONNECT_TIMEOUT", 5.0)
    await svc.start(phone="+79991234567")
    ok = await _wait_state(
        svc, predicate=lambda: svc.state == MaxAuthState.WAITING_CODE,
        timeout=8.0,
    )
    assert ok, f"expected WAITING_CODE, got {svc.state}"
    # Well past the 5s connect bound now — the round must still be
    # waiting for the operator, not FAILED/ConnectTimeout.
    await asyncio.sleep(5.2)
    assert svc.state == MaxAuthState.WAITING_CODE
    await svc.cancel()


@pytest.mark.asyncio
async def test_stale_provider_waiting_does_not_overwrite_new_round(
    tmp_store, sms_future, password_future
):
    """P1 #2: a provider callback carrying the previous round's
    credential generation must not publish state over the newer
    attempt."""
    adapter = _SlowThenWaitingAdapter(sms_future, password_future)
    svc = _make_service(tmp_store, sms_future, password_future, adapter)
    await svc.start(phone="+79991234567")
    ok = await _wait_state(
        svc, predicate=lambda: svc.state == MaxAuthState.WAITING_CODE,
        timeout=8.0,
    )
    assert ok
    # Start a NEW round via resend (start() while in flight is a
    # no-op by design — the duplicate-SMS guard). resend() bumps
    # both the auth generation and the credential generations.
    stale_gen = sms_future.current_generation()
    await svc.resend_code()
    assert svc.state in (MaxAuthState.CONNECTING, MaxAuthState.REQUESTING_CODE)
    # Simulate the OLD round's provider callback firing with the
    # stale generation.
    svc._on_provider_waiting("sms", stale_gen)
    assert svc.state in (
        MaxAuthState.CONNECTING,
        MaxAuthState.REQUESTING_CODE,
    ), "stale provider callback must not publish WAITING_CODE"
    # The CURRENT round's callback still works.
    ok = await _wait_state(
        svc, predicate=lambda: svc.state == MaxAuthState.WAITING_CODE,
        timeout=8.0,
    )
    assert ok
    await svc.cancel()


@pytest.mark.asyncio
async def test_submit_code_in_wrong_state_does_not_mutate_round(
    tmp_store, sms_future, password_future
):
    """P1 #2: an invalid secondary request (submit while the
    round is still connecting) must not touch the active
    snapshot."""
    adapter = _SlowThenWaitingAdapter(sms_future, password_future)
    svc = _make_service(tmp_store, sms_future, password_future, adapter)
    await svc.start(phone="+79991234567")
    snap_before = svc.snapshot
    assert snap_before.state in (
        MaxAuthState.CONNECTING,
        MaxAuthState.REQUESTING_CODE,
    )
    out = await svc.submit_code(code="123456")
    assert out.state == snap_before.state
    assert out.updated_at == snap_before.updated_at
    assert out.last_error is None
    await svc.cancel()


@pytest.mark.asyncio
async def test_invalid_phone_error_persists_masked_only(
    tmp_store, sms_future, password_future
):
    """P1 #5: a validation failure must persist a FAILED snapshot
    whose last_error carries NO full phone digits."""
    adapter = _SlowThenWaitingAdapter(sms_future, password_future)
    svc = _make_service(tmp_store, sms_future, password_future, adapter)
    snap = await svc.start(phone="12345")  # too short to be a real number
    assert snap.state == MaxAuthState.FAILED
    assert snap.last_error is not None
    raw_digits = "12345"
    assert raw_digits not in snap.last_error
    # The persisted file must be masked as well.
    on_disk = json.loads((Path(tmp_store._path)).read_text())
    assert raw_digits not in json.dumps(on_disk, ensure_ascii=False)
    await svc.cancel()


def test_phone_errors_mask_raw_input():
    """P1 #5: every PhoneNormalisationError message is safe to
    persist — the raw input never appears verbatim."""
    for raw in ("12345", "+7 999 123 45 67 доб 5", "abcdefgh", "+99812345678901234"):
        with pytest.raises(PhoneNormalisationError) as ei:
            parse_phone(raw)
        msg = str(ei.value)
        digits = "".join(ch for ch in raw if ch.isdigit())
        if len(digits) > 4:
            assert digits not in msg, f"raw digits leaked for {raw!r}: {msg}"
