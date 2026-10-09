"""Authoritative phone-number normalisation and validation.

All SHMAX surfaces — MAX auth start, Telegram contact matching,
operator-display formatting — go through this module. We use
``phonenumbers`` (the Python port of Google's libphonenumber)
because:

* It is the single most maintained phone parser available.
* It knows every country's national-number length, mobile prefixes
  and country-code prefixes — including the Russian 8-prefix
  convention used since Soviet times.
* It produces an E.164 canonical form we can hand to PyMax
  and Telethon without ambiguity.

The audit caught three concrete bugs in the previous
implementation:

  a) ``8 (999) 123-45-67`` was treated as a malformed input
     because the previous normaliser expected an explicit ``+``
     or a digit-only string.

  b) National Russian numbers starting with ``8`` were being
     prepended with ``+`` without first stripping the trunk
     prefix. ``+89...`` is not a valid country-code prefix and
     MAX may treat the verification as a different country.

  c) The resend path was reusing the masked phone hint
     (``***4567``) as the canonical number. ``request_code``
     cannot work against that.

Three things land in this module:

  * ``NormalisedPhone`` — value type that always carries
    ``e164`` (the canonical auth value) and a ``hint`` (last
    4 digits, never the raw phone).
  * ``parse(raw, default_region)`` — does the libphonenumber
    work and returns a ``NormalisedPhone`` or raises
    ``PhoneNormalisationError``.
  * ``default_region_for(operator_locale)`` — picks the
    sensible country default for the wizard country selector.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumber, PhoneNumberFormat


class PhoneNormalisationError(ValueError):
    """Raised when a phone string cannot be canonicalised."""


def _mask_raw(raw: str) -> str:
    """Mask a raw user-supplied phone for error strings.

    Validation errors end up in persistent places (the MAX auth
    snapshot's ``last_error`` lives in a JSON file on disk and is
    surfaced in the web UI), so they must never carry the full
    raw number. We keep only the last four digits as a hint —
    enough for the operator to recognise a typo, useless for
    impersonation.
    """
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) <= 4:
        return f"***{digits}" if digits else "(пусто)"
    return f"***{digits[-4:]}"


@dataclass(frozen=True)
class NormalisedPhone:
    """A phone number in canonical E.164 form plus a masked hint.

    The ``e164`` value is what SHMAX hands to MAX / Telegram;
    the ``hint`` is the last-4-digits form used for logs and
    UI display. Never log or expose ``e164``.
    """

    e164: str  # "+79991234567"
    country_code: int
    national_number: str  # "9991234567"
    hint: str  # "***4567"
    region: str  # "RU" / "DE" / ...

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"NormalisedPhone(hint={self.hint!r}, region={self.region!r})"

    def __str__(self) -> str:
        # Never print the canonical form via repr() — only the
        # masked hint is safe to surface in stack traces.
        return self.hint


_DEFAULT_REGION_PRIORITY: tuple[str, ...] = (
    "RU",
    "BY",
    "KZ",
    "UA",
    "UZ",
    "AM",
    "AZ",
    "GE",
    "MD",
    "IL",
)


def _strip_extension(raw: str) -> tuple[str, str | None]:
    """Strip an extension suffix (``ext. 12``/``доб 12``/``#12``).

    Returns ``(base, ext)``. ``base`` is what we hand to the
    parser; ``ext`` is preserved separately so the wizard can
    surface a friendly error rather than silently discarding it.
    """
    s = raw.strip()
    # English: ext., x, #, extension
    m = re.match(
        r"^(?P<base>.*?)(?:[,;]?\s*(?:ext\.?|x|extension)\s*[-:=]?\s*#?\s*(?P<ext>\d+))\s*$",
        s,
        flags=re.IGNORECASE,
    )
    if m:
        return m.group("base").strip(), m.group("ext")
    # Russian: доб.
    m = re.match(
        r"^(?P<base>.*?)(?:[,;]?\s*доб\.?\s*[-:=]?\s*#?\s*(?P<ext>\d+))\s*$",
        s,
        flags=re.IGNORECASE,
    )
    if m:
        return m.group("base").strip(), m.group("ext")
    return s, None


def parse(
    raw: str,
    *,
    default_region: str = "RU",
    allowed_regions: Iterable[str] | None = None,
) -> NormalisedPhone:
    """Parse a free-form phone string into a NormalisedPhone.

    ``default_region`` is the country assumed for numbers that
    do not include a leading ``+`` — by default ``"RU"``, but
    the wizard's country selector overrides it.

    If ``allowed_regions`` is supplied, the parsed number's
    country code MUST appear in the set; otherwise the parse
    is rejected with a region-not-allowed error. This guards
    against a Russian-default wizard silently accepting a
    German number, which would then reach MAX with a Russian
    formatting expectation.

    Raises ``PhoneNormalisationError`` with an actionable
    message for every rejection path.
    """
    if not raw or not raw.strip():
        raise PhoneNormalisationError("Введите номер телефона")
    base, ext = _strip_extension(raw)
    if ext is not None:
        raise PhoneNormalisationError(
            "Добавочный номер не поддерживается. "
            "Введите только основной номер телефона."
        )
    try:
        parsed: PhoneNumber = phonenumbers.parse(
            base, default_region if not base.strip().startswith("+") else None
        )
    except NumberParseException as exc:
        raise PhoneNormalisationError(
            _error_for_parse_exception(exc, raw=base)
        ) from exc
    if not phonenumbers.is_possible_number(parsed):
        raise PhoneNormalisationError(
            f"Номер не выглядит как реальный телефон ({_mask_raw(base)}). "
            "Проверьте длину и префикс страны."
        )
    if not phonenumbers.is_valid_number(parsed):
        raise PhoneNormalisationError(
            f"Номер не проходит проверку корректности ({_mask_raw(base)})."
        )
    if allowed_regions is not None:
        region = phonenumbers.region_code_for_number(parsed) or ""
        allowed = {r.upper() for r in allowed_regions}
        if region and region.upper() not in allowed:
            raise PhoneNormalisationError(
                f"Страна номера {region!r} не разрешена. "
                f"Разрешённые регионы: {sorted(allowed)}"
            )
    e164 = phonenumbers.format_number(parsed, PhoneNumberFormat.E164)
    region = phonenumbers.region_code_for_number(parsed) or default_region
    national = str(parsed.national_number)
    hint = _last_four_digits(e164)
    return NormalisedPhone(
        e164=e164,
        country_code=parsed.country_code,
        national_number=national,
        hint=hint,
        region=region,
    )


def _last_four_digits(e164: str) -> str:
    digits = re.sub(r"\D", "", e164)
    if len(digits) <= 4:
        return f"***{digits}"
    tail = digits[-4:]
    return f"***{tail}"


def _error_for_parse_exception(exc: NumberParseException, *, raw: str) -> str:
    # P1 #5: the raw user input MUST NOT appear in error text —
    # these messages are persisted in the auth snapshot and shown
    # in the UI. Only the masked form is safe here.
    shown = _mask_raw(raw)
    err = exc.error_type
    if err == NumberParseException.INVALID_COUNTRY_CODE:
        return (
            f"Неизвестный код страны в номере {shown}. "
            "Используйте международный формат с +."
        )
    if err == NumberParseException.NOT_A_NUMBER:
        # 'too short' / 'too long' land here too.
        return (
            f"Не похоже на номер телефона ({shown}). "
            "Проверьте, что вы ввели только цифры и, "
            "при необходимости, префикс страны с +."
        )
    if err == NumberParseException.TOO_SHORT_AFTER_IDD:
        return (
            f"После международного префикса слишком мало цифр ({shown}). "
            "Проверьте номер."
        )
    if err == NumberParseException.TOO_SHORT_NSN:
        return (
            f"Слишком короткий национальный номер ({shown}). "
            "Проверьте длину."
        )
    return f"Не удалось разобрать номер телефона ({shown})."


def format_for_display(hint: str) -> str:
    """Render a ``***1234`` hint back to a friendly display.

    If the hint is already in canonical form (with a leading
    ``+``), keep it as-is. The wizard may pass either the
    masked hint or the raw E.164; this helper normalises.
    """
    if not hint:
        return ""
    if hint.startswith("+"):
        return hint
    return hint


def default_region_for(operator_locale: str | None) -> str:
    """Pick a sensible region default for the wizard selector.

    ``operator_locale`` is the locale string from the browser
    / server config (e.g. ``"ru-RU"``). Returns the first
    region we recognise. Unknown locales fall back to ``"RU"``
    so the most common operator case Just Works.
    """
    if not operator_locale:
        return "RU"
    parts = operator_locale.replace("_", "-").split("-")
    candidate = parts[-1].upper() if parts else ""
    if candidate and candidate in _DEFAULT_REGION_PRIORITY:
        return candidate
    return "RU"


__all__ = [
    "NormalisedPhone",
    "PhoneNormalisationError",
    "default_region_for",
    "format_for_display",
    "parse",
]