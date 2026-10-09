"""Domain enums for SHMAX.

These are the only enums the Core layer is allowed to know about.
Transport-specific enums (PyMax ChatType, Telegram MessageOrigin,
...) must be translated at the adapter boundary.
"""

from __future__ import annotations

from enum import Enum


class Direction(str, Enum):
    """Which way a delivery is travelling."""

    MAX_TO_TELEGRAM = "max_to_telegram"
    TELEGRAM_TO_MAX = "telegram_to_max"


class ChatKind(str, Enum):
    """Kind of a remote chat, normalized."""

    DIALOG = "dialog"     # 1:1 conversation
    GROUP = "group"       # multi-user chat / forum
    CHANNEL = "channel"   # broadcast
    UNKNOWN = "unknown"


class AttachmentKind(str, Enum):
    """Kind of an inbound or outbound attachment."""

    TEXT = "text"
    PHOTO = "photo"
    DOCUMENT = "document"
    VIDEO = "video"
    AUDIO = "audio"
    VOICE = "voice"
    STICKER = "sticker"
    CONTACT = "contact"
    LOCATION = "location"
    POLL = "poll"
    OTHER = "other"


class DeliveryState(str, Enum):
    """Persistent delivery-queue state machine."""

    PENDING = "pending"
    PROCESSING = "processing"
    DELIVERED = "delivered"
    RETRY = "retry"
    FAILED = "failed"


class Role(str, Enum):
    """V1 ACL role hierarchy."""

    OWNER = "owner"
    ADMIN = "admin"
    OPERATOR = "operator"
    READ_ONLY = "read_only"


__all__ = [
    "Attachment",
    "AttachmentKind",
    "ChatKind",
    "DeliveryResult",
    "DeliveryState",
    "Direction",
    "NormalizedMessage",
    "ReplyRef",
    "Role",
    "SenderRef",
    "ChatRef",
]
