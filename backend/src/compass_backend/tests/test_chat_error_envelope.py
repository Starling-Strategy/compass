"""Regression tests for the chat 500 diagnostic envelope (sweep-PR-2).

When `build_chat_response` raises an unhandled exception, three things
must happen so the L1/L2 verdict pipeline can still see the failure:

1. A `TurnErrorEnvelope` is persisted via `SessionStore.save_error_envelope`,
   carrying `trace_id`, `error_type`, `error_message`, and `stage`.
2. The bubbled exception is annotated with `trace_id` so the transport
   layer can include it in the 500 response body (or SSE error event)
   without re-entering a span context.
3. The chat route surfaces a structured 500 (JSON clients) or a
   terminal `error` SSE event (SSE clients) so the trace_id reaches
   the consumer.

`HTTPException` paths are explicitly excluded — those are intentional
control-flow signals (e.g. 413 from `_enforce_message_length`) where
the orchestrator already shaped the failure for the client.
"""

from __future__ import annotations

from typing import Any

import anyio
import pytest
from fastapi.testclient import TestClient

from compass_backend.api.app import create_app
from compass_backend.config import Settings
from compass_backend.contracts import ChatRequest
from compass_backend.execution import ExecutionRefusal
from compass_backend.orchestration.chat import (
    CHAT_ERROR_ENVELOPE_BODY,
    build_chat_response,
)
from compass_backend.session import (
    InMemorySessionStore,
    SavedTurnResult,
    TurnErrorEnvelope,
)
from compass_backend.contracts import SessionState

from compass_backend.tests.test_mvp_chat import FakeQueryExecutor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _RaisingPlannerAgent:
    """A planner that always raises on .run() to simulate stage failure."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def run(self, user_prompt: str, **kwargs: Any) -> Any:
        raise self._exc


class _RecordingSessionStore:
    """SessionStore stub that records save_error_envelope invocations.

    Delegates everything else to InMemorySessionStore so the load/save
    paths behave as the real shape.
    """

    def __init__(
        self,
        *,
        raise_on_envelope: Exception | None = None,
    ) -> None:
        self._inner = InMemorySessionStore()
        self._raise_on_envelope = raise_on_envelope
        self.envelopes: list[TurnErrorEnvelope] = []

    async def load(self, *args: Any, **kwargs: Any) -> SessionState:
        return await self._inner.load(*args, **kwargs)

    async def save_turn(self, *args: Any, **kwargs: Any) -> SavedTurnResult:
        return await self._inner.save_turn(*args, **kwargs)

    async def snapshots_for_session(self, *args: Any, **kwargs: Any):
        return await self._inner.snapshots_for_session(*args, **kwargs)

    async def save_error_envelope(
        self, envelope: TurnErrorEnvelope
    ) -> SavedTurnResult:
        self.envelopes.append(envelope)
        if self._raise_on_envelope is not None:
            raise self._raise_on_envelope
        return await self._inner.save_error_envelope(envelope)


# ---------------------------------------------------------------------------
# Orchestrator-layer tests
# ---------------------------------------------------------------------------


def test_orchestrator_persists_envelope_on_unhandled_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A planner-stage failure must persist a TurnErrorEnvelope with the
    user message + error type/message + trace_id, and the bubbled
    exception must carry that trace_id."""
    monkeypatch.setattr(
        "compass_backend.orchestration.chat.get_current_trace_id",
        lambda: "trace-stub-123",
    )
    store = _RecordingSessionStore()
    planner = _RaisingPlannerAgent(RuntimeError("planner exploded"))

    async def run_response() -> None:
        await build_chat_response(
            ChatRequest(message="Hi"),
            planner_agent=planner,  # type: ignore[arg-type]
            store=store,  # type: ignore[arg-type]
            executor=FakeQueryExecutor(ExecutionRefusal(message="unused")),
            app_settings=Settings(session_store_backend="memory"),
        )

    with pytest.raises(RuntimeError) as excinfo:
        anyio.run(run_response)

    assert "planner exploded" in str(excinfo.value)
    # Trace id was attached for the transport layer to surface:
    assert getattr(excinfo.value, "trace_id", None) == "trace-stub-123"

    assert len(store.envelopes) == 1
    envelope = store.envelopes[0]
    assert envelope.user_message == "Hi"
    assert envelope.assistant_message == CHAT_ERROR_ENVELOPE_BODY
    assert envelope.error_type == "RuntimeError"
    assert envelope.error_message == "planner exploded"
    assert envelope.stage == "build_chat_response"
    assert envelope.trace_id == "trace-stub-123"
    # Session id from request was None → orchestrator resolved a fresh one
    # off the loaded SessionState before the failure (planner runs after
    # _load_session_for_turn). Either way it must be non-empty.
    assert envelope.session_id


def test_orchestrator_skips_envelope_for_http_exception() -> None:
    """HTTPException paths are intentional control-flow signals; the
    orchestrator MUST NOT persist a duplicate error envelope for them.

    `_enforce_message_length` raises HTTPException(422) when the user
    message exceeds settings.chat_message_max_chars. We drive that path
    with a short limit + an over-length message.
    """
    store = _RecordingSessionStore()

    async def run_response() -> None:
        await build_chat_response(
            ChatRequest(message="x" * 100),
            planner_agent=_RaisingPlannerAgent(RuntimeError("unused")),  # type: ignore[arg-type]
            store=store,  # type: ignore[arg-type]
            executor=FakeQueryExecutor(ExecutionRefusal(message="unused")),
            app_settings=Settings(
                session_store_backend="memory",
                chat_message_max_chars=10,
            ),
        )

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        anyio.run(run_response)

    assert excinfo.value.status_code == 422
    # Critically: no envelope persisted for HTTPException.
    assert store.envelopes == []


def test_orchestrator_envelope_persist_failure_does_not_mask_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If save_error_envelope itself raises, the orchestrator must still
    re-raise the *original* exception, not the persist-layer exception.

    This guards against a noisy DB outage masking the actual product
    failure the operator is trying to diagnose.
    """
    monkeypatch.setattr(
        "compass_backend.orchestration.chat.get_current_trace_id",
        lambda: "trace-stub-456",
    )
    store = _RecordingSessionStore(
        raise_on_envelope=RuntimeError("DB unreachable"),
    )
    planner = _RaisingPlannerAgent(ValueError("real failure"))

    async def run_response() -> None:
        await build_chat_response(
            ChatRequest(message="diagnose me"),
            planner_agent=planner,  # type: ignore[arg-type]
            store=store,  # type: ignore[arg-type]
            executor=FakeQueryExecutor(ExecutionRefusal(message="unused")),
            app_settings=Settings(session_store_backend="memory"),
        )

    with pytest.raises(ValueError) as excinfo:
        anyio.run(run_response)
    # The *real* error bubbles — not the persist error.
    assert "real failure" in str(excinfo.value)
    # The orchestrator still tried to persist the envelope:
    assert len(store.envelopes) == 1


# ---------------------------------------------------------------------------
# Transport-layer tests (chat route boundary)
# ---------------------------------------------------------------------------


def test_chat_simple_route_returns_500_with_trace_id_on_orchestrator_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSON clients hitting `/api/v1/chat/simple` receive a 500 whose
    `detail` includes `trace_id` and `error_type`."""
    monkeypatch.setattr(
        "compass_backend.orchestration.chat.get_current_trace_id",
        lambda: "trace-stub-route-1",
    )
    planner = _RaisingPlannerAgent(RuntimeError("oh no"))
    store = _RecordingSessionStore()

    with TestClient(
        create_app(planner_agent=planner, session_store=store)  # type: ignore[arg-type]
    ) as client:
        resp = client.post("/api/v1/chat/simple", json={"message": "hi"})

    assert resp.status_code == 500
    body = resp.json()
    detail = body.get("detail", body)
    assert detail["trace_id"] == "trace-stub-route-1"
    assert detail["error_type"] == "RuntimeError"
    assert detail["error"] == "internal_error"
    # The envelope was written before the 500 was emitted:
    assert len(store.envelopes) == 1
    assert store.envelopes[0].trace_id == "trace-stub-route-1"


def test_chat_route_returns_500_for_json_accept_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/api/v1/chat` with Accept: application/json must return a 500
    with the trace_id in the detail body (the JSON branch)."""
    monkeypatch.setattr(
        "compass_backend.orchestration.chat.get_current_trace_id",
        lambda: "trace-stub-route-2",
    )
    planner = _RaisingPlannerAgent(KeyError("missing field"))
    store = _RecordingSessionStore()

    with TestClient(
        create_app(planner_agent=planner, session_store=store)  # type: ignore[arg-type]
    ) as client:
        resp = client.post(
            "/api/v1/chat",
            json={"message": "hi"},
            headers={"Accept": "application/json"},
        )

    assert resp.status_code == 500
    detail = resp.json().get("detail", resp.json())
    assert detail["trace_id"] == "trace-stub-route-2"
    assert detail["error_type"] == "KeyError"


def test_chat_route_yields_sse_error_event_for_sse_accept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/api/v1/chat` with Accept: text/event-stream must return a 200
    EventSource stream whose only events are `error` and `done`, with
    the trace_id in the `error` event payload.

    Browser EventSource clients cannot read the body of a non-2xx
    response — without this branch the trace_id never reaches the user.
    """
    monkeypatch.setattr(
        "compass_backend.orchestration.chat.get_current_trace_id",
        lambda: "trace-stub-route-sse",
    )
    planner = _RaisingPlannerAgent(RuntimeError("sse path failure"))
    store = _RecordingSessionStore()

    with TestClient(
        create_app(planner_agent=planner, session_store=store)  # type: ignore[arg-type]
    ) as client:
        resp = client.post(
            "/api/v1/chat",
            json={"message": "hi"},
            headers={"Accept": "text/event-stream"},
        )

    assert resp.status_code == 200
    body = resp.text
    # First event is `error`, then `done`. We assert the trace_id
    # appears in the body so frontends can parse it.
    assert "event: error" in body
    assert "trace-stub-route-sse" in body
    assert "RuntimeError" in body
    assert "event: done" in body
    # Envelope written exactly once even on the SSE branch.
    assert len(store.envelopes) == 1
