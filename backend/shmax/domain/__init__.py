"""Unified domain model for SHMAX.

This package is the **only** shape the Core layer speaks.
Adapters must translate their transport-specific objects
(PyMax Message, aiogram Message, Telethon Update, ...) into
the dataclasses defined here before any Core code touches them.

The domain is intentionally framework-free: no FastAPI, no
SQLAlchemy, no Pydantic. Pure Python dataclasses so it can
be unit-tested in microseconds and reused by anything that
needs a SHMAX-shaped value object.
"""

from .enums import (
    AttachmentKind,
    ChatKind,
    DeliveryState,
    Direction,
    Role,
)
from .models import (
    Attachment,
    ChatRef,
    DeliveryOutcome,
    DeliveryResult,
    NormalizedMessage,
    OutboundMessage,
    ReplyRef,
    SenderRef,
    TelegramPeerId,
)

__all__ = [
    "Attachment",
    "AttachmentKind",
    "ChatKind",
    "ChatRef",
    "DeliveryOutcome",
    "DeliveryResult",
    "DeliveryState",
    "Direction",
    "NormalizedMessage",
    "OutboundMessage",
    "ReplyRef",
    "Role",
    "SenderRef",
    "TelegramPeerId",
]
