"""Pydantic AI model-call settings for Compass agents.

This module owns cross-provider request settings. Provider-specific behavior
such as Anthropic prompt caching should be layered on top when the concrete
agent module is admitted.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_ai.settings import ModelSettings, ServiceTier, ThinkingLevel
from pydantic_settings import BaseSettings, SettingsConfigDict

PlannerThinkingPolicy = Literal["off", "selected", "all"]
PlannerThinkingEffort = Literal["low", "medium", "high"]


class AgentProfile(StrEnum):
    """Named model-call profiles used by active Compass agents."""

    ANSWER_STYLIST = "answer_stylist"
    CLARIFY_STYLIST = "clarify_stylist"
    CATALOG_ADJUDICATOR = "catalog_adjudicator"
    CRITERION_CLASSIFIER = "criterion_classifier"
    JUDGE = "judge"
    PLANNER = "planner"


class AgentModelSettings(BaseSettings):
    """Environment-backed defaults for Pydantic AI `ModelSettings`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="COMPASS_AGENT_",
        case_sensitive=False,
        extra="ignore",
    )

    default_temperature: float = Field(
        default=0.0,
        ge=0.0,
        description="Default deterministic temperature for Compass agent calls.",
    )
    default_timeout_seconds: float = Field(
        default=90.0,
        gt=0.0,
        description="Default per-request model timeout in seconds.",
    )
    default_parallel_tool_calls: bool | None = Field(
        default=None,
        description="Whether Pydantic AI may ask supported models for parallel tool calls.",
    )
    default_thinking: ThinkingLevel | None = Field(
        default=None,
        description="Cross-provider reasoning/thinking setting.",
    )
    default_service_tier: ServiceTier | None = Field(
        default=None,
        description="Cross-provider service tier for model requests.",
    )

    answer_stylist_model: str = Field(
        default="gateway/anthropic:claude-opus-4-6",
        description=(
            "High-capability model for optional answer-layer rewriting over "
            "sealed validated Compass answer briefs. (Sonnet 5 also 400s on the "
            "temperature=0 payload — see the planner note; reverted to Opus 4.6.)"
        ),
    )
    answer_stylist_max_tokens: int = Field(default=2048, ge=1)
    answer_stylist_timeout_seconds: float = Field(
        default=30.0,
        gt=0.0,
        description=(
            "Hard wall-clock ceiling for the answer-stylist call; on timeout "
            "the deterministic renderer body is used. Bounds the Opus tail "
            "(observed p95 50s / max 139s) without shrinking other agents' timeout."
        ),
    )
    clarify_stylist_model: str = Field(
        default="gateway/anthropic:claude-opus-4-6",
        description=(
            "Focused stylist model for composing clarify-route questions. "
            "Defaults to the answer-stylist model; override via "
            "COMPASS_AGENT_CLARIFY_STYLIST_MODEL."
        ),
    )
    clarify_stylist_max_tokens: int = Field(default=1024, ge=1)
    planner_model: str = Field(
        default="gateway/anthropic:claude-sonnet-4-6",
        description=(
            "Vanilla Pydantic AI model string for the typed planner. Sonnet 5 was "
            "trialed (2026-07) but did NOT clear the A/B gate in "
            "docs/how-we-pick-models.md — the shipped temperature=0 payload 400s on "
            "Sonnet 5 (temperature deprecated), and temperature-corrected it showed a "
            "structured-output schema failure + 90% route agreement (<95%). Staying "
            "on Sonnet 4.6. Override via COMPASS_AGENT_PLANNER_MODEL."
        ),
    )
    planner_thinking_policy: PlannerThinkingPolicy = Field(
        default="off",
        description=(
            "Legacy strict-planner thinking policy. Keep off for SSN-271 unless "
            "explicitly testing provider/schema compatibility."
        ),
    )
    planner_thinking_max_effort: PlannerThinkingEffort = Field(
        default="medium",
        description="Maximum thinking effort used when planner thinking is selected.",
    )
    catalog_adjudicator_model: str = Field(
        default="gateway/anthropic:claude-haiku-4-5",
        description="Small deterministic model for candidate-only catalog adjudication.",
    )
    catalog_adjudicator_max_tokens: int = Field(default=1024, ge=1)
    criterion_classifier_model: str = Field(
        default="gateway/anthropic:claude-haiku-4-5",
        description=(
            "Small deterministic model for AI criterion classifier (Safeguard 4). "
            "Same default as enrichment_model; override via COMPASS_AGENT_CRITERION_CLASSIFIER_MODEL."
        ),
    )
    criterion_classifier_max_tokens: int = Field(default=1024, ge=1)
    judge_model: str = Field(
        default="gateway/anthropic:claude-haiku-4-5",
        description=(
            "Pydantic AI model string used by verdict pipeline agents "
            "to grade responses against compass.criteria judge-prompt criteria."
        ),
    )
    judge_max_tokens: int = Field(default=1024, ge=1)
    judge_request_limit: int = Field(default=2, ge=1)
    judge_max_concurrency: int = Field(default=4, ge=1)
    judge_max_queue: int = Field(default=64, ge=1)
    planner_max_tokens: int = Field(default=4096, ge=1)

    @field_validator("default_temperature")
    @classmethod
    def validate_temperature(cls, value: float) -> float:
        """Keep Compass defaults in the normal model temperature range."""

        if value > 2.0:
            raise ValueError("Temperature should be 2.0 or lower")
        return value


agent_model_settings = AgentModelSettings()


def _compact_model_settings(**values: Any) -> ModelSettings:
    """Build a `ModelSettings` dict without passing unset options."""

    return {key: value for key, value in values.items() if value is not None}


def model_settings_for_profile(
    profile: AgentProfile,
    source: AgentModelSettings = agent_model_settings,
) -> ModelSettings:
    """Return Pydantic AI settings for a named Compass agent profile."""

    max_tokens = {
        AgentProfile.ANSWER_STYLIST: source.answer_stylist_max_tokens,
        AgentProfile.CLARIFY_STYLIST: source.clarify_stylist_max_tokens,
        AgentProfile.CATALOG_ADJUDICATOR: source.catalog_adjudicator_max_tokens,
        AgentProfile.CRITERION_CLASSIFIER: source.criterion_classifier_max_tokens,
        AgentProfile.JUDGE: source.judge_max_tokens,
        AgentProfile.PLANNER: source.planner_max_tokens,
    }[profile]

    return _compact_model_settings(
        max_tokens=max_tokens,
        temperature=source.default_temperature,
        timeout=source.default_timeout_seconds,
        parallel_tool_calls=source.default_parallel_tool_calls,
        thinking=source.default_thinking,
        service_tier=source.default_service_tier,
    )
