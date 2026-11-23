"""Media failures carry fixed diagnostic codes, never authenticated URLs."""

PERMANENT_MEDIA_REASONS = frozenset({
    "external_video", "file_too_large", "invalid_file_id",
    "invalid_image", "missing_media_reference", "preview_unavailable",
    "request_rejected", "untrusted_media_url",
})
MEDIA_REASONS = PERMANENT_MEDIA_REASONS | {
    "api_rejected", "bot_not_connected", "conversion_timeout", "empty_download",
    "http_response_failed", "media_gone",
    "invalid_file_path", "media_url_unavailable", "photo_reference_unavailable",
    "rate_limited", "request_failed", "transport_failed", "unexpected_failure",
}


class MediaFailure(ValueError):
    def __init__(self, operation: str, reason: str):
        self.operation = operation
        self.reason = reason
        super().__init__(reason)

    @property
    def safe_reason(self) -> str:
        return self.reason if self.reason in MEDIA_REASONS else "unexpected_failure"



class MediaGroupRejected(MediaFailure):
    """Explicit Bot API rejection permits deterministic single-item fallback.

    This is never used for network errors or ambiguous timeouts.
    """
