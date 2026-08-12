"""Regression tests for the SSE drain generator's task lifecycle.

Issue #1081: when the HTTP client disconnects mid-turn, sse-starlette stops
iterating the drain generator and `aclose()`s it. The orchestrator task it
launched must be cancelled in that case — otherwise an abandoned turn runs the
full planner -> execute -> render pipeline to completion, burning gateway
tokens and holding a pooled DB connection for a turn nobody is listening to.
"""

import asyncio

from compass_backend.api.chat_stream import _drain


async def test_drain_cancels_orchestrator_on_client_disconnect() -> None:
    """`aclose()` before the sentinel cancels the still-running orchestrator."""

    queue: asyncio.Queue = asyncio.Queue()
    await queue.put({"event": "text", "data": "partial"})

    # Stand-in for `_run_orchestrator_into_queue`: a long-running task that
    # would otherwise keep going (and keep spending) after the client leaves.
    orch_task = asyncio.create_task(asyncio.sleep(3600))

    agen = _drain(queue, orch_task)
    first = await agen.__anext__()
    assert first == {"event": "text", "data": "partial"}

    # Client disconnects mid-stream -> sse-starlette closes the generator.
    await agen.aclose()

    assert orch_task.cancelled()


async def test_drain_awaits_orchestrator_on_normal_completion() -> None:
    """The sentinel path still drains to completion and never cancels."""

    queue: asyncio.Queue = asyncio.Queue()
    await queue.put({"event": "done", "data": "{}"})
    await queue.put(None)  # sentinel posted by the orchestrator's finally

    completed = asyncio.Event()

    async def _orchestrator() -> None:
        completed.set()

    orch_task = asyncio.create_task(_orchestrator())

    events = [event async for event in _drain(queue, orch_task)]

    assert events == [{"event": "done", "data": "{}"}]
    assert completed.is_set()
    assert orch_task.done()
    assert not orch_task.cancelled()
