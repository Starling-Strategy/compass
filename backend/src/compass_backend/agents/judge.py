"""Judge Agent factory.

Owns construction of the LLM-judge Agent used by the quality layer
(GoldenCriterion / CriterionRecord judgment evaluators). Lives in
`agents/` rather than `quality/` because Agent construction with
diagnostic hooks is an `agents/` concern. The quality layer supplies
the output_type, instructions, and validator; this module wires the
runtime (model settings, concurrency limits, diagnostic capabilities).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel
from pydantic_ai import Agent, ConcurrencyLimit, UsageLimits

from compass_backend.agents.hooks import build_diagnostic_hooks
from compass_backend.agents.model_settings import (
    AgentProfile,
    agent_model_settings,
    model_settings_for_profile,
)

OutputT = TypeVar("OutputT", bound=BaseModel)


def build_judge_agent(
    output_type: type[OutputT],
    instructions: str,
    output_validator: Callable[[OutputT], OutputT] | None = None,
) -> Agent[None, OutputT]:
    """Construct a Compass judge Agent with diagnostic hooks wired.

    Args:
        output_type:      Pydantic model the agent must return.
        instructions:     System prompt — judging rubric instructions.
        output_validator: Optional output_validator callback; raise
                          ModelRetry inside to force a re-roll.
    """

    agent: Agent[None, OutputT] = Agent(
        agent_model_settings.judge_model,
        output_type=output_type,
        instructions=instructions,
        model_settings=model_settings_for_profile(AgentProfile.JUDGE),
        retries={"output": 1},
        max_concurrency=ConcurrencyLimit(
            max_running=agent_model_settings.judge_max_concurrency,
            max_queued=agent_model_settings.judge_max_queue,
        ),
        capabilities=[build_diagnostic_hooks("judge")],
    )
    if output_validator is not None:
        agent.output_validator(output_validator)
    return agent


def judge_usage_limits() -> UsageLimits:
    """Per-run usage limits for one judgment rule."""

    return UsageLimits(
        request_limit=agent_model_settings.judge_request_limit,
        output_tokens_limit=agent_model_settings.judge_max_tokens,
    )


__all__ = ["build_judge_agent", "judge_usage_limits"]
