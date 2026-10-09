"""Tests for the TG → MAX author-attribution formatter.

The audit caught that the bridge delivered TG messages into MAX
without any human attribution — MAX recipients could not tell
which Telegram participant wrote what. This module pins the
audit-mandated rules:

  - Persisted operator-chosen nickname wins over Telegram's
    first/last name.
  - Sanitisation strips newlines, control characters, bidi
    marks and zero-width spaces — Telegram display names can
    be any Unicode.
  - Long names are truncated with an ellipsis.
  - Attribution is on its own line so MAX clients with leading-
    whitespace collapsing still render cleanly.
  - Service markers (\\u200Bshmax:…) live separately so MAX
    does not show them.
  - When no safe name is available, no attribution is emitted.
"""

from __future__ import annotations

import pytest

from shmax.bridge.attribution import (
    attribute_caption,
    attribute_text,
    resolve_author_label,
)


@pytest.mark.parametrize(
    "nick,first,last,username,expected",
    [
        # Persisted nickname wins.
        ("Иван", "Телеграм Имя", "Фамилия", "ignored", "[Иван]:"),
        # Empty nickname → fallback to full Telegram name.
        ("", "Иван", "Петров", "ignored", "[Иван Петров]:"),
        ("   ", "Иван", None, "ignored", "[Иван]:"),
        # Username-only as last resort.
        (None, None, None, "@ivan", "[@ivan]:"),
        # No name at all → None.
        (None, None, None, None, None),
        # Cleanup of bidi marks / control chars.
        ("\u200BИван\u0007", None, None, None, "[Иван]:"),
        # Truncation.
        ("x" * 100, None, None, None, f"[{'x' * 63}\u2026]:"),
    ],
)
def test_resolve_author_label_priority(nick, first, last, username, expected):
    label = resolve_author_label(
        persisted_nickname=nick,
        telegram_first_name=first,
        telegram_last_name=last,
        telegram_username=username,
    )
    assert label.label == expected


def test_resolve_author_label_strips_newlines():
    label = resolve_author_label(
        persisted_nickname="Иван\r\nПривет",
    )
    # The newline is replaced with a space and collapsed.
    assert label.label == "[Иван Привет]:"


def test_resolve_author_label_no_name():
    label = resolve_author_label(
        persisted_nickname="",
        telegram_first_name=None,
        telegram_username=None,
    )
    assert label.label is None


def test_attribute_text_with_label():
    assert attribute_text(
        text="Привет, встречаемся завтра.",
        label="[Иван]:",
    ) == "[Иван]:\nПривет, встречаемся завтра."


def test_attribute_text_without_label_passes_body_through():
    # Audit says: do not invent an attribution when no safe name
    # is available.
    assert attribute_text(text="Привет", label=None) == "Привет"


def test_attribute_text_empty_body():
    # When there is no body, the attribution line is returned by
    # itself (still useful as a one-line "X is typing" marker if
    # the audit ever needs it).
    assert attribute_text(text="", label="[Иван]:") == "[Иван]:"


def test_attribute_caption_with_label():
    out = attribute_caption(
        caption="Вид с площади.",
        label="[Иван]:",
    )
    assert out == "[Иван]:\nВид с площади."


def test_attribute_caption_without_label():
    assert attribute_caption(caption="Вид с площади.", label=None) == "Вид с площади."


def test_attribute_caption_only_label():
    assert attribute_caption(caption=None, label="[Иван]:") == "[Иван]:"


def test_attribute_caption_both_empty():
    assert attribute_caption(caption=None, label=None) is None


def test_attribute_caption_truncates_long_nickname():
    label = resolve_author_label(
        persisted_nickname="x" * 200,
    ).label
    # 63 visible chars + ellipsis; the brackets add 4 more.
    assert label.startswith("[") and label.endswith("]:")
    visible = label[1:-2]  # strip "[" and "]:"
    assert visible.endswith("\u2026")
    assert len(visible) == 64


def test_marker_separated_from_attribution():
    # Service markers must NEVER appear inside the attribution
    # line — the audit says MAX recipients see only the
    # attribution, not our internal marker.
    body = "Привет"
    label = "[Иван]:"
    attributed = attribute_text(text=body, label=label)
    marker = "\u200Bshmax:00000000000000000000000000000000\n"
    full = f"{marker}{attributed}"
    # The marker line is preserved on its own line.
    assert full.startswith(marker)
    # The attribution line does not contain the marker.
    assert "\u200Bshmax:" not in attributed