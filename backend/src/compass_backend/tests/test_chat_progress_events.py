"""Tests for mid-flight progress/tool-call SSE events on /api/v1/chat.

These cover the restructured SSE path that opens an `EventSourceResponse`
immediately and streams `stage_start` / `stage_end` (plus `tool_call_*`)
between the existing `trace` and the terminal block. The contract:

- The SSE response interleaves at least one `stage_start`/`stage_end`
  pair between `trace` and `done`.
- Stages appear in pipeline order: `planner` -> `catalog_resolve`
  -> `execute` -> `persist` (skipping `catalog_resolve` if the
  cross-box reviewer is a no-op, which is fine — only ordering matters
  for whatever stages DO fire).
- Direct callers of `chat_response_sse_payloads(ChatResponse)` see
  no new events — the progress sink is route-only.
- A planner exception still produces a terminal `error`+`done` pair
  on the SSE stream, carrying the trace_id.
- Tool-call events flow when the planner agent invokes a tool — we
  drive this synthetically with a stub `event_stream_handler` call
  rather than depending on a real LLM tool roundtrip.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterable
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic_ai import Agent
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.test import TestModel

from compass_backend.api.app import create_app
from compass_backend.api.sse import chat_response_sse_payloads
from compass_backend.contracts import (
    ChatResponse,
    DirectResponse,
    PlannerTurn,
    SessionState,
)
from compass_backend.progress import (
    ChatProgressSink,
    NullProgressSink,
    make_planner_event_stream_handler,
)
from compass_backend.session import InMemorySessionStore

from compass_backend.tests.test_chat_error_envelope import (
    _RaisingPlannerAgent,
    _RecordingSessionStore,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _agent_for_turn(turn: PlannerTurn) -> Agent:
    # call_tools=[] keeps the canned-output TestModel from auto-invoking the
    # always-attached Compass catalog toolset (#1248); the offline test app
    # has no live DB pool for a tool call to reach.
    return Agent(
        TestModel(
            custom_output_args=turn.model_dump(mode="json"),
            call_tools=[],
        ),
        output_type=PlannerTurn,
    )


def _direct_turn() -> PlannerTurn:
    return PlannerTurn(
        route="direct",
        confidence=0.95,
        direct_response=DirectResponse(
            message="Hello from Compass.",
            reason="Greeting does not require execution.",
        ),
    )


def _events_from_body(body: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse an SSE body into a list of (event_name, payload) tuples."""

    events: list[tuple[str, dict[str, Any]]] = []
    current_event: str | None = None
    current_data: list[str] = []

    def _flush() -> None:
        nonlocal current_event, current_data
        if current_event is None:
            current_data = []
            return
        data_str = "".join(current_data)
        try:
            payload = json.loads(data_str) if data_str else {}
        except json.JSONDecodeError:
            payload = {"raw": data_str}
        events.append((current_event, payload))
        current_event = None
        current_data = []

    for raw_line in body.replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            _flush()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            if current_event is not None and current_data:
                _flush()
            current_event = line[6:].strip()
        elif line.startswith("data:"):
            current_data.append(line[5:].strip())
    _flush()
    return events


def _suppress_fire_and_forget(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Stop the verdict pipeline from trying to open a Postgres pool.

    The unit tests don't run against a real DB, and the pool open failure
    would surface as a noisy warning rather than a failure — but we still
    block it so test logs stay clean.
    """

    mock = MagicMock()
    monkeypatch.setattr(
        "compass_backend.api.chat.verdict_pipeline.fire_and_forget",
        mock,
    )
    return mock


# ---------------------------------------------------------------------------
# 1. Real SSE route emits stage_start/stage_end events in pipeline order
# ---------------------------------------------------------------------------


def test_sse_route_interleaves_stage_events_between_trace_and_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _suppress_fire_and_forget(monkeypatch)

    with TestClient(
        create_app(
            planner_agent=_agent_for_turn(_direct_turn()),
            session_store=InMemorySessionStore(),
        )
    ) as client:
        response = client.post("/api/v1/chat", json={"message": "hello"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _events_from_body(response.text)
    kinds = [name for name, _ in events]

    # `done` is the terminal frame, `trace` carries the trace_id and now
    # arrives after the stage events (it's part of the terminal block
    # emitted by chat_response_sse_payloads once the orchestrator
    # finishes).
    assert kinds[-1] == "done"
    assert "trace" in kinds
    assert kinds.index("trace") < kinds.index("done")
    # At least one full stage pair must appear before the trace event.
    trace_idx = kinds.index("trace")
    pre_trace = kinds[:trace_idx]
    assert any(name == "stage_start" for name in pre_trace)
    assert any(name == "stage_end" for name in pre_trace)

    # Stage names that fire must respect the pipeline order. Some stages
    # (e.g. catalog_resolve) are no-ops for the direct route and may not
    # appear at all; only assert relative ordering of those that do.
    stage_order = [
        "planner",
        "catalog_resolve",
        "execute",
        "persist",
    ]
    starts_in_order = [
        payload.get("stage", f"<missing-stage:{payload!r}>")
        for name, payload in events
        if name == "stage_start"
    ]
    assert starts_in_order, f"no stage_start events parsed from {events!r}"
    for stage in starts_in_order:
        assert stage in stage_order, f"unexpected stage {stage!r}"
    # The starts list must be a subsequence of the canonical order.
    iter_canonical = iter(stage_order)
    for stage in starts_in_order:
        assert any(stage == candidate for candidate in iter_canonical), (
            f"stage {stage!r} appeared out of order in {starts_in_order!r}"
        )


# ---------------------------------------------------------------------------
# 2. Direct callers of chat_response_sse_payloads see no new events
# ---------------------------------------------------------------------------


def test_chat_response_sse_payloads_unchanged_for_direct_callers() -> None:
    """The progress sink is route-only — direct callers see the same
    event vocabulary they always did."""

    response = ChatResponse(
        message="Hello from Compass.",
        turn=_direct_turn(),
        session=SessionState(session_id="session-1", turn_count=1),
        snapshot_id="snapshot-1",
    )
    payloads = list(chat_response_sse_payloads(response))
    kinds = [event["event"] for event in payloads]
    assert "stage_start" not in kinds
    assert "stage_end" not in kinds
    assert "tool_call_start" not in kinds
    assert "tool_call_end" not in kinds
    assert kinds == ["trace", "status", "text", "done"]


# ---------------------------------------------------------------------------
# 3. Planner exception still terminates with error + done on SSE
# ---------------------------------------------------------------------------


def test_sse_route_emits_terminal_error_when_planner_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "compass_backend.orchestration.chat.get_current_trace_id",
        lambda: "trace-stub-progress-error",
    )
    _suppress_fire_and_forget(monkeypatch)

    planner = _RaisingPlannerAgent(RuntimeError("planner exploded"))
    store = _RecordingSessionStore()

    with TestClient(
        create_app(
            planner_agent=planner,  # type: ignore[arg-type]
            session_store=store,  # type: ignore[arg-type]
        )
    ) as client:
        response = client.post(
            "/api/v1/chat",
            json={"message": "hi"},
            headers={"Accept": "text/event-stream"},
        )

    assert response.status_code == 200
    events = _events_from_body(response.text)
    kinds = [name for name, _ in events]
    assert "error" in kinds
    assert kinds[-1] == "done"
    error_idx = kinds.index("error")
    done_idx = kinds.index("done")
    assert error_idx < done_idx
    error_payload = next(
        payload for name, payload in events if name == "error"
    )
    assert error_payload.get("trace_id") == "trace-stub-progress-error"
    assert error_payload.get("error_type") == "RuntimeError"


# ---------------------------------------------------------------------------
# 4. Pydantic-AI tool-call events translate to tool_call_start/end on the sink
# ---------------------------------------------------------------------------


class _StubCatalogToolReturn:
    """Stand-in for `CompassCatalogToolOutput` — only the `candidates`
    attribute matters for the MVP summarizer."""

    def __init__(self, candidate_count: int) -> None:
        self.candidates = [{"label": f"candidate-{i}"} for i in range(candidate_count)]


async def _drive_handler(
    handler,
    events_to_send: list[Any],
) -> None:
    """Feed a synthetic async iterable of pydantic-ai events into the handler."""

    async def _stream() -> AsyncIterable[Any]:
        for event in events_to_send:
            yield event

    await handler(None, _stream())


def test_tool_call_events_emit_start_and_end_with_summary() -> None:
    import asyncio

    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=64)
    sink = ChatProgressSink(queue)
    handler = make_planner_event_stream_handler(sink)

    tool_call = ToolCallPart(
        tool_name="search_official_catalog_candidates",
        args={"query": "starting teacher salary"},
        tool_call_id="toolu_test_01",
    )
    tool_return = ToolReturnPart(
        tool_name="search_official_catalog_candidates",
        content=_StubCatalogToolReturn(candidate_count=3),
        tool_call_id="toolu_test_01",
        timestamp=datetime.now(timezone.utc),
    )

    asyncio.run(
        _drive_handler(
            handler,
            [
                FunctionToolCallEvent(part=tool_call),
                FunctionToolResultEvent(part=tool_return),
            ],
        )
    )

    emitted: list[dict[str, Any]] = []
    while not queue.empty():
        item = queue.get_nowait()
        assert item is not None
        # `data` is JSON-encoded at the emit boundary — decode for asserts.
        emitted.append({"event": item["event"], "data": json.loads(item["data"])})

    assert [item["event"] for item in emitted] == [
        "tool_call_start",
        "tool_call_end",
    ]
    assert emitted[0]["data"]["tool_call_id"] == "toolu_test_01"
    assert emitted[0]["data"]["tool_name"] == "search_official_catalog_candidates"
    assert emitted[1]["data"]["tool_call_id"] == "toolu_test_01"
    assert emitted[1]["data"]["result_summary"] == "Found 3 catalog candidates"
    assert emitted[1]["data"]["status"] in {"success", "failed", "denied"}


def test_null_progress_sink_drops_emits() -> None:
    sink = NullProgressSink()
    # Should not raise nor record anything observable.
    sink.emit("stage_start", {"stage": "planner", "message": "ignored"})
    sink.emit("tool_call_start", {"tool_call_id": "x", "tool_name": "y"})


# ---------------------------------------------------------------------------
# 5. SSE 422 still returns on over-length messages (lifted to route)
# ---------------------------------------------------------------------------


def test_sse_route_returns_422_for_over_length_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-flight length check at the route boundary must still
    return 422 on both transports — the SSE branch can't reshape an HTTP
    status once the EventSourceResponse opens, so the check runs before."""

    _suppress_fire_and_forget(monkeypatch)
    from compass_backend.config import Settings

    settings = Settings(
        session_store_backend="memory",
        chat_message_max_chars=10,
    )

    with TestClient(
        create_app(
            planner_agent=_agent_for_turn(_direct_turn()),
            session_store=InMemorySessionStore(),
            app_settings=settings,
        )
    ) as client:
        sse_resp = client.post(
            "/api/v1/chat",
            json={"message": "x" * 100},
            headers={"Accept": "text/event-stream"},
        )
        json_resp = client.post(
            "/api/v1/chat",
            json={"message": "x" * 100},
            headers={"Accept": "application/json"},
        )

    assert sse_resp.status_code == 422
    assert json_resp.status_code == 422
