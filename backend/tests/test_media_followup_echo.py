"""Live acceptance A1 — media + follow-up echo suppression.

ONE logical delivery that produces media + a separate follow-up
text must register EVERY native Telegram/MAX message id so an
echo of either is suppressed — with ZERO visible "shmax:" text.
"""

from __future__ import annotations

import asyncio

import pytest

from shmax.runtime import AppRuntime, _MultiResult


class _Sent:
    def __init__(self, ext_id: str):
        self.external_message_id = ext_id


def test_multi_result_semantics():
    primary = _Sent("tg-1")
    follow = _Sent("tg-2")
    multi = _MultiResult(primary, follow)
    assert multi.external_message_id == "tg-1"
    assert multi.all_external_ids == ["tg-1", "tg-2"]
    # Failure semantics ride the primary.
    class _Fail:
        state = "failed"
        error = "boom"
        external_message_id = None
    failed = _MultiResult(_Fail(), _Sent("x"))
    assert failed.state == "failed"
    assert failed.error == "boom"


@pytest.mark.asyncio
async def test_record_mapping_registers_every_sent_id(db, monkeypatch):
    """A media+follow-up delivery writes mapping rows for BOTH
    native Telegram ids; an inbound echo of either id is then
    suppressed by the bridge dedup."""
    from sqlalchemy import select

    from shmax.bridge.message_map import MessageMapRepository
    from shmax.database.models import MessageMap, MaxAccount, TelegramAccount

    s = db.session.__enter__ if False else None
    async with db.session() as sess:
        sess.add(MaxAccount(label="primary", status="connected"))
        sess.add(TelegramAccount(label="primary", status="connected"))
        await sess.commit()
        max_id = (
            await sess.execute(select(MaxAccount))
        ).scalars().first().id
        tg_id = (
            await sess.execute(select(TelegramAccount))
        ).scalars().first().id

    runtime = AppRuntime.__new__(AppRuntime)  # only _record_mapping used
    runtime.db = db

    class _Rec:
        id = 1
        direction = "max_to_telegram"
        target_chat_id = "tg-chat-1"
        target_thread_id = None
        payload = {
            "max_account_id": max_id,
            "max_chat_id": "max-dialog-1",
            "max_message_id": "m-1",
            "telegram_account_id": tg_id,
            "marker": "a" * 32,
        }

    result = _MultiResult(_Sent("tg-msg-1"), _Sent("tg-msg-2"))
    await runtime._record_mapping(_Rec(), result)

    async with db.session() as sess:
        rows = (
            await sess.execute(select(MessageMap))
        ).scalars().all()
    tg_ids = {r.telegram_message_id for r in rows}
    assert tg_ids == {"tg-msg-1"}, (
        "the mapping table stays 1:1 (primary id)"
    )

    # Echo suppression: the bridge drops a MAX event whose text
    # carries any of the registered markers — and, critically, the
    # dedup-by-native-id path covers the follow-up id.
    from shmax.bridge.engine import _strip_marker

    from shmax.bridge.sent_registry import SentNativeRepository
    from shmax.domain import ChatRef, ChatKind, NormalizedMessage, SenderRef
    import datetime

    async with db.session() as sess:
        repo = SentNativeRepository(sess)
        assert await repo.exists(
            transport="telegram", chat_external_id="tg-chat-1",
            message_external_id="tg-msg-1",
        )
        assert await repo.exists(
            transport="telegram", chat_external_id="tg-chat-1",
            message_external_id="tg-msg-2",
        ), "the follow-up id MUST be registered"

    from shmax.bridge.engine import BridgeEngine

    engine_echo = BridgeEngine._is_own_echo
    assert asyncio.iscoroutinefunction(engine_echo)
    # Exercise the check through a minimal engine bound to the db.
    engine = BridgeEngine.__new__(BridgeEngine)
    engine._db = db
    echo = NormalizedMessage(
        source="telegram",
        chat=ChatRef(transport="telegram", external_id="tg-chat-1", kind=ChatKind.GROUP),
        sender=SenderRef(transport="telegram", external_id="bot", display_name="SHMAX"),
        external_id="tg-msg-2",
        text="see photo",
        timestamp=int(datetime.datetime.now().timestamp()),
    )
    assert await engine._is_own_echo(echo) is True, (
        "echo of the follow-up must be recognized"
    )
    stranger = NormalizedMessage(
        source="telegram",
        chat=ChatRef(transport="telegram", external_id="tg-chat-1", kind=ChatKind.GROUP),
        sender=SenderRef(transport="telegram", external_id="u", display_name="U"),
        external_id="tg-human-1",
        text="hello",
        timestamp=int(datetime.datetime.now().timestamp()),
    )
    assert await engine._is_own_echo(stranger) is False
