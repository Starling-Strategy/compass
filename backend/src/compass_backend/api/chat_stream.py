"""SSE streaming wrapper for `/api/v1/chat`.

The route handler in `api/chat.py` used to await `build_chat_response` to
completion and then open an `EventSourceResponse` over a finished
`ChatResponse`. That meant the client sat silent for the full turn before
seeing anything. This module restructures the SSE branch so the response
opens immediately and events flow as work happens:

1. Open the SSE response immediately, backed by an `asyncio.Queue`.
2. Schedule `build_chat_response` as a background task that pushes
   `stage_start`/`stage_end` and `tool_call_*` events into the queue via
   a `ChatProgressSink`, then pushes the terminal `text`/`data`/
   `citations`/`done` events from `chat_response_sse_payloads` once the
   orchestrator returns.
3. The SSE drain generator pulls from the queue until a `None` sentinel.

Exceptions inside the orchestrator task are caught and emitted as a
terminal `error` + `done` envelope so clients can read a structured
application-level failure (carrying the `trace_id`) instead of an opaque
connection-level 500. The verdict pipeline's `fire_and_forget` runs from
the task's `finally` block when a response was produced.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import nullcontext
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

import asyncpg
from sse_starlette.sse import EventSourceResponse

from compass_backend.answer_layer.nctq_context import NctqContextRepoProtocol
from compass_backend.db._turn_connection import turn_connection
from compass_backend.api.sse import chat_response_sse_payloads
from compass_backend.catalog import CatalogRecallService
from compass_backend.config import Settings
from compass_backend.contracts import ChatRequest, ChatResponse
from compass_backend.contracts.auth import AuthenticatedUser
from compass_backend.execution import QueryExecutor
from compass_backend.orchestration import build_chat_response
from compass_backend.planning import PlannerAgent, PlannerDeps
from compass_backend.planning.catalog_pipeline import CatalogPlanPipeline
from compass_backend.progress import ChatProgressSink
from compass_backend.session import SessionStore
from pydantic_ai.toolsets import AbstractToolset

_logger = logging.getLogger(__name__)

# Bound the queue so a runaway tool loop can't unbounded-grow it. 256 is
# generous (the planner today fires <10 events per turn) but cheap.
_PROGRESS_QUEUE_MAXSIZE = 256

FireAndForget = Callable[..., None]


def chat_streaming_response(
    *,
    chat_request: ChatRequest,
    planner_agent: PlannerAgent,
    store: SessionStore,
    executor: QueryExecutor,
    app_settings: Settings,
    auth_user: AuthenticatedUser | None,
    catalog_recall_service: CatalogRecallService | None,
    planner_toolsets: (
        Sequence[AbstractToolset[PlannerDeps]] | None
    ),
    chat_pool: asyncpg.Pool | None,
    fire_and_forget: FireAndForget,
    nctq_context_repo: NctqContextRepoProtocol | None = None,
    catalog_pipeline: CatalogPlanPipeline | None = None,
) -> EventSourceResponse:
    """Build an `EventSourceResponse` whose stream is fed by a background task."""

    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(
        maxsize=_PROGRESS_QUEUE_MAXSIZE
    )
    sink = ChatProgressSink(queue)
    orch_task = asyncio.create_task(
        _run_orchestrator_into_queue(
            queue=queue,
            sink=sink,
            chat_request=chat_request,
            planner_agent=planner_agent,
            store=store,
            executor=executor,
            app_settings=app_settings,
            auth_user=auth_user,
            catalog_recall_service=catalog_recall_service,
            planner_toolsets=planner_toolsets,
            chat_pool=chat_pool,
            fire_and_forget=fire_and_forget,
            nctq_context_repo=nctq_context_repo,
            catalog_pipeline=catalog_pipeline,
        )
    )
    return EventSourceResponse(_drain(queue, orch_task), ping=30)


async def _drain(
    queue: asyncio.Queue[dict[str, Any] | None],
    orch_task: asyncio.Task[None],
) -> AsyncIterator[dict[str, Any]]:
    """Yield SSE events from the shared queue until the sentinel arrives.

    The orchestrator task is responsible for posting the sentinel `None`
    in its `finally`, even when the request handler dies — so we don't
    need a timeout here. We `await orch_task` after the sentinel so any
    unhandled exception from the task surfaces in logs instead of being
    silently swallowed.

    On client disconnect, sse-starlette stops iterating this generator and
    calls `aclose()` on it, which raises `GeneratorExit` at the suspended
    `yield`. Without the `finally` below, `orch_task` would keep running to
    full completion — burning gateway tokens and holding a pooled DB
    connection for a turn nobody is listening to. The `finally` cancels the
    orphaned task so the paid pipeline stops and the connection is released.
    Cancellation propagates into `build_chat_response` as `CancelledError`
    (a `BaseException`, so the task's `except Exception` does not intercept
    it); `response` stays `None`, so no verdict fire-and-forget is scheduled.
    """

    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item
        try:
            await orch_task
        except Exception:
            _logger.exception("compass.chat_stream.orchestrator_task_failed")
    finally:
        if not orch_task.done():
            orch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await orch_task


async def _run_orchestrator_into_queue(
    *,
    queue: asyncio.Queue[dict[str, Any] | None],
    sink: ChatProgressSink,
    chat_request: ChatRequest,
    planner_agent: PlannerAgent,
    store: SessionStore,
    executor: QueryExecutor,
    app_settings: Settings,
    auth_user: AuthenticatedUser | None,
    catalog_recall_service: CatalogRecallService | None,
    planner_toolsets: (
        Sequence[AbstractToolset[PlannerDeps]] | None
    ),
    chat_pool: asyncpg.Pool | None,
    fire_and_forget: FireAndForget,
    nctq_context_repo: NctqContextRepoProtocol | None = None,
    catalog_pipeline: CatalogPlanPipeline | None = None,
) -> None:
    """Run the orchestrator, push terminal SSE events into the queue, sentinel.

    Exceptions inside `build_chat_response` are caught here so the SSE
    response can carry a structured `error`+`done` envelope (the JS
    `EventSource` API cannot read a non-2xx response body, so a 500
    branch would lose the trace_id at the client).
    """

    import json  # local import — only used on the error path

    response: ChatResponse | None = None
    ctx_cm = turn_connection(chat_pool) if chat_pool is not None else nullcontext()
    try:
        async with ctx_cm:
            response = await build_chat_response(
                chat_request,
                planner_agent=planner_agent,
                store=store,
                executor=executor,
                app_settings=app_settings,
                auth_user=auth_user,
                catalog_recall_service=catalog_recall_service,
                planner_toolsets=planner_toolsets,
                progress=sink,
                nctq_context_repo=nctq_context_repo,
                catalog_pipeline=catalog_pipeline,
            )
            for payload in chat_response_sse_payloads(response):
                await queue.put(payload)
    except Exception as exc:  # noqa: BLE001
        trace_id = getattr(exc, "trace_id", None)
        await queue.put(
            {
                "event": "error",
                "data": json.dumps(
                    {
                        "trace_id": trace_id,
                        "error_type": type(exc).__name__,
                        "error": "internal_error",
                        "message": (
                            "This turn failed before completing. Share "
                            "the trace id with the team if the issue "
                            "persists."
                        ),
                    }
                ),
            }
        )
        await queue.put(
            {
                "event": "done",
                "data": json.dumps(
                    {
                        "trace_id": trace_id,
                        "error": "internal_error",
                    }
                ),
            }
        )
    finally:
        if response is not None:
            try:
                fire_and_forget(
                    response,
                    pool=chat_pool,
                    source=app_settings,
                    user_message=chat_request.message,
                )
            except Exception:
                _logger.exception(
                    "compass.chat_stream.fire_and_forget_schedule_failed"
                )
        await queue.put(None)


__all__ = ["chat_streaming_response"]
