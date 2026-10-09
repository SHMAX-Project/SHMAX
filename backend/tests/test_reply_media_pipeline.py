"""Regression tests for reply metadata (P0 #10) and media
fallback (P0 #11) in the REAL inbound pipeline.

Pins:

* the shared normalizer extracts reply_to / attachments /
  sender display name from RAW aiogram and Telethon objects —
  the wire-level event drops them;
* the bridge turns a telegram reply into a MAX native reply via
  the persistent message map (and the reverse);
* unsupported attachment kinds become a controlled
  "[Неподдерживаемое вложение: …]" text fallback in the real
  engine path — never silently dropped, never a crash;
* supported media flows through the media pipeline into the
  queue payload contract (media_ref / media_kind).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shmax.adapters.max.fake import FakeMaxAdapter
from shmax.adapters.normalize import normalize_inbound
from shmax.adapters.telegram.fake import FakeTelegramAdapter
from shmax.auth.pairing import AclService
from shmax.bridge.engine import BridgeConfig, BridgeEngine
from shmax.bridge.message_map import MessageMapRepository
from shmax.database.models import Base, MaxAccount, TelegramAccount, TelegramUser
from shmax.domain.enums import AttachmentKind, ChatKind
from shmax.domain.models import Attachment, ChatRef, NormalizedMessage, ReplyRef, SenderRef
from shmax.queue.repository import DeliveryQueueRepository
from shmax.runtime import AppRuntime
from shmax.routing.repository import RoutesRepository
from shmax.users.repository import TelegramUsersRepository


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
        s.add(TelegramUser(user_id="u1", role="operator", approved=True))
        await s.commit()

    class _DbShim:
        def session(self):
            return sf()

    class _Env:
        session_factory = sf
        db_shim = _DbShim()

        def bridge(self, media_fetcher=None):
            s = sf()
            return BridgeEngine(
                config=BridgeConfig(
                    max_account_id=1,
                    telegram_account_id=1,
                    telegram_target_chat_id="-100999",
                ),
                routes=RoutesRepository(s),
                mapping=MessageMapRepository(s),
                queue=DeliveryQueueRepository(s),
                max_adapter=FakeMaxAdapter(),
                telegram_adapter=FakeTelegramAdapter(),
                acl=AclService(s),
                users=TelegramUsersRepository(self.db_shim),
                media_fetcher=media_fetcher,
            ), s

    yield _Env()
    await eng.dispose()


# ----------------------------------------------------------------------
# P0 #10 — reply metadata from RAW objects
# ----------------------------------------------------------------------
def test_aiogram_reply_metadata_survives_normalization():
    """A raw aiogram reply must produce event.reply_to with the
    replied-to message id — the old pipeline discarded it."""

    class _FakeAiogramMessage:
        def __init__(self):
            self.chat = type("Chat", (), {"type": "supergroup", "id": -100999, "title": "W"})()
            self.message_id = 777
            self.reply_to_message = type("R", (), {"message_id": 555, "text": "original"})()
            self.from_user = type("U", (), {"id": 42, "full_name": "Alice", "username": "alice"})()
            self.text = "a reply"
            self.date = None

    ev = type("TE", (), {"raw": _FakeAiogramMessage()})()
    n = normalize_inbound("telegram", ev)
    assert n.reply_to is not None
    assert n.reply_to.transport == "telegram"
    assert n.reply_to.message_external_id == "555"
    assert n.reply_to.chat_external_id == "-100999"
    # sender display name also survives (attribution input)
    assert n.sender.display_name == "Alice"


def test_telethon_reply_metadata_survives_normalization():
    class _ReplyTo:
        reply_to_msg_id = 888

    class _Msg:
        id = 999
        reply_to = _ReplyTo()
        message = "hi"

    class _Raw:
        message = _Msg()
        chat = None

    n = normalize_inbound("telegram", type("TE", (), {"raw": _Raw()})())
    assert n.reply_to is not None
    assert n.reply_to.message_external_id == "888"


def test_max_reply_metadata_survives_normalization():
    class _RawMaxMsg:
        reply_to = 31337

    from shmax.adapters.max.base import MaxEvent

    n = normalize_inbound(
        "max",
        MaxEvent(event_id="e1", chat_id="c1", sender_id="m1", text="x", raw=_RawMaxMsg()),
    )
    assert n.reply_to is not None
    assert n.reply_to.transport == "max"
    assert n.reply_to.message_external_id == "31337"


@pytest.mark.asyncio
async def test_telegram_reply_maps_to_max_native_reply(env):
    """TG reply → mapped MAX message id in the queue payload."""
    bridge, s = env.bridge()
    await RoutesRepository(s).upsert(
        max_account_id=1, max_chat_id="max-chat",
        telegram_account_id=1, telegram_chat_id="-100999",
        telegram_thread_id="100",
    )
    await s.commit()
    mapping = MessageMapRepository(s)
    # Seed: MAX message 31337 is displayed in TG as message 555.
    await mapping.create_for_max_to_tg(
        max_account_id=1, max_chat_id="max-chat", max_message_id="31337",
        telegram_account_id=1, telegram_chat_id="-100999",
        telegram_thread_id="100", telegram_message_id="555",
        outbound_marker_value="seed",
    )
    await s.commit()

    event = NormalizedMessage(
        source="telegram",
        chat=ChatRef(transport="telegram", external_id="-100999", kind=ChatKind.GROUP),
        sender=SenderRef(transport="telegram", external_id="u1"),
        external_id="777",
        text="a reply",
        metadata={"thread_id": "100"},
        reply_to=ReplyRef(
            transport="telegram",
            chat_external_id="-100999",
            message_external_id="555",
            thread_id="100",
        ),
    )
    accepted = await bridge.handle_telegram_event(event)
    assert accepted is True
    rows = await DeliveryQueueRepository(s).counts_by_state()
    assert rows.get("pending", 0) >= 1
    # The enqueued payload must reference the MAX message id.
    from sqlalchemy import select

    from shmax.database.models import DeliveryQueueItem
    items = (await s.execute(select(DeliveryQueueItem))).scalars().all()
    payloads = [i.payload for i in items if i.payload]
    assert any(
        p.get("reply_to_max_message_id") == "31337" for p in payloads
    ), f"no native reply payload: {payloads}"


# ----------------------------------------------------------------------
# P0 #11 — media / fallback in the real engine path
# ----------------------------------------------------------------------
def _tg_with_photo(msg_id="m-photo", kind=AttachmentKind.PHOTO):
    return NormalizedMessage(
        source="telegram",
        chat=ChatRef(transport="telegram", external_id="-100999", kind=ChatKind.GROUP),
        sender=SenderRef(transport="telegram", external_id="u1"),
        external_id=msg_id,
        text="look at this",
        metadata={"thread_id": "100"},
        attachments=(Attachment(kind=kind, payload={"file_id": "fid-1"}, caption=None),),
    )


@pytest.mark.asyncio
async def test_supported_media_flows_through_pipeline(env):
    async def fetcher(source, attachment):
        class _Stored:
            media_ref = "photo:/cache/x.jpg"
            kind = "photo"
            filename = "x.jpg"
            mime = "image/jpeg"
            size = 12
            sha256 = "deadbeef"

        return _Stored()

    bridge, s = env.bridge(media_fetcher=fetcher)
    await RoutesRepository(s).upsert(
        max_account_id=1, max_chat_id="max-chat",
        telegram_account_id=1, telegram_chat_id="-100999",
        telegram_thread_id="100",
    )
    await s.commit()
    assert await bridge.handle_telegram_event(_tg_with_photo()) is True
    from sqlalchemy import select

    from shmax.database.models import DeliveryQueueItem
    item = (await s.execute(select(DeliveryQueueItem))).scalars().first()
    assert item is not None
    p = item.payload
    assert p.get("media_ref") == "photo:/cache/x.jpg"
    assert p.get("media_kind") == "photo"
    # unified attachment representation, not raw payload dicts
    assert p["attachments"][0]["kind"] == "photo"
    assert "file_id" in p["attachments"][0]["payload"]


@pytest.mark.asyncio
async def test_unsupported_media_becomes_visible_fallback(env):
    """A sticker must NOT vanish: the payload text carries the
    controlled fallback notice; no media fields are set."""
    bridge, s = env.bridge(media_fetcher=None)
    await RoutesRepository(s).upsert(
        max_account_id=1, max_chat_id="max-chat",
        telegram_account_id=1, telegram_chat_id="-100999",
        telegram_thread_id="100",
    )
    await s.commit()
    event = NormalizedMessage(
        source="telegram",
        chat=ChatRef(transport="telegram", external_id="-100999", kind=ChatKind.GROUP),
        sender=SenderRef(transport="telegram", external_id="u1"),
        external_id="m-sticker",
        text="",
        metadata={"thread_id": "100"},
        attachments=(Attachment(kind=AttachmentKind.STICKER, payload={"file_id": "s1"}),),
    )
    assert await bridge.handle_telegram_event(event) is True
    from sqlalchemy import select

    from shmax.database.models import DeliveryQueueItem
    item = (await s.execute(select(DeliveryQueueItem))).scalars().first()
    assert item is not None
    text = item.payload.get("text", "")
    assert "[Неподдерживаемое вложение: стикер]" in text
    assert not item.payload.get("media_ref")


@pytest.mark.asyncio
async def test_fetch_failure_degrades_to_fallback(env):
    """A failing downloader must not crash the bridge — the
    message still flows with a visible notice."""

    async def failing_fetcher(source, attachment):
        raise RuntimeError("download exploded")

    bridge, s = env.bridge(media_fetcher=failing_fetcher)
    await RoutesRepository(s).upsert(
        max_account_id=1, max_chat_id="max-chat",
        telegram_account_id=1, telegram_chat_id="-100999",
        telegram_thread_id="100",
    )
    await s.commit()
    assert await bridge.handle_telegram_event(_tg_with_photo(msg_id="m-fail")) is True
    from sqlalchemy import select

    from shmax.database.models import DeliveryQueueItem
    item = (await s.execute(select(DeliveryQueueItem))).scalars().first()
    assert "[Неподдерживаемое вложение: фото]" in item.payload.get("text", "")


# ----------------------------------------------------------------------
# P1 #3 — media never swallows the message text / caption
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_media_with_text_delivers_both(env):
    """text + supported attachment: the recipient gets the media
    AND the text (author attribution + message body)."""
    from shmax.media.pipeline import MediaPipeline

    class _Stored:
        media_ref = "photo:/cache/x.jpg"
        kind = "photo"
        filename = "x.jpg"
        mime = "image/jpeg"
        size = 12
        sha256 = "deadbeef"

    async def fetcher(source, attachment):
        return _Stored()

    bridge, s = env.bridge(media_fetcher=fetcher)
    await RoutesRepository(s).upsert(
        max_account_id=1, max_chat_id="max-chat",
        telegram_account_id=1, telegram_chat_id="-100999",
        telegram_thread_id="100",
    )
    await s.commit()
    tg = bridge._tg
    assert await bridge.handle_telegram_event(_tg_with_photo(msg_id="m-txt")) is True

    from shmax.runtime import AppRuntime
    # Drive the real worker delivery path with a tiny runtime shim.
    delivered = {"media": False, "text": False}

    class _TgStub:
        async def send_media(self, *, chat_id, media_ref, thread_id=None, kind="document", caption=None):
            delivered["media"] = True
            return type("R", (), {"external_message_id": "tg-1"})()

        async def send_message(self, *, chat_id, text, thread_id=None):
            delivered["text"] = True
            delivered["text_value"] = text
            return type("R", (), {"external_message_id": "tg-2"})()

        async def send_reply(self, **kwargs):
            delivered["text"] = True
            return type("R", (), {"external_message_id": "tg-3"})()

    class _MaxStub:
        async def send_media(self, *, chat_id, media_ref, kind="document", caption=None):
            delivered["media"] = True
            return type("R", (), {"external_message_id": "mx-1"})()

        async def send_text(self, *, chat_id, text):
            delivered["text"] = True
            delivered["text_value"] = text
            return type("R", (), {"external_message_id": "mx-2"})()

        async def send_reply(self, **kwargs):
            delivered["text"] = True
            return type("R", (), {"external_message_id": "mx-3"})()

    class _Rec:
        id = 1
        direction = "telegram_to_max"
        target_chat_id = "max-chat"
        target_thread_id = None
        attempts = 0
        payload = {
            "direction": "telegram_to_max",
            "text": "[Bob]: look at this",
            "media_ref": "photo:/cache/x.jpg",
            "media_kind": "photo",
            "caption": None,
            "marker": "m",
            "max_account_id": 1,
            "telegram_account_id": 1,
            "source_telegram_chat_id": "-100999",
            "source_telegram_message_id": "m-txt",
        }

    class _ShimRuntime:
        _tg = _TgStub()
        _max = _MaxStub()

    result = await AppRuntime._deliver(_ShimRuntime(), _Rec())
    assert delivered["media"] is True
    assert delivered["text"] is True
    assert "look at this" in delivered.get("text_value", "")


@pytest.mark.asyncio
async def test_media_with_caption_uses_native_caption(env):
    """caption + media: the caption rides the media send natively
    and no duplicate text message is produced."""
    class _Stored:
        media_ref = "photo:/cache/c.jpg"
        kind = "photo"
        filename = "c.jpg"
        mime = "image/jpeg"
        size = 12
        sha256 = "cafe"

    async def fetcher(source, attachment):
        return _Stored()

    bridge, s = env.bridge(media_fetcher=fetcher)
    await RoutesRepository(s).upsert(
        max_account_id=1, max_chat_id="max-chat",
        telegram_account_id=1, telegram_chat_id="-100999",
        telegram_thread_id="100",
    )
    await s.commit()
    # The MAX→TG payload carries caption separately (from the
    # attachment); here we verify the worker branch.
    delivered = {"media": 0, "text": 0, "caption": None}

    class _TgStub:
        async def send_media(self, *, chat_id, media_ref, thread_id=None, kind="document", caption=None):
            delivered["media"] += 1
            delivered["caption"] = caption
            return type("R", (), {"external_message_id": "tg-c"})()

        async def send_message(self, **kwargs):
            delivered["text"] += 1
            return type("R", (), {"external_message_id": "tg-d"})()

        async def send_reply(self, **kwargs):
            delivered["text"] += 1
            return type("R", (), {"external_message_id": "tg-e"})()

    class _Rec:
        id = 2
        direction = "max_to_telegram"
        target_chat_id = "-100999"
        target_thread_id = None
        attempts = 0
        payload = {
            "direction": "max_to_telegram",
            "text": "",
            "media_ref": "photo:/cache/c.jpg",
            "media_kind": "photo",
            "caption": "pic caption",
            "marker": "m2",
            "max_account_id": 1,
            "max_chat_id": "max-chat",
            "max_message_id": "mx-9",
            "telegram_account_id": 1,
        }

    class _MaxStub:
        async def send_media(self, *, chat_id, media_ref, kind="document", caption=None):
            delivered["media"] += 1
            delivered["caption"] = caption
            return type("R", (), {"external_message_id": "mx-c"})()

        async def send_text(self, **kwargs):
            delivered["text"] += 1
            return type("R", (), {"external_message_id": "mx-d"})()

    class _ShimRuntime:
        _tg = _TgStub()
        _max = _MaxStub()

    await AppRuntime._deliver(_ShimRuntime(), _Rec())
    assert delivered["media"] == 1
    assert delivered["text"] == 0, "caption must ride natively, no duplicate text"
    assert delivered["caption"] == "pic caption"
