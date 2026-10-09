"""Domain layer tests."""

from __future__ import annotations

from shmax.domain import (
    Attachment,
    AttachmentKind,
    ChatKind,
    ChatRef,
    DeliveryOutcome,
    DeliveryResult,
    DeliveryState,
    Direction,
    NormalizedMessage,
    ReplyRef,
    Role,
    SenderRef,
)


def test_normalized_message_construction():
    msg = NormalizedMessage(
        source="telegram",
        chat=ChatRef(transport="telegram", external_id="100", kind=ChatKind.GROUP, title="Работа"),
        sender=SenderRef(transport="telegram", external_id="42", display_name="Иван"),
        external_id="9001",
        text="привет",
        timestamp=1700000000,
        reply_to=ReplyRef(transport="telegram", chat_external_id="100", message_external_id="9000"),
        attachments=(Attachment(kind=AttachmentKind.PHOTO, payload={"file_id": "abc"}),),
        metadata={"raw": "ignored"},
    )
    assert msg.has_attachments
    assert msg.chat.title == "Работа"
    assert msg.reply_to.message_external_id == "9000"


def test_direction_enum_values():
    assert Direction.MAX_TO_TELEGRAM.value == "max_to_telegram"
    assert Direction.TELEGRAM_TO_MAX.value == "telegram_to_max"


def test_delivery_result_defaults():
    r = DeliveryOutcome(state=DeliveryState.PENDING)
    assert r.external_id is None
    assert r.error is None
    assert r.attempts == 0


def test_role_enum_includes_owner():
    assert Role.OWNER.value == "owner"
