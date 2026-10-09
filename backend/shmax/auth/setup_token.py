"""One-time setup token service.

Tokens are random 24-byte values; we only ever store their
SHA-256 hash on disk (via the ``setup_tokens`` table). Tokens
expire after a configurable TTL and are marked consumed when
used, so each token is good for exactly one successful use.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import secrets
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database.models import SetupToken


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SetupTokenInfo:
    token: str  # plaintext, only returned once
    expires_at: _dt.datetime
    purpose: str


class SetupTokenService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, *, ttl: _dt.timedelta, purpose: str = "bootstrap") -> SetupTokenInfo:
        token = secrets.token_urlsafe(24)
        expires = _dt.datetime.now(_dt.timezone.utc) + ttl
        row = SetupToken(
            token_hash=_hash(token),
            expires_at=expires,
            purpose=purpose,
        )
        self._session.add(row)
        await self._session.commit()
        return SetupTokenInfo(token=token, expires_at=expires, purpose=purpose)

    async def validate(self, token: str) -> bool:
        """Return True iff token exists, is unconsumed, and is not expired."""
        if not token:
            return False
        now = _dt.datetime.now(_dt.timezone.utc)
        stmt = select(SetupToken).where(
            SetupToken.token_hash == _hash(token),
            SetupToken.consumed_at.is_(None),
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return False
        # SQLite returns naive datetimes; normalize for comparison.
        expires = row.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=_dt.timezone.utc)
        if expires <= now:
            return False
        return True

    async def consume(self, token: str, *, commit: bool = True) -> bool:
        if not token:
            return False
        now = _dt.datetime.now(_dt.timezone.utc)
        stmt = select(SetupToken).where(
            SetupToken.token_hash == _hash(token),
            SetupToken.consumed_at.is_(None),
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return False
        expires = row.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=_dt.timezone.utc)
        if expires <= now:
            return False
        row.consumed_at = now
        if commit:
            await self._session.commit()
        else:
            await self._session.flush()
        return True


__all__ = ["SetupTokenService", "SetupTokenInfo"]
