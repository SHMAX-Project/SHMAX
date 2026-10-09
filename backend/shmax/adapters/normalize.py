"""Adapter → NormalizedMessage translation.

The runtime calls : `` ``normalize_inbound`` for every event
consumed from MAX or Telegram. The function MUST preserve the
full set the Core needs to forward the message — there is no
separate "richer normalizer" used only in tests.

This module is the single source of truth for that
translation. Production callers (the runtime listener loops)
and tests share it.
"""

from __future__ import annotations

from typing import Any

from ..domain import ChatKind, ChatRef, NormalizedMessage, ReplyRef, SenderRef
from ..domain.enums import AttachmentKind
from ..domain.models import Attachment


def _chat_kind_from_telegram(chat: Any) -> ChatKind:
    """Pick a ChatKind from a Telegram chat instance.

    Audit P0 #7: the aiogram ``Chat`` OBJECT must never imply
    GROUP by its class name — both a basic group and a private
    chat are aiogram ``Chat`` instances. The authoritative field
    is ``chat.type`` (``"private" / "group" / "supergroup" /
    "channel"``). The class-name fallback below is only for
    Telethon objects, which expose distinct classes (``User``,
    ``Chat``, ``Channel``). Anything undeterminable is UNKNOWN —
    fail closed.
    """
    # aiogram (and Bot API generally): chat.type carries the raw
    # Telegram type string.
    t = getattr(chat, "type", None)
    if isinstance(t, str):
        if t == "private":
            return ChatKind.DIALOG
        if t in ("group", "supergroup"):
            return ChatKind.GROUP
        if t == "channel":
            return ChatKind.CHANNEL
        return ChatKind.UNKNOWN
    # Telethon: distinct payload classes per peer type.
    cls = getattr(chat, "__class__", None)
    name = cls.__name__ if cls else ""
    if name == "User":
        return ChatKind.DIALOG
    if name == "Chat":
        return ChatKind.GROUP
    if name == "Channel":
        if getattr(chat, "megagroup", False) or getattr(chat, "gigagroup", False):
            return ChatKind.GROUP
        return ChatKind.CHANNEL
    return ChatKind.UNKNOWN


def _reply_to_id_from_raw(raw: Any) -> str | None:
    """Extract the replied-to message id from a raw library object.

    Audit P0 #10: the real inbound pipeline MUST preserve reply
    metadata. Supported shapes:

    * aiogram ``Message.reply_to_message.message_id`` (Bot API);
    * Telethon ``event.message.reply_to.reply_to_msg_id``;
    * generic ``raw.reply_to`` / ``raw.reply_to_message_id`` ints
      or strings (PyMax duck-typing).
    """
    if raw is None:
        return None
    r = getattr(raw, "reply_to_message", None)
    if r is not None and getattr(r, "message_id", None) is not None:
        return str(r.message_id)
    msg = getattr(raw, "message", None)  # Telethon NewMessage
    if msg is not None:
        rt = getattr(msg, "reply_to", None)
        top = getattr(rt, "reply_to_top_id", None) if rt else None
        mid = getattr(rt, "reply_to_msg_id", None) if rt else None
        if mid is not None:
            return str(top if top else mid)
    rt = getattr(raw, "reply_to", None)
    if isinstance(rt, (int, str)) and str(rt).isdigit():
        return str(rt)
    mid = getattr(raw, "reply_to_message_id", None)
    if isinstance(mid, (int, str)) and str(mid).isdigit():
        return str(mid)
    return None


def _attachments_from_raw(raw: Any) -> tuple[Attachment, ...]:
    """Extract attachments from a raw library object.

    Audit P0 #10/#11: never silently drop media. Produces the
    unified :class:`Attachment` representation (kind + payload +
    caption) for both aiogram and Telethon shapes.
    """
    if raw is None:
        return ()
    msg = getattr(raw, "message", None)  # Telethon NewMessage
    if msg is not None and hasattr(msg, "id"):
        raw = msg
    out: list[Attachment] = []
    caption = getattr(raw, "caption", None)

    def _payload_from_media(m: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for attr in ("file_id", "file_name", "mime_type", "duration",
                     "width", "height", "id", "access_hash", "file_size", "is_animated", "is_video"):
            val = getattr(m, attr, None)
            if val is not None:
                payload[attr] = val
        return payload

    photo = getattr(raw, "photo", None)
    if photo:
        # aiogram Bot API: list[PhotoSize]; Telethon: Photo.sizes.
        sizes = photo if isinstance(photo, (list, tuple)) else (getattr(photo, "sizes", None) or ())
        biggest = max(sizes, key=lambda p: (getattr(p, "width", 0) * getattr(p, "height", 0), getattr(p, "file_size", 0) or 0)) if sizes else photo
        payload = _payload_from_media(biggest if isinstance(photo, (list, tuple)) else photo)
        if hasattr(biggest, "file_id"):
            payload["file_id"] = biggest.file_id
        out.append(Attachment(kind=AttachmentKind.PHOTO, payload=payload, caption=caption))
    document = getattr(raw, "document", None)
    if document is not None:
        payload = _payload_from_media(document)
        payload.setdefault("file_id", payload.get("id"))
        out.append(
            Attachment(
                kind=AttachmentKind.DOCUMENT,
                payload=payload,
                caption=caption,
            )
        )
    video = getattr(raw, "video", None)
    if video is not None:
        out.append(Attachment(kind=AttachmentKind.VIDEO, payload=_payload_from_media(video), caption=caption))
    voice = getattr(raw, "voice", None)
    if voice is not None:
        out.append(Attachment(kind=AttachmentKind.VOICE, payload=_payload_from_media(voice), caption=None))
    video_note = getattr(raw, "video_note", None)
    if video_note is not None:
        payload = _payload_from_media(video_note)
        payload["video_note"] = True
        out.append(Attachment(kind=AttachmentKind.VIDEO, payload=payload, caption=None))
    audio = getattr(raw, "audio", None)
    if audio is not None:
        out.append(Attachment(kind=AttachmentKind.AUDIO, payload=_payload_from_media(audio), caption=None))
    sticker = getattr(raw, "sticker", None)
    if sticker is not None:
        payload = _payload_from_media(sticker)
        payload["emoji"] = getattr(sticker, "emoji", None)
        thumbnail = getattr(sticker, "thumbnail", None)
        if thumbnail:
            payload["thumbnail_file_id"] = thumbnail.file_id
        out.append(Attachment(kind=AttachmentKind.STICKER, payload=payload, caption=None))
    animation = getattr(raw, "animation", None)
    if animation is not None:
        out.append(Attachment(kind=AttachmentKind.OTHER, payload=_payload_from_media(animation), caption=caption))
    location = getattr(raw, "location", None)
    if location is not None:
        out.append(
            Attachment(
                kind=AttachmentKind.LOCATION,
                payload={
                    "latitude": getattr(location, "latitude", None),
                    "longitude": getattr(location, "longitude", None),
                },
                caption=None,
            )
        )
    contact = getattr(raw, "contact", None)
    if contact is not None:
        out.append(
            Attachment(
                kind=AttachmentKind.CONTACT,
                payload={
                    "phone_number": getattr(contact, "phone_number", None),
                    "first_name": getattr(contact, "first_name", None),
                    "last_name": getattr(contact, "last_name", None),
                },
                caption=None,
            )
        )
    return tuple(out)


def _sender_display_name_from_raw(raw: Any) -> str | None:
    """Best-effort sender display name from a raw object."""
    if raw is None:
        return None
    user = getattr(raw, "from_user", None)  # aiogram
    if user is not None:
        return getattr(user, "full_name", None) or getattr(user, "first_name", None)
    msg = getattr(raw, "message", None)
    sender = getattr(raw, "sender", None)  # Telethon event
    target = sender if sender is not None else getattr(msg, "sender", None) if msg is not None else None
    if target is None and msg is not None:
        target = getattr(msg, "sender", None)
    if target is not None:
        return (
            getattr(target, "title", None)
            or getattr(target, "first_name", None)
        )
    return None


def _pymax_reply_to_id(raw: Any) -> str | None:
    """Reply id from a REAL PyMax 2.4.1 ``Message``.

    PyMax 2.4.1 carries the replied-to object on
    ``Message.link`` — a discriminated union
    ``ReplyLink | ForwardLink`` where ``ReplyLink.message`` is
    the nested replied-to ``Message``. (The generic duck-typed
    ``reply_to`` probing does NOT see this field.)
    """
    link = getattr(raw, "link", None)
    if link is None:
        return None
    link_type = str(getattr(link, "type", ""))
    if "REPLY" not in link_type.upper():
        return None
    nested = getattr(link, "message", None)
    nested_id = getattr(nested, "id", None)
    if nested_id is None:
        return None
    return str(nested_id)


def _pymax_attachments_from_raw(raw: Any, event_id: str = "", chat_id: str = "") -> tuple[Attachment, ...]:
    """Attachments from a REAL PyMax 2.4.1 ``Message.attaches``.

    Verified against maxapi-python 2.4.1:

    * ``PhotoAttachment``  — ``base_url`` (direct URL), photo_id
    * ``VideoAttachment``  — ``video_id`` (+token/thumbnail);
      fetchable via ``Client.get_video_by_id``
    * ``FileAttachment``   — ``file_id``/``name``/``size``;
      fetchable via ``Client.get_file_by_id``
    * ``AudioAttachment``  — ``url`` (direct) / token, duration
    * ``StickerAttachment`` / ``ContactAttachment`` /
      ``ShareAttachment`` / ``PollAttachment`` /
      ``ControlAttachment`` / ``InlineKeyboardAttachment`` /
      ``CallAttachment`` / ``UnknownAttachment`` — carried with
      their kind so the bridge can emit the controlled fallback.
    """
    if raw is None:
        return ()
    attaches = getattr(raw, "attaches", None) or []
    out: list[Attachment] = []
    for att in attaches:
        att_type = str(getattr(att, "type", "")).upper()
        base = {
            "pymax_type": att_type or "UNKNOWN",
            # Id-based fetches (FILE / VIDEO) need the source
            # message coordinates for Client.get_file_by_id /
            # get_video_by_id.
            "chat_id": chat_id,
            "message_id": event_id,
        }
        if "PHOTO" in att_type:
            payload = {
                **base,
                "url": getattr(att, "base_url", None),
                "photo_id": getattr(att, "photo_id", None),
                "width": getattr(att, "width", None),
                "height": getattr(att, "height", None),
            }
            out.append(Attachment(kind=AttachmentKind.PHOTO, payload=payload))
        elif "VIDEO" in att_type:
            payload = {
                **base,
                "video_id": getattr(att, "video_id", None),
                "token": getattr(att, "token", None),
                "duration": getattr(att, "duration", None),
            }
            out.append(Attachment(kind=AttachmentKind.VIDEO, payload=payload))
        elif "FILE" in att_type:
            payload = {
                **base,
                "file_id": getattr(att, "file_id", None),
                "file_name": getattr(att, "name", None),
                "size": getattr(att, "size", None),
            }
            out.append(Attachment(kind=AttachmentKind.DOCUMENT, payload=payload))
        elif "AUDIO" in att_type:
            payload = {
                **base,
                "url": getattr(att, "url", None),
                "token": getattr(att, "token", None),
                "duration": getattr(att, "duration", None),
            }
            out.append(Attachment(kind=AttachmentKind.AUDIO, payload=payload))
        elif "STICKER" in att_type:
            payload = {
                **base,
                "url": getattr(att, "url", None) or getattr(att, "base_url", None),
                "sticker_id": getattr(att, "sticker_id", None),
            }
            out.append(Attachment(kind=AttachmentKind.STICKER, payload=payload))
        elif "CONTACT" in att_type:
            payload = {
                **base,
                "first_name": getattr(att, "first_name", None),
                "last_name": getattr(att, "last_name", None),
                "name": getattr(att, "name", None),
            }
            out.append(Attachment(kind=AttachmentKind.CONTACT, payload=payload))
        else:
            # SHARE / POLL / CONTROL / CALL / INLINE KEYBOARD /
            # UNKNOWN — kind preserved for the controlled fallback.
            out.append(Attachment(kind=AttachmentKind.OTHER, payload=base))
    return tuple(out)


def normalize_max(event: Any) -> NormalizedMessage:
    """Translate a :class:`MaxEvent` (or any duck-typed event with
    ``chat_id``, ``sender_id``, ``event_id``, ``text``) into a
    full :class:`NormalizedMessage`.

    The translation is conservative: rich fields that the
    adapter object exposes (reply_to, attachments, raw
    metadata) are carried through unchanged so downstream
    consumers can use them.
    """
    from .max.base import MaxEvent

    chat_id = getattr(event, "chat_id", "")
    sender_id = getattr(event, "sender_id", "") or ""
    event_id = getattr(event, "event_id", "")
    text = getattr(event, "text", "") or ""
    reply_to = getattr(event, "reply_to", None)
    attachments = tuple(getattr(event, "attachments", ()) or ())
    raw = getattr(event, "raw", None)
    if reply_to is None and raw is not None:
        reply_id = _pymax_reply_to_id(raw) or _reply_to_id_from_raw(raw)
        if reply_id:
            # The replied-to message lives in the SAME MAX chat;
            # the engine resolves it against the message_map.
            reply_to = ReplyRef(
                transport="max",
                chat_external_id=str(chat_id),
                message_external_id=reply_id,
            )
    if not attachments:
        attachments = _pymax_attachments_from_raw(raw, str(event_id), str(chat_id))
    timestamp = int(getattr(event, "timestamp", 0) or 0)
    metadata = dict(getattr(event, "metadata", {}) or {})
    display_name = _sender_display_name_from_raw(raw)
    return NormalizedMessage(
        source="max",
        chat=ChatRef(
            transport="max",
            external_id=str(chat_id),
            kind=ChatKind.DIALOG,
        ),
        sender=SenderRef(
            transport="max",
            external_id=str(sender_id),
            display_name=display_name,
        ),
        external_id=str(event_id),
        text=str(text),
        timestamp=timestamp,
        reply_to=reply_to,
        attachments=attachments,
        metadata=metadata,
    )


def normalize_telegram(event: Any) -> NormalizedMessage:
    """Translate a :class:`TelegramEvent` into a rich
    :class:`NormalizedMessage`.

    The translation pulls:

    * ``thread_id`` from the event's ``thread_id`` (which the
      adapter set from ``reply_to_top_id`` for forum topics);
    * the chat kind from ``event.chat`` if present (Telethon
      exposes ``Channel`` / ``Chat`` / ``User`` instances);
    * any attachments and reply_to carried on the event;
    * the raw event under ``metadata['raw']`` so handlers
      can still inspect library-specific fields.
    """
    from .telegram.base import TelegramEvent

    chat_id = getattr(event, "chat_id", "")
    sender_id = getattr(event, "sender_id", "") or ""
    update_id = getattr(event, "update_id", "")
    raw = getattr(event, "raw", None)
    if not chat_id and raw is not None:
        raw_chat = getattr(raw, "chat", None)
        if raw_chat is not None and getattr(raw_chat, "id", None) is not None:
            chat_id = str(raw_chat.id)
    text = getattr(event, "text", "") or ""
    thread_id = getattr(event, "thread_id", None)
    reply_to = getattr(event, "reply_to", None)
    attachments = tuple(getattr(event, "attachments", ()) or ())
    timestamp = int(getattr(event, "timestamp", 0) or 0)
    chat = getattr(event, "chat", None)
    # If the event didn't carry a pre-extracted ``chat``, look
    # for it on ``raw`` (Telethon's NewMessage event exposes the
    # chat there; aiogram messages carry ``raw.chat``).
    if chat is None and raw is not None:
        chat = getattr(raw, "chat", None)
    # Audit P0 #7: an undeterminable chat is UNKNOWN — the old
    # default-to-GROUP made every DM look like group traffic.
    chat_kind = (
        _chat_kind_from_telegram(chat) if chat is not None else ChatKind.UNKNOWN
    )
    event_meta = getattr(event, "metadata", None) or {}
    metadata: dict[str, Any] = {
        "thread_id": thread_id,
    }
    # Structural caption knowledge from the adapter boundary: when
    # the source API exposed the media caption AS the message text,
    # composition may suppress that duplicate without lexical
    # search (surgical P1).
    if event_meta.get("text_is_media_caption") is True:
        metadata["text_is_media_caption"] = True
    if raw is not None:
        metadata["raw"] = raw
    # Audit P0 #10: pull the full reply / attachment / sender
    # metadata from the raw library object — the wire-level
    # TelegramEvent intentionally drops these, and losing them
    # here broke native replies and media end-to-end.
    if reply_to is None:
        reply_id = _reply_to_id_from_raw(raw)
        if reply_id:
            reply_to = ReplyRef(
                transport="telegram",
                chat_external_id=str(chat_id),
                message_external_id=reply_id,
                thread_id=thread_id,
            )
    if not attachments:
        attachments = _attachments_from_raw(raw)
    display_name = _sender_display_name_from_raw(raw)
    return NormalizedMessage(
        source="telegram",
        chat=ChatRef(
            transport="telegram",
            external_id=str(chat_id),
            kind=chat_kind,
            title=getattr(chat, "title", None)
            or getattr(chat, "first_name", None),
        ),
        sender=SenderRef(
            transport="telegram",
            external_id=str(sender_id),
            display_name=display_name,
        ),
        external_id=str(update_id),
        text=str(text),
        timestamp=timestamp,
        reply_to=reply_to,
        attachments=attachments,
        metadata=metadata,
    )


def normalize_inbound(source: str, event: Any) -> NormalizedMessage:
    """Dispatch on ``source`` (``max`` / ``telegram``).

    Accepts either a transport-specific event or an already
    :class:`NormalizedMessage` (passes through).
    """
    from .telegram.base import TelegramEvent

    if isinstance(event, NormalizedMessage):
        return event
    if isinstance(event, TelegramEvent) or source == "telegram":
        return normalize_telegram(event)
    return normalize_max(event)


__all__ = [
    "normalize_inbound",
    "normalize_max",
    "normalize_telegram",
]