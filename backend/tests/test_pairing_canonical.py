"""Regression tests for the canonical pairing chain (P0 #2, P0 #3)
and Telegram user onboarding (P1 #8).

Pins:

* the wizard's issue endpoint and the bot's /shmax_connect
  handler share ONE code store (PairingCodesRepository);
* a successful group pairing persists the CANONICAL
  TelegramTarget with validated facts — not just a route;
* group validation against the REAL adapter facts: a group with
  topics off or a bot without manage_topics is rejected with the
  exact missing rights and the code is NOT consumed;
* /start and /setname respond instead of silently consuming.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shmax.database.models import Base


@pytest_asyncio.fixture
async def db(tmp_path):
    from shmax.database import Database, DatabaseSettings as _DbSettings
    from shmax.database.migrations import run_migrations_sync

    settings = _DbSettings(data_dir=tmp_path)
    run_migrations_sync(settings.url)
    database = Database(settings)
    yield database
    await database.dispose()


def _make_handler(db, *, adapter=None, limiter=None):
    from shmax.api.app import _build_telegram_command_handler
    from shmax.pairing.repository import PairingCodesRepository
    from shmax.users.repository import TelegramUsersRepository

    return _build_telegram_command_handler(
        users=TelegramUsersRepository(db),
        pairing_codes=PairingCodesRepository(db.session),
        secret_store=None,
        db=db,
        adapter_getter=(lambda: adapter) if adapter is not None else None,
        limiter=limiter,
    )


def _payload(code: str, *, chat_kind="supergroup", chat_id="-100999", thread_id=None):
    async def _respond(text):
        _payload.responses.append(text)

    _payload.responses = []
    p = {
        "user_id": "u1",
        "display_name": "Alice",
        "username": "alice",
        "chat_kind": chat_kind,
        "chat_id": chat_id,
        "chat_title": "Work Group",
        "thread_id": thread_id,
        "text": f"/shmax_connect {code}",
        "respond": _respond,
    }
    return p


# ----------------------------------------------------------------------
# P0 #2 — one code store, canonical target persistence
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_wizard_issue_and_bot_consume_share_one_store(db):
    """The wizard endpoint and the /shmax_connect handler must
    use the SAME code store. Issues via the repository the bot
    consumes from; the handler accepts it and persists the
    canonical target."""
    from shmax.adapters.telegram.fake import FakeTelegramAdapter

    from shmax.pairing.repository import PairingCodesRepository

    repo = PairingCodesRepository(db.session)
    code = await repo.issue()  # canonical V1: no MAX chat binding

    handler = _make_handler(db, adapter=FakeTelegramAdapter())
    await handler("/shmax_connect", _payload(code.code))

    from sqlalchemy import select

    from shmax.database.models import TelegramTarget
    async with db.session() as s:
        targets = (await s.execute(select(TelegramTarget))).scalars().all()
    assert len(targets) == 1, "canonical TelegramTarget must be persisted"
    t = targets[0]
    assert t.chat_id == "-100999"
    assert t.is_forum is True
    assert t.chat_type == "supergroup"
    assert t.validated_at is not None
    # A fully validated target has no missing permissions.
    import json as _json

    assert _json.loads(t.missing_permissions or "[]") == []


@pytest.mark.asyncio
async def test_pairing_success_persists_validated_target(db):
    """With a live adapter the target gets the validated facts."""
    from shmax.adapters.telegram.fake import FakeTelegramAdapter

    from shmax.database.models import MaxAccount
    from shmax.pairing.repository import PairingCodesRepository

    async with db.session() as s:
        s.add(MaxAccount(label="primary", status="connected"))
        await s.commit()

    adapter = FakeTelegramAdapter()
    repo = PairingCodesRepository(db.session)
    code = await repo.issue(max_chat_id="max-work", max_chat_title="Work")

    handler = _make_handler(db, adapter=adapter)
    await handler("/shmax_connect", _payload(code.code, thread_id=55))

    from sqlalchemy import select

    from shmax.database.models import Route, TelegramTarget
    async with db.session() as s:
        t = (await s.execute(select(TelegramTarget))).scalars().first()
        routes = (await s.execute(select(Route))).scalars().all()
    assert t is not None
    assert t.validated_at is not None
    assert t.bot_status == "administrator"
    assert (t.missing_permissions or "[]") in ("None", "[]", None)
    assert t.title == "Work Group"
    # MAX-chat-bound code => route persisted with a topic.
    assert any(r.max_chat_id == "max-work" for r in routes)


@pytest.mark.asyncio
async def test_pairing_responds_in_telegram(db):
    """A successful pairing must produce a useful Telegram reply."""
    from shmax.adapters.telegram.fake import FakeTelegramAdapter

    from shmax.pairing.repository import PairingCodesRepository

    adapter = FakeTelegramAdapter()
    repo = PairingCodesRepository(db.session)
    code = await repo.issue()
    handler = _make_handler(db, adapter=adapter)
    await handler("/shmax_connect", _payload(code.code))
    assert _payload.responses, "handler must respond in Telegram"
    assert "SHMAX" in _payload.responses[-1]


# ----------------------------------------------------------------------
# P0 #3 — forum / admin rights must be REAL
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pairing_rejected_without_topics_and_code_not_consumed(db):
    """A group without forum topics must be rejected with the
    exact missing right; the code stays valid for a retry."""
    from shmax.adapters.telegram.fake import FakeTelegramAdapter

    from shmax.pairing.repository import PairingCodesRepository

    adapter = FakeTelegramAdapter()
    adapter.chat_summary = {
        "id": "-100999",
        "type": "supergroup",
        "title": "Work Group",
        "is_forum": False,
    }
    adapter.bot_permissions = {
        "status": "member",
        "can_send_messages": True,
        "can_manage_topics": False,
        "is_member": True,
    }
    repo = PairingCodesRepository(db.session)
    code = await repo.issue()
    handler = _make_handler(db, adapter=adapter)

    await handler("/shmax_connect", _payload(code.code))

    replies = "\n".join(_payload.responses)
    assert "Темы" in replies, f"missing-rights reply got: {replies}"
    # The code was NOT consumed — a corrected retry succeeds.
    from sqlalchemy import select

    from shmax.database.models import TelegramTarget
    async with db.session() as s:
        targets = (await s.execute(select(TelegramTarget))).scalars().all()
    assert not targets, "unvalidated group must not be marked paired"
    # Retry with correct facts works.
    adapter.chat_summary["is_forum"] = True
    adapter.bot_permissions["can_manage_topics"] = True
    adapter.bot_permissions["status"] = "administrator"
    await handler("/shmax_connect", _payload(code.code, thread_id=3))
    async with db.session() as s:
        targets = (await s.execute(select(TelegramTarget))).scalars().all()
    assert len(targets) == 1


@pytest.mark.asyncio
async def test_pairing_rejected_when_bot_lacks_manage_topics(db):
    from shmax.adapters.telegram.fake import FakeTelegramAdapter

    from shmax.pairing.repository import PairingCodesRepository

    adapter = FakeTelegramAdapter()
    adapter.bot_permissions = {
        "status": "member",
        "can_send_messages": True,
        "can_manage_topics": False,
        "is_member": True,
    }
    repo = PairingCodesRepository(db.session)
    code = await repo.issue()
    handler = _make_handler(db, adapter=adapter)
    await handler("/shmax_connect", _payload(code.code))
    replies = "\n".join(_payload.responses)
    assert "управлять темами" in replies


@pytest.mark.asyncio
async def test_pairing_rejected_for_basic_group_and_channel(db):
    """A plain (non-super) group or a channel is not a valid
    SHMAX target."""
    from shmax.adapters.telegram.fake import FakeTelegramAdapter

    from shmax.pairing.repository import PairingCodesRepository

    repo = PairingCodesRepository(db.session)
    code = await repo.issue()
    for kind, summary_type in (("group", "group"), ("channel", "channel")):
        adapter = FakeTelegramAdapter()
        adapter.chat_summary = {
            "id": "-100999",
            "type": summary_type,
            "title": "X",
            "is_forum": summary_type == "supergroup",
        }
        handler = _make_handler(db, adapter=adapter)
        await handler("/shmax_connect", _payload(code.code, chat_kind=kind))
        assert _payload.responses, f"no reply for {kind}"
        joined = "\n".join(_payload.responses)
        if kind == "channel":
            # The chat-kind gate rejects before validation runs.
            assert "нужно отправить в группе" in joined
        else:
            # Validation catches the plain-group type.
            assert "супергрупп" in joined
    # The code was never consumed — no target was persisted.
    from sqlalchemy import select

    from shmax.database.models import TelegramTarget
    async with db.session() as s:
        targets = (await s.execute(select(TelegramTarget))).scalars().all()
    assert not targets


# ----------------------------------------------------------------------
# P1 #3 — brute-force resistance
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pairing_invalid_attempts_rate_limited(db):
    """Repeated wrong codes from one chat get rate-limited."""
    from shmax.auth.rate_limit import RateLimitConfig, RateLimiter

    limiter = RateLimiter(
        RateLimitConfig(max_attempts=3, window_seconds=60, lockout_seconds=120)
    )
    handler = _make_handler(db, limiter=limiter)
    for i in range(3):
        await handler(
            "/shmax_connect", _payload(f"bogus-{i}")
        )
    # Fourth attempt is locked out — even with a VALID code.
    from shmax.pairing.repository import PairingCodesRepository

    repo = PairingCodesRepository(db.session)
    code = await repo.issue()
    _payload.responses = []
    await handler("/shmax_connect", _payload(code.code))
    assert any(
        "Слишком много" in r for r in _payload.responses
    ), f"expected lockout reply, got {_payload.responses}"


# ----------------------------------------------------------------------
# P1 #8 — onboarding UX
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_start_replies_pending_status(db):
    handler = _make_handler(db)

    async def _respond(text):
        _start_responses.append(text)

    _start_responses = []
    await handler("/start", {
        "user_id": "u2",
        "display_name": "Bob",
        "username": None,
        "chat_kind": "private",
        "chat_id": "dm1",
        "text": "/start",
        "respond": _respond,
    })
    assert any("зарегистрированы" in r for r in _start_responses)

    # After approval the greeting changes.
    from shmax.users.repository import TelegramUsersRepository

    await TelegramUsersRepository(db).set_approval(
        user_id="u2", approved=True, role="operator"
    )
    _start_responses.clear()
    await handler("/start", {
        "user_id": "u2",
        "display_name": "Bob",
        "username": None,
        "chat_kind": "private",
        "chat_id": "dm1",
        "text": "/start",
        "respond": _respond,
    })
    assert any("подтверждён" in r for r in _start_responses)


@pytest.mark.asyncio
async def test_setname_replies_success_and_usage(db):
    handler = _make_handler(db)

    async def _respond(text):
        _responses.append(text)

    _responses = []
    base = {
        "user_id": "u3",
        "display_name": "Cara",
        "username": None,
        "chat_kind": "private",
        "chat_id": "dm2",
        "respond": _respond,
    }
    await handler("/setname", {**base, "text": "/setname"})
    assert any("Использование" in r for r in _responses)
    await handler("/setname", {**base, "text": "/setname Александра"})
    assert any("сохранён" in r for r in _responses)
    from sqlalchemy import select

    from shmax.database.models import TelegramUser
    async with db.session() as s:
        row = (
            await s.execute(
                select(TelegramUser).where(TelegramUser.user_id == "u3")
            )
        ).scalars().first()
    assert row.nickname == "Александра"
