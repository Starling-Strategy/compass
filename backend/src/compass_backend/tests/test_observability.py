"""Tests for fresh Compass Logfire observability helpers."""

from __future__ import annotations

from pydantic import SecretStr

import compass_backend.observability as observability
from compass_backend.observability import (
    compass_logfire_scrubbing_callback,
    get_current_trace_id,
    logfire_url_for_trace,
    sanitize_logfire_text,
    trace_id_from_span,
)
from compass_backend.config import Settings


def test_sanitize_logfire_text_truncates_and_redacts_secret_like_values() -> None:
    value = "prefix Bearer super-secret-token " + ("x" * 3000)

    sanitized = sanitize_logfire_text(value, max_chars=120)

    assert "Bearer [redacted]" in sanitized
    assert "super-secret-token" not in sanitized
    assert len(sanitized) <= 120
    assert sanitized.endswith("...")


def test_logfire_url_for_trace_uses_project_live_view() -> None:
    assert logfire_url_for_trace("abc123") == (
        "https://logfire-us.pydantic.dev/murmuration/nctqai/live?traceId=abc123"
    )
    assert logfire_url_for_trace(None) is None


def test_get_current_trace_id_returns_none_without_active_span() -> None:
    assert get_current_trace_id() is None


class _TraceContext:
    def __init__(self, trace_id: int, is_valid: bool = True) -> None:
        self.trace_id = trace_id
        self.is_valid = is_valid


class _TraceableSpan:
    def __init__(self, context: _TraceContext) -> None:
        self._context = context

    def get_span_context(self) -> _TraceContext:
        return self._context


class _WrappedTraceableSpan:
    def __init__(self, outer: _TraceContext, inner: _TraceableSpan) -> None:
        self._context = outer
        self._span = inner

    def get_span_context(self) -> _TraceContext:
        return self._context


def test_trace_id_from_span_extracts_logfire_span_context() -> None:
    trace_id = 0x19DDBABF86E85DEC07850E4835F4F18

    assert trace_id_from_span(_TraceableSpan(_TraceContext(trace_id))) == (
        "019ddbabf86e85dec07850e4835f4f18"
    )


def test_trace_id_from_span_checks_wrapped_span_when_outer_context_is_invalid() -> None:
    trace_id = 0x19DDBABF86E85DEC07850E4835F4F18
    span = _WrappedTraceableSpan(
        outer=_TraceContext(0, is_valid=False),
        inner=_TraceableSpan(_TraceContext(trace_id)),
    )

    assert trace_id_from_span(span) == "019ddbabf86e85dec07850e4835f4f18"


def test_logfire_placeholder_token_does_not_enable_observability() -> None:
    settings = Settings(
        logfire_token=SecretStr("pylf_v1_us_your_write_token_here"),
    )

    assert settings.logfire_enabled is False


def test_non_placeholder_logfire_token_enables_observability() -> None:
    settings = Settings(
        logfire_token=SecretStr("pylf_v1_us_real_token_for_tests"),
    )

    assert settings.logfire_enabled is True


class _ScrubMatch:
    def __init__(self, path: tuple[str, ...], value: object) -> None:
        self.path = path
        self.value = value


def test_compass_scrubbing_callback_preserves_safe_trace_keys() -> None:
    match = _ScrubMatch(("attributes", "session_id"), "session-1")

    assert compass_logfire_scrubbing_callback(match) == "session-1"


def test_compass_scrubbing_callback_keeps_default_secret_scrubbing() -> None:
    match = _ScrubMatch(("attributes", "password"), "secret-value")

    assert compass_logfire_scrubbing_callback(match) is None


def test_setup_instrumentation_configures_pydantic_ai_without_content(
    monkeypatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class _FakeLogfire:
        def instrument_pydantic_ai(self, **kwargs: object) -> None:
            calls.append(("pydantic_ai", dict(kwargs)))

        def instrument_asyncpg(self) -> None:
            calls.append(("asyncpg", {}))

        def instrument_httpx(self) -> None:
            calls.append(("httpx", {}))

    monkeypatch.setattr(observability, "LOGFIRE_AVAILABLE", True)
    monkeypatch.setattr(observability, "logfire", _FakeLogfire())
    monkeypatch.setattr(observability, "_logfire_enabled", lambda: True)
    monkeypatch.setattr(observability, "log_info", lambda *args, **kwargs: None)
    monkeypatch.setattr(observability, "log_warn", lambda *args, **kwargs: None)

    observability.setup_instrumentation()

    assert calls[0] == (
        "pydantic_ai",
        {"include_content": False, "include_binary_content": False},
    )
    assert calls[1:] == [("asyncpg", {}), ("httpx", {})]
