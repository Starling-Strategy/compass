"""Shared Pydantic shapes for the Compass Quality Scorecard surface.

Moved from ``nctqai.services.compass_quality.models`` (PR #579) so the
backend computation layer and the dashboard presentation layer share one
canonical set of model classes without a circular dependency.

The dashboard re-exports these via ``nctqai.services.compass_quality``;
all downstream imports that previously pointed to ``nctqai.services.compass_quality.models``
continue to work unchanged.
"""
from __future__ import annotations
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# VENDORED-COPY PATCH (dashboard slice only): canonical main imports
# VerdictOutcome from compass_backend.db.rows, which pulls in
# compass_backend.artifacts (and a wider chain) not vendored into the standalone
# dashboard slice. The symbol is a trivial Literal, so we inline it here.
# Re-apply after any sync from policy-advisor:main. (See AnswerLayerMode in config.py.)
VerdictOutcome = Literal["pass", "fail", "error"]

_KEBAB_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")

CANONICAL_DIM_SLUGS = (
    "selection-accuracy",
    "data-fidelity",
    "coverage-state-labeling",
    "filter-accuracy",
    "sort-accuracy",
    "citation-accuracy",
    "consistency",
)


class BuildContext(BaseModel):
    build_id: str
    sweep_id: str
    criterion_set_version: str
    cases: int
    trials: int
    threshold_version: str | None = None
    threshold_review_date: str | None = None


class DimensionScore(BaseModel):
    dim_slug: str
    name: str
    definition: str
    score_pct: int = Field(ge=0, le=100)
    n_trials: int = Field(ge=0)
    regressed: bool
    threshold_pct: int | None = Field(default=None, ge=0, le=100)
    threshold_status: Literal["pass", "fail", "no_data"] = "no_data"
    exemplar_case_count: int = Field(default=0, ge=0)
    exemplar_status: Literal["complete", "incomplete"] = "incomplete"
    latest_sweep_run_id: str | None = None
    latest_finished_at: str | None = None
    previous_score_pct: int | None = Field(default=None, ge=0, le=100)
    delta_pct: int | None = None

    @field_validator("dim_slug")
    @classmethod
    def _kebab(cls, v: str) -> str:
        if not _KEBAB_RE.fullmatch(v):
            raise ValueError(f"dim_slug must be kebab-case, got {v!r}")
        return v


class ScorecardSnapshot(BaseModel):
    build: BuildContext
    dimensions: list[DimensionScore]

    @field_validator("dimensions")
    @classmethod
    def _exactly_seven_canonical(cls, v: list[DimensionScore]) -> list[DimensionScore]:
        slugs = [d.dim_slug for d in v]
        if slugs != list(CANONICAL_DIM_SLUGS):
            raise ValueError(
                f"dimensions must be the 7 canonical slugs in order; got {slugs}"
            )
        return v


class Trial(BaseModel):
    outcome: VerdictOutcome
    session_id: str
    reason: str | None = None
    answer_excerpt: str | None = None


class ScenarioCase(BaseModel):
    scenario_id: str
    case_id: str
    name: str
    pass_rate_pct: int = Field(ge=0, le=100)
    trials: list[Trial]


class Verdict(BaseModel):
    criterion_id: str
    # Must match every value RecordEvaluator subclasses can write into
    # compass.verdicts.judge_source. Drift here silently null-rejects the
    # affected rows at the dashboard's parse boundary — see the 2026-05-26
    # codebase audit (F2) for the scenario_fit incident.
    judge_source: Literal[
        "deterministic",
        "judge_prompt",
        "span_assertion",
        "inline_deterministic",
        "scenario_fit",
    ]
    outcome: VerdictOutcome
    reason: str | None = None
    answer_text_excerpt: str | None = None


class DimensionDetail(BaseModel):
    build: BuildContext
    dimension: DimensionScore
    cases: list[ScenarioCase]


class ConversationTurn(BaseModel):
    turn_index: int
    user_text: str
    assistant_text: str
    verdicts: list[Verdict]


class ConversationWithVerdicts(BaseModel):
    session_id: str
    trace_id: str | None
    scenario_id: str | None
    scenario_name: str | None
    started_at: str
    turns: list[ConversationTurn]


__all__ = [
    "CANONICAL_DIM_SLUGS",
    "BuildContext",
    "DimensionScore",
    "ScorecardSnapshot",
    "Trial",
    "ScenarioCase",
    "Verdict",
    "DimensionDetail",
    "ConversationTurn",
    "ConversationWithVerdicts",
]
