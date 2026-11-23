"""Permanent request validation failures must not repeat unchanged payloads."""


from .media.diagnostics import MediaFailure, PERMANENT_MEDIA_REASONS


class InvalidPayload(ValueError):
    pass


def permanent_delivery_error(exc):
    if isinstance(exc, InvalidPayload):
        return True
    if isinstance(exc, MediaFailure):
        return exc.safe_reason in PERMANENT_MEDIA_REASONS
    from aiogram.exceptions import (
        TelegramBadRequest,
        TelegramForbiddenError,
        TelegramUnauthorizedError,
    )

    return isinstance(
        exc, (TelegramBadRequest, TelegramForbiddenError, TelegramUnauthorizedError)
    )


def safe_delivery_error(exc):
    name = type(exc).__name__
    if isinstance(exc, MediaFailure):
        return "MediaFailure:" + exc.safe_reason
    if isinstance(exc, InvalidPayload):
        codes={'route changed':'route_changed','target disabled':'target_disabled','group grant revoked':'group_grant_revoked',
               'automatic overview disabled':'automatic_overview_disabled','route disabled':'route_disabled',
               'legacy delivery requires review':'legacy_delivery_requires_review',
               'empty outbound payload':'empty_outbound_payload',
               'mirror has no confirmed MAX mapping':'missing_mirror_mapping'}
        return 'InvalidPayload:'+codes.get(str(exc),'invalid_payload')
    from aiogram.exceptions import TelegramBadRequest

    if isinstance(exc, TelegramBadRequest):
        text = str(exc).lower()
        for fragment, code in [
            ("message text is empty", "empty_text"),
            ("chat not found", "chat_not_found"),
            ("message thread not found", "topic_not_found"),
            ("can't parse entities", "invalid_markup"),
        ]:
            if fragment in text:
                return "TelegramBadRequest:" + code
        return "TelegramBadRequest:invalid_request"
    return name
