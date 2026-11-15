"""Safe, transport-independent failures at the control boundary."""


class ControlError(Exception):
    def __init__(self, code, status, message, *, details=None, retryable=False, retry_at=None, provenance="local"):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}
        self.retryable = retryable
        self.retry_at = retry_at
        self.provenance = provenance
