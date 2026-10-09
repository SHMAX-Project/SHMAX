"""Phone normalisation and validation tests.

Covers every audit-flagged case:

  - Russian trunk-8 prefix (``8 (999) 123-45-67``).
  - International +49 / +31 numbers.
  - Spaced, bracketed and hyphenated input.
  - Invalid country codes.
  - Extension rejection.
  - Length validation.

Plus the resend-with-canonical-phone regression: ``resend_code``
must hand MAX the E.164 stored at start(), not the masked
phone_hint the wizard renders.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from shmax.adapters.max.providers import (
    CredentialFuture,
    QueuedPasswordProvider,
    QueuedSmsProvider,
)
from shmax.auth.max import MaxAuthService, MaxAuthState, MaxAuthStateStore
from shmax.util.phone import (
    NormalisedPhone,
    PhoneNormalisationError,
    default_region_for,
    parse,
)


# --- pure-phone parser cases -----------------------------------------


@pytest.mark.parametrize(
    "raw,expected_e164,expected_region",
    [
        ("89991234567", "+79991234567", "RU"),
        ("79991234567", "+79991234567", "RU"),
        ("+79991234567", "+79991234567", "RU"),
        ("+7 999 123 45 67", "+79991234567", "RU"),
        ("8 (999) 123-45-67", "+79991234567", "RU"),
        ("+49 30 12345678", "+493012345678", "DE"),
        ("+31 6 12345678", "+31612345678", "NL"),
        ("+1 202 555 0143", "+12025550143", "US"),
    ],
)
def test_parse_happy_paths(raw, expected_e164, expected_region):
    n = parse(raw)
    assert isinstance(n, NormalisedPhone)
    assert n.e164 == expected_e164
    assert n.region == expected_region
    assert n.hint.startswith("***") and len(n.hint) == 7


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "abc",
        "8964325",  # too short
        "+1 555 0100",  # 555-01xx are reserved, not valid
        "+99912345678",  # 999 is not a real country code
        "+7 999 123 45 67 ext. 12",
        "+7 999 123 45 67 доб. 12",
    ],
)
def test_parse_rejects_with_actionable_message(raw):
    with pytest.raises(PhoneNormalisationError) as exc:
        parse(raw)
    # The error message must contain *something* the operator
    # can act on; never an opaque "5xx" or empty string.
    assert str(exc.value)


def test_default_region_for():
    assert default_region_for("ru_RU") == "RU"
    assert default_region_for("ru-RU") == "RU"
    assert default_region_for(None) == "RU"
    # Unknown locales fall back to RU rather than crashing.
    assert default_region_for("xx-XX") == "RU"


# --- MAX auth integration: canonical phone + resend --------------


class _AdapterProbe:
    """Records the phone handed to prepare_sms() so we can
    assert resend hands MAX the canonical E.164, not the hint.
    """

    def __init__(self, sms_future, password_future) -> None:
        self._sms_provider = QueuedSmsProvider(future=sms_future)
        self._password_provider = QueuedPasswordProvider(
            future=password_future
        )
        self.calls: list[str] = []

    async def prepare_sms(self, *, phone: str) -> None:
        self.calls.append(phone)
        await self._sms_provider.get_code(phone)


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


@pytest.mark.asyncio
async def test_start_normalises_russian_trunk_8(
    tmp_store, sms_future, password_future
):
    adapter = _AdapterProbe(sms_future, password_future)
    svc = MaxAuthService(
        adapter=adapter,
        store=tmp_store,
        sms_future=sms_future,
        password_future=password_future,
    )
    snap = await svc.start(phone="8 (999) 123-45-67")
    assert snap.state == MaxAuthState.CONNECTING
    await asyncio.sleep(0.05)
    # prepare_sms was called with the E.164 form — never the raw
    # trunk-8 string.
    assert adapter.calls and adapter.calls[0] == "+79991234567"


@pytest.mark.asyncio
async def test_start_rejects_unparseable_phone(
    tmp_store, sms_future, password_future
):
    svc = MaxAuthService(
        adapter=_AdapterProbe(sms_future, password_future),
        store=tmp_store,
        sms_future=sms_future,
        password_future=password_future,
    )
    snap = await svc.start(phone="abc")
    assert snap.state == MaxAuthState.FAILED
    assert snap.last_error_class == "PhoneInvalid"


@pytest.mark.asyncio
async def test_resend_uses_canonical_phone_not_masked_hint(
    tmp_store, sms_future, password_future
):
    adapter = _AdapterProbe(sms_future, password_future)
    svc = MaxAuthService(
        adapter=adapter,
        store=tmp_store,
        sms_future=sms_future,
        password_future=password_future,
    )
    await svc.start(phone="+79991234567")
    await asyncio.sleep(0.05)
    adapter.calls.clear()
    snap = await svc.resend_code()
    assert snap.state == MaxAuthState.CONNECTING
    await asyncio.sleep(0.05)
    # The audit regression: resend must hand MAX the canonical
    # phone, never the masked display form.
    assert adapter.calls
    assert all(c.startswith("+") for c in adapter.calls)
    assert all(c != "***4567" for c in adapter.calls)
    assert adapter.calls[0] == "+79991234567"


@pytest.mark.asyncio
async def test_cancel_clears_canonical_phone(
    tmp_store, sms_future, password_future
):
    svc = MaxAuthService(
        adapter=_AdapterProbe(sms_future, password_future),
        store=tmp_store,
        sms_future=sms_future,
        password_future=password_future,
    )
    await svc.start(phone="+79991234567")
    await svc.cancel()
    # After cancel the canonical phone is dropped so a fresh
    # resend (or any retry path) can no longer hand MAX the
    # previous round's number. The state guard at the top of
    # resend_code() rejects the call because state is
    # NOT_STARTED.
    assert svc._canonical_phone is None
    snap = await svc.resend_code()
    assert snap.state == MaxAuthState.NOT_STARTED