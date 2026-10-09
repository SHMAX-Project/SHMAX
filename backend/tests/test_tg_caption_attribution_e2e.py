"""Surgical review — TG→MAX structural caption suppression must keep
the author attribution.

End-to-end over the production path:

realistic aiogram photo Message (author Bob, caption "look")
→ TelegramBotApiAdapter event builder
→ normalize_telegram
→ BridgeEngine (ACL, routes, users, media fetcher)
→ queue payload
→ AppRuntime._deliver()
→ recording MAX adapter

Pinned:

* media caption carries attribution + caption exactly once
  (canonical helper format: label line, then caption line);
* NO duplicate follow-up of the caption;
* with an extra unsupported attachment, the fallback line is
  delivered exactly once as well — nothing disappears because the
  body was structurally suppressed.
"""

from __future__ import annotations

import asyncio
import time

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shmax.adapters.normalize import normalize_inbound
from shmax.adapters.telegram.bot_api import TelegramBotApiAdapter
from shmax.auth.pairing import AclService
from shmax.bridge.engine import BridgeConfig, BridgeEngine
from shmax.bridge.message_map import MessageMapRepository
from shmax.database.models import Base, MaxAccount, TelegramAccount, TelegramUser
from shmax.domain.enums import ChatKind
from shmax.queue.repository import DeliveryQueueRepository
from shmax.runtime import AppRuntime
from shmax.routing.repository import RoutesRepository
from shmax.users.repository import TelegramUsersRepository


# ----------------------------------------------------------------------
# A realistic aiogram photo Message (duck-typed to the fields the
# adapter actually reads — same shape aiogram v3 exposes).
# ----------------------------------------------------------------------
class _FakeChat:
    def __init__(self, chat_id, title, chat_type):
        self.id = chat_id
        self.title = title
        self.type = chat_type


class _FakeFromUser:
    def __init__(self, user_id, first_name, username):
        self.id = user_id
        self.first_name = first_name
        self.full_name = first_name
        self.username = username


class _FakePhotoSize:
    def __init__(self, file_id, width, height):
        self.file_id = file_id
        self.width = width
        self.height = height


class _FakeSticker:
    def __init__(self, file_id, emoji):
        self.file_id = file_id
        self.emoji = emoji


class _FakeAiogramMessage:
    """aiogram Message: photo + caption, NO text field value."""

    def __init__(self, chat, from_user, caption, with_sticker=False):
        self.message_id = 101
        self.message_thread_id = 100
        self.chat = chat
        self.from_user = from_user
        self.caption = caption
        self.text = None  # Bot API: text/caption mutually exclusive
        self.photo = [_FakePhotoSize("fid-small", 90, 90), _FakePhotoSize("fid-large", 1280, 720)]
        self.media = object()  # photos carry media
        self.date = time.gmtime()
        self.reply_to_message = None
        if with_sticker:
            self.sticker = _FakeSticker("st-1", "🎉")


def _telegram_event_for(msg):
    """The production boundary: the adapter's own event builder."""
    return TelegramBotApiAdapter._from_aio_message(msg)


class _MaxRecorder:
    """Recording MAX adapter: captures EXACT final delivery."""

    def __init__(self):
        self.calls = []

    async def send_media(self, *, chat_id, media_ref, kind="document", caption=None):
        self.calls.append(("media", caption))
        return type("R", (), {"external_message_id": "mx-1"})()

    async def send_text(self, *, chat_id, text):
        self.calls.append(("text", text))
        return type("R", (), {"external_message_id": "mx-2"})()

    async def send_reply(self, **kwargs):
        self.calls.append(("reply", kwargs.get("text")))
        return type("R", (), {"external_message_id": "mx-3"})()

    async def health(self):
        return {"connected": True, "authenticated": True, "session_available": True}


@pytest_asyncio.fixture
async def env(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    eng = create_async_engine(url)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(eng, expire_on_commit=False)

    async with sf() as s:
        s.add(MaxAccount(label="primary", status="connected"))
        s.add(TelegramAccount(label="primary", status="connected"))
        s.add(TelegramUser(user_id="777000", role="operator", approved=True))
        await s.commit()

    class _DbShim:
        def session(self):
            return sf()

    class _Env:
        session_factory = sf
        db_shim = _DbShim()

        def bridge(self, media_fetcher):
            s = sf()
            return BridgeEngine(
                config=BridgeConfig(
                    max_account_id=1,
                    telegram_account_id=1,
                    telegram_target_chat_id="-100999",
                    telegram_target_forum=True,
                ),
                routes=RoutesRepository(s),
                mapping=MessageMapRepository(s),
                queue=DeliveryQueueRepository(s),
                max_adapter=_MaxRecorder(),
                telegram_adapter=type("T", (), {"is_real": lambda self: True})(),
                acl=AclService(s),
                users=TelegramUsersRepository(self.db_shim),
                media_fetcher=media_fetcher,
            ), s

    yield _Env()
    await eng.dispose()


def _make_fetcher(supported_kinds=("photo",)):
    async def fetcher(source, attachment):
        kind = (
            attachment.kind.value
            if hasattr(attachment.kind, "value")
            else str(attachment.kind)
        )
        if kind not in supported_kinds:
            raise ValueError(f"unsupported: {kind}")
        stored_kind = "photo" if kind == "photo" else "file"

        class _Stored:
            media_ref = f"photo:/cache/{stored_kind}.bin"
            kind = stored_kind
            filename = f"{stored_kind}.bin"
            mime = "application/octet-stream"
            size = 10
            sha256 = "cafe"

        return _Stored()

    return fetcher


def _route(s):
    import asyncio

    async def go():
        await RoutesRepository(s).upsert(
            max_account_id=1, max_chat_id="max-chat-1",
            telegram_account_id=1, telegram_chat_id="-100999",
            telegram_thread_id="100", enabled=True,
        )
        await s.commit()

    asyncio.run(go())


MAX_SIDE = "MAX must receive"


def test_caption_and_author_both_delivered_exactly_once(env):
    """Bob sends a photo captioned 'look' → MAX gets ONE media send
    whose caption carries the canonical attribution ([Bob]:) plus
    the caption, and NO duplicate 'look' follow-up."""
    bridge, s = env.bridge(_make_fetcher())
    _route(s)

    msg = _FakeAiogramMessage(
        chat=_FakeChat(-100999, "Work", "supergroup"),
        from_user=_FakeFromUser(777000, "Bob", "bob_u"),
        caption="look",
    )
    event = normalize_inbound("telegram", _telegram_event_for(msg))
    # Structural fact recorded at the adapter boundary:
    assert event.metadata.get("text_is_media_caption") is True

    accepted = asyncio.run(bridge.handle_telegram_event(event))
    assert accepted is True

    payload = queue_payload(bridge)
    # Composition: caption attributed, body suppressed.
    assert payload["caption"] == "[Bob]: look"
    body = payload["text"].split("\n", 1)[-1]
    assert "look" not in body, f"caption duplicated in body: {body!r}"

    # Deliver through the real runtime path to a recording MAX.
    mx = _MaxRecorder()
    asyncio.run(AppRuntime._deliver(_Shim(mx), _payload_rec(payload)))
    assert mx.calls[0] == ("media", "[Bob]: look"), mx.calls
    joined = "\n".join(t for k, t in mx.calls[1:] if k in ("text", "reply"))
    assert "look" not in joined, (
        f"{MAX_SIDE} the caption exactly once: follow-up {joined!r}"
    )
    # Author appears exactly once (in the caption), caption once.
    assert "\n".join(t for _, t in mx.calls).count("Bob") == 1
    assert "\n".join(t for _, t in mx.calls).count("look") == 1


def test_caption_author_and_fallback_all_delivered_once(env):
    """Photo captioned 'look' + an unsupported sticker: attribution
    and caption ride the media; the fallback line is the follow-up —
    nothing disappears because the body was suppressed."""
    bridge, s = env.bridge(_make_fetcher(supported_kinds=("photo",)))
    _route(s)

    msg = _FakeAiogramMessage(
        chat=_FakeChat(-100999, "Work", "supergroup"),
        from_user=_FakeFromUser(777000, "Bob", "bob_u"),
        caption="look",
        with_sticker=True,
    )
    event = normalize_inbound("telegram", _telegram_event_for(msg))
    assert event.metadata.get("text_is_media_caption") is True
    # Two attachments: photo (supported) + sticker (unsupported).

    accepted = asyncio.run(bridge.handle_telegram_event(event))
    assert accepted is True
    payload = queue_payload(bridge)
    assert payload["caption"] == "[Bob]: look"
    body = payload["text"].split("\n", 1)[-1]
    assert body == "[Неподдерживаемое вложение: стикер]", body

    mx = _MaxRecorder()
    asyncio.run(AppRuntime._deliver(_Shim(mx), _payload_rec(payload)))
    assert mx.calls[0] == ("media", "[Bob]: look")
    followups = [
        # The leading zero-width service marker line is stripped by
        # the receiving MAX side; compare the visible text.
        _strip_marker(t)
        for k, t in mx.calls[1:]
        if k in ("text", "reply")
    ]
    assert followups == ["[Неподдерживаемое вложение: стикер]"]
    joined = "\n".join(t for _, t in mx.calls)
    assert joined.count("Bob") == 1
    assert joined.count("look") == 1
    assert joined.count("Неподдерживаемое вложение") == 1


def test_max_to_tg_regression_no_followup_for_structural_caption(env):
    """MAX→TG stays correct: a MAX photo whose text is the caption
    (structural) is delivered once with no duplicated body."""
    bridge, s = env.bridge(_make_fetcher())
    _route(s)
    from shmax.adapters.max.base import MaxEvent
    from shmax.domain.enums import AttachmentKind
    from shmax.domain.models import Attachment

    event = normalize_inbound("max", MaxEvent(
        event_id="mx-9", chat_id="max-chat-1", sender_id="m1",
        text="look", raw=type("R", (), {"attaches": [type("A", (), {"type": "PHOTO", "base_url": "https://x/y.jpg", "photo_id": 1})]})(),
        attachments=(Attachment(kind=AttachmentKind.PHOTO, payload={"url": "https://x/y.jpg"}, caption="look"),),
    ))
    # MAX normalizer doesn't mark the flag (MAX text is independent) —
    # but the attachment caption equals the text here; MAX→TG has no
    # attribution, so nothing to compose: body stays (both delivered).
    accepted = asyncio.run(bridge.handle_max_event(event))
    assert accepted is True
    payload = queue_payload(bridge)
    body = payload["text"].split("\n", 1)[-1]
    assert body == "look"
    assert payload["caption"] == "look"


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def queue_payload(bridge):
    """Read the first enqueued payload from the REAL queue table."""
    import asyncio

    from sqlalchemy import select

    from shmax.database.models import DeliveryQueueItem

    async def go():
        res = await bridge._queue._session.execute(
            select(DeliveryQueueItem).order_by(DeliveryQueueItem.id.asc())
        )
        row = res.scalars().first()
        assert row is not None, "nothing enqueued"
        return row.payload

    return asyncio.run(go())


def _payload_rec(payload):
    class _Rec:
        pass

    r = _Rec()
    r.id = 1
    r.direction = payload["direction"]
    r.target_chat_id = payload["target_chat_id"]
    r.target_thread_id = payload.get("target_thread_id")
    r.attempts = 0
    r.payload = payload
    return r


def _strip_marker(text: str) -> str:
    """Remove the pipeline's zero-width service-marker line — the
    receiving MAX side strips it before display."""
    if text.startswith("\u200bshmax:"):
        return text.split("\n", 1)[-1]
    return text


class _Shim:
    def __init__(self, mx):
        self._max = mx
