"""Tests for the shared diagnostic Hooks factory.

These exercise the hook *contract* with a throwaway agent + TestModel/FunctionModel,
not the live Compass agents. They lock in: which Logfire spans fire, with which
attributes, in which order, for each of the four hook-relevant scenarios:
ModelRetry, unsupported-thinking error, clean run, and cache-token capture.
"""
from __future__ import annotations

from typing import Any

import logfire
import pytest
from pydantic import BaseModel, SecretStr
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RequestUsage

from compass_backend.agents.hooks import (
    build_diagnostic_hooks,
)
from compass_backend.config import settings
from compass_backend.tests.pydantic_ai_helpers import output_tool_response


@pytest.fixture(autouse=True)
def _enable_logfire_for_hook_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``settings.logfire_enabled`` True so ``compass_span`` emits.

    ``compass_span`` (in ``compass_backend/api/observability.py``) returns a
    ``_NullSpan`` when ``_logfire_enabled()`` is False (no production
    ``LOGFIRE_TOKEN``). The ``capfire`` fixture from ``logfire.testing``
    initializes the Logfire SDK with an in-memory exporter — but spans only
    reach that exporter if ``compass_span`` actually calls ``logfire.span``.
    Setting a non-placeholder token here makes ``logfire_enabled`` True for
    the test session without touching production code.
    """

    monkeypatch.setattr(
        settings,
        "logfire_token",
        SecretStr("pylf_v1_us_real_token_for_hook_tests"),
    )


class _Echo(BaseModel):
    text: str


def _build_test_agent(*, agent_name: str, model: Any) -> Agent[None, _Echo]:
    return Agent(
        model,
        output_type=_Echo,
        capabilities=[build_diagnostic_hooks(agent_name)],
    )


@pytest.mark.asyncio
async def test_hooks_observe_model_retry(capfire: logfire.testing.CaptureLogfire) -> None:
    """ModelRetry from output_validator must increment retry_count via hooks."""
    seen_attempts: list[int] = []

    test_model = TestModel(custom_output_args={"text": "ok"})
    agent = _build_test_agent(agent_name="probe", model=test_model)

    @agent.output_validator
    def _first_call_rejects(output: _Echo) -> _Echo:
        seen_attempts.append(len(seen_attempts) + 1)
        if len(seen_attempts) == 1:
            raise ModelRetry("force retry on first attempt")
        return output

    result = await agent.run("ping")
    assert result.output.text == "ok"
    assert len(seen_attempts) == 2

    run_spans = [
        s for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "compass.agent.run"
    ]
    assert len(run_spans) == 1
    attrs = run_spans[0]["attributes"]
    assert attrs["agent_name"] == "probe"
    assert attrs["attempts"] == 2
    assert attrs["retry_count"] == 1
    assert attrs["had_model_request_error"] is False


@pytest.mark.asyncio
async def test_hooks_observe_unsupported_thinking_error(
    capfire: logfire.testing.CaptureLogfire,
) -> None:
    """A provider's unsupported-thinking error must be flagged in the hook event."""

    class _FakeThinkingError(Exception):
        pass

    async def _function(messages: list[Any], info: AgentInfo) -> Any:
        raise _FakeThinkingError("thinking parameter is not supported")

    agent = _build_test_agent(agent_name="probe", model=FunctionModel(_function))

    with pytest.raises(_FakeThinkingError):
        await agent.run("ping")

    error_events = [
        s for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "compass.agent.model_request.error"
    ]
    assert len(error_events) >= 1
    attrs = error_events[0]["attributes"]
    assert attrs["agent_name"] == "probe"
    assert attrs["is_unsupported_thinking"] is True


@pytest.mark.asyncio
async def test_hooks_clean_run_records_zero_retries(
    capfire: logfire.testing.CaptureLogfire,
) -> None:
    """A successful first-try run should record attempts=1, retry_count=0."""
    agent = _build_test_agent(
        agent_name="probe",
        model=TestModel(custom_output_args={"text": "ok"}),
    )
    result = await agent.run("ping")
    assert result.output.text == "ok"

    run_spans = [
        s for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "compass.agent.run"
    ]
    assert len(run_spans) == 1
    attrs = run_spans[0]["attributes"]
    assert attrs["attempts"] == 1
    assert attrs["retry_count"] == 0


@pytest.mark.asyncio
async def test_hooks_capture_cache_read_tokens(
    capfire: logfire.testing.CaptureLogfire,
) -> None:
    """Cache-read / cache-write token usage must be lifted to compass.agent.run attrs.

    The hook reads ``usage.details["cache_read_tokens"]`` /
    ``usage.details["cache_write_tokens"]`` (the gateway/pydantic-ai 1.82+ key)
    and accumulates via ``_add``. ``None`` means "missing"; ``0`` means
    "zero, observed" — so an explicit ``cache_write_tokens=0`` (perfect cache
    hit, no write) must surface as ``0`` on the span, not be stripped.
    """

    async def _function(messages: list[Any], info: AgentInfo) -> ModelResponse:
        return output_tool_response(
            info,
            {"text": "ok"},
            usage=RequestUsage(
                input_tokens=10,
                output_tokens=5,
                details={"cache_read_tokens": 1024, "cache_write_tokens": 0},
            ),
        )

    agent = _build_test_agent(agent_name="probe", model=FunctionModel(_function))
    result = await agent.run("ping")
    assert result.output.text == "ok"

    run_spans = [
        s for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "compass.agent.run"
    ]
    assert len(run_spans) == 1
    attrs = run_spans[0]["attributes"]
    assert attrs["cache_read_tokens"] == 1024
    assert attrs["cache_write_tokens"] == 0
    assert attrs["input_tokens"] == 10
    assert attrs["output_tokens"] == 5


# ---------------------------------------------------------------------------
# Tier 2.5 audit additions: response metadata + cache hit rate + error classes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hooks_capture_response_model_provider_and_finish_reason(
    capfire: logfire.testing.CaptureLogfire,
) -> None:
    """compass.agent.run carries model_actual + provider_name + finish_reason from ModelResponse.

    Note: FunctionModel overwrites response.model_name with its own _model_name on
    every request (function.py:153). We pass model_name="claude-sonnet-4-6" to the
    FunctionModel constructor to control what that override sets. provider_name and
    finish_reason are NOT overwritten by FunctionModel, so they survive from the
    ModelResponse we construct.
    """

    async def _function(messages: list[Any], info: AgentInfo) -> ModelResponse:
        return output_tool_response(
            info,
            {"text": "ok"},
            usage=RequestUsage(input_tokens=8, output_tokens=4),
            provider_name="anthropic",
            finish_reason="stop",
        )

    agent = _build_test_agent(
        agent_name="probe",
        model=FunctionModel(_function, model_name="claude-sonnet-4-6"),
    )
    result = await agent.run("ping")
    assert result.output.text == "ok"

    run_spans = [
        s for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "compass.agent.run"
    ]
    assert len(run_spans) == 1
    attrs = run_spans[0]["attributes"]
    assert attrs["model_actual"] == "claude-sonnet-4-6"
    assert attrs["provider_name"] == "anthropic"
    assert attrs["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_hooks_compute_cache_hit_rate_from_token_counts(
    capfire: logfire.testing.CaptureLogfire,
) -> None:
    """cache_hit_rate is (cache_read / (cache_read + input_fresh)) rounded to 4 dp."""

    async def _function(messages: list[Any], info: AgentInfo) -> ModelResponse:
        return output_tool_response(
            info,
            {"text": "ok"},
            usage=RequestUsage(
                input_tokens=200,
                output_tokens=10,
                details={"cache_read_tokens": 800, "cache_write_tokens": 0},
            ),
        )

    agent = _build_test_agent(agent_name="probe", model=FunctionModel(_function))
    await agent.run("ping")

    run_spans = [
        s for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "compass.agent.run"
    ]
    attrs = run_spans[0]["attributes"]
    # 800 cached out of (800 + 200) total input = 0.8
    assert attrs["cache_hit_rate"] == 0.8


@pytest.mark.asyncio
async def test_hooks_classify_rate_limit_error_by_message_keyword(
    capfire: logfire.testing.CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Errors mentioning 'rate limit' are categorized as error_category='rate_limit'.

    A rate-limit error now drives the in-process retry budget before surfacing
    (issue #1127); zero the backoff so the budget exhausts without real sleeping.
    """
    import compass_backend.agents.hooks as hooks_mod

    monkeypatch.setattr(hooks_mod, "_RATE_LIMIT_RETRY_BASE_SECONDS", 0.0)

    class _ProviderHttpError(Exception):
        """Stand-in for an upstream provider's HTTP error class."""
        pass

    async def _function(messages: list[Any], info: AgentInfo) -> Any:
        raise _ProviderHttpError("Anthropic API rate limit exceeded (429)")

    agent = _build_test_agent(agent_name="probe", model=FunctionModel(_function))

    with pytest.raises(_ProviderHttpError):
        await agent.run("ping")

    # The model_request.error event carries the classification eagerly.
    error_events = [
        s for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "compass.agent.model_request.error"
    ]
    assert len(error_events) >= 1
    assert error_events[0]["attributes"]["error_class"] == "_ProviderHttpError"
    assert error_events[0]["attributes"]["error_category"] == "rate_limit"

    # The run span (emitted on the error path via _run_error) carries it too.
    run_spans = [
        s for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "compass.agent.run"
    ]
    assert len(run_spans) == 1
    run_attrs = run_spans[0]["attributes"]
    assert run_attrs["had_model_request_error"] is True
    assert run_attrs["error_class"] == "_ProviderHttpError"
    assert run_attrs["error_category"] == "rate_limit"


def test_classify_error_categorizes_known_classes_directly() -> None:
    """_classify_error matches well-known SDK exception class names before message keywords."""
    from compass_backend.agents.hooks import _classify_error

    class RateLimitError(Exception):
        pass

    class TimeoutError(Exception):  # noqa: A001 — shadowing builtin on purpose for the test
        pass

    class AuthenticationError(Exception):
        pass

    class ServiceUnavailableError(Exception):
        pass

    assert _classify_error(RateLimitError("anything")) == "rate_limit"
    assert _classify_error(TimeoutError("anything")) == "timeout"
    assert _classify_error(AuthenticationError("anything")) == "auth"
    assert _classify_error(ServiceUnavailableError("anything")) == "service_unavailable"
    # Unknown class with no signal in message → 'other'
    assert _classify_error(ValueError("nothing interesting")) == "other"


def test_classify_gateway_404_route_not_found_as_rate_limit() -> None:
    """The Pydantic AI Gateway returns HTTP 404 'Route not found: anthropic' as a
    throttle (above ~80 sonnet req/min) instead of a 429. It must classify as a
    rate_limit, not 'other', so the in-process retry path engages (issue #1127).
    """
    from compass_backend.agents.hooks import _classify_error

    class _GatewayHttpError(Exception):
        pass

    assert (
        _classify_error(
            _GatewayHttpError("status_code: 404, body: Route not found: anthropic")
        )
        == "rate_limit"
    )
    # The bare 'Route not found' phrasing must also bucket as rate_limit.
    assert _classify_error(_GatewayHttpError("Route not found")) == "rate_limit"


@pytest.mark.asyncio
async def test_hooks_retry_gateway_throttle_in_process(
    capfire: logfire.testing.CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gateway 404 throttle is retried in-process and never surfaces to the caller.

    The first model request raises the gateway throttle (404 'Route not found:
    anthropic'); the second succeeds. The agent run must complete with the valid
    output — the throttle must not propagate out as an exception (issue #1127).
    """
    import compass_backend.agents.hooks as hooks_mod

    # Make the backoff instant so the test does not actually sleep.
    monkeypatch.setattr(hooks_mod, "_RATE_LIMIT_RETRY_BASE_SECONDS", 0.0)

    calls: list[int] = []

    class _GatewayHttpError(Exception):
        pass

    async def _function(messages: list[Any], info: AgentInfo) -> ModelResponse:
        calls.append(1)
        if len(calls) == 1:
            raise _GatewayHttpError(
                "status_code: 404, body: Route not found: anthropic"
            )
        return output_tool_response(info, {"text": "ok"})

    agent = _build_test_agent(agent_name="probe", model=FunctionModel(_function))

    result = await agent.run("ping")
    assert result.output.text == "ok"
    # The handler was invoked twice: once throttled, once successful.
    assert len(calls) == 2

    # The retried throttle is still observed on the run span.
    run_spans = [
        s for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "compass.agent.run"
    ]
    assert len(run_spans) == 1
    attrs = run_spans[0]["attributes"]
    assert attrs["had_model_request_error"] is True
    assert attrs["error_category"] == "rate_limit"


@pytest.mark.asyncio
async def test_hooks_do_not_retry_non_rate_limit_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-throttle model error (auth) is not retried — it surfaces immediately."""
    import compass_backend.agents.hooks as hooks_mod

    monkeypatch.setattr(hooks_mod, "_RATE_LIMIT_RETRY_BASE_SECONDS", 0.0)

    calls: list[int] = []

    class _AuthError(Exception):
        pass

    async def _function(messages: list[Any], info: AgentInfo) -> ModelResponse:
        calls.append(1)
        raise _AuthError("401 unauthorized: invalid api key")

    agent = _build_test_agent(agent_name="probe", model=FunctionModel(_function))

    with pytest.raises(_AuthError):
        await agent.run("ping")
    # No retry for an auth failure — exactly one model request.
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_hooks_retry_budget_exhaustion_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A throttle that never clears exhausts the retry budget and re-raises.

    The throttle must not be silently swallowed: once the in-process budget is
    spent, the original error surfaces so the failure stays visible.
    """
    import compass_backend.agents.hooks as hooks_mod

    monkeypatch.setattr(hooks_mod, "_RATE_LIMIT_RETRY_BASE_SECONDS", 0.0)
    monkeypatch.setattr(hooks_mod, "_RATE_LIMIT_MAX_ATTEMPTS", 3)

    calls: list[int] = []

    class _GatewayHttpError(Exception):
        pass

    async def _function(messages: list[Any], info: AgentInfo) -> ModelResponse:
        calls.append(1)
        raise _GatewayHttpError("Route not found: anthropic")

    agent = _build_test_agent(agent_name="probe", model=FunctionModel(_function))

    with pytest.raises(_GatewayHttpError):
        await agent.run("ping")
    # Initial attempt plus (budget - 1) retries = budget total attempts.
    assert len(calls) == 3
