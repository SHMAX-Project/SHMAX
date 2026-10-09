"""Media failures carry fixed diagnostic codes, never authenticated URLs."""
class MediaFailure(ValueError):
    def __init__(self, operation: str, reason: str):
        self.operation = operation
        self.reason = reason
        super().__init__(reason)
