"""BLOCKER 8 — Real adapter event normalization in production.

The runtime normalizer must preserve:

* external_message_id
* chat (full ChatRef including title + kind)
* sender (full SenderRef)
* thread_id (via ReplyRef and/or metadata)
* attachments (full tuple)
* timestamp
* raw metadata only when safe

There must be exactly one normalizer; ``normalize_inbound`` is
the shared entry point used by both the runtime listener
loops and the bridge handlers.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from shmax.adapters.max.base import MaxEvent
from shmax.adapters.normalize import (
    normalize_inbound,
    normalize_max,
    normalize_telegram,
)
from shmax.adapters.telegram.base import TelegramEvent
from shmax.domain import ChatKind, ChatRef, NormalizedMessage


def _dt_attr(name, value):
    return property(lambda self: getattr(self, name, value))


class _MaxEventFull:
    """Stand-in with rich fields."""

    chat_id = "max-dialog-1"
    sender_id = "m1"
    event_id = "evt-1"
    text = "hello"
    timestamp = 1_700_000_000
    reply_to = "ref"
    attachments = ("a1", "a2")
    metadata = {"k": "v"}


def test_max_normalizer_preserves_rich_fields():
    e = _MaxEventFull()
    n = normalize_max(e)
    assert isinstance(n, NormalizedMessage)
    assert n.external_id == "evt-1"
    assert n.chat.external_id == "max-dialog-1"
    assert n.chat.kind == ChatKind.DIALOG
    assert n.sender.external_id == "m1"
    assert n.text == "hello"
    assert n.timestamp == 1_700_000_000
    assert n.reply_to == "ref"
    assert n.attachments == ("a1", "a2")
    assert n.metadata == {"k": "v"}


def test_telegram_normalizer_preserves_thread_id():
    e = TelegramEvent(
        update_id="upd-1",
        chat_id="tg-chat-1",
        thread_id="topic-42",
        sender_id="u1",
        text="hi",
    )
    n = normalize_telegram(e)
    assert isinstance(n, NormalizedMessage)
    assert n.external_id == "upd-1"
    assert n.chat.external_id == "tg-chat-1"
    assert n.metadata.get("thread_id") == "topic-42"


def test_normalize_inbound_dispatches_on_source():
    e = TelegramEvent(
        update_id="upd-2",
        chat_id="c2",
        thread_id="topic-1",
        sender_id="u1",
        text="hi",
    )
    n = normalize_inbound("telegram", e)
    assert n.chat.external_id == "c2"
    n2 = normalize_inbound("max", MaxEvent(chat_id="c1", sender_id="m", event_id="e", text="t"))
    assert n2.chat.external_id == "c1"


def test_normalize_inbound_passes_through_normalized_message():
    nm = NormalizedMessage(
        source="max",
        chat=ChatRef(transport="max", external_id="x"),
        sender=None,
        external_id="y",
        text="z",
    )
    assert normalize_inbound("telegram", nm) is nm


def test_normalize_telegram_uses_chat_kind_from_real_class():
    """A Channel instance with megagroup=True becomes GROUP."""
    class Channel:
        megagroup = True
        title = "My Supergroup"
    class RawEvent:
        chat = Channel()
    e = TelegramEvent(
        update_id="u",
        chat_id="c",
        thread_id=None,
        sender_id="s",
        text="t",
        raw=RawEvent(),
    )
    n = normalize_telegram(e)
    assert n.chat.kind == ChatKind.GROUP
    assert n.chat.title == "My Supergroup"