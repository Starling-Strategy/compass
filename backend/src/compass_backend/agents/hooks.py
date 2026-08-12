"""Shared Pydantic AI ``Hooks`` factory for Compass agents.

Single source of diagnostic emission for the recognition spine. One factory call
per agent; each call returns a fresh ``Hooks`` instance with ``agent_name`` baked
into closures so handlers don't need to introspect ``ctx.agent.name``.

Emits per agent run a Logfire span ``compass.agent.run`` with structured
attributes. Lifts ``gen_ai.usage.details.cache_read_tokens`` /
``cache_write_tokens`` into the span attributes so the Accuracy Framework
Worksheet (SSN-205) Speed dimension can compute cache hit rate without
per-agent JSON parsing.

Per-run state lives in a ``ContextVar``; each asyncio task receives its own
copy-on-write view, so ``asyncio.gather`` over multiple agent runs cannot
collide. We do not store a reset ``Token`` because pydantic_ai's ``wrap_run``
runs ``before_run`` inside an inner ``asyncio.create_task`` while ``after_run``
runs in the outer task — Tokens minted in the inner context cannot be reset
from the outer one. Instead we simply ``.set(None)`` to clear the slot on
completion; the outer task's view is already independent of ours.
"""
from __future__ import annotations

import asyncio
import os
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import logfire
from pydantic_ai import RunContext
from pydantic_ai.capabilities.hooks import Hooks

from compass_backend.observability import compass_span
from compass_backend.planning.thinking import is_unsupported_thinking_error


@dataclass
class _RunDiagnostics:
    agent_name: str
    attempts: int = 0
    retry_count: int = 0
    had_model_request_error: bool = False
    had_unsupported_thinking: bool = False
    had_run_error: bool = False
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    model_actual: str | None = None
    provider_name: str | None = None
    finish_reason: str | None = None
    error_class: str | None = None
    error_category: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


_current: ContextVar[_RunDiagnostics | None] = ContextVar(
    "compass_agent_run_diagnostics", default=None
)

# In-process retry budget for gateway throttles classified as ``rate_limit``.
# The Pydantic AI Gateway throttles at ~80 sonnet req/min and signals it with a
# 404 "Route not found: anthropic" (issue #1127). Retrying here — inside the
# model-request wrapper, shared by every Compass agent — keeps the throttle from
# wrapping to an HTTP 500 that pa-eval's runner cannot retry (it sees status 500,
# not 404). ``_RATE_LIMIT_MAX_ATTEMPTS`` counts the initial call plus retries, so
# the default of 5 means up to 4 re-issues. Bounded exponential backoff caps at
# ``_RATE_LIMIT_RETRY_MAX_SECONDS``; on exhaustion the original error re-raises so
# a throttle that never clears stays visible.
_RATE_LIMIT_MAX_ATTEMPTS = int(os.environ.get("COMPASS_AGENT_RATE_LIMIT_MAX_ATTEMPTS", "5"))
_RATE_LIMIT_RETRY_BASE_SECONDS = float(
    os.environ.get("COMPASS_AGENT_RATE_LIMIT_RETRY_BASE_SECONDS", "2")
)
_RATE_LIMIT_RETRY_MAX_SECONDS = float(
    os.environ.get("COMPASS_AGENT_RATE_LIMIT_RETRY_MAX_SECONDS", "30")
)


def _add(target: int | None, delta: int | None) -> int | None:
    if delta is None:
        return target
    return (target or 0) + delta


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_diagnostic_hooks(agent_name: str) -> Hooks[Any]:
    """Return a fresh ``Hooks`` instance for one Compass agent.

    The returned capability emits a ``compass.agent.run`` Logfire span at the
    end of every agent run, with structured attributes covering attempts,
    retries, model-request errors, unsupported-thinking signals, and token /
    cache-token usage. Per-run state is tracked in a ``ContextVar`` so that
    concurrent runs (e.g. ``asyncio.gather``) do not collide.
    """
    hooks: Hooks[Any] = Hooks()

    @hooks.on.before_run
    async def _before_run(ctx: RunContext[Any]) -> None:
        _current.set(_RunDiagnostics(agent_name=agent_name))

    @hooks.on.model_request
    async def _retry_rate_limited_request(
        ctx: RunContext[Any], *, request_context: Any, handler: Any
    ) -> Any:
        """Retry a gateway-throttled model request in-process.

        Wraps the actual model call. When the call raises an error classified as
        ``rate_limit`` (including the gateway's 404 "Route not found: anthropic"
        throttle), sleeps with bounded exponential backoff and re-issues the
        request, up to ``_RATE_LIMIT_MAX_ATTEMPTS`` total. Any other error — and a
        throttle that survives the budget — re-raises unchanged, so non-transient
        failures stay visible. ``_after_model_request`` / ``_model_request_error``
        still see the final attempt's response or error for observability.
        """
        max_attempts = max(1, _RATE_LIMIT_MAX_ATTEMPTS)
        for attempt in range(1, max_attempts + 1):
            try:
                return await handler(request_context)
            except Exception as error:  # noqa: BLE001 — re-raised below unless retryable
                if attempt >= max_attempts or _classify_error(error) != "rate_limit":
                    raise
                # The error is caught here, so pydantic_ai's on_model_request_error
                # hook never fires for the retried attempt. Record the throttle on
                # the run diagnostics directly so a silently-retried 404 still shows
                # up as a model-request error on compass.agent.run.
                diagnostics = _current.get()
                if diagnostics is not None:
                    diagnostics.had_model_request_error = True
                    diagnostics.error_class = type(error).__name__
                    diagnostics.error_category = "rate_limit"
                delay = min(
                    _RATE_LIMIT_RETRY_BASE_SECONDS * (2 ** (attempt - 1)),
                    _RATE_LIMIT_RETRY_MAX_SECONDS,
                )
                logfire.warn(
                    "compass.agent.model_request.rate_limit_retry",
                    agent_name=agent_name,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    delay_seconds=delay,
                    error=str(error),
                )
                if delay > 0:
                    await asyncio.sleep(delay)
        # Unreachable: the loop either returns or raises on the final attempt.
        raise RuntimeError("rate-limit retry loop exhausted unexpectedly")

    @hooks.on.before_model_request
    async def _before_model_request(ctx: RunContext[Any], request_context: Any, /) -> Any:
        diagnostics = _current.get()
        if diagnostics is not None:
            diagnostics.attempts += 1
            if diagnostics.attempts > 1:
                diagnostics.retry_count += 1
        return request_context

    @hooks.on.after_model_request
    async def _after_model_request(
        ctx: RunContext[Any], *, request_context: Any, response: Any
    ) -> Any:
        diagnostics = _current.get()
        if diagnostics is None:
            return response

        # Response-level metadata (last value wins across retries; this is
        # intentional — the final attempt's model/provider/finish_reason is
        # what produced the run's output).
        model_actual = getattr(response, "model_name", None)
        if model_actual:
            diagnostics.model_actual = str(model_actual)
        provider_name = getattr(response, "provider_name", None)
        if provider_name:
            diagnostics.provider_name = str(provider_name)
        finish_reason = getattr(response, "finish_reason", None)
        if finish_reason:
            diagnostics.finish_reason = str(finish_reason)

        usage = getattr(response, "usage", None)
        if usage is None:
            return response
        # NOTE: usage.details is typed dict[str, int] in pydantic-ai 1.x. The
        # Logfire Gateway returns usage.pydantic_ai_gateway.cost_estimate (a
        # float USD value) per request, but the upstream Anthropic / OpenAI
        # SDKs strip the gateway's nested field when parsing into their typed
        # response dataclasses — so cost is lost before it reaches us. Fixing
        # this needs an upstream pydantic-ai change to preserve gateway
        # metadata in ModelResponse.metadata or ModelResponse.provider_details.
        # Tracked in the Tier 2.5 audit followup.
        details = getattr(usage, "details", None) or {}
        diagnostics.cache_read_tokens = _add(
            diagnostics.cache_read_tokens, _safe_int(details.get("cache_read_tokens"))
        )
        diagnostics.cache_write_tokens = _add(
            diagnostics.cache_write_tokens, _safe_int(details.get("cache_write_tokens"))
        )
        diagnostics.input_tokens = _add(
            diagnostics.input_tokens, _safe_int(getattr(usage, "input_tokens", None))
        )
        diagnostics.output_tokens = _add(
            diagnostics.output_tokens, _safe_int(getattr(usage, "output_tokens", None))
        )
        return response

    @hooks.on.model_request_error
    async def _model_request_error(
        ctx: RunContext[Any], *, request_context: Any, error: Exception
    ) -> Any:
        diagnostics = _current.get()
        is_thinking = is_unsupported_thinking_error(error)
        error_class = type(error).__name__
        error_category = _classify_error(error)
        if diagnostics is not None:
            diagnostics.had_model_request_error = True
            if is_thinking:
                diagnostics.had_unsupported_thinking = True
            diagnostics.error_class = error_class
            diagnostics.error_category = error_category
        logfire.info(
            "compass.agent.model_request.error",
            agent_name=agent_name,
            attempt=(diagnostics.attempts if diagnostics else 0),
            is_unsupported_thinking=is_thinking,
            error_class=error_class,
            error_category=error_category,
            error=str(error),
        )
        # Re-raise so other on_model_request_error handlers (and ultimately the
        # framework) still see the error. Returning a ModelResponse here would
        # mask the failure.
        raise error

    @hooks.on.run_error
    async def _run_error(ctx: RunContext[Any], *, error: BaseException) -> Any:
        diagnostics = _current.get()
        if diagnostics is not None:
            diagnostics.had_run_error = True
        # Emit the run span on the error path too so failed runs are observable.
        _emit_run_span(diagnostics)
        _release_token()
        raise error

    @hooks.on.after_run
    async def _after_run(ctx: RunContext[Any], *, result: Any) -> Any:
        diagnostics = _current.get()
        _emit_run_span(diagnostics)
        _release_token()
        return result

    return hooks


def _cache_hit_rate(
    cache_read: int | None, input_fresh: int | None
) -> float | None:
    """Return cached fraction of input tokens (0.0–1.0), or None when unknown.

    cache_read_tokens is the input portion served from prompt cache.
    input_tokens is the fresh (non-cached) input. Their sum is what the model
    actually read in. Reporting the ratio lets operators see cache effectiveness
    at a glance without joining the raw token columns.
    """
    if cache_read is None and input_fresh is None:
        return None
    total = (cache_read or 0) + (input_fresh or 0)
    if total <= 0:
        return None
    return round((cache_read or 0) / total, 4)


# "route not found" is the Pydantic AI Gateway's throttle signal: above
# ~80 sonnet req/min it returns HTTP 404 "Route not found: anthropic" instead
# of a 429 (issue #1127). Treat it as rate-limit so the in-process retry below
# engages instead of bucketing it as an opaque routing error.
_RATE_LIMIT_KEYWORDS = (
    "rate limit",
    "rate-limit",
    "429",
    "too many requests",
    "quota",
    "route not found: anthropic",
    "route not found",
)
_TIMEOUT_KEYWORDS = ("timeout", "timed out", "deadline exceeded")
_AUTH_KEYWORDS = ("401", "unauthorized", "auth", "key not found", "invalid api key")
_UNAVAILABLE_KEYWORDS = ("503", "service unavailable", "overloaded", "upstream")


def _classify_error(error: BaseException) -> str:
    """Return a coarse category for a model-request error.

    The categories are intentionally limited so they're useful as a filterable
    span attribute. Uses class name first (exact classes for known cases) then
    falls back to message-keyword matching. Returns 'other' when no signal.
    """
    class_name = type(error).__name__
    # Class-name matches first (cheap, exact)
    if class_name in ("RateLimitError", "TooManyRequests"):
        return "rate_limit"
    if class_name in ("TimeoutError", "ReadTimeout", "WriteTimeout", "ConnectTimeout"):
        return "timeout"
    if class_name in ("AuthenticationError", "PermissionDeniedError"):
        return "auth"
    if class_name in ("ServiceUnavailableError", "InternalServerError", "BadGatewayError"):
        return "service_unavailable"
    # Fall back to message keywords (case-insensitive)
    message = str(error).lower()
    if any(keyword in message for keyword in _RATE_LIMIT_KEYWORDS):
        return "rate_limit"
    if any(keyword in message for keyword in _TIMEOUT_KEYWORDS):
        return "timeout"
    if any(keyword in message for keyword in _AUTH_KEYWORDS):
        return "auth"
    if any(keyword in message for keyword in _UNAVAILABLE_KEYWORDS):
        return "service_unavailable"
    return "other"


def _emit_run_span(diagnostics: _RunDiagnostics | None) -> None:
    if diagnostics is None:
        return
    with compass_span(
        "compass.agent.run",
        agent_name=diagnostics.agent_name,
        attempts=diagnostics.attempts,
        retry_count=diagnostics.retry_count,
        had_model_request_error=diagnostics.had_model_request_error,
        had_unsupported_thinking=diagnostics.had_unsupported_thinking,
        had_run_error=diagnostics.had_run_error,
        cache_read_tokens=diagnostics.cache_read_tokens,
        cache_write_tokens=diagnostics.cache_write_tokens,
        input_tokens=diagnostics.input_tokens,
        output_tokens=diagnostics.output_tokens,
        cache_hit_rate=_cache_hit_rate(
            diagnostics.cache_read_tokens, diagnostics.input_tokens
        ),
        model_actual=diagnostics.model_actual,
        provider_name=diagnostics.provider_name,
        finish_reason=diagnostics.finish_reason,
        error_class=diagnostics.error_class,
        error_category=diagnostics.error_category,
    ):
        pass


def _release_token() -> None:
    _current.set(None)
