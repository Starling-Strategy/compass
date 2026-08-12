"""Observability helpers for the Compass API shell."""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

LOGFIRE_BASE_URL = "https://logfire-us.pydantic.dev/murmuration/nctqai"
_MAX_LOGFIRE_TEXT_CHARS = 2_000
_SECRET_PATTERNS = (
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(r"pylf_[A-Za-z0-9_=-]+", re.IGNORECASE),
    re.compile(r"sk-[A-Za-z0-9_-]+", re.IGNORECASE),
    re.compile(r"(?i)(password|token|api[_-]?key|secret)\s*[:=]\s*[^,\s]+"),
)
_SAFE_SCRUBBED_ATTRIBUTE_KEYS = {
    "auth_user_is_admin",
    "auth_user_present",
    "requested_session_id",
    "session_id",
    "snapshot_id",
    "trace_id",
}

try:
    import logfire

    LOGFIRE_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on optional install extras
    logfire = None  # type: ignore[assignment]
    LOGFIRE_AVAILABLE = False


def _logfire_enabled() -> bool:
    try:
        from compass_backend.config import settings

        return settings.logfire_enabled
    except Exception:
        return False


def log_event(event: str, level: str = "info", **kwargs: Any) -> None:
    """Log structured events through Logfire when configured."""

    exc_info = kwargs.pop("_exc_info", False)
    if LOGFIRE_AVAILABLE and _logfire_enabled():
        log_fn = getattr(logfire, level, logfire.info)
        log_fn(event, **kwargs)
        return

    stdlib_level = "warning" if level == "warn" else level
    log_fn = getattr(logger, stdlib_level, logger.info)
    if kwargs:
        log_fn("%s: %s", event, kwargs, exc_info=exc_info)
    else:
        log_fn(event, exc_info=exc_info)


def log_debug(event: str, **kwargs: Any) -> None:
    """Log a debug-level event."""

    log_event(event, level="debug", **kwargs)


def log_info(event: str, **kwargs: Any) -> None:
    """Log an info-level event."""

    log_event(event, level="info", **kwargs)


def log_warn(event: str, **kwargs: Any) -> None:
    """Log a warning-level event."""

    log_event(event, level="warn", **kwargs)


def log_error(event: str, **kwargs: Any) -> None:
    """Log an error-level event."""

    log_event(event, level="error", **kwargs)


def compass_logfire_scrubbing_callback(match: Any) -> object | None:
    """Allow safe Compass trace identifiers through Logfire's scrubber."""

    path = getattr(match, "path", ())
    if (
        len(path) == 2
        and path[0] == "attributes"
        and path[1] in _SAFE_SCRUBBED_ATTRIBUTE_KEYS
    ):
        return getattr(match, "value", None)
    return None


def sanitize_logfire_text(
    value: object,
    *,
    max_chars: int = _MAX_LOGFIRE_TEXT_CHARS,
) -> str:
    """Return a bounded, scrubbed string safe to attach to Logfire spans."""

    text = value if isinstance(value, str) else repr(value)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_redacted_match, text)
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return "." * max_chars
    return text[: max_chars - 3] + "..."


def _redacted_match(match: re.Match[str]) -> str:
    value = match.group(0)
    if value.lower().startswith("bearer "):
        return "Bearer [redacted]"
    if "=" in value:
        key = value.split("=", 1)[0]
        return f"{key}=[redacted]"
    if ":" in value:
        key = value.split(":", 1)[0]
        return f"{key}: [redacted]"
    if value.lower().startswith("pylf_"):
        return "pylf_[redacted]"
    if value.lower().startswith("sk-"):
        return "sk-[redacted]"
    return "[redacted]"


def logfire_url_for_trace(trace_id: str | None) -> str | None:
    """Return the Compass Logfire live-view URL for a trace ID."""

    if not trace_id:
        return None
    return f"{LOGFIRE_BASE_URL}/live?traceId={trace_id}"


def get_current_trace_id() -> str | None:
    """Return the current OpenTelemetry trace ID when a span is active."""

    try:
        from opentelemetry import trace

        return trace_id_from_span(trace.get_current_span())
    except Exception:
        return None


def trace_id_from_span(span: object | None) -> str | None:
    """Return a trace ID from a span-like object when one is available."""

    for context in _span_contexts(span, seen=set()):
        trace_id = _trace_id_from_context(context)
        if trace_id is not None:
            return trace_id
    return None


def _trace_id_from_context(context: object | None) -> str | None:
    if context is None or not getattr(context, "is_valid", False):
        return None
    trace_id = getattr(context, "trace_id", None)
    if not isinstance(trace_id, int) or trace_id <= 0:
        return None
    return format(trace_id, "032x")


def _span_contexts(span: object | None, *, seen: set[int]) -> list[object]:
    if span is None:
        return []
    span_id = id(span)
    if span_id in seen:
        return []
    seen.add(span_id)
    contexts: list[object] = []
    getter = getattr(span, "get_span_context", None)
    if callable(getter):
        try:
            context = getter()
        except Exception:
            context = None
        if context is not None:
            contexts.append(context)
    context = getattr(span, "context", None)
    if context is not None:
        contexts.append(context)
    for attr_name in ("_span", "span"):
        inner_span = getattr(span, attr_name, None)
        if inner_span is not None and inner_span is not span:
            contexts.extend(_span_contexts(inner_span, seen=seen))
    return contexts


def compass_span(name: str, **attributes: Any):
    """Return a Logfire span context manager or a no-op replacement."""

    if not LOGFIRE_AVAILABLE or not _logfire_enabled():
        return _NullSpan()
    try:
        return logfire.span(name, **_span_attributes(attributes))
    except Exception as exc:  # pragma: no cover - defensive telemetry path
        logger.warning("Logfire span creation failed: %s", exc)
        return _NullSpan()


def compass_turn_span(**attributes: Any):
    """Return the root span context manager for one Compass chat turn."""

    return compass_span("compass.turn", **attributes)


def set_span_attributes(span: object, **attributes: Any) -> None:
    """Attach attributes to a span, swallowing telemetry-only failures."""

    setter = getattr(span, "set_attribute", None)
    if setter is None:
        return
    for key, value in _span_attributes(attributes).items():
        try:
            setter(key, value)
        except Exception:
            continue


async def flush_on_error(timeout: float = 2.0) -> None:
    """Best-effort Logfire flush for fast-failing chat paths."""

    if not LOGFIRE_AVAILABLE or not _logfire_enabled():
        return
    try:
        force_flush = getattr(logfire, "force_flush", None)
        if force_flush is None:
            return
        result = force_flush(timeout_millis=int(timeout * 1000))
        if hasattr(result, "__await__"):
            await result
    except Exception:
        return


def _span_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in attributes.items():
        if value is None:
            continue
        if isinstance(value, str):
            result[key] = sanitize_logfire_text(value)
        elif isinstance(value, (int, float, bool)):
            result[key] = value
        elif isinstance(value, (list, tuple, set)):
            result[key] = [sanitize_logfire_text(item) for item in value]
        else:
            result[key] = sanitize_logfire_text(value)
    return result


class _NullSpan:
    """No-op span with the same narrow surface used by chat tracing."""

    def __enter__(self) -> "_NullSpan":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False

    def set_attribute(self, key: str, value: object) -> None:
        return None


def setup_instrumentation() -> None:
    """Install library instrumentation once Logfire is configured."""

    if not LOGFIRE_AVAILABLE or not _logfire_enabled():
        return

    for name, instrument, kwargs in (
        (
            "pydantic_ai",
            logfire.instrument_pydantic_ai,
            {"include_content": False, "include_binary_content": False},
        ),
        ("asyncpg", logfire.instrument_asyncpg, {}),
        ("httpx", logfire.instrument_httpx, {}),
    ):
        try:
            instrument(**kwargs)
            log_info("Logfire instrumentation enabled", library=name)
        except Exception as exc:  # pragma: no cover - defensive observability path
            log_warn("Logfire instrumentation failed", library=name, error=str(exc))


def setup_logfire() -> None:
    """Configure Logfire for the API process when a token is present."""

    if not LOGFIRE_AVAILABLE:
        logger.info("Logfire not installed; observability disabled")
        return

    from compass_backend.config import settings

    if not settings.logfire_enabled:
        logger.info("Logfire token not configured; observability disabled")
        return

    logfire.configure(
        token=settings.logfire_token.get_secret_value() if settings.logfire_token else None,
        service_name=settings.logfire_project,
        environment=settings.environment,
        scrubbing=logfire.ScrubbingOptions(
            callback=compass_logfire_scrubbing_callback,
        ),
        console=(
            logfire.ConsoleOptions(
                verbose=True,
                include_timestamps=True,
            )
            if settings.environment == "development"
            else False
        ),
    )
    setup_instrumentation()
    log_info(
        "Logfire enabled",
        project=settings.logfire_project,
        environment=settings.environment,
    )
