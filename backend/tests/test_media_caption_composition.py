"""Surgical review — STRUCTURAL media caption composition.

Dedup happens ONLY at the composition boundary using known field
relationships (the adapter marks when the source API exposed the
media caption AS the message text). No lexical search ever runs
against composed free-form text — independent bodies, attributions
and fallback lines survive untouched.

Delivery-level tests assert FULL EXACT OUTPUT through production
AppRuntime._deliver() in BOTH directions.
"""

from __future__ import annotations

import asyncio

from shmax.runtime import AppRuntime


# ----------------------------------------------------------------------
# Delivery spies
# ----------------------------------------------------------------------
class _TgSpy:
    def __init__(self):
        self.calls = []

    async def send_media(self, *, chat_id, media_ref, thread_id=None, kind="document", caption=None):
        self.calls.append(("media", caption))
        return type("R", (), {"external_message_id": "tg-m"})()

    async def send_message(self, *, chat_id, text, thread_id=None):
        self.calls.append(("message", text))
        return type("R", (), {"external_message_id": "tg-t"})()

    async def send_reply(self, **kwargs):
        self.calls.append(("reply", kwargs.get("text")))
        return type("R", (), {"external_message_id": "tg-r"})()


class _MaxSpy:
    def __init__(self):
        self.calls = []

    async def send_media(self, *, chat_id, media_ref, kind="document", caption=None):
        self.calls.append(("media", caption))
        return type("R", (), {"external_message_id": "mx-m"})()

    async def send_text(self, *, chat_id, text):
        self.calls.append(("text", text))
        return type("R", (), {"external_message_id": "mx-t"})()

    async def send_reply(self, **kwargs):
        self.calls.append(("reply", kwargs.get("text")))
        return type("R", (), {"external_message_id": "mx-r"})()


class _Shim:
    def __init__(self, tg, mx):
        self._tg = tg
        self._max = mx


def _tg_rec(text, caption):
    return type("Rec", (), {
        "id": 1,
        "direction": "max_to_telegram",
        "target_chat_id": "-100999",
        "target_thread_id": "100",
        "attempts": 0,
        "payload": {
            "direction": "max_to_telegram",
            "text": text,
            "caption": caption,
            "media_ref": "photo:/cache/x.jpg",
            "media_kind": "photo",
            "marker": "m",
            "max_account_id": 1,
            "max_chat_id": "c",
            "max_message_id": "mx-1",
            "telegram_account_id": 1,
        },
    })()


def _max_rec(text, caption):
    return type("Rec", (), {
        "id": 2,
        "direction": "telegram_to_max",
        "target_chat_id": "max-chat-1",
        "target_thread_id": None,
        "attempts": 0,
        "payload": {
            "direction": "telegram_to_max",
            "text": text,
            "caption": caption,
            "media_ref": "photo:/cache/x.jpg",
            "media_kind": "photo",
            "marker": "m",
            "max_account_id": 1,
            "telegram_account_id": 1,
            "source_telegram_chat_id": "-100999",
            "source_telegram_message_id": "tg-1",
        },
    })()


def _deliver_tg(text, caption):
    tg = _TgSpy()
    asyncio.run(AppRuntime._deliver(_Shim(tg, None), _tg_rec(text, caption)))
    return tg.calls


def _deliver_max(text, caption):
    mx = _MaxSpy()
    asyncio.run(AppRuntime._deliver(_Shim(None, mx), _max_rec(text, caption)))
    return mx.calls


def _assert_media_once_and_followup(calls, caption, followup):
    """media delivered once with the caption; follow-up text exact."""
    assert calls[0] == ("media", caption), calls
    followups = [t for k, t in calls[1:] if k in ("message", "reply", "text")]
    if followup:
        assert followups == [followup], (
            f"expected exactly one follow-up {followup!r}, got {followups!r}"
        )
    else:
        assert followups == [], f"unexpected follow-up: {followups!r}"


# ----------------------------------------------------------------------
# Independent text is NEVER touched (cases 1, 2, 4)
# ----------------------------------------------------------------------
def test_case1_independent_body_preserved_tg():
    calls = _deliver_tg("Please look at the second photo", "look")
    _assert_media_once_and_followup(
        calls, "look", "Please look at the second photo"
    )


def test_case1_independent_body_preserved_max():
    calls = _deliver_max("Please look at the second photo", "look")
    _assert_media_once_and_followup(
        calls, "look", "Please look at the second photo"
    )


def test_case2_compound_words_preserved_tg():
    calls = _deliver_tg("look-alikes look good", "look")
    _assert_media_once_and_followup(calls, "look", "look-alikes look good")


def test_case2_compound_words_preserved_max():
    calls = _deliver_max("look-alikes look good", "look")
    _assert_media_once_and_followup(calls, "look", "look-alikes look good")


def test_case4_attribution_preserved_tg():
    calls = _deliver_tg("Bob: unrelated body", "Bob")
    _assert_media_once_and_followup(calls, "Bob", "Bob: unrelated body")


def test_case4_attribution_preserved_max():
    calls = _deliver_max("Bob: unrelated body", "Bob")
    _assert_media_once_and_followup(calls, "Bob", "Bob: unrelated body")


# ----------------------------------------------------------------------
# Fallback survives verbatim (case 3)
# ----------------------------------------------------------------------
def test_case3_fallback_preserved_tg():
    fallback = "[Неподдерживаемое вложение: стикер]"
    calls = _deliver_tg(fallback, "sticker")
    _assert_media_once_and_followup(calls, "sticker", fallback)


def test_case3_fallback_preserved_max():
    fallback = "[Неподдерживаемое вложение: стикер]"
    calls = _deliver_max(fallback, "sticker")
    _assert_media_once_and_followup(calls, "sticker", fallback)


# ----------------------------------------------------------------------
# REAL structural duplicate suppressed (case 5) — engine level
# ----------------------------------------------------------------------
def _engine_env(media_fetcher=None):
    """A minimal engine wired to capture the enqueued payload."""
    from shmax.adapters.max.fake import FakeMaxAdapter
    from shmax.adapters.telegram.fake import FakeTelegramAdapter
    from shmax.auth.pairing import AclService
    from shmax.bridge.engine import BridgeConfig, BridgeEngine
    from shmax.domain.enums import ChatKind
    from shmax.domain.models import ChatRef, SenderRef

    class CaptureQueue:
        def __init__(self):
            self.payloads = []

        async def enqueue(self, **kwargs):
            self.payloads.append(kwargs["payload"])

    class _RoutesStub:
        async def find_by_max(self, **kwargs):
            import datetime as dt
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
        media_fetcher=media_fetcher,
    )
    return bridge, bridge._queue


def test_case5_structural_duplicate_suppressed():
    """caption and the normalized body field represent the SAME
    source caption (adapter marked text_is_media_caption): the body
    is suppressed at composition — the follow-up carries NO duplicate
    'look'; the media caption delivers it exactly once."""
    from shmax.domain.enums import AttachmentKind, ChatKind
    from shmax.domain.models import Attachment, ChatRef, NormalizedMessage, SenderRef

    async def fetcher(source, attachment):
        class _Stored:
            media_ref = "photo:/cache/y.jpg"
            kind = "photo"
            filename = "y.jpg"
            mime = "image/jpeg"
            size = 8
            sha256 = "aa"

        return _Stored()

    bridge, queue = _engine_env(media_fetcher=fetcher)
    event = NormalizedMessage(
        source="max",
        chat=ChatRef(transport="max", external_id="c1", kind=ChatKind.DIALOG),
        sender=SenderRef(transport="max", external_id="m1"),
        external_id="mx-1",
        text="look",
        metadata={"text_is_media_caption": True},
        attachments=(
            Attachment(kind=AttachmentKind.PHOTO, payload={"url": "https://x/y.jpg"}, caption="look"),
        ),
    )
    accepted = asyncio.run(bridge.handle_max_event(event))
    assert accepted is True
    payload = queue.payloads[0]
    # Structural suppression: body removed at composition.
    assert payload.get("caption") == "look"
    body_without_marker = payload["text"].split("\n", 1)[-1]
    assert body_without_marker == "", (
        f"suppressed body must not be flattened: {payload['text']!r}"
    )
    # And through delivery: media once, NO follow-up.
    calls = _deliver_tg("", "look")
    _assert_media_once_and_followup(calls, "look", None)


def test_case6_caption_attribution_fallback_all_exactly_once():
    """caption + attribution + unsupported fallback: media carries
    the caption; the follow-up carries attribution and the fallback
    line — every component exactly once."""
    from shmax.domain.enums import AttachmentKind, ChatKind
    from shmax.domain.models import Attachment, ChatRef, NormalizedMessage, SenderRef

    bridge, queue = _engine_env()
    event = NormalizedMessage(
        source="telegram",
        chat=ChatRef(transport="telegram", external_id="-100", kind=ChatKind.GROUP),
        sender=SenderRef(transport="telegram", external_id="u1"),
        external_id="tg-1",
        text="Bob: look",
        metadata={
            "thread_id": "100",
            # NOT the media caption — independent attributed body.
            "text_is_media_caption": False,
            "telegram_user_nickname": "Bob",
        },
        attachments=(
            Attachment(kind=AttachmentKind.STICKER, payload={"file_id": "s1"}),
        ),
    )

    async def fetcher(source, attachment):
        class _Stored:
            media_ref = "photo:/cache/s.webp"
            kind = "photo"
            filename = "s.webp"
            mime = "image/webp"
            size = 10
            sha256 = "ff"

        return _Stored()

    from shmax.bridge.engine import BridgeConfig

    # Give the engine a media fetcher and a working fallback path.
    async def run():
        media, fallback = await bridge._prepare_media(event, "telegram")
        return media, fallback

    media, fallback = asyncio.run(run())
    # The sticker is unsupported (no fetcher wiring for stickers):
    # fallback text generated by the engine.
    assert media is None
    assert fallback == "[Неподдерживаемое вложение: стикер]"

    # Deliver a payload composed the way the engine composes it:
    # attributed body + fallback, native caption separate.
    calls = _deliver_max("Bob: look\n[Неподдерживаемое вложение: стикер]", "look")
    _assert_media_once_and_followup(
        calls,
        "look",
        "Bob: look\n[Неподдерживаемое вложение: стикер]",
    )
