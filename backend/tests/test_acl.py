"""ACL service tests."""

from __future__ import annotations

import pytest

from shmax.auth import AclService
from shmax.auth.pairing import DEFAULT_PERMISSIONS
from shmax.database.models import AclRule
from shmax.domain.enums import Role


@pytest.mark.asyncio
async def test_owner_has_all_permissions(db_session):
    svc = AclService(db_session)
    for perm in ("view_chat", "send_message", "manage_routes", "manage_users", "manage_system"):
        d = await svc.check(principal=Role.OWNER.value, permission=perm)
        assert d.allowed is True, perm


@pytest.mark.asyncio
async def test_read_only_cannot_send(db_session):
    svc = AclService(db_session)
    d = await svc.check(principal=Role.READ_ONLY.value, permission="send_message")
    assert d.allowed is False
    # read_only can still view
    d2 = await svc.check(principal=Role.READ_ONLY.value, permission="view_chat")
    assert d2.allowed is True
    # but cannot manage_routes / manage_users / manage_system
    for perm in ("manage_routes", "manage_users", "manage_system"):
        d3 = await svc.check(principal=Role.READ_ONLY.value, permission=perm)
        assert d3.allowed is False, perm


@pytest.mark.asyncio
async def test_unknown_principal_is_denied(db_session):
    """V1 fail-closed posture: a Telegram user who has not been
    paired MUST NOT send messages."""
    svc = AclService(db_session)
    d = await svc.check(principal="telegram:9999", permission="send_message")
    assert d.allowed is False
    assert "unknown_principal" in d.reason


@pytest.mark.asyncio
async def test_explicit_deny_rule_wins(db_session):
    db_session.add(
        AclRule(scope="global", principal=Role.OPERATOR.value, permission="manage_system", allow=False)
    )
    await db_session.commit()
    svc = AclService(db_session)
    d = await svc.check(principal=Role.OPERATOR.value, permission="manage_system")
    assert d.allowed is False


@pytest.mark.asyncio
async def test_chat_scoped_rule_overrides_global(db_session):
    db_session.add(
        AclRule(scope="global", principal="telegram:42", permission="send_message", allow=True)
    )
    db_session.add(
        AclRule(scope="chat", principal="telegram:42", scope_ref="chat-1", permission="send_message", allow=False)
    )
    await db_session.commit()
    svc = AclService(db_session)
    d1 = await svc.check(principal="telegram:42", permission="send_message", scope="chat", scope_ref="chat-1")
    d2 = await svc.check(principal="telegram:42", permission="send_message", scope="chat", scope_ref="chat-2")
    assert d1.allowed is False  # chat rule wins
    assert d2.allowed is True  # falls back to global


@pytest.mark.asyncio
async def test_default_permissions_shape(db_session):
    # Owner > admin > operator > read_only is the intended gradient.
    assert "manage_system" in DEFAULT_PERMISSIONS[Role.OWNER]
    assert "manage_system" not in DEFAULT_PERMISSIONS[Role.ADMIN]
    assert "send_message" not in DEFAULT_PERMISSIONS[Role.READ_ONLY]
