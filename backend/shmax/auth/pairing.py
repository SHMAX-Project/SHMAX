"""ACL and pairing services.

* :class:`AclService` — answers "is this principal allowed to do
  this thing for this scope?" with role-based defaults plus
  per-rule overrides.
* :class:`PairingCodeService` — issues short-lived pairing codes
  for Telegram groups and Telegram users.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import secrets
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database.models import AclRule, TelegramUser
from ..domain.enums import Role


# Default role permissions.
DEFAULT_PERMISSIONS: dict[Role, set[str]] = {
    Role.OWNER: {"view_chat", "send_message", "manage_routes", "manage_users", "manage_system"},
    Role.ADMIN: {"view_chat", "send_message", "manage_routes", "manage_users"},
    Role.OPERATOR: {"view_chat", "send_message", "manage_routes"},
    Role.READ_ONLY: {"view_chat"},
}


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AclDecision:
    allowed: bool
    reason: str = ""


class AclService:
    """Role + per-rule ACL enforcement.

    Decision order:

    1. **Explicit per-rule override wins.** If a rule matching
       the principal exists, the most-specific scope (user > chat
       > global) wins.
    2. **Role default.** If the principal is a known role
       (``owner | admin | operator | read_only``), the role's
       permission set is used.
    3. **Unknown principal: DENY.** A principal that is neither
       a role nor has an explicit rule is denied by default.
       This is the V1 fail-closed posture: a Telegram user
       who has not been paired MUST NOT send messages.

    The same default applies to ``view_chat``; unknown users
    cannot even read the bridge state.

    Pairing flow: the operator pairs a Telegram group/user via
    :class:`PairingCodeService`. Successful consumption creates
    a ``TelegramUser`` row with a chosen role, which becomes a
    known principal on subsequent ACL checks.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def check(
        self,
        *,
        principal: str,
        permission: str,
        scope: str = "global",
        scope_ref: Optional[str] = None,
    ) -> AclDecision:
        # Resolve the effective role for this principal. Known
        # role names ("owner", "admin", "operator",
        # "read_only") are used as-is. Otherwise we look up
        # ``TelegramUser.user_id == principal`` to read its
        # ``role`` column — pairing materialises that row.
        # If neither path finds a role, the principal is
        # denied.
        role = await self._resolve_role(principal)
        # Check the ``blocked`` flag on the underlying
        # TelegramUser row — explicit per-rule allow entries
        # CANNOT revive a blocked user (V1 contract).
        try:
            from ..database.models import TelegramUser
            stmt = select(TelegramUser).where(TelegramUser.user_id == principal)
            user = (await self._session.execute(stmt)).scalars().first()
            if user is not None and getattr(user, "blocked", False):
                return AclDecision(
                    allowed=False, reason=f"blocked:{principal}"
                )
        except Exception:  # noqa: BLE001
            pass

        # 1. Explicit per-rule overrides win regardless of role.
        candidates: list[AclRule] = []
        stmt = select(AclRule).where(AclRule.principal == principal)
        for row in (await self._session.execute(stmt)).scalars():
            if row.scope == "user" and scope_ref == row.scope_ref:
                candidates.append(row)
            elif row.scope == "chat" and scope_ref == row.scope_ref:
                candidates.append(row)
            elif row.scope == "global":
                candidates.append(row)
        # Most specific first; first match wins.
        for row in sorted(candidates, key=lambda r: ["user", "chat", "global"].index(r.scope)):
            if row.permission != permission:
                continue
            return AclDecision(allowed=row.allow, reason=f"rule:{row.scope}:{row.permission}")
        # 2. Role default — only if the principal is known
        # (either as a role literal or as a paired Telegram user).
        if role is not None:
            base_allowed = permission in DEFAULT_PERMISSIONS.get(role, set())
            return AclDecision(allowed=base_allowed, reason=f"role:{role.value}")
        # 3. Unknown principal — DENY. Fail closed.
        return AclDecision(allowed=False, reason=f"unknown_principal:{principal}")

    async def _resolve_role(self, principal: str) -> Optional[Role]:
        role_names = {r.value for r in Role}
        if principal in role_names:
            return Role(principal)
        # Look up a paired Telegram user by id. Avoid a hard
        # import at module top to keep this module import-safe
        # from the CLI.
        try:
            from ..database.models import TelegramUser
        except Exception:  # noqa: BLE001
            return None
        stmt = select(TelegramUser).where(TelegramUser.user_id == principal)
        user = (await self._session.execute(stmt)).scalars().first()
        if user is None:
            return None
        if getattr(user, "blocked", False):
            return None
        try:
            return Role(user.role)
        except ValueError:
            return None


# ---------------------------------------------------------------------------
# Pairing codes
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PairingCode:
    code: str  # plaintext, only returned once
    purpose: str  # "group" | "user"
    expires_at: _dt.datetime


class PairingCodeService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def issue(self, *, purpose: str, ttl: _dt.timedelta) -> PairingCode:
        code = secrets.token_urlsafe(8).replace("_", "-").replace("/", "-")
        # 8 url-safe chars => ~10 chars plaintext
        code = code[:12]
        expires = _dt.datetime.now(_dt.timezone.utc) + ttl
        # Store the hashed code in setup_tokens so the same
        # persistence layer handles pairing. (Re-using the
        # table keeps migrations stable.)
        from ..database.models import SetupToken

        self._session.add(
            SetupToken(
                token_hash=_hash(f"pairing:{purpose}:{code}"),
                expires_at=expires,
                purpose=f"pairing:{purpose}",
            )
        )
        await self._session.commit()
        return PairingCode(code=code, purpose=purpose, expires_at=expires)

    async def validate(self, code: str, *, purpose: str) -> bool:
        from ..database.models import SetupToken

        now = _dt.datetime.now(_dt.timezone.utc)
        stmt = select(SetupToken).where(
            SetupToken.token_hash == _hash(f"pairing:{purpose}:{code}"),
            SetupToken.consumed_at.is_(None),
            SetupToken.purpose == f"pairing:{purpose}",
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return False
        expires = row.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=_dt.timezone.utc)
        return expires > now

    async def consume(self, code: str, *, purpose: str) -> bool:
        from ..database.models import SetupToken

        now = _dt.datetime.now(_dt.timezone.utc)
        stmt = select(SetupToken).where(
            SetupToken.token_hash == _hash(f"pairing:{purpose}:{code}"),
            SetupToken.consumed_at.is_(None),
            SetupToken.purpose == f"pairing:{purpose}",
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
        await self._session.commit()
        return True


__all__ = [
    "AclDecision",
    "AclService",
    "DEFAULT_PERMISSIONS",
    "PairingCode",
    "PairingCodeService",
]
