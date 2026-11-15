"""Safe, transport-independent failures at the control boundary."""


class ControlError(Exception):
    def __init__(self, code, status, message, *, details=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}
