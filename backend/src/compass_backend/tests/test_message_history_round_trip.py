"""W0.5 (#832): canonical pydantic-ai message_history round-trip + threading.

These tests pin the contract:
1. ``message_history_from_snapshots`` deserializes prior planner runs from
   each snapshot's ``planner_evidence.new_messages_json`` and concatenates
   them in turn-index order.
2. ``run_planner`` forwards a non-empty ``message_history`` into the planner
   agent's ``run(..., message_history=...)`` kwargs (canonical pydantic-ai
   shape; replaces the pre-W0.5 prose-rebuilt ``recent_transcript``).
3. ConversationMemory has the transient (``exclude=True``)
   ``message_history`` field so it can carry the loaded list without
   leaking into serialized turn snapshots.
"""

from __future__ import annotations

from typing import Any

import anyio
import pytest
from pydantic import ValidationError
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from compass_backend.contracts import (
    ConversationMemory,
    PlannerRunEvidence,
    PlannerTurn,
    TurnSnapshot,
)
from compass_backend.contracts.planning import (
    DirectResponse,
)
from compass_backend.planning.planner import run_planner
from compass_backend.session.memory import message_history_from_snapshots


def _direct_turn() -> PlannerTurn:
    return PlannerTurn(
        route="direct",
        confidence=0.9,
        direct_response=DirectResponse(message="Hello.", reason="test"),
    )


def _snapshot_with_messages(
    turn_index: int,
    *,
    user_text: str,
    model_text: str,
) -> TurnSnapshot:
    """Build a TurnSnapshot whose planner_evidence carries a real pa-AI message pair."""

    messages = [
        ModelRequest(parts=[UserPromptPart(content=user_text)]),
        ModelResponse(parts=[TextPart(content=model_text)]),
    ]
    serialized = ModelMessagesTypeAdapter.dump_json(messages).decode("utf-8")
    return TurnSnapshot(
        session_id="sess-1",
        turn_index=turn_index,
        user_message=user_text,
        assistant_message=model_text,
        planner_turn=_direct_turn(),
        planner_evidence=PlannerRunEvidence(
            new_messages_json=serialized,
            message_count=2,
        ),
    )


def test_message_history_from_snapshots_concatenates_in_turn_order() -> None:
    s1 = _snapshot_with_messages(1, user_text="Hi.", model_text="Hello.")
    s2 = _snapshot_with_messages(2, user_text="Again?", model_text="Sure.")

    # Pass snapshots in the wrong order to confirm the loader sorts by turn_index.
    history = message_history_from_snapshots([s2, s1])

    assert len(history) == 4
    # Turn 1's user prompt first.
    first_request = history[0]
    assert isinstance(first_request, ModelRequest)
    assert any(
        isinstance(part, UserPromptPart) and part.content == "Hi."
        for part in first_request.parts
    )
    # Turn 2's user prompt last (before its model response).
    third_request = history[2]
    assert isinstance(third_request, ModelRequest)
    assert any(
        isinstance(part, UserPromptPart) and part.content == "Again?"
        for part in third_request.parts
    )


def test_message_history_from_snapshots_skips_snapshots_without_evidence() -> None:
    s_with = _snapshot_with_messages(1, user_text="Hi.", model_text="Hello.")
    s_without = TurnSnapshot(
        session_id="sess-1",
        turn_index=2,
        user_message="Plain.",
        assistant_message="Direct response.",
        planner_turn=_direct_turn(),
        planner_evidence=None,
    )

    history = message_history_from_snapshots([s_with, s_without])

    # Only the snapshot with evidence contributes messages.
    assert len(history) == 2


def test_message_history_from_snapshots_tolerates_corrupt_payload() -> None:
    """A garbled new_messages_json must not block the next turn."""

    good = _snapshot_with_messages(1, user_text="Hi.", model_text="Hello.")
    bad = TurnSnapshot(
        session_id="sess-1",
        turn_index=2,
        user_message="Garbled.",
        assistant_message="Garbled.",
        planner_turn=_direct_turn(),
        planner_evidence=PlannerRunEvidence(
            new_messages_json="{not valid json at all",
            message_count=0,
        ),
    )

    history = message_history_from_snapshots([good, bad])

    # The good snapshot still contributes; the bad one is silently skipped.
    assert len(history) == 2


def test_conversation_memory_holds_message_history_without_serializing_it() -> None:
    """The message_history field is exclude=True — present in memory, absent from dumps."""

    msg = ModelRequest(parts=[UserPromptPart(content="Hi.")])
    memory = ConversationMemory(message_history=[msg])

    assert memory.message_history == [msg]
    dumped = memory.model_dump()
    assert "message_history" not in dumped


def test_conversation_memory_rejects_non_model_message_history_items() -> None:
    with pytest.raises(ValidationError):
        ConversationMemory(message_history=[{"content": "not a ModelMessage"}])


class _RecordingPlannerAgent:
    """Capture every kwarg passed to .run() so we can assert on message_history."""

    def __init__(self) -> None:
        self.kwargs: list[dict[str, Any]] = []

    async def run(self, prompt: str, **kwargs: Any) -> Any:
        self.kwargs.append({"prompt": prompt, **kwargs})

        class _FakeResult:
            output = _direct_turn()
            new_messages_json_value = b"[]"

            def new_messages_json(self) -> bytes:
                return self.new_messages_json_value

            def usage(self) -> dict[str, int]:
                return {}

            def all_messages(self) -> list[Any]:
                return []

        return _FakeResult()


def test_run_planner_threads_message_history_into_agent_run() -> None:
    """Non-empty message_history must arrive as a planner.run(...) kwarg."""

    agent = _RecordingPlannerAgent()
    prior_messages = [
        ModelRequest(parts=[UserPromptPart(content="Compare salaries.")]),
        ModelResponse(parts=[TextPart(content="Compared.")]),
    ]

    async def run_it() -> None:
        await run_planner(
            "Sort highest first.",
            model="test-model",
            agent=agent,
            message_history=prior_messages,
        )

    anyio.run(run_it)

    assert agent.kwargs, "run_planner must dispatch the planner agent"
    forwarded = agent.kwargs[0].get("message_history")
    assert forwarded == prior_messages, (
        "run_planner must pass message_history through to agent.run()"
    )


def test_run_planner_omits_message_history_when_empty() -> None:
    """An empty message_history must NOT be forwarded — pydantic-ai treats an
    empty history as the absence of one, but we keep the kwarg out so the
    agent's first-turn behavior is identical to no-history."""

    agent = _RecordingPlannerAgent()

    async def run_it() -> None:
        await run_planner(
            "Hello.",
            model="test-model",
            agent=agent,
            message_history=[],
        )

    anyio.run(run_it)

    assert agent.kwargs
    assert "message_history" not in agent.kwargs[0]
