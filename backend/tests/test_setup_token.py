"""Setup token tests."""

from __future__ import annotations

import asyncio
import datetime as _dt

import pytest


@pytest.mark.asyncio
async def test_create_validate_consume(db_session):
    from shmax.auth import SetupTokenService

    svc = SetupTokenService(db_session)
    info = await svc.create(ttl=_dt.timedelta(minutes=5), purpose="bootstrap")
    assert info.token
    assert await svc.validate(info.token)
    assert await svc.consume(info.token)
    assert not await svc.validate(info.token)


@pytest.mark.asyncio
async def test_expired_token_rejected(db_session):
    from shmax.auth import SetupTokenService

    svc = SetupTokenService(db_session)
    info = await svc.create(ttl=_dt.timedelta(seconds=-1), purpose="bootstrap")
    assert not await svc.validate(info.token)
    assert not await svc.consume(info.token)


@pytest.mark.asyncio
async def test_consume_only_once(db_session):
    from shmax.auth import SetupTokenService

    svc = SetupTokenService(db_session)
    info = await svc.create(ttl=_dt.timedelta(minutes=5), purpose="bootstrap")
    assert await svc.consume(info.token)
    # Second consume should be a no-op returning False.
    assert not await svc.consume(info.token)
