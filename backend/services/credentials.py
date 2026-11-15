"""Engine-bound bearer references; cookie/header bytes never enter SQLite."""

from copy import deepcopy
import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import secrets

from backend.services.auth_context import validate_fresh_cookies
from backend.services.control_errors import ControlError

MAX_CONTEXTS = 128
MAX_CONTEXT_BYTES = 65536
MAX_CONTEXT_RECEIPTS = 10000


@dataclass
class Context:
    url: str
    job_id: str | None
    expires_at: float
    cookies: list = field(repr=False)
    user_agent: str | None = field(repr=False)
    referer: str | None = field(repr=False)


class CredentialStore:
    def __init__(self, owner, clock):
        self.owner, self.clock = owner, clock
        self.contexts = {}
        self.receipts = {}
        self.expiry_handle = None

    def _prune(self):
        now = self.clock()
        for reference, context in list(self.contexts.items()):
            if context.expires_at <= now:
                del self.contexts[reference]

    def _expire(self):
        self.expiry_handle = None
        self._prune()
        self._schedule_expiry()

    def _schedule_expiry(self):
        if self.expiry_handle is not None:
            self.expiry_handle.cancel()
            self.expiry_handle = None
        if self.contexts:
            deadline = min(context.expires_at for context in self.contexts.values())
            self.expiry_handle = asyncio.get_running_loop().call_later(max(0, deadline - self.clock()), self._expire)

    def create(self, params, key):
        self._prune()
        if params.instance_id != self.owner.id:
            raise ControlError("AUTH_CONTEXT_EXPIRED", 410, "The engine changed; share a fresh browser context")
        values = params.model_dump()
        encoded = json.dumps(values, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        if len(encoded) > MAX_CONTEXT_BYTES:
            raise ControlError("MESSAGE_TOO_LARGE", 413, "The browser context exceeds its supported size")
        # The fingerprint and key receipt are also volatile. A repeated key is
        # retained after expiration, while its secret-bearing context is erased.
        identity = hashlib.sha256(encoded).digest()
        if key in self.receipts:
            previous, result = self.receipts[key]
            if previous != identity:
                raise ControlError("IDEMPOTENCY_CONFLICT", 409, "This key was used for a different browser context")
            if result["reference"] not in self.contexts:
                raise ControlError("AUTH_CONTEXT_EXPIRED", 410, "Share a fresh browser context with a new key")
            return dict(result)
        try:
            validate_fresh_cookies(params.url, values["cookies"], self.clock())
        except ValueError as error:
            raise ControlError("BAD_REQUEST", 422, "Browser cookies must be fresh and belong to the selected media site") from error
        if len(self.contexts) >= MAX_CONTEXTS or len(self.receipts) >= MAX_CONTEXT_RECEIPTS:
            raise ControlError("CAPACITY_EXCEEDED", 409, "The bounded browser-context capacity has been reached")
        expiry = min([self.clock() + params.ttl_seconds] + [cookie["expirationDate"] for cookie in values["cookies"]
                     if cookie.get("expirationDate")])
        reference = secrets.token_urlsafe(32)
        self.contexts[reference] = Context(params.url, params.job_id, expiry, deepcopy(values["cookies"]),
                                           params.user_agent, params.referer)
        result = {"reference": reference, "instance_id": self.owner.id, "expires_at": expiry}
        self.receipts[key] = identity, dict(result)
        self._schedule_expiry()
        return result

    def resolve(self, reference, url, job_id=None):
        self._prune()
        context = self.contexts.get(reference)
        if context is None:
            raise ControlError("AUTH_CONTEXT_EXPIRED", 410, "Share a fresh browser context before continuing")
        if context.url != url or context.job_id != job_id:
            raise ControlError("AUTH_CONTEXT_SCOPE", 403, "This browser context belongs to different media or a different job")
        try:
            validate_fresh_cookies(url, context.cookies, self.clock())
        except ValueError as error:
            del self.contexts[reference]
            raise ControlError("AUTH_CONTEXT_EXPIRED", 410, "Share fresh browser cookies before continuing") from error
        return {"identity": reference, "cookies": deepcopy(context.cookies), "scope_url": context.url,
                "user_agent": context.user_agent, "referer": context.referer}

    def close(self):
        if self.expiry_handle is not None:
            self.expiry_handle.cancel()
            self.expiry_handle = None
        self.contexts.clear()
        self.receipts.clear()
