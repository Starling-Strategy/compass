"""Rendering contracts for Compass responses."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RenderStatus = Literal["rendered", "validation_failed"]

# Manifest path tags include every ResultType variant plus non-deterministic
# routes (policy_guidance) that the orchestrator can surface back to the UI.
# Keep this in sync with ResultType — the deterministic variants — plus any
# non-execution path the chat route can emit.
ManifestPath = Literal[
    "metric_ranking",
    "metric_lookup",
    "metric_count",
    "metric_trend",
    "profile_lookup",
    "peer_comparison",
    "composite_ranking",
    "policy_guidance",
    "publication",
]


class ResponseManifest(BaseModel):
    """Rendered response plus traceable rendering metadata."""

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1)
    status: RenderStatus
    format: Literal["markdown"] = "markdown"
    result_type: ManifestPath | None = None
    validation_valid: bool
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)


__all__ = ["ManifestPath", "RenderStatus", "ResponseManifest"]
