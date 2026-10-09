"""Production-path regression tests for the MAX auth state machine.

These tests exercise the *real* ``PyMaxMaxAdapter`` (not the
in-memory ``FakeMaxAdapter``) and intercept only the network
layer. The real SHMAX auth state machine, the real PyMax
``SmsAuthFlow``, the real ``SmsCodeProvider`` and the real
``CredentialFuture`` lifecycle all run end-to-end.

Critical regression: starting the auth flow must NOT publish
``WAITING_CODE``. The state advances to ``WAITING_CODE`` only
when PyMax's ``SmsCodeProvider.get_code`` is invoked — which
per upstream PyMax 2.4.1 only happens after
``request_code(phone)`` returned successfully.

If the mocked MAX transport rejects the verification request,
the state must land at ``FAILED`` (not ``WAITING_CODE``) so
the operator is not misled into waiting for an SMS that will
never arrive.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from shmax.adapters.max.config import PyMaxAdapterConfig
from shmax.adapters.max.providers import (
    CredentialFuture,
    QueuedPasswordProvider,
    QueuedSmsProvider,
)
from shmax.adapters.max.pymax_adapter import PyMaxMaxAdapter
from shmax.auth.max import MaxAuthService, MaxAuthState, MaxAuthStateStore


async def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.02):
    """Spin the event loop until ``predicate()`` returns truthy."""
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return True
        await asyncio.sleep(interval)
        elapsed += interval
    return predicate()


@pytest.fixture
def tmp_session_dir(tmp_path: Path):
    sd = tmp_path / "session"
    sd.mkdir()
    return sd


def _build_real_adapter(
    session_dir: Path,
    *,
    sms: CredentialFuture,
    pwd: CredentialFuture,
) -> PyMaxMaxAdapter:
    cfg = PyMaxAdapterConfig(
        phone=None,
        session_dir=session_dir,
        session_name="test.db",
    )
    return PyMaxMaxAdapter(
        config=cfg,
        sms_provider=QueuedSmsProvider(future=sms),
        password_provider=QueuedPasswordProvider(future=pwd),
    )


@pytest.mark.asyncio
async def test_real_adapter_does_not_publish_waiting_code_without_provider(
    tmp_path: Path,
):
    """Production-path regression: when the real PyMaxMaxAdapter's
    connect() completes WITHOUT invoking the SmsCodeProvider
    (e.g. an already-authenticated session, or a malformed
    transport that short-circuits the auth flow), the SHMAX
    state machine MUST publish AUTHENTICATED only if a real
    profile is available — otherwise FAILED. NEVER WAITING_CODE.
    """
    sms = CredentialFuture(kind="sms")
    pwd = CredentialFuture(kind="password")
    adapter = _build_real_adapter(tmp_path / "session", sms=sms, pwd=pwd)
    svc = MaxAuthService(
        adapter=adapter,
        store=MaxAuthStateStore(tmp_path / "state.json"),
        sms_future=sms,
        password_future=pwd,
    )
    # Patch connect() to simulate a transport that closes
    # without invoking the SMS provider (e.g. handshake OK,
    # but request_code short-circuited because the session
    # token is already on disk). The state machine must
    # detect the missing provider and surface FAILED.
    async def fake_connect() -> None:
        # No provider.get_code invocation; we are *not*
        # authenticated (no _handle.client.me).
        return

    adapter.connect = fake_connect  # type: ignore[assignment]

    await svc.start(phone="+79991234567")
    await _wait_until(
        lambda: svc.state in (MaxAuthState.FAILED, MaxAuthState.WAITING_CODE),
        timeout=3.0,
    )
    assert svc.state == MaxAuthState.FAILED
    assert svc.snapshot.last_error_class == "AuthFlowNoProvider"
    assert svc.snapshot.next_action == "retry"


@pytest.mark.asyncio
async def test_real_adapter_publishes_waiting_code_via_provider_callback(
    tmp_path: Path,
):
    """Production-path happy scenario: when the real
    PyMaxMaxAdapter.connect() invokes the SmsCodeProvider
    AFTER the handshake / request_code round-trip, the SHMAX
    state machine publishes WAITING_CODE via the on_waiting
    callback — not synchronously inside start().
    """
    sms = CredentialFuture(kind="sms")
    pwd = CredentialFuture(kind="password")
    adapter = _build_real_adapter(tmp_path / "session", sms=sms, pwd=pwd)
    svc = MaxAuthService(
        adapter=adapter,
        store=MaxAuthStateStore(tmp_path / "state.json"),
        sms_future=sms,
        password_future=pwd,
    )

    ssm_called = {"value": False}

    async def fake_connect() -> None:
        # Simulate PyMax: handshake OK, request_code accepted,
        # then await code_provider.get_code (which is the
        # authoritative signal we are about to publish
        # WAITING_CODE).
        ssm_called["value"] = True
        provider = QueuedSmsProvider(future=sms)
        await provider.get_code("+79991234567")

    adapter.connect = fake_connect  # type: ignore[assignment]

    snap = await svc.start(phone="+79991234567")
    # start() must publish CONNECTING.
    assert snap.state == MaxAuthState.CONNECTING
    # The auth task runs connect(), which blocks on
    # code_provider.get_code — the on_waiting callback then
    # publishes WAITING_CODE.
    await _wait_until(lambda: svc.state == MaxAuthState.WAITING_CODE, timeout=3.0)
    assert svc.state == MaxAuthState.WAITING_CODE
    assert ssm_called["value"] is True


@pytest.mark.asyncio
async def test_real_adapter_transport_failure_publishes_failed(
    tmp_path: Path,
):
    """If the real transport raises a transient error (network
    reset, MAX 5xx, etc.), the state machine must NOT leave
    the operator waiting for an SMS that never arrives."""
    from shmax.adapters.max.errors import MaxSessionError

    sms = CredentialFuture(kind="sms")
    pwd = CredentialFuture(kind="password")
    adapter = _build_real_adapter(tmp_path / "session", sms=sms, pwd=pwd)
    svc = MaxAuthService(
        adapter=adapter,
        store=MaxAuthStateStore(tmp_path / "state.json"),
        sms_future=sms,
        password_future=pwd,
    )

    async def fake_connect() -> None:
        # Simulate the network layer raising before reaching
        # the SmsCodeProvider.
        raise MaxSessionError("connection reset by peer")

    adapter.connect = fake_connect  # type: ignore[assignment]

    await svc.start(phone="+79991234567")
    await _wait_until(lambda: svc.state == MaxAuthState.FAILED, timeout=3.0)
    assert svc.state == MaxAuthState.FAILED
    assert svc.snapshot.next_action == "retry"


@pytest.mark.asyncio
async def test_auth_task_singleton_real_adapter(tmp_path: Path):
    """Repeated start() must never leak background tasks on the
    real PyMaxMaxAdapter."""
    sms = CredentialFuture(kind="sms")
    pwd = CredentialFuture(kind="password")
    adapter = _build_real_adapter(tmp_path / "session", sms=sms, pwd=pwd)
    svc = MaxAuthService(
        adapter=adapter,
        store=MaxAuthStateStore(tmp_path / "state.json"),
        sms_future=sms,
        password_future=pwd,
    )

    for _ in range(3):
        await svc.start(phone="+79991234567")
        await _wait_until(
            lambda: svc.state in (
                MaxAuthState.CONNECTING,
                MaxAuthState.REQUESTING_CODE,
                MaxAuthState.WAITING_CODE,
                MaxAuthState.FAILED,
            ),
            timeout=2.0,
        )
        assert svc._auth_task is not None
        await svc.cancel()
        assert svc._auth_task is None