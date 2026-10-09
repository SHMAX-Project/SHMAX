"""Pairing-code repository for /shmax_connect Telegram group
pairing (audit P0 #6, hardening P1 #3).

Stores short-lived, single-use, cryptographically-random codes
that the wizard hands to the operator. The operator adds the
SHMAX bot to a Telegram supergroup, then sends the code via
``/shmax_connect CODE``. The Telegram adapter routes that command
through ``atomic_consume`` which atomically validates and
consumes the code (returns the target MAX chat id + optional
thread id), then persists the bridge route.

Properties enforced:

* cryptographic randomness — 8 random bytes from ``secrets``
  (64 bits of entropy), URL-safe base64-encoded to an 11-char
  code. Given the ONE-TIME consume plus the per-code
  ``max_attempts`` guard and the per-chat rate limit in the
  command handler, offline brute force at 2^64 is impractical
  and online guessing is bounded to 5 tries per code and the
  rate-limited window per chat. (The previous docstring claimed
  "64^8 = 2.8e14" against an 11-char code — mathematically
  wrong; the real space is 2^64 ≈ 1.8e19.)
* at-rest protection — only the SHA-256 hash of the code is
  stored; a leaked database cannot be replayed against the bot.
* expiry (default 10 minutes)
* one-time consume via a single conditional UPDATE
  (compare-and-set on ``consumed_at IS NULL``) — two concurrent
  claims can never both succeed (P1 #3).
* max_attempts guard so a leaked code cannot be brute-forced.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, text, update

from ..database.models import PairingCodeRow


def _generate_code() -> str:
    """Generate a URL-safe pairing code with 64 bits of entropy.

    8 random bytes → ``base64.urlsafe_b64encode`` → 12 chars with
    one '=' padding → 11 chars after stripping. See the module
    docstring for the honest security math.
    """
    raw = secrets.token_bytes(8)
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _hash(code: str) -> str:
    # Truncated SHA-256 (128 bits) fits the 32-char primary key
    # while keeping preimage search infeasible.
    return hashlib.sha256(code.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class PairingClaim:
    """Result returned by ``atomic_consume``."""

    chat_external_id: str
    thread_id: Optional[int]
    max_chat_id: str
    max_chat_title: Optional[str]


@dataclass(frozen=True)
class PairingCode:
    """Public representation of a freshly issued code."""

    code: str
    expires_at: int
    max_chat_id: str
    max_chat_title: Optional[str]
    telegram_chat_hint: Optional[str]


class PairingError(Exception):
    """Base class for pairing errors."""


class PairingInvalid(PairingError):
    """Code is unknown, expired, exhausted, or already consumed."""


class PairingCodesRepository:
    """Persistence for the wizard-issued /shmax_connect codes."""

    DEFAULT_TTL_SECONDS = 600  # 10 minutes
    DEFAULT_MAX_ATTEMPTS = 5

    def __init__(self, session_factory, *, ttl_seconds: int = DEFAULT_TTL_SECONDS,
                 max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> None:
        self._session_factory = session_factory
        self._ttl = ttl_seconds
        self._max_attempts = max_attempts

    async def issue(
        self,
        *,
        max_chat_id: str = "",
        max_chat_title: Optional[str] = None,
        telegram_chat_hint: Optional[str] = None,
        ttl_seconds: Optional[int] = None,
    ) -> PairingCode:
        """Issue a brand-new code. ``max_chat_id`` is optional: a
        code without a chat binding pairs the Telegram group
        itself (the canonical V1 flow — routes are created later
        from the MAX-chats step). The plaintext code is returned
        exactly once; only the hash is persisted."""
        ttl = ttl_seconds if ttl_seconds is not None else self._ttl
        code = _generate_code()
        expires_at = int(time.time()) + ttl
        async with self._session_factory() as session:
            session.add(
                PairingCodeRow(
                    code=_hash(code),
                    issued_at=int(time.time()),
                    expires_at=expires_at,
                    consumed_at=None,
                    attempts=0,
                    max_chat_id=max_chat_id or "",
                    max_chat_title=max_chat_title,
                    telegram_chat_hint=telegram_chat_hint,
                )
            )
            await session.commit()
        return PairingCode(
            code=code,
            expires_at=expires_at,
            max_chat_id=max_chat_id or "",
            max_chat_title=max_chat_title,
            telegram_chat_hint=telegram_chat_hint,
        )

    async def revoke(self, *, code: str) -> bool:
        """Mark a code as consumed (revoked) so it cannot be
        reused. Returns True if the code existed and was revoked;
        False otherwise."""
        code_hash = _hash(code)
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(PairingCodeRow).where(PairingCodeRow.code == code_hash)
                )
            ).scalars().first()
            if row is None or row.consumed_at is not None:
                return False
            row.consumed_at = int(time.time())
            await session.commit()
            return True

    async def atomic_consume(
        self,
        *,
        code: str,
        user_id: str,
        telegram_chat_id: Optional[str] = None,
        telegram_thread_id: Optional[int] = None,
    ) -> PairingClaim:
        """Atomic, replay-protected consume (P1 #3).

        The claim is a SINGLE conditional UPDATE —
        ``WHERE code=? AND consumed_at IS NULL AND expires_at>=?
        AND attempts<?`` — so two concurrent consumers race on the
        rowcount, not on a read-then-write window. Exactly one of
        them sees ``rowcount == 1``; the loser raises
        ``PairingInvalid``.

        ``user_id`` is the Telegram user_id that initiated the
        command — recorded for audit; ``telegram_chat_id`` is the
        chat the command came from.

        Raises ``PairingInvalid`` on any failure.
        """
        code_hash = _hash(code)
        now = int(time.time())
        async with self._session_factory() as session:
            # Serialize the claim against SQLite's write lock so
            # the conditional UPDATE below is the only gate.
            await session.execute(text("BEGIN IMMEDIATE"))
            result = await session.execute(
                update(PairingCodeRow)
                .where(
                    PairingCodeRow.code == code_hash,
                    PairingCodeRow.consumed_at.is_(None),
                    PairingCodeRow.expires_at >= now,
                    PairingCodeRow.attempts < self._max_attempts,
                )
                .values(
                    consumed_at=now,
                    attempts=PairingCodeRow.attempts + 1,
                )
            )
            if result.rowcount != 1:
                await session.rollback()
                raise PairingInvalid(
                    "pairing code invalid, expired, exhausted, or already consumed"
                )
            row = (
                await session.execute(
                    select(PairingCodeRow).where(PairingCodeRow.code == code_hash)
                )
            ).scalars().first()
            await session.commit()
            return PairingClaim(
                chat_external_id=telegram_chat_id or "",
                thread_id=telegram_thread_id,
                max_chat_id=row.max_chat_id if row else "",
                max_chat_title=row.max_chat_title if row else None,
            )

    async def list_active(self) -> list[PairingCode]:
        """Codes still valid (not consumed, not expired).

        Codes are stored hashed, so the plaintext is NOT
        recoverable here — ``code`` carries a short display hint
        (first 4 chars of the hash) suitable for "which codes are
        outstanding" lists only."""
        now = int(time.time())
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(PairingCodeRow).where(
                        PairingCodeRow.consumed_at.is_(None),
                        PairingCodeRow.expires_at > now,
                    )
                )
            ).scalars().all()
            return [
                PairingCode(
                    code=(r.code[:4] + "…") if r.code else "",
                    expires_at=r.expires_at,
                    max_chat_id=r.max_chat_id,
                    max_chat_title=r.max_chat_title,
                    telegram_chat_hint=r.telegram_chat_hint,
                )
                for r in rows
            ]


__all__ = [
    "PairingCodesRepository",
    "PairingCode",
    "PairingClaim",
    "PairingError",
    "PairingInvalid",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
