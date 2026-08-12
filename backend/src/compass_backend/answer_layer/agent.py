"""Pydantic AI answer-stylist agent factory."""

from __future__ import annotations

from pydantic_ai import Agent

from compass_backend.agents.hooks import build_diagnostic_hooks
from compass_backend.agents.model_settings import (
    AgentProfile,
    agent_model_settings,
    model_settings_for_profile,
)
from compass_backend.contracts.answer_layer import AnswerDraft
from compass_backend.instructions.loader import prompt_text


def build_answer_stylist_agent() -> Agent[None, AnswerDraft]:
    """Construct the answer-layer stylist with Compass production guardrails.

    Intentionally omits ``retries`` and an agent-level ``output_validator``
    (unlike the judge/adjudicator factories): the answer layer validates and
    rejects at the service boundary (``answer_layer/validation.py``) and falls
    back to the deterministic rendered body rather than re-rolling the model, so
    adding a validator here would change that fallback contract.
    """

    return Agent(
        agent_model_settings.answer_stylist_model,
        output_type=AnswerDraft,
        instructions=prompt_text("answer_style_guides", "default.md"),
        model_settings=model_settings_for_profile(AgentProfile.ANSWER_STYLIST),
        capabilities=[build_diagnostic_hooks("answer_stylist")],
    )


__all__ = ["build_answer_stylist_agent"]
