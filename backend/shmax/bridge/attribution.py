"""Author-attribution formatter for TG → MAX bridge messages.

The audit caught that messages the operator's MAX account sends on
behalf of a Telegram participant carried no human attribution at all
— MAX recipients could not tell who in the group actually wrote the
message.

The convention we adopt is the plain "Telegram style" attribution:

    [Иван]: Привет, встречаемся завтра.

Rules:

  - Source of the name is the :class:`shmax.database.models.TelegramUser`
    row's persisted ``nickname`` (the operator's chosen nickname
    set via /setname). If that is empty / pending we fall back to the
    Telegram ``first_name + last_name`` from the inbound event.
  - The Telegram @username is rendered ONLY when no readable full name
    is available — i.e. private accounts with no first/last name and
    no display_name. We do not lead with @username.
  - The label is plain, never Telegram markup, never HTML.
  - Length cap is 64 visible characters — longer names are truncated
    with a trailing ellipsis.
  - Whitespace is trimmed; newlines, control chars and bidi marks
    are stripped. Empty results are reported back as ``None`` and the
    caller MUST omit the attribution line.
  - Attribution is computed once and stored in the queued delivery so
    retries — including ones that happen after the user renamed
    themselves — keep the original label consistent.
  - Service markers (``\\u200Bshmax:<uuid>``) live on a SEPARATE line
    from the attribution — they are never mixed into the attribution
    line so MAX does not show them to the user.
  - MAX → Telegram messages are NEVER prefixed with a Telegram
    author label; the audit says this is a privilege, not a route.

Functions in this module are pure. No DB, no asyncio. They are
exercised directly by unit tests under ``tests/test_attribution.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


_MAX_LABEL_LEN = 64

_CONTROL_CHAR_RE = re.compile(
    r"[\u0000-\u001f\u007f\u200b\u200e-\u200f\u202a-\u202e\u2066-\u2069]"
)


@dataclass(frozen=True)
class AuthorLabel:
    """The resolved author attribution for an outbound MAX message.

    ``label`` is the text rendered into MAX, including the
    ``[Name]`` brackets — or ``None`` when no attribution can be
    safely produced. ``fallback_used`` flags the rare case where we
    fell back to the Telegram full name because the persisted
    nickname was missing or empty.
    """

    label: Optional[str]
    fallback_used: bool


def _sanitize(name: str) -> str:
    """Strip control / format characters and collapse whitespace.

    Names can come from Telegram ``first_name`` / ``last_name`` /
    ``username`` / arbitrary user-set display_name values. Any
    newline, carriage return, bidi marker, zero-width space or
    control character is removed so the resulting label is one
    clean line. Newlines / carriage returns become single spaces
    so adjacent words stay readable.
    """
    if not name:
        return ""
    # Replace line breaks with spaces first so a "first\\nlinemin"
    # style input does not become "firstlinemin" when the breaks
    # are stripped below.
    replaced = name.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    cleaned = _CONTROL_CHAR_RE.sub("", replaced)
    cleaned = re.sub(r"\s+", " ", cleaned, flags=re.UNICODE).strip()
    return cleaned


def _truncate(name: str, limit: int = _MAX_LABEL_LEN) -> str:
    if len(name) <= limit:
        return name
    # Reserve room for the ellipsis.
    return name[: max(0, limit - 1)].rstrip() + "\u2026"


def resolve_author_label(
    *,
    persisted_nickname: Optional[str],
    telegram_first_name: Optional[str] = None,
    telegram_last_name: Optional[str] = None,
    telegram_username: Optional[str] = None,
) -> AuthorLabel:
    """Pick the canonical author label for a TG → MAX send.

    Priority:

      1. ``persisted_nickname`` (operator-chosen via /setname) — if
         non-empty after sanitisation.
      2. ``first_name + last_name`` from the inbound Telegram event.
      3. ``telegram_username`` (without the leading ``@``) — last
         resort. Telegram usernames are pure ASCII.

    The function NEVER returns a label derived from a sender's own
    claim of identity — only from operator-confirmed persisted data
    or from the verified Telegram account header. Nicknames are a
    display label; ACL is keyed on the numeric Telegram user id.
    """
    nickname = _sanitize(persisted_nickname or "")
    if nickname:
        return AuthorLabel(label=f"[{_truncate(nickname)}]:", fallback_used=False)
    full = _sanitize(
        " ".join(filter(None, [telegram_first_name, telegram_last_name]))
    )
    if full:
        return AuthorLabel(
            label=f"[{_truncate(full)}]:", fallback_used=True
        )
    if telegram_username:
        cleaned_username = _sanitize(telegram_username).lstrip("@")
        if cleaned_username:
            return AuthorLabel(
                label=f"[@{_truncate(cleaned_username)}]:", fallback_used=True
            )
    return AuthorLabel(label=None, fallback_used=False)


def attribute_text(
    *,
    text: str,
    label: Optional[str],
    inline: bool = False,
) -> str:
    """Compose the outgoing MAX text from the operator's body and the
    resolved author label.

    Layout::

        [Иван]:                 <-- attribution line
        <original body>         <-- whatever the TG user wrote

    The attribution line is on its own line so MAX clients that
    collapse leading whitespace still render it cleanly. If ``label``
    is None the body is returned as-is — the audit explicitly says
    do not invent an attribution when no safe name is available.
    """
    if not label:
        return text
    if not text:
        return label
    if inline and (text == label or text.startswith(label + " ") or text.startswith(label + "\n")):
        return text
    return f"{label}{' ' if inline else chr(10)}{text}"


def attribute_caption(
    *,
    caption: Optional[str],
    label: Optional[str],
    inline: bool = False,
) -> Optional[str]:
    """Same contract as :func:`attribute_text` but for media captions.

    If both are None, the caller omits the caption. Audit says: for
    supported media, include attribution in the caption; if the
    caption is impossible (no caption slot), the caller MUST fall
    back to a clearly linked textual attribution in the next
    message — never silently drop the marker.
    """
    if not caption and not label:
        return None
    if not label:
        return caption
    if not caption:
        return label
    return attribute_text(text=caption, label=label, inline=inline)


__all__ = [
    "AuthorLabel",
    "attribute_caption",
    "attribute_text",
    "resolve_author_label",
]