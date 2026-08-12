"""In-process per-identity rate limiting for the cost-bearing chat routes.

Issue #1079: ``/chat`` and ``/chat/simple`` drive paid LLM gateway calls, so
an unthrottled flood from one identity is a financial-DoS on the gateway bill.
This module provides a fixed-window limiter keyed on the caller's resolved
identity (authenticated key/email, else client IP), plus the small pure
helpers the transport layer uses to derive that identity and to exempt
trusted admin callers.

Scope and caveats (documented, not hidden):

- **Per worker.** Counters live in process memory, so N uvicorn workers each
  limit independently — global throughput is up to N x the configured ceiling.
  Acceptable for a single-container staging deployment; a Postgres/Redis-backed
  shared limiter and a per-key daily budget (via ``api_keys.request_count``)
  are the follow-ups noted on the issue.
- **Per identity.** When API-key auth is on, the identity is the key/email and
  limiting is robust. When auth is off, the identity is the client IP, which
  only distinguishes callers if ``trusted_proxies`` is configured (otherwise
  every request behind the reverse proxy shares the proxy's IP). Enabling auth
  is the stronger control.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Callable

# Prune the per-identity table when it grows past this many live keys so a
# burst of distinct identities (e.g. spoofed/rotating source IPs) can't grow
# memory without bound. Expired windows are dropped on the next prune.
_PRUNE_THRESHOLD = 10_000


@dataclass(frozen=True)
class RateLimitDecision:
    """Outcome of a single limiter check."""

    allowed: bool
    retry_after_seconds: int


@dataclass
class _Window:
    count: int
    reset_at: float


class FixedWindowRateLimiter:
    """At most ``max_requests`` per ``window_seconds`` per identity.

    Synchronous and event-loop-atomic: there is no ``await`` between reading
    and incrementing a window, so concurrent coroutines in one worker cannot
    interleave mid-check. That makes it safe to call from async handlers
    without a lock. ``time_fn`` is injectable so tests can drive the window
    deterministically instead of sleeping.
    """

    def __init__(
        self,
        *,
        max_requests: int,
        window_seconds: float = 60.0,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_requests < 1:
            raise ValueError("max_requests must be >= 1")
        self._max = max_requests
        self._window = float(window_seconds)
        self._time = time_fn
        self._windows: dict[str, _Window] = {}

    def check(self, identity: str) -> RateLimitDecision:
        """Record one request for ``identity`` and report whether to allow it."""

        now = self._time()
        window = self._windows.get(identity)
        if window is None or now >= window.reset_at:
            if len(self._windows) >= _PRUNE_THRESHOLD:
                self._prune(now)
            self._windows[identity] = _Window(count=1, reset_at=now + self._window)
            return RateLimitDecision(allowed=True, retry_after_seconds=0)
        if window.count >= self._max:
            retry_after = max(1, math.ceil(window.reset_at - now))
            return RateLimitDecision(allowed=False, retry_after_seconds=retry_after)
        window.count += 1
        return RateLimitDecision(allowed=True, retry_after_seconds=0)

    def _prune(self, now: float) -> None:
        expired = [key for key, win in self._windows.items() if now >= win.reset_at]
        for key in expired:
            del self._windows[key]


def rate_limit_identity(*, auth_user: Any | None, client_ip_value: str) -> str:
    """Resolve the limiter key for a request.

    Precedence mirrors ``deps.request_identifier``: authenticated email, then
    api-key id, then client IP. Pure (takes the resolved IP rather than a
    ``Request``) so it is unit-testable without a transport object.
    """

    email = getattr(auth_user, "email", None)
    if email:
        return f"user:{email}"
    api_key_id = getattr(auth_user, "api_key_id", None)
    if api_key_id:
        return f"api_key:{api_key_id}"
    return f"ip:{client_ip_value}"


def is_rate_limit_exempt(auth_user: Any | None) -> bool:
    """Admin callers are exempt.

    The limiter exists to cap anonymous and leaked-regular-key floods, not the
    trusted internal traffic that legitimately runs near the gateway ceiling —
    notably the pa-eval sweep harness, which authenticates with an admin key.
    """

    return bool(getattr(auth_user, "is_admin", False))


__all__ = [
    "FixedWindowRateLimiter",
    "RateLimitDecision",
    "is_rate_limit_exempt",
    "rate_limit_identity",
]
