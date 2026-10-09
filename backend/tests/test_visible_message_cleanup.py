"""Live acceptance #16 — visible message cleanup.

The internal ``shmax:<id>`` marker travels in queue/mapping
metadata ONLY: the visible Telegram/MAX text must contain ZERO
``shmax:`` occurrences while the author attribution
``[NAME]:`` stays exactly once, and reply mapping keeps working.
"""

from __future__ import annotations

import asyncio
import datetime as _dt

import pytest
from sqlalchemy import select

from shmax.adapters.max.fake import FakeMaxAdapter
from shmax.adapters.normalize import normalize_inbound
from shmax.auth.pairing import AclService
from shmax.bridge.engine import BridgeConfig, BridgeEngine
from shmax.database.models import MaxAccount, TelegramAccount, TelegramUser
from shmax.domain import (ChatRef, ChatKind, NormalizedMessage, SenderRef)
from shmax.queue.repository import DeliveryQueueRepository
from shmax.routing.repository import RoutesRepository
from shmax.users.repository import TelegramUsersRepository


class CaptureQueue:
    def __init__(self):
        self.payloads = []

    async def enqueue(self, **kwargs):
        self.payloads.append(kwargs["payload"])


async def _bridge(db, db_session) -> tuple[BridgeEngine, CaptureQueue]:
    db_session.add(MaxAccount(label="primary", status="connected"))
    db_session.add(TelegramAccount(label="primary", status="connected"))
    db_session.add(
        TelegramUser(user_id="111222333", display_name="PITERLAND_MAX",
                     role="operator", approved=True, blocked=False)
    )
    await db_session.commit()
    max_id = (await db_session.execute(select(MaxAccount))).scalars().first().id
    tg_id = (await db_session.execute(select(TelegramAccount))).scalars().first().id
    queue = CaptureQueue()
    bridge = BridgeEngine(
        config=BridgeConfig(
            max_account_id=max_id, telegram_account_id=tg_id,
            telegram_target_chat_id="tg-chat-1",
        ),
        routes=RoutesRepository(db_session),
        mapping=__import__(
            "shmax.bridge.message_map", fromlist=["MessageMapRepository"]
        ).MessageMapRepository(db_session),
        queue=queue,
        max_adapter=FakeMaxAdapter(),
        telegram_adapter=None,
        acl=AclService(db_session),
        users=TelegramUsersRepository(db),
        default_permission="view_chat",
    )
    return bridge, queue


def _max_event(text: str, msg_id: str = "m-1") -> NormalizedMessage:
    return NormalizedMessage(
        source="max",
        chat=ChatRef(transport="max", external_id="max-dialog-1", kind=ChatKind.DIALOG),
        sender=SenderRef(
            transport="max", external_id="111222333",
            display_name="PITERLAND_MAX",
        ),
        external_id=msg_id,
        text=text,
        timestamp=int(_dt.datetime.now().timestamp()),
    )


@pytest.mark.asyncio
async def test_max_to_tg_text_no_visible_marker_author_once(db, db_session):
    bridge, queue = await _bridge(db, db_session)
    await bridge._routes.upsert(
        max_account_id=bridge._config.max_account_id,
        max_chat_id="max-dialog-1",
        telegram_account_id=bridge._config.telegram_account_id,
        telegram_chat_id="tg-chat-1",
    )
    event = _max_event("Привет")
    assert await bridge.handle_max_event(event) is True
    payload = queue.payloads[0]
    visible = payload["text"]
    # ZERO internal markers in the visible text.
    assert "shmax:" not in visible
    assert "\u200B" not in visible
    # Author attribution exactly once.
    assert visible.count("[PITERLAND_MAX]:") == 1
    assert visible.endswith("Привет")
    # The marker STILL exists internally (mapping/idempotency).
    assert payload.get("marker"), "marker must exist in queue metadata"
    assert "shmax:" not in payload["marker"]


@pytest.mark.asyncio
async def test_max_to_tg_media_caption_no_marker_author_once(db, db_session):
    bridge, queue = await _bridge(db, db_session)
    await bridge._routes.upsert(
        max_account_id=bridge._config.max_account_id,
        max_chat_id="max-dialog-1",
        telegram_account_id=bridge._config.telegram_account_id,
        telegram_chat_id="tg-chat-1",
    )
    from shmax.domain import Attachment
    from shmax.domain.enums import AttachmentKind

    event = NormalizedMessage(
        source="max",
        chat=ChatRef(transport="max", external_id="max-dialog-1", kind=ChatKind.DIALOG),
        sender=SenderRef(
            transport="max", external_id="111222333",
            display_name="PITERLAND_MAX",
        ),
        external_id="m-media-1",
        text="",
        timestamp=int(_dt.datetime.now().timestamp()),
        attachments=(
            Attachment(kind=AttachmentKind.PHOTO, payload={"ref": "x"},
                       caption="Кот на закате"),
        ),
    )

    async def _fake_prepare(ev, source):
        return {"media_ref": "file-1", "media_kind": "photo",
                "caption": ev.attachments[0].caption}, None

    bridge._prepare_media = _fake_prepare  # type: ignore[method-assign]
    assert await bridge.handle_max_event(event) is True
    payload = queue.payloads[0]
    visible_sources = [str(payload.get("text") or ""), str(payload.get("caption") or "")]
    assert all("shmax:" not in s for s in visible_sources)
    # The attributed caption carries the author exactly once.
    assert sum(s.count("[PITERLAND_MAX]:") for s in visible_sources) == 1
    assert payload.get("marker")


@pytest.mark.asyncio
async def test_tg_to_max_text_no_marker_mapping_works(db, db_session):
    bridge, queue = await _bridge(db, db_session)
    db_session.add(
        TelegramUser(user_id="tg-9", display_name="Bob", role="operator",
                     approved=True, blocked=False)
    )
    await bridge._routes.upsert(
        max_account_id=bridge._config.max_account_id,
        max_chat_id="max-dialog-1",
        telegram_account_id=bridge._config.telegram_account_id,
        telegram_chat_id="tg-chat-1",
    )
    await db_session.commit()
    event = NormalizedMessage(
        source="telegram",
        chat=ChatRef(transport="telegram", external_id="tg-chat-1", kind=ChatKind.GROUP),
        sender=SenderRef(transport="telegram", external_id="tg-9", display_name="Bob"),
        external_id="tg-1",
        text="Привет из Telegram",
        timestamp=int(_dt.datetime.now().timestamp()),
    )
    assert await bridge.handle_telegram_event(event) is True
    payload = queue.payloads[0]
    visible = payload["text"]
    assert "shmax:" not in visible and "\u200B" not in visible
    assert visible.count("[Bob]:") == 1
    # Mapping prerequisites: the marker is present in metadata and
    # the source ids travel for create_for_tg_to_max.
    assert payload.get("marker")
    assert payload["source_telegram_message_id"] == "tg-1"
    # Reply mapping path resolves via the marker even without the
    # visible prefix.
    from shmax.bridge.message_map import MessageMapRepository

    repo = MessageMapRepository(db_session)
    await repo.create_for_max_to_tg(
        max_account_id=bridge._config.max_account_id,
        max_chat_id="max-dialog-1",
        max_message_id="m-src-1",
        telegram_account_id=bridge._config.telegram_account_id,
        telegram_chat_id="tg-chat-1",
        telegram_message_id="tg-mapped-1",
        telegram_thread_id=None,
        outbound_marker_value=payload["marker"],
    )
    found = await bridge._mapping.find_by_marker(payload["marker"])
    assert found is not None and found.max_message_id == "m-src-1"


def test_fallback_humanized():
    from shmax.bridge.engine import BridgeEngine

    assert BridgeEngine._fallback_text(["other"]) == "[Неподдерживаемое вложение]"
    assert BridgeEngine._fallback_text(["UNKNOWN_X"]) == "[Неподдерживаемое вложение]"
    assert BridgeEngine._fallback_text(["sticker"]) == "[Неподдерживаемое вложение: стикер]"
    assert BridgeEngine._fallback_text(["poll"]) == "[Неподдерживаемое вложение: опрос]"
    # Combined: known + generic — generic adds nothing.
    assert BridgeEngine._fallback_text(["sticker", "other"]) == (
        "[Неподдерживаемое вложение: стикер]"
    )


@pytest.mark.asyncio
async def test_max_to_tg_fallback_other_not_exposed(db, db_session):
    bridge, queue = await _bridge(db, db_session)
    await bridge._routes.upsert(
        max_account_id=bridge._config.max_account_id,
        max_chat_id="max-dialog-1",
        telegram_account_id=bridge._config.telegram_account_id,
        telegram_chat_id="tg-chat-1",
    )

    async def _fake_prepare(ev, source):
        return None, "[Неподдерживаемое вложение]"

    bridge._prepare_media = _fake_prepare  # type: ignore[method-assign]
    event = _max_event("Смотри", msg_id="m-fb-1")
    assert await bridge.handle_max_event(event) is True
    payload = queue.payloads[0]
    assert "Смотри" in payload["text"]
    assert payload["text"].endswith("[Неподдерживаемое вложение]")
    assert "shmax:" not in payload["text"]
    assert payload["text"].count("[PITERLAND_MAX]:") == 1
