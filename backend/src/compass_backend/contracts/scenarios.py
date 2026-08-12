"""Scenario-case contracts used by debug and replacement-gate tooling."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ScenarioCaseStep(BaseModel):
    """One runnable user step from a B-spine Case."""

    model_config = ConfigDict(extra="forbid")

    prompt: str
    step_complete_when: str | None = None


class ScenarioCaseDebugResponse(BaseModel):
    """Read-only B-spine case payload expected by Compass debug links."""

    model_config = ConfigDict(extra="forbid")

    id: int
    case_id: int
    case_code: str
    scenario_id: int
    scenario_code: str
    scenario_title: str
    initial_user_message: str
    input_steps: list[ScenarioCaseStep] = Field(default_factory=list)
    what_we_are_testing: str | None = None
    expected_behaviour: str
    feature: str | None = None
    type: str = "golden"
    sort_order: int = 0
    active: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None
    criteria: list[dict] = Field(default_factory=list)
    example_conversations: list[dict] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _alias_id_to_case_id(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "case_id" not in data and "id" in data:
                data = {**data, "case_id": data["id"]}
            if "id" not in data and "case_id" in data:
                data = {**data, "id": data["case_id"]}
        return data


class ScenarioCaseListResponse(BaseModel):
    """Read-only B-spine case list payload consumed by the dashboard."""

    model_config = ConfigDict(extra="forbid")

    cases: list[ScenarioCaseDebugResponse] = Field(default_factory=list)
