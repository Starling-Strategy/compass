"""Mid-flight chat progress sink for SSE transport.

`build_chat_response` runs through four user-visible stages (planner,
catalog_resolve, execute, persist) and may invoke planner tools while it
works. Today nothing reaches the client until the orchestrator returns a
finished `ChatResponse`. The progress sink lets the orchestrator and the
planner's pydantic-ai event stream push advisory events into a shared
`asyncio.Queue` that the SSE transport drains as work happens.

The sink is transport-concern only: events emitted here never enter the
verdict ledger, the turn snapshot, or any logfire span. They exist purely
so the client can render "doing work" feedback.

Two sink shapes:
- `ChatProgressSink` — backed by a queue; emits drop on overflow.
- `NullProgressSink` — no-op default for callers that don't stream.

Plus `make_planner_event_stream_handler(sink)` which adapts
pydantic-ai's `EventStreamHandler` protocol into sink emits.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterable, Awaitable, Callable
from typing import Any

from pydantic_ai.messages import (
    AgentStreamEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
)

_logger = logging.getLogger(__name__)


class ChatProgressSink:
    """Bounded async queue wrapper for mid-flight chat events.

    The transport layer constructs one sink per SSE request, hands it to
    `build_chat_response` (via `_TurnContext.progress`), and reads the
    queue from the SSE drain generator.

    `emit` is non-blocking and drops on overflow — progress events are
    advisory. Terminal events (text/data/citations/done) bypass this sink
    and use `await queue.put` to ensure delivery.
    """

    def __init__(self, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        self._queue = queue

    @property
    def queue(self) -> asyncio.Queue[dict[str, Any] | None]:
        return self._queue

    def emit(self, event: str, data: dict[str, Any]) -> None:
        # JSON-encode the payload at the emit boundary so the SSE
        # transport sends real strings, not Python repr (`sse-starlette`
        # str()s whatever sits in `data`).
        try:
            self._queue.put_nowait(
                {"event": event, "data": json.dumps(data)}
            )
        except asyncio.QueueFull:
            _logger.debug(
                "compass.progress.dropped event=%s (queue full)",
                event,
            )


class NullProgressSink:
    """No-op sink for callers that don't stream progress (JSON, tests)."""

    def emit(self, event: str, data: dict[str, Any]) -> None:
        return None


ProgressSink = ChatProgressSink | NullProgressSink


def make_planner_event_stream_handler(
    sink: ProgressSink,
) -> Callable[
    [Any, AsyncIterable[AgentStreamEvent]],
    Awaitable[None],
]:
    """Build a pydantic-ai EventStreamHandler that emits tool-call frames.

    Matches the `EventStreamHandler` TypeAlias from pydantic-ai:
    `Callable[[RunContext, AsyncIterable[AgentStreamEvent]], Awaitable[None]]`.

    Only tool call/result events are surfaced — model-text and thinking
    events are not user-visible progress on the Compass SSE.
    """

    async def handler(
        _run_context: Any,
        stream: AsyncIterable[AgentStreamEvent],
    ) -> None:
        async for event in stream:
            if isinstance(event, FunctionToolCallEvent):
                sink.emit(
                    "tool_call_start",
                    {
                        "tool_call_id": event.part.tool_call_id,
                        "tool_name": event.part.tool_name,
                    },
                )
            elif isinstance(event, FunctionToolResultEvent):
                sink.emit(
                    "tool_call_end",
                    {
                        "tool_call_id": event.part.tool_call_id,
                        "status": getattr(event.part, "outcome", "success"),
                        "result_summary": _summarize_tool_result(
                            getattr(event.part, "content", None)
                        ),
                    },
                )

    return handler


def _summarize_tool_result(content: Any) -> str | None:
    """Render a short human label for a tool return, or `None` if unknown.

    MVP recognizes the planner's catalog toolset output. Everything else
    falls through to `None` so the frontend renders a generic "Done" badge.
    """

    candidates = getattr(content, "candidates", None)
    if isinstance(candidates, list):
        count = len(candidates)
        noun = "candidate" if count == 1 else "candidates"
        return f"Found {count} catalog {noun}"
    return None


__all__ = [
    "ChatProgressSink",
    "NullProgressSink",
    "ProgressSink",
    "make_planner_event_stream_handler",
]
