"""Regression tests for the bridge ACL audit findings.

P0 #6 — MAX→TG must NOT consult the Telegram principal ACL:
        a MAX sender is message attribution, not a Telegram
        authorization principal. No TelegramUser row may be
        required for a MAX sender to be bridged.
P0 #7 — TG→MAX must be fail-closed: no configured target denies
        everything; only the configured target group passes;
        forum traffic requires a known mapped topic (no silent
        chat-level fallback, no system topic); channel / DM /
        unknown chat kinds are denied; the normalizer must use
        the real aiogram chat.type instead of assuming GROUP.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shmax.adapters.max.fake import FakeMaxAdapter
from shmax.adapters.normalize import normalize_inbound
from shmax.adapters.telegram.fake import FakeTelegramAdapter
from shmax.auth.pairing import AclService
from shmax.bridge.engine import BridgeEngine, BridgeConfig
from shmax.bridge.message_map import MessageMapRepository
from shmax.database.models import Base, Route, TelegramTarget
from shmax.domain.enums import ChatKind
from shmax.domain.models import ChatRef, NormalizedMessage, SenderRef
from shmax.queue.repository import DeliveryQueueRepository
from shmax.routing.repository import RoutesRepository
from shmax.users.repository import TelegramUsersRepository


@pytest_asyncio.fixture
async def env(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    eng = create_async_engine(url)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(eng, expire_on_commit=False)

    from shmax.database.models import MaxAccount, TelegramAccount

    async with sf() as s:
        s.add(MaxAccount(label="primary", status="connected"))
        s.add(TelegramAccount(label="primary", status="connected"))
        await s.commit()

    class _Env:
        def __init__(self):
            self.session_factory = sf
            self.engine = eng

        def bridge(self, *, target=None, forum=False):
            s = sf()
            return BridgeEngine(
                config=BridgeConfig(
                    max_account_id=1,
                    telegram_account_id=1,
                    telegram_target_chat_id=target,
                    telegram_target_forum=forum,
                ),
                routes=RoutesRepository(s),
                mapping=MessageMapRepository(s),
                queue=DeliveryQueueRepository(s),
                max_adapter=FakeMaxAdapter(),
                telegram_adapter=FakeTelegramAdapter(),
                acl=AclService(s),
                users=TelegramUsersRepository(
                    _DatabaseShim(sf)
                ),
            ), s

    env = _Env()
    yield env
    await eng.dispose()


class _DatabaseShim:
    """Minimal Database-like shim exposing ``session`` for the
    TelegramUsersRepository."""

    def __init__(self, session_factory):
        self._sf = session_factory

    def session(self):
        return self._sf()


def _max_event(chat_id="max-dialog-1", sender="m1", msg_id="mx-1"):
    from shmax.adapters.max.base import MaxEvent

    return normalize_inbound(
        "max",
        MaxEvent(event_id=msg_id, chat_id=chat_id, sender_id=sender, text="hi"),
    )


def _tg_event(
    chat_id="-100999",
    sender="u1",
    msg_id="tg-1",
    kind=ChatKind.GROUP,
    thread_id=None,
):
    metadata = {"thread_id": thread_id} if thread_id is not None else {}
    return NormalizedMessage(
        source="telegram",
        chat=ChatRef(transport="telegram", external_id=chat_id, kind=kind),
        sender=SenderRef(transport="telegram", external_id=sender),
        external_id=msg_id,
        text="hi from telegram",
        metadata=metadata,
    )


# ----------------------------------------------------------------------
# P0 #6 — MAX→TG is attribution, not authorization
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_max_sender_without_telegram_row_is_bridged(env):
    """A MAX sender with NO TelegramUser row must reach the
    queue — the previous engine denied it as unknown_principal
    unless a fake TelegramUser('m1') was planted."""
    bridge, s = env.bridge()
    await RoutesRepository(s).upsert(
        max_account_id=1,
        max_chat_id="max-dialog-1",
        telegram_account_id=1,
        telegram_chat_id="-100999",
    )
    await s.commit()
    accepted = await bridge.handle_max_event(_max_event())
    assert accepted is True


# ----------------------------------------------------------------------
# P0 #7 — TG→MAX fail closed
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_tg_to_max_denied_when_no_target_configured(env):
    """'target not configured' must DENY — never accept-all."""
    bridge, s = env.bridge(target=None)
    await RoutesRepository(s).upsert(
        max_account_id=1,
        max_chat_id="max-chat",
        telegram_account_id=1,
        telegram_chat_id="-100999",
    )
    await TelegramUsersRepository(_DatabaseShim(env.session_factory)).register_or_update(
        user_id="u1", display_name="U", username=None
    )
    await TelegramUsersRepository(_DatabaseShim(env.session_factory)).set_approval(
        user_id="u1", approved=True, role="operator"
    )
    await s.commit()
    assert await bridge.handle_telegram_event(_tg_event()) is False


@pytest.mark.asyncio
async def test_tg_to_max_requires_registered_approved_user(env):
    bridge, s = env.bridge(target="-100999")
    await s.commit()
    # No TelegramUser rows at all → denied.
    assert await bridge.handle_telegram_event(_tg_event()) is False


@pytest.mark.asyncio
async def test_tg_to_max_denies_dm_and_channel_kinds(env):
    """The normalizer must feed the real chat kind — DMs and
    channels are not group traffic even when the chat id matches."""
    from shmax.database.models import TelegramUser

    bridge, s = env.bridge(target="-100999")
    await RoutesRepository(s).upsert(
        max_account_id=1,
        max_chat_id="max-chat",
        telegram_account_id=1,
        telegram_chat_id="-100999",
        telegram_thread_id=None,
    )
    repo = TelegramUsersRepository(_DatabaseShim(env.session_factory))
    await repo.register_or_update(user_id="u1", display_name="U", username=None)
    await repo.set_approval(user_id="u1", approved=True, role="operator")
    await s.commit()
    assert (
        await bridge.handle_telegram_event(
            _tg_event(kind=ChatKind.DIALOG, msg_id="dm-1")
        )
        is False
    )
    assert (
        await bridge.handle_telegram_event(
            _tg_event(kind=ChatKind.CHANNEL, msg_id="ch-1")
        )
        is False
    )


@pytest.mark.asyncio
async def test_tg_to_max_no_silent_topic_fallback_in_forum(env):
    """Inside a forum target an unmapped topic must not fall back
    to the chat-level route."""
    from shmax.database.models import TelegramUser

    bridge, s = env.bridge(target="-100999", forum=True)
    await RoutesRepository(s).upsert(
        max_account_id=1,
        max_chat_id="max-chat",
        telegram_account_id=1,
        telegram_chat_id="-100999",
        telegram_thread_id="100",  # chat-level-ish row with a topic
    )
    repo = TelegramUsersRepository(_DatabaseShim(env.session_factory))
    await repo.register_or_update(user_id="u1", display_name="U", username=None)
    await repo.set_approval(user_id="u1", approved=True, role="operator")
    await s.commit()
    # Known topic → accepted.
    assert (
        await bridge.handle_telegram_event(_tg_event(thread_id="100")) is True
    )
    # Unknown topic → denied (previously it fell back and passed).
    assert (
        await bridge.handle_telegram_event(
            _tg_event(thread_id="999", msg_id="tg-2")
        )
        is False
    )
    # No topic at all in a forum → denied.
    assert (
        await bridge.handle_telegram_event(_tg_event(msg_id="tg-3")) is False
    )


@pytest.mark.asyncio
async def test_tg_to_max_system_topic_is_not_bridge_traffic(env):
    bridge, s = env.bridge(target="-100999", forum=True)
    await RoutesRepository(s).upsert(
        max_account_id=1,
        max_chat_id="-system-",
        telegram_account_id=1,
        telegram_chat_id="-100999",
        telegram_thread_id="77",
        label="system",
    )
    repo = TelegramUsersRepository(_DatabaseShim(env.session_factory))
    await repo.register_or_update(user_id="u1", display_name="U", username=None)
    await repo.set_approval(user_id="u1", approved=True, role="owner")
    await s.commit()
    assert (
        await bridge.handle_telegram_event(_tg_event(thread_id="77")) is False
    )


# ----------------------------------------------------------------------
# P0 #7 — normalizer chat-kind honesty
# ----------------------------------------------------------------------
def test_normalizer_uses_aiogram_chat_type_not_class_name():
    """An aiogram Chat with type='private' must map to DIALOG —
    the class name 'Chat' must not imply GROUP."""

    class _FakeAiogramMessage:
        def __init__(self, chat_type):
            self.chat = type("Chat", (), {"type": chat_type, "id": 5})()
            self.text = "x"

    m = _FakeAiogramMessage("private")
    n = normalize_inbound("telegram", type("E", (), {"raw": m})())
    assert n.chat.kind == ChatKind.DIALOG

    m2 = _FakeAiogramMessage("supergroup")
    n2 = normalize_inbound("telegram", type("E", (), {"raw": m2})())
    assert n2.chat.kind == ChatKind.GROUP


def test_normalizer_unknown_chat_is_unknown_not_group():
    n = normalize_inbound("telegram", type("E", (), {"raw": None})())
    assert n.chat.kind == ChatKind.UNKNOWN
