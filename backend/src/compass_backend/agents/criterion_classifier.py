"""Criterion-classifier Agent factory.

Owns construction of the criterion-classifier Agent used by Spec B
Safeguard 4 (AI-assisted criterion selection). The output_type is
built by the caller (quality/classifier.py) because it carries
`Literal[*runtime_active_codes]` — a runtime-bound enum that makes
fabricated criterion codes JSON-schema-impossible.

This module owns the Agent runtime concerns (model settings, diagnostic
hooks, instructions). The caller owns the output schema.
"""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel
from pydantic_ai import Agent, UsageLimits

from compass_backend.instructions.loader import load_model_instructions
from compass_backend.agents.hooks import build_diagnostic_hooks
from compass_backend.agents.model_settings import (
    AgentProfile,
    agent_model_settings,
    model_settings_for_profile,
)

OutputT = TypeVar("OutputT", bound=BaseModel)

_INSTRUCTIONS = load_model_instructions("criterion_classifier.md")


def build_criterion_classifier_agent(output_type: type[OutputT]) -> Agent[None, OutputT]:
    """Construct a criterion-classifier Agent with diagnostic hooks wired."""

    return Agent(
        agent_model_settings.criterion_classifier_model,
        output_type=output_type,
        instructions=_INSTRUCTIONS,
        model_settings=model_settings_for_profile(AgentProfile.CRITERION_CLASSIFIER),
        retries={"output": 1},
        capabilities=[build_diagnostic_hooks("criterion_classifier")],
    )


def criterion_classifier_usage_limits() -> UsageLimits:
    """Per-run usage limits for the criterion classifier."""

    return UsageLimits(
        request_limit=2,
        output_tokens_limit=agent_model_settings.criterion_classifier_max_tokens,
    )


__all__ = ["build_criterion_classifier_agent", "criterion_classifier_usage_limits"]
