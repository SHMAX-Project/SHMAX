"""Media failures carry fixed diagnostic codes, never authenticated URLs."""
class MediaFailure(ValueError):
    def __init__(self, operation: str, reason: str):
        self.operation = operation
        self.reason = reason
        super().__init__(reason)


class MediaGroupRejected(MediaFailure):
    """Explicit Bot API rejection permits deterministic single-item fallback.

    This is never used for network errors or ambiguous timeouts.
    """
