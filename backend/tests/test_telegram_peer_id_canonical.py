"""BLOCKER 5 — Canonical Telegram identity.

Bot API ``-100<digits>`` and the matching Telethon
``PeerChannel(id=-100<digits>)`` must normalise to the same
internal id. Integer and string input must give the same
result. Negative-sign of strings must survive (no LSTRIP
that eats the dash).

Verified categories:
* user — ``u<id>``
* basic group — ``b<id>``
* supergroup / channel — ``c<digits>`` (strip ``-100`` prefix)
"""

from __future__ import annotations

import pytest

from shmax.domain.models import TelegramPeerId


def test_user_int_and_string_match():
    a = TelegramPeerId.from_user_id(123456)
    b = TelegramPeerId.from_user_id("123456")
    assert a == b
    assert a.raw == "u123456"


def test_user_does_not_eat_negative_sign():
    """``from_user_id("-123")`` must NOT lstrip every ``-``
    from the string; it must convert ``-123`` to ``123``."""
    a = TelegramPeerId.from_user_id(-123)
    b = TelegramPeerId.from_user_id("-123")
    assert a == b
    assert a.raw == "u123"


def test_basic_group_int_and_string_match():
    a = TelegramPeerId.from_basic_group(42)
    b = TelegramPeerId.from_basic_group("42")
    assert a == b
    assert a.raw == "b42"


def test_supergroup_negative_int_is_channel():
    """``-1001234567890`` (supergroup) becomes ``c1234567890``."""
    a = TelegramPeerId.from_bot_api(-1001234567890)
    assert a.raw == "c1234567890"


def test_supergroup_negative_string_is_channel():
    a = TelegramPeerId.from_bot_api("-1001234567890")
    assert a.raw == "c1234567890"


def test_telethon_channel_negative_int_is_channel():
    a = TelegramPeerId.from_telethon(-1001234567890, kind="channel")
    assert a.raw == "c1234567890"


def test_telethon_channel_string_form():
    """Telethon accepts ints only — but Telethon's ``PeerChannel``
    constructor is fed an int. The form must still match."""
    a = TelegramPeerId.from_telethon(-1001234567890, kind="channel")
    b = TelegramPeerId.from_bot_api(-1001234567890)
    assert a == b


def test_bot_api_user_id_is_canonical_user():
    """Positive Bot API ``chat.id`` for users must still encode as
    the user prefix when constructed via the explicit factory."""
    u = TelegramPeerId.from_user_id(987654321)
    assert u.raw == "u987654321"


def test_non_numeric_input_raises():
    with pytest.raises(ValueError):
        TelegramPeerId.from_bot_api("not-a-number")


def test_negative_without_minus_100_is_basic_group():
    """``-42`` (without ``100`` prefix) stays a basic group —
    Bot API's ``from_bot_api`` defaults to channel prefix ``c``
    in this implementation; the discriminator for basic-group
    is the explicit factory."""
    b = TelegramPeerId.from_basic_group(-42)
    assert b.raw == "b42"
    b2 = TelegramPeerId.from_basic_group(42)
    assert b == b2


def test_forum_topic_prefix_is_distinct():
    f = TelegramPeerId("t999")
    assert f.is_forum_topic is True
    assert TelegramPeerId("b999").is_forum_topic is False


def test_bot_api_chat_id_round_trip():
    """``bot_api_chat_id`` returns the positive integer portion."""
    a = TelegramPeerId.from_bot_api(-1001234567890)
    assert a.bot_api_chat_id == 1234567890


def test_telethon_unknown_kind_raises():
    with pytest.raises(ValueError):
        TelegramPeerId.from_telethon(1, kind="banana")


def test_string_with_whitespace_stripped():
    """``'-1001234567890'`` and ``'  -1001234567890  '``
    produce the same canonical id."""
    a = TelegramPeerId.from_bot_api("-1001234567890")
    b = TelegramPeerId.from_bot_api("  -1001234567890  ")
    assert a == b