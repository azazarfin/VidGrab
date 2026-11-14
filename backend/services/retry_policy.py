"""Finite retry classification with representable upstream deadline authority."""

import errno
import math
import random
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

from yt_dlp.networking.exceptions import HTTPError, RequestError, TransportError

MAX_DEADLINE = 253402300799.0
REQUEST_CODES = frozenset({"UPSTREAM_RATE_LIMITED", "UPSTREAM_TRANSIENT", "TRANSPORT_ERROR",
                           "ADMISSION_LIMITED", "REQUEST_BUDGET_EXHAUSTED", "REQUEST_REJECTED", "AUTH_REQUIRED",
                           "UPSTREAM_PERMANENT"})
RETRYABLE_CODES = frozenset({"UPSTREAM_RATE_LIMITED", "UPSTREAM_TRANSIENT", "TRANSPORT_ERROR", "ADMISSION_LIMITED"})


class RequestFailure(RequestError):
    def __init__(self, code, *, retry_at=None, consumes_retry=True):
        if code not in REQUEST_CODES or type(consumes_retry) is not bool:
            raise ValueError("Invalid request failure classification")
        if retry_at is not None and (type(retry_at) not in (int, float) or not math.isfinite(retry_at)
                                     or not 0 <= retry_at <= MAX_DEADLINE):
            raise ValueError("Invalid request retry deadline")
        self.code = code
        self.retry_at = retry_at
        self.consumes_retry = consumes_retry
        super().__init__(code)


def retry_after(value, now):
    if not isinstance(value, str) or len(value) > 128:
        return None
    value = value.strip()
    try:
        deadline = now + int(value) if value.isascii() and value.isdigit() else parsedate_to_datetime(value).timestamp()
        return max(now, deadline) if math.isfinite(deadline) and deadline <= MAX_DEADLINE else None
    except (ValueError, TypeError, OverflowError):
        return None


def backoff(retry_count, jitter=random.random):
    base = min(300.0, 5.0 * 2 ** min(max(retry_count, 0), 6))
    return base * (0.5 + 0.5 * min(1.0, max(0.0, jitter())))


def nested_errors(error):
    pending, seen = [error], set()
    while pending and len(seen) < 32:
        current = pending.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        pending.extend([current.__cause__, current.__context__, getattr(current, "cause", None)])
        info = getattr(current, "exc_info", None)
        if isinstance(info, tuple) and len(info) == 3:
            pending.append(info[1])


@dataclass(frozen=True)
class RetryDecision:
    status: str
    code: str
    retry_count: int
    retry_at: float | None = None


def decide(error, retry_count, max_retries, now, *, jitter=random.random):
    from backend.services.resume import requires_fresh_auth
    if requires_fresh_auth(error):
        return RetryDecision("waiting_for_auth", "AUTH_REQUIRED", retry_count)
    failure = None
    for nested in nested_errors(error):
        if isinstance(nested, RequestFailure):
            failure = nested
            break
        if isinstance(nested, HTTPError):
            if nested.status in (429, 500, 502, 503, 504):
                failure = RequestFailure("UPSTREAM_RATE_LIMITED" if nested.status == 429 else "UPSTREAM_TRANSIENT",
                                         retry_at=retry_after(nested.response.get_header("Retry-After"), now))
            else:
                return RetryDecision("failed", "UPSTREAM_PERMANENT", retry_count)
        elif isinstance(nested, (TimeoutError, ConnectionError, TransportError)) or (
                isinstance(nested, OSError) and nested.errno in
                (errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH)):
            failure = RequestFailure("TRANSPORT_ERROR")
    if failure is not None and failure.code == "AUTH_REQUIRED":
        return RetryDecision("waiting_for_auth", "AUTH_REQUIRED", retry_count)
    if failure is None or failure.code not in RETRYABLE_CODES or (failure.consumes_retry and retry_count >= max_retries):
        return RetryDecision("failed", failure.code if failure else "UNCLASSIFIED_FAILURE", retry_count)
    deadline = max(now + backoff(retry_count, jitter), failure.retry_at or now)
    return RetryDecision("retry_wait", failure.code, retry_count + int(failure.consumes_retry), deadline)
