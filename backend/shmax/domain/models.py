"""Pure-Python domain dataclasses.

No third-party types leak in here. Adapters convert at the
boundary; Core consumes only these shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .enums import (
    AttachmentKind,
    ChatKind,
    DeliveryState,
    Direction,
)


# ----------------------------------------------------------------------
# Identity references
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class ChatRef:
    """Pointer to a chat on a specific transport.

    ``external_id`` is whatever the transport hands us
    (MAX chat id ``int``, Telegram chat id ``int``,
    Telegram forum topic ``message_thread_id`` ``int``).
    The Core never inspects it; routing tables own the mapping.
    """

    transport: str  # "max" | "telegram"
    external_id: str
    kind: ChatKind = ChatKind.UNKNOWN
    title: str | None = None


@dataclass(frozen=True)
class SenderRef:
    """Pointer to a user / sender on a specific transport."""

    transport: str
    external_id: str
    display_name: str | None = None
    handle: str | None = None


@dataclass(frozen=True)
class ReplyRef:
    """Pointer to the message being replied to, on the same
    transport as :class:`NormalizedMessage`.

    ``thread_id`` is the Telegram forum topic id when the
    reply happens inside a forum thread; it MUST be carried
    through the bridge so destination adapters do not
    silently fall back to ``thread_id=None`` (which would
    collapse the reply into the group root).
    """

    transport: str
    chat_external_id: str
    message_external_id: str
    thread_id: str | None = None


@dataclass(frozen=True)
class DeliveryResult:
    """Concrete result of a successful adapter send.

    V1 contract: adapters MUST return a non-empty
    ``external_message_id`` for every successful delivery so
    the bridge can write the message_map row. ``thread_id``
    is preserved when the destination is a forum topic.
    ``external_chat_id`` echoes the destination chat id
    so the worker can verify it without re-reading the
    queue payload.
    """

    external_chat_id: str
    external_message_id: str
    thread_id: str | None = None
    timestamp: int | None = None


@dataclass(frozen=True)
class TelegramPeerId:
    """Canonical Telegram chat id used inside Core.

    Telegram represents a chat through three different shapes:

    * **Bot API**: ``chat.id`` is a (possibly negative) integer
      string. Supergroups and channels appear as
      ``-100<digits>`` (always negative). Basic groups appear as
      a positive int. Users appear as a positive int.
    * **Telethon**: ``PeerChannel.id`` / ``PeerChat.id`` /
      ``PeerUser.id`` are Python ints — for supergroups the
      value is the same ``-100<digits>`` form.
    * **TL raw**: can be a positive integer for users / basic
      groups or a negative ``-100<digits>`` for supergroups.

    To prevent collisions and to round-trip cleanly between Bot
    API and Telethon, we encode chat ids uniformly as strings
    with a single-letter prefix that tags the chat type:

    * ``u<positive>`` — user (1:1 chat).
    * ``b<positive>`` — basic group (small negative-id group;
      rare in modern Telegram but supported).
    * ``c<positive>`` — supergroup / channel. Telethon's
      ``-100<digits>`` becomes ``c<digits-stripped>`` (drop the
      canonical ``100``). Bot API's ``-100<digits>`` becomes
      the same canonical form.
    * ``t<positive>`` — forum topic thread id (a secondary
      scope inside a supergroup).

    The canonical form is independent of which library produced
    it: a Bot API ``"-1001234567890"`` and a Telethon
    ``PeerChannel(id=-1001234567890)`` both render as
    ``"c1234567890"``.

    Adapters MUST convert raw library output to
    :class:`TelegramPeerId` so the Core never sees an
    ambiguous positive integer.
    """

    raw: str

    def __str__(self) -> str:
        return self.raw

    @classmethod
    def from_bot_api(cls, chat_id: int | str) -> "TelegramPeerId":
        """Convert a Bot API chat id (int or string) to the
        canonical form.

        ``chat_id`` may be:

        * a positive int — basic group / user / forum;
        * a negative int — supergroup / channel (Telegram Bot
          API returns ``-100<digits>`` for those);
        * a string form of either.
        """
        raw = str(chat_id).strip()
        is_negative = raw.startswith("-")
        digits = raw.lstrip("-")
        if not digits.isdigit():
            raise ValueError(f"non-numeric Bot API chat_id: {chat_id!r}")
        positive = int(digits)
        # Negative form is Telegram's supergroup marker; strip
        # the canonical ``100`` prefix and encode as ``c``.
        if is_negative and str(positive).startswith("100"):
            return cls(f"c{int(str(positive)[3:])}")
        return cls(f"c{positive}")

    @classmethod
    def from_user_id(cls, user_id: int | str) -> "TelegramPeerId":
        return cls(f"u{int(str(user_id).lstrip('-'))}")

    @classmethod
    def from_basic_group(cls, id: int | str) -> "TelegramPeerId":
        return cls(f"b{int(str(id).lstrip('-'))}")

    @classmethod
    def from_chat_id(cls, id: int | str) -> "TelegramPeerId":
        """Default discriminator: numeric ``-100`` prefix →
        channel/supergroup; otherwise basic group."""
        raw = str(id).strip()
        is_negative = raw.startswith("-")
        digits = raw.lstrip("-")
        if not digits.isdigit():
            raise ValueError(f"non-numeric Telegram chat_id: {id!r}")
        positive = int(digits)
        if is_negative and str(positive).startswith("100"):
            return cls(f"c{positive}")
        return cls(f"b{positive}")

    @classmethod
    def from_telethon(cls, peer_id: int, *, kind: str) -> "TelegramPeerId":
        """Convert a Telethon raw peer id to canonical form.

        ``kind`` is one of ``user | chat | channel``.

        The kind hint is authoritative: ``user`` always produces
        ``u<positive>``; ``chat`` always produces
        ``b<positive>``; ``channel`` always produces
        ``c<positive>`` with the ``-100`` prefix stripped.
        """
        if kind == "user":
            return cls(f"u{abs(int(peer_id))}")
        if kind == "chat":
            return cls(f"b{abs(int(peer_id))}")
        if kind == "channel":
            positive = abs(int(peer_id))
            digits = str(positive)
            # Strip the canonical ``100`` Telegram uses for supergroups.
            if digits.startswith("100"):
                positive = int(digits[3:])
            return cls(f"c{positive}")
        raise ValueError(f"unknown Telethon peer kind: {kind!r}")

    @property
    def is_forum_topic(self) -> bool:
        return self.raw.startswith("t")

    @property
    def bot_api_chat_id(self) -> int:
        """Return the canonical positive integer chat id.

        For a channel/supergroup ``c<positive>``, this is the
        ``<digits>`` portion (i.e. the id a Bot API user would
        type minus the ``-100`` prefix).
        """
        digits = self.raw[1:]
        return int(digits)

    @property
    def bot_api_supergroup_id(self) -> int:
        """Return the ``-100<digits>`` form Telegram uses for
        Bot API supergroups / channels."""
        return int(f"-100{self.raw[1:]}")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TelegramPeerId):
            return NotImplemented
        return self.raw == other.raw

    def __hash__(self) -> int:
        return hash(self.raw)


# ----------------------------------------------------------------------
# Attachments
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Attachment:
    """Single media / payload attached to a message.

    ``payload`` carries transport-specific metadata (file id,
    mime, size, url). The Core treats it as opaque.
    """

    kind: AttachmentKind
    payload: dict[str, Any] = field(default_factory=dict)
    caption: str | None = None


# ----------------------------------------------------------------------
# Messages
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class NormalizedMessage:
    """Single normalized inbound event from a transport.

    Lifecycle: an adapter builds this from a PyMax
    :class:`pymax.Message` or an aiogram / Telethon update, then
    hands it to the Core. After that, no transport-specific
    objects are referenced.
    """

    # Direction this message is travelling (inbound only here;
    # outbound is represented by ``OutboundMessage`` below).
    source: str  # "max" | "telegram"
    chat: ChatRef
    sender: SenderRef | None
    external_id: str
    text: str = ""
    timestamp: int = 0  # unix seconds
    reply_to: ReplyRef | None = None
    attachments: tuple[Attachment, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def has_attachments(self) -> bool:
        return bool(self.attachments)


@dataclass(frozen=True)
class DeliveryOutcome:
    """Result of an attempted delivery by the queue worker.

    The queue and the UI both consume this shape.

    V1 distinguishes between this queue-level outcome and
    :class:`DeliveryResult` (the adapter-level return value
    that carries the destination chat id + message id).
    """

    state: DeliveryState
    external_id: str | None = None  # delivered message id (or None on failure)
    error: str | None = None
    attempts: int = 0
    direction: Direction | None = None


# ----------------------------------------------------------------------
# Outbound payload
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class OutboundMessage:
    """Message produced by the Core for an adapter to deliver.

    The bridge between Core and adapter layer. Carries a
    :class:`NormalizedMessage` (the original inbound) plus the
    routing decision so the adapter knows where to send.
    """

    target: ChatRef
    text: str = ""
    attachments: tuple[Attachment, ...] = ()
    reply_to: ReplyRef | None = None
    direction: Direction = Direction.MAX_TO_TELEGRAM
    metadata: dict[str, Any] = field(default_factory=dict)
