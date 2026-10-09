"""BLOCKER 3 + BLOCKER 4 — Telethon 1.45.0 contract.

These tests assert against the actual installed SDK source
so they catch the kinds of bugs that bit the previous
iteration:

* wrong import paths (``CreateForumTopicRequest`` was under
  ``channels`` in the previous code but actually lives under
  ``messages`` in 1.45.0);
* nonexistent ``random_id`` keyword on ``client.send_message``;
* wrong proxy tuple shapes (numeric ``kind=3`` is wrong; the
  string ``'socks5'`` is the contract);
* MTProxy connection class only (no instance);
* topic id extraction uses the real ``MessageActionTopicCreate``
  and reads the service message id, NOT a non-existent
  ``action.id`` field;
* ``reply_to_top_id`` is the forum topic root, not
  ``reply_to_msg_id``.
"""

from __future__ import annotations

import pytest

from shmax.adapters.telegram.mtproto import TelegramMtProtoAdapter
from shmax.adapters.telegram.config import ProxyConfig


# ----------------------------------------------------------------------
# Telethon 1.45.0 SDK source-of-truth assertions
# ----------------------------------------------------------------------
def test_create_forum_topic_lives_in_messages_module():
    """CreateForumTopicRequest is in telethon.tl.functions.messages
    in 1.45.0, NOT in channels."""
    try:
        from telethon.tl.functions.messages import CreateForumTopicRequest  # noqa: F401
    except ImportError as e:  # pragma: no cover - import-order
        pytest.fail(f"CreateForumTopicRequest missing from messages: {e}")


def test_message_action_topic_create_has_no_id_field():
    """``MessageActionTopicCreate`` (the real TL class) has NO
    ``id`` field. The topic id is the parent service message id."""
    from telethon.tl.types import MessageActionTopicCreate
    import inspect

    sig = inspect.signature(MessageActionTopicCreate.__init__)
    params = list(sig.parameters.keys())
    assert "id" not in params, (
        f"MessageActionTopicCreate.__init__ must not accept 'id'; got {params}"
    )
    # Confirmed fields:
    assert "title" in params
    assert "icon_color" in params


def test_telegram_client_send_message_has_no_random_id():
    """``client.send_message`` does NOT accept ``random_id``."""
    from telethon import TelegramClient
    import inspect

    sig = inspect.signature(TelegramClient.send_message)
    params = list(sig.parameters.keys())
    assert "random_id" not in params, (
        f"client.send_message must not accept random_id; got {params}"
    )


def test_proxy_tuple_uses_string_scheme():
    """Telethon proxy tuple is ``('socks5', host, port)`` —
    integer kind codes changed between Telethon versions."""
    cfg = ProxyConfig(scheme="socks5", host="127.0.0.1", port=1080)
    tup = TelegramMtProtoAdapter._build_socks_proxy(cfg)
    assert tup[0] == "socks5"
    assert tup[1] == "127.0.0.1"
    assert tup[2] == 1080


def test_proxy_tuple_with_auth_5_fields():
    cfg = ProxyConfig(
        scheme="socks5", host="127.0.0.1", port=1080, username="u", password="p"
    )
    tup = TelegramMtProtoAdapter._build_socks_proxy(cfg)
    assert tup == ("socks5", "127.0.0.1", 1080, "u", "p")


def test_proxy_tuple_unsupported_scheme():
    cfg = ProxyConfig(scheme="https", host="127.0.0.1", port=443)
    with pytest.raises(ValueError):
        TelegramMtProtoAdapter._build_socks_proxy(cfg)


def test_signed_int64_random_id_in_int64_range():
    """random_id must be signed int64 for low-level TL requests."""
    for _ in range(50):
        rid = TelegramMtProtoAdapter._signed_int64_random_id()
        assert -(1 << 63) <= rid <= (1 << 63) - 1


def test_extract_topic_id_uses_real_message_action_class_name():
    """The TL class is ``MessageActionTopicCreate`` — not
    ``MessageActionForumTopicCreate``."""
    class _Action:
        __class__ = type("MessageActionTopicCreate", (), {})
        def __init__(self) -> None:
            pass

    class _Msg:
        def __init__(self) -> None:
            self.action = _Action()
            self.id = 4242

    class _Upd:
        def __init__(self) -> None:
            self.message = _Msg()

    class _Result:
        def __init__(self) -> None:
            self.updates = [_Upd()]

    assert TelegramMtProtoAdapter._extract_topic_id(_Result()) == "4242"


def test_extract_topic_id_ignores_unrelated_action():
    class _Action:
        __class__ = type("MessageActionChatCreate", (), {})

    class _Msg:
        def __init__(self) -> None:
            self.action = _Action()
            self.id = 9999

    class _Result:
        def __init__(self) -> None:
            self.updates = [_Upd(_Msg())]

    class _Upd:
        def __init__(self, msg: Any) -> None:
            self.message = msg

    # Unrelated action type — extract returns None.
    assert TelegramMtProtoAdapter._extract_topic_id(_Result()) is None


def test_extract_topic_id_returns_none_when_no_updates():
    class _Result:
        updates = []
    assert TelegramMtProtoAdapter._extract_topic_id(_Result()) is None


def test_make_mtproxy_connection_returns_tuple():
    from shmax.adapters.telegram.config import (
        MTProxyConfig,
        TransportConfig,
        TransportMode,
    )

    cfg = TransportConfig(
        mode=TransportMode.MTPROTO_PROXY,
        api_id=12345,
        api_hash="hash",
        bot_token="123:abc",
        mtproxy=MTProxyConfig(host="h", port=443, secret="abcd"),
    )
    a = TelegramMtProtoAdapter(cfg)
    tup = a._make_mtproxy_connection("h", 443, "abcd")
    assert tup == ("h", 443, "abcd")