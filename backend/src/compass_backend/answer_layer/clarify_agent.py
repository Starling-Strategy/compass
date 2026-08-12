"""Pydantic AI clarify-stylist agent factory."""

from __future__ import annotations

from pydantic_ai import Agent

from compass_backend.agents.hooks import build_diagnostic_hooks
from compass_backend.agents.model_settings import (
    AgentProfile,
    agent_model_settings,
    model_settings_for_profile,
)
from compass_backend.contracts.answer_layer import ClarifyDraft
from compass_backend.instructions.loader import load_model_instructions


def build_clarify_stylist_agent() -> Agent[None, ClarifyDraft]:
    """Construct the focused clarify-route stylist agent."""

    return Agent(
        agent_model_settings.clarify_stylist_model,
        output_type=ClarifyDraft,
        instructions=load_model_instructions("clarify_stylist.md"),
        model_settings=model_settings_for_profile(AgentProfile.CLARIFY_STYLIST),
        capabilities=[build_diagnostic_hooks("clarify_stylist")],
    )


__all__ = ["build_clarify_stylist_agent"]
