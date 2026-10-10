"""P0 #3 — normalization against REAL PyMax 2.4.1 objects.

The review verified the actual SDK layout:
  * reply  — ``Message.link`` (ReplyLink / ForwardLink discriminated
    union, ``ReplyLink.message`` = nested replied-to Message)
  * attachments — ``Message.attaches`` (PhotoAttachment /
    VideoAttachment / FileAttachment / AudioAttachment /
    StickerAttachment / ContactAttachment / ShareAttachment / ...)

These tests build REAL pymax model instances (imported from the
installed maxapi-python==2.4.1) — not invented duck types — and pin
the production normalizer output.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time

import pytest

pymax = pytest.importorskip("pymax", reason="maxapi-python not installed")

from pymax.types.domain.attachments.audio import AudioAttachment
from pymax.types.domain.attachments.contact import ContactAttachment
from pymax.types.domain.attachments.file import FileAttachment
from pymax.types.domain.attachments.photo import PhotoAttachment
from pymax.types.domain.attachments.poll import (
    PollAnswer,
    PollAttachment,
    PollFlags,
    PollState,
)
from pymax.types.domain.attachments.sticker import StickerAttachment
from pymax.types.domain.attachments.video import VideoAttachment
from pymax.types.domain.message import Message as PyMaxMessage
from pymax.types.domain.message import ReplyLink

from shmax.adapters.max.fake import FakeMaxAdapter
from shmax.adapters.normalize import normalize_inbound
from shmax.adapters.telegram.fake import FakeTelegramAdapter
from shmax.auth.pairing import AclService
from shmax.bridge.engine import BridgeConfig, BridgeEngine
from shmax.domain.enums import AttachmentKind, ChatKind
from shmax.domain.models import ChatRef, NormalizedMessage, SenderRef


def _pymax_message(**overrides) -> PyMaxMessage:
    base = dict(
        id=100500,
        chat_id=987654321,
        sender=111222333,
        text="hello from MAX",
        time=int(time.time()),
        type="USER",
        cid=None,
        attaches=[],
    )
    base.update(overrides)
    return PyMaxMessage(**base)


class _MaxEventShim:
    """The production listener feeds raw PyMax Messages through
    the shared normalizer (the PyMax adapter wraps them)."""

    def __init__(self, raw: PyMaxMessage):
        self.raw = raw
        self.chat_id = str(raw.chat_id)
        self.sender_id = str(raw.sender)
        self.event_id = str(raw.id)
        self.text = raw.text
        self.timestamp = raw.time
        self.reply_to = None
        self.attachments = ()
        self.metadata = {}


def test_pymax_reply_link_maps_to_reply_ref():
    replied = _pymax_message(id=999)
    msg = _pymax_message(
        id=1000,
        link=ReplyLink(type="REPLY", message=replied, chat_id=987654321),
    )
    n = normalize_inbound("max", _MaxEventShim(msg))
    assert n.reply_to is not None
    assert n.reply_to.transport == "max"
    assert n.reply_to.message_external_id == "999"
    assert n.reply_to.chat_external_id == "987654321"


def test_pymax_photo_attachment_maps_with_url():
    msg = _pymax_message(
        attaches=[
            PhotoAttachment(
                base_url="https://example.com/photo.jpg",
                width=800,
                height=600,
                photo_id=42,
                photo_token="tok",
                type="PHOTO",
            )
        ]
    )
    n = normalize_inbound("max", _MaxEventShim(msg))
    assert len(n.attachments) == 1
    a = n.attachments[0]
    assert a.kind == AttachmentKind.PHOTO
    assert a.payload["url"] == "https://example.com/photo.jpg"
    assert a.payload["photo_id"] == 42
    # coordinates for id-based fetch paths
    assert a.payload["chat_id"] == "987654321"
    assert a.payload["message_id"] == "100500"


def test_pymax_file_attachment_maps_document_with_coordinates():
    msg = _pymax_message(
        attaches=[
            FileAttachment(file_id=77, name="report.pdf", size=1234, token="t", type="FILE")
        ]
    )
    n = normalize_inbound("max", _MaxEventShim(msg))
    a = n.attachments[0]
    assert a.kind == AttachmentKind.DOCUMENT
    assert a.payload["file_id"] == 77
    assert a.payload["file_name"] == "report.pdf"
    assert a.payload["chat_id"] == "987654321"
    assert a.payload["message_id"] == "100500"


def test_pymax_video_attachment_maps_video():
    msg = _pymax_message(
        attaches=[
            VideoAttachment(
                height=1080, width=1920, video_id=55, type="VIDEO",
                thumbnail="t", token="tok", video_type=0,
            )
        ]
    )
    n = normalize_inbound("max", _MaxEventShim(msg))
    a = n.attachments[0]
    assert a.kind == AttachmentKind.VIDEO
    assert a.payload["video_id"] == 55


def test_pymax_audio_attachment_maps_voice():
    msg = _pymax_message(
        attaches=[
            AudioAttachment(duration=3, audio_id=9, type="AUDIO", url="https://example.com/a.ogg", token="t")
        ]
    )
    n = normalize_inbound("max", _MaxEventShim(msg))
    a = n.attachments[0]
    assert a.kind == AttachmentKind.AUDIO
    assert a.payload["url"] == "https://example.com/a.ogg"


def test_pymax_sticker_and_contact_are_unsupported_kinds():
    msg = _pymax_message(
        attaches=[
            StickerAttachment(
                url="https://example.com/s.json", sticker_id=3, type="STICKER",
                width=512, height=512, sticker_type="json", audio=False, time=0,
            ),
            ContactAttachment(first_name="Ivan", type="CONTACT"),
        ]
    )
    n = normalize_inbound("max", _MaxEventShim(msg))
    kinds = {a.kind for a in n.attachments}
    assert kinds == {AttachmentKind.STICKER, AttachmentKind.CONTACT}


def test_pymax_poll_is_other_and_reaches_fallback():
    msg = _pymax_message(
        attaches=[
            PollAttachment(
                title="Vote",
                answers=[PollAnswer(text="yes")],
                type="POLL",
                poll_id=1,
                version=1,
                settings=PollFlags(0),
                state=PollState(total=0, voter_preview_ids=[]),
            )
        ]
    )
    n = normalize_inbound("max", _MaxEventShim(msg))
    assert n.attachments[0].kind == AttachmentKind.OTHER

    # Through the REAL engine path the unsupported kind becomes the
    # controlled fallback text and never a media send.
    class CaptureQueue:
        def __init__(self):
            self.payloads = []

        async def enqueue(self, **kwargs):
            self.payloads.append(kwargs["payload"])

    class _RoutesStub:
        async def find_by_max(self, **kwargs):
            from shmax.routing.repository import RouteRecord
            return RouteRecord(
                id=1, max_account_id=1, max_chat_id=kwargs["max_chat_id"],
                telegram_account_id=1, telegram_chat_id="-100",
                telegram_thread_id=None, enabled=True, label=None,
                created_at=dt.datetime.now(), updated_at=dt.datetime.now(),
            )

    class _MappingStub:
        async def find_by_max(self, **kwargs):
            return None

    class _UsersStub:
        pass

    bridge = BridgeEngine(
        config=BridgeConfig(max_account_id=1, telegram_account_id=1),
        routes=_RoutesStub(),
        mapping=_MappingStub(),
        queue=CaptureQueue(),
        max_adapter=FakeMaxAdapter(),
        telegram_adapter=FakeTelegramAdapter(),
        acl=AclService(None),
        users=_UsersStub(),
    )
    event = NormalizedMessage(
        source="max",
        chat=ChatRef(transport="max", external_id="987654321", kind=ChatKind.DIALOG),
        sender=SenderRef(transport="max", external_id="111222333"),
        external_id=str(msg.id),
        text=msg.text,
        attachments=normalize_inbound("max", _MaxEventShim(msg)).attachments,
    )
    accepted = asyncio.run(bridge.handle_max_event(event))
    assert accepted is True
    payload = bridge._queue.payloads[0]
    assert "hello from MAX" in payload["text"]
    assert payload["text"].endswith("[Неподдерживаемое вложение]")  # humanized: internal "other" is never shown
    assert "other" not in payload["text"]
    assert not payload.get("media_ref")
