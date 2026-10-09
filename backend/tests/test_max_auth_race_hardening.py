"""P1 #2 — MAX auth cancel / generation race hardening.

Pinned:

* invalid secondary start() while in flight NEVER mutates the
  active round (busy guard runs before validation);
* the provider callback carries the generation captured when the
  provider was created — a concurrently-invalidated future cannot
  publish state for the new round;
* a timed-out round awaits the cancelled connect task and
  disconnects the adapter — no leaked half-open connection.
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


async def _wait(predicate, timeout: float = 3.0):
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return True
        await asyncio.sleep(0.01)
        elapsed += 0.01
    return predicate()


class _SlowAdapter:
    """prepare_sms hangs forever; records disconnect calls."""

    def __init__(self, sms_future, password_future) -> None:
        self._sms_provider = QueuedSmsProvider(future=sms_future)
        self._password_provider = QueuedPasswordProvider(future=password_future)
        self.disconnects = 0
        self.prepare_calls: list[str] = []

    async def prepare_sms(self, *, phone: str) -> None:
        await asyncio.sleep(3600)

    async def connect(self) -> None:
        await asyncio.sleep(3600)

    async def disconnect(self) -> None:
        self.disconnects += 1

    async def get_profile(self):
        return MaxProfile(user_id="", display_name="", handle=None)

    async def get_chats(self):
        return []

    async def listen_events(self):
        if False:  # pragma: no cover
            yield type("E", (), {})()


@pytest.mark.asyncio
async def test_invalid_secondary_start_cannot_overwrite_active_round(
    tmp_store, sms_future, password_future
):
    """P1 #2: the busy guard must run BEFORE phone validation —
    an invalid secondary request is a no-op, not a FAILED."""
    adapter = _SlowAdapter(sms_future, password_future)
    svc = MaxAuthService(
        adapter=adapter,
        store=tmp_store,
        sms_future=sms_future,
        password_future=password_future,
    )
    # Round 1 in flight.
    await svc.start(phone="+79991234567")
    assert svc.state == MaxAuthState.CONNECTING
    # An invalid SECONDARY request: under the old ordering this
    # published FAILED over the active round.
    out = await svc.start(phone="not-a-phone")
    assert svc.state != MaxAuthState.FAILED
    assert out is svc.snapshot
    await svc.cancel()


@pytest.mark.asyncio
async def test_stale_provider_callback_cannot_publish_new_state(
    tmp_store, sms_future, password_future
):
    """P1 #2: the callback generation is captured when the provider
    was created. If the future is invalidated concurrently (gen
    bumped), the stale callback must be dropped, not published."""
    received: list[tuple[str, int]] = []
    sms_future.set_callbacks(on_waiting=lambda kind, gen: received.append((kind, gen)))

    provider = QueuedSmsProvider(future=sms_future)

    async def _call_provider():
        await provider.get_code("+79991234567")

    task = asyncio.ensure_future(_call_provider())
    await _wait(lambda: len(received) == 1)
    captured_gen = received[0][1]
    assert captured_gen == 0

    # Concurrent invalidate bumps the shared generation.
    sms_future.invalidate()
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(task, timeout=1.0)

    # A NEW round captures gen=1; replaying the OLD callback with
    # gen=0 must be rejected by the service.
    adapter = _SlowAdapter(sms_future, password_future)
    svc = MaxAuthService(
        adapter=adapter,
        store=tmp_store,
        sms_future=sms_future,
        password_future=password_future,
    )
    await svc.start(phone="+79991234567")
    assert svc.state in (MaxAuthState.CONNECTING, MaxAuthState.REQUESTING_CODE)
    svc._on_provider_waiting("sms", captured_gen)
    assert svc.state not in (MaxAuthState.WAITING_CODE, MaxAuthState.WAITING_PASSWORD)
    await svc.cancel()


@pytest.mark.asyncio
async def test_timeout_disconnects_adapter_and_awaits_task(
    tmp_store, sms_future, password_future, monkeypatch
):
    """P1 #2: a timed-out round disconnects the adapter; the
    cancelled connect task is awaited before the round exits."""
    adapter = _SlowAdapter(sms_future, password_future)
    svc = MaxAuthService(
        adapter=adapter,
        store=tmp_store,
        sms_future=sms_future,
        password_future=password_future,
    )
    monkeypatch.setattr(svc, "_CONNECT_TIMEOUT", 0.3)
    await svc.start(phone="+79991234567")
    ok = await _wait(
        lambda: svc.state == MaxAuthState.FAILED
        and svc.snapshot.last_error_class == "ConnectTimeout",
        timeout=3.0,
    )
    assert ok, f"expected bounded FAILED, got {svc.state}"
    # The adapter was disconnected during timeout cleanup.
    assert adapter.disconnects >= 1
    await svc.cancel()


# ----------------------------------------------------------------------
# P1 #3 (surgical) — immutable generation ownership
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_old_provider_callback_carries_old_generation(
    tmp_store, sms_future, password_future
):
    """create provider generation 1 → invalidate/start generation 2 →
    invoke the OLD provider's get_code() → its callback still carries
    generation 1, and generation-2 state remains unchanged."""
    received: list[tuple[str, int]] = []
    sms_future.set_callbacks(
        on_waiting=lambda kind, gen: received.append((kind, gen))
    )

    old_provider = QueuedSmsProvider(future=sms_future)
    assert old_provider.generation == 0  # generation 1 (first attempt)

    # New attempt: the service re-arms providers, bumping the future.
    adapter = _SlowAdapter(sms_future, password_future)
    svc = MaxAuthService(
        adapter=adapter,
        store=tmp_store,
        sms_future=sms_future,
        password_future=password_future,
    )
    await svc.start(phone="+79991234567")
    assert sms_future.current_generation() == 1  # generation 2

    # The OLD provider (attempt 1) is invoked after the invalidate.
    # Its generation is IMMUTABLY 0, so CredentialFuture.wait
    # rejects it before any callback can fire — a stale provider
    # can never publish under the new generation.
    stale_task = asyncio.ensure_future(old_provider.get_code("+79991234567"))
    # …and the stale wait fails fast (immutable generation 0 vs
    # current 1) — the stale provider can never fulfill or publish
    # under the new attempt.
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(stale_task, timeout=1.0)

    # PyMax invokes the ATTEMPT-2 provider (the adapter holds the
    # re-armed one). Its callback carries generation 1 — the new
    # attempt — and the service publishes WAITING_CODE.
    fresh_provider = adapter._sms_provider
    assert fresh_provider.generation == 1
    assert old_provider.generation == 0

    fresh_task = asyncio.ensure_future(fresh_provider.get_code("+79991234567"))
    ok = await _wait(
        lambda: svc.state == MaxAuthState.WAITING_CODE, timeout=3.0
    )
    assert ok, f"fresh attempt must reach WAITING_CODE, got {svc.state}"
    assert all(gen == 1 for _k, gen in received), (
        f"only new-attempt callbacks may publish: {received}"
    )
    # Fulfil the fresh round with the attempt's own generation.
    assert svc._sms.fulfill(generation=1, value="123456") is True
    assert await asyncio.wait_for(fresh_task, timeout=1.0) == "123456"
    await svc.cancel()
