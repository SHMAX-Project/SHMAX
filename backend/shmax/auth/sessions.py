"""Signed-cookie session tokens.

We use ``itsdangerous.URLSafeTimedSerializer`` so we get HMAC
signing, constant-time comparison, and an absolute expiry for
free. Sessions are stored in a single cookie and never on the
server — the cookie carries the username, role, and an expiry
timestamp.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


@dataclass(frozen=True)
class SessionData:
    session_id: str
    username: str
    role: str
    issued_at: int  # unix seconds
    csrf_token: str


class SessionSigner:
    def __init__(self, secret: str, *, max_age_seconds: int = 60 * 60 * 8) -> None:
        if not secret:
            raise ValueError("session secret must not be empty")
        self._serializer = URLSafeTimedSerializer(secret, salt="shmax-session")
        self._max_age = max_age_seconds

    def issue(self, *, username: str, role: str) -> SessionData:
        import secrets as _secrets
        import time as _time

        now = int(_time.time())
        return SessionData(
            session_id=_secrets.token_urlsafe(24),
            username=username,
            role=role,
            issued_at=now,
            csrf_token=secrets.token_urlsafe(32),
        )

    def serialize(self, data: SessionData) -> str:
        return self._serializer.dumps(
            {
                "s": data.session_id,
                "u": data.username,
                "r": data.role,
                "t": data.issued_at,
                "c": data.csrf_token,
            }
        )

    def deserialize(self, token: str, *, now: Optional[int] = None) -> Optional[SessionData]:
        try:
            payload = self._serializer.loads(token, max_age=self._max_age)
        except (BadSignature, SignatureExpired):
            return None
        # session_id is required for server-side revocation.
        # Legacy cookies minted before the field existed get
        # a fallback id derived from the username + csrf_token,
        # so the operator stays logged in but the session
        # row is renewed on next request.
        sid = payload.get("s")
        if not sid:
            sid = (
                payload["u"]
                + ":"
                + payload["c"][:8]
            )
        return SessionData(
            session_id=sid,
            username=payload["u"],
            role=payload["r"],
            issued_at=int(payload["t"]),
            csrf_token=payload["c"],
        )


__all__ = ["SessionSigner", "SessionData"]
