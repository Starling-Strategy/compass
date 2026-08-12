"""Answer-layer agent wiring tests."""

from __future__ import annotations

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from compass_backend.agents.model_settings import (
    AgentProfile,
    model_settings_for_profile,
)
from compass_backend.contracts.answer_layer import AnswerDraft


async def test_build_answer_stylist_agent_wires_contract_prompt_settings_and_hooks(
    monkeypatch,
) -> None:
    """The answer stylist is a first-class production agent factory.

    Rather than patching ``Agent`` itself (which would pass even if the factory
    body were replaced with ``pass`` or any call that ignored its arguments),
    this builds the *real* ``pydantic_ai.Agent`` and asserts on the constructed
    object's wiring. Only the production gateway model string is swapped for a
    ``TestModel`` instance so construction does not require gateway credentials;
    every other argument (``output_type``, ``instructions``, ``model_settings``)
    is wired by the unmodified factory body and then verified — including a real
    run that must yield an ``AnswerDraft``.
    """

    from compass_backend.answer_layer import agent as answer_agent

    test_model = TestModel()
    monkeypatch.setattr(
        answer_agent.agent_model_settings, "answer_stylist_model", test_model
    )

    built = answer_agent.build_answer_stylist_agent()

    # The factory must return a real pydantic_ai Agent, not a stand-in.
    assert isinstance(built, Agent)
    # The structured output contract is wired onto the real agent.
    assert built.output_type is AnswerDraft
    # The model string the factory reads is the one it constructs the agent with.
    assert built.model is test_model
    # Production guardrail settings flow through from the ANSWER_STYLIST profile.
    assert built.model_settings == model_settings_for_profile(
        AgentProfile.ANSWER_STYLIST
    )
    # Reviewed style-guide instructions are wired as the system prompt.
    # (The section/filename of the load is covered by the next test; here we
    # only confirm the real loaded prompt reached the constructed agent.)
    expected_prompt = answer_agent.prompt_text("answer_style_guides", "default.md")
    assert expected_prompt in built._instructions

    # Running the real agent under the TestModel proves end-to-end wiring:
    # a factory that dropped output_type would not return a typed AnswerDraft.
    result = await built.run("rewrite this brief")
    assert isinstance(result.output, AnswerDraft)


def test_answer_stylist_agent_loads_reviewed_style_guide(monkeypatch) -> None:
    """The factory loads the reviewed markdown style guide by section/name."""

    from compass_backend.answer_layer import agent as answer_agent

    prompt_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(answer_agent, "Agent", lambda _model, **kwargs: kwargs)
    monkeypatch.setattr(
        answer_agent,
        "prompt_text",
        lambda section, filename: prompt_calls.append((section, filename)) or "PROMPT",
    )
    monkeypatch.setattr(answer_agent, "build_diagnostic_hooks", lambda _name: object())

    answer_agent.build_answer_stylist_agent()

    assert prompt_calls == [("answer_style_guides", "default.md")]


def test_build_clarify_stylist_agent_returns_pydantic_ai_agent_with_clarify_draft_output(
    monkeypatch,
) -> None:
    """The clarify stylist is a first-class production agent factory."""

    from compass_backend.answer_layer import clarify_agent
    from compass_backend.contracts.answer_layer import ClarifyDraft
    from compass_backend.agents.model_settings import (
        AgentProfile,
        agent_model_settings,
    )

    calls: dict = {}

    def fake_agent(model: str, **kwargs) -> dict:
        calls["model"] = model
        calls["kwargs"] = kwargs
        return {"model": model, **kwargs}

    monkeypatch.setattr(clarify_agent, "Agent", fake_agent)
    monkeypatch.setattr(
        clarify_agent, "load_model_instructions", lambda filename: "CLARIFY PROMPT"
    )
    monkeypatch.setattr(
        clarify_agent,
        "model_settings_for_profile",
        lambda profile: {"profile": profile.value, "max_tokens": 1024},
    )
    monkeypatch.setattr(
        clarify_agent,
        "build_diagnostic_hooks",
        lambda agent_name: f"hooks:{agent_name}",
    )

    built = clarify_agent.build_clarify_stylist_agent()

    assert built["model"] == agent_model_settings.clarify_stylist_model
    assert calls["kwargs"]["output_type"] is ClarifyDraft
    assert calls["kwargs"]["instructions"] == "CLARIFY PROMPT"
    assert calls["kwargs"]["model_settings"] == {
        "profile": AgentProfile.CLARIFY_STYLIST.value,
        "max_tokens": 1024,
    }
    assert calls["kwargs"]["capabilities"] == ["hooks:clarify_stylist"]
