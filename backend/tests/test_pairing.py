"""Pairing code tests."""

from __future__ import annotations

import datetime as _dt

import pytest

from shmax.auth import PairingCodeService


@pytest.mark.asyncio
async def test_pairing_group_issue_validate_consume(db_session):
    svc = PairingCodeService(db_session)
    code = await svc.issue(purpose="group", ttl=_dt.timedelta(minutes=10))
    assert await svc.validate(code.code, purpose="group")
    assert await svc.consume(code.code, purpose="group")
    assert not await svc.validate(code.code, purpose="group")


@pytest.mark.asyncio
async def test_pairing_purpose_mismatch_rejected(db_session):
    svc = PairingCodeService(db_session)
    code = await svc.issue(purpose="group", ttl=_dt.timedelta(minutes=10))
    assert not await svc.validate(code.code, purpose="user")


@pytest.mark.asyncio
async def test_pairing_expired_rejected(db_session):
    svc = PairingCodeService(db_session)
    code = await svc.issue(purpose="group", ttl=_dt.timedelta(seconds=-1))
    assert not await svc.validate(code.code, purpose="group")
