"""CompassEvaluatorContext — typed adapter wrapping pydantic_evals.EvaluatorContext.

Compass-specific fields (artifact_snapshot, trace_id, span_attributes) are passed
through alongside the framework's standard inputs/output/metadata. Both the live
L2 path (PR 6) and the offline CLI (PR 7) construct one of these per turn before
running evaluators.

`plan` and `result` carry the typed planner output + deterministic execution
artifact through to dimension-bridge validators (`validators/registry.py`).
Those validators forward both to `quality.validate_result()` and filter the
returned findings by `dimension` label. Typed by string to avoid pulling
`compass_backend.contracts.planning.QueryPlan` and `compass_backend.artifacts.ResultSet`
into this module's import graph; bridge validators do the import at call time.

`artifact_snapshot` is a typed `ArtifactSnapshot(BaseModel)` (was `dict` pre-Bucket-D).
The pydantic dataclass decorator coerces a dict on construction so the
pydantic-evals adapter round trip (`criteria.py::_PydanticEvalsAdapter.evaluate`
and `_PydanticRecordEvalsAdapter.evaluate`) keeps working: passing a JSON-shaped
dict in `ctx.metadata["compass_ctx"]` still produces a typed context.
"""

from __future__ import annotations

from dataclasses import field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.dataclasses import dataclass

from compass_backend.artifacts import ResultSet
from compass_backend.artifacts.citations import CitationRef
from compass_backend.contracts.planning import PlannerRoute, QueryPlan
from compass_backend.db.rows import ScenarioExpectation

# `QueryPlan` and `ResultSet` are imported eagerly (not TYPE_CHECKING) because
# pydantic dataclasses validate at construction time and need the concrete
# types resolvable. Neither module imports back from `compass_backend.quality`,
# so there is no circular-import risk.


class ArtifactTableRow(BaseModel):
    """One compact data row visible to evaluators.

    Matches the projection in `quality.verdict_pipeline._build_context`. Only
    the fields a deterministic rule or judge prompt needs to reason about the
    rendered table. Adding a field here means the producer in
    `verdict_pipeline` must populate it; `extra="forbid"` enforces the contract.
    """

    model_config = ConfigDict(extra="forbid")

    district_name: str
    state: str | None = None
    display_value: str
    sort_display_value: str | None = None
    # The metric this cell belongs to. Long-form multi-metric tables (a ranked
    # lookup that displays comparison metrics) emit one row per district-metric
    # cell, so a flat scan of `display_value` interleaves columns. The
    # `sort_descending` validator uses this to isolate the ranked metric's
    # column before checking monotonicity (#1460). None on legacy/single-metric
    # snapshots, where the whole table is one column.
    metric_name: str | None = None


class SelectionExpected(BaseModel):
    """Projection of QueryPlan.selection used by selection_set_matches_request.

    Not the full SelectionSpec — only the three fields the validator at
    `validators/registry.py:_validate_selection_set_matches_request` reads to
    compare requested-vs-returned. Defining it as a separate shape keeps a
    clean evaluator boundary: a future SelectionSpec field addition does not
    silently leak into evaluator code.
    """

    model_config = ConfigDict(extra="forbid")

    scope: Literal[
        "all_covered_districts",
        "state",
        "named_districts",
        "largest_districts",
    ] | None = None
    states: list[str] = Field(default_factory=list)
    districts: list[str] = Field(default_factory=list)


class ArtifactSnapshot(BaseModel):
    """Typed snapshot of the artifacts produced for one chat turn.

    Replaces `dict[str, Any]` on CompassEvaluatorContext. Prefilter rules and
    deterministic validators access fields directly (`ctx.artifact_snapshot.table`)
    instead of dict keys (`ctx.artifact_snapshot.get("table")`). A misspelled
    field name becomes a static type-check error AND an AttributeError at
    runtime — turning what was a silent Type-II miss into a loud failure.
    """

    model_config = ConfigDict(extra="forbid")

    table: list[ArtifactTableRow] = Field(default_factory=list)
    citations: list[CitationRef] = Field(default_factory=list)
    selection_expected: SelectionExpected | None = None
    # Name of the metric the rows are ordered by, when a multi-metric ranked
    # lookup displayed comparison columns alongside the ranked one (#1220's
    # `MetricLookupResult.ranked_by_metric_name`). The `sort_descending`
    # validator filters `table` to rows whose `metric_name` matches this so it
    # checks the ranked column only, not a column-interleaved row scan (#1460).
    # None for single-metric tables and non-ranked lookups.
    ranked_by_metric_name: str | None = None


@dataclass
class CompassEvaluatorContext:
    """Compass-specific evaluation context.

    A pydantic dataclass (not stdlib `@dataclass`) so nested Pydantic models
    coerce automatically when constructed from a dict — this preserves the
    round-trip behaviour of `_PydanticEvalsAdapter` and
    `_PydanticRecordEvalsAdapter` in `criteria.py`, which do
    `CompassEvaluatorContext(**compass_ctx_data)` when the dict comes from
    `pydantic_evals.EvaluatorContext.metadata["compass_ctx"]`.
    """

    session_id: str
    turn_index: int | None
    step_index: int | None = None
    case_id: int | None = None
    scenario_id: int | None = None

    artifact_snapshot: ArtifactSnapshot = field(default_factory=ArtifactSnapshot)
    answer_text: str = ""
    intent: str | None = None
    user_message: str = ""
    # ↑ raw user message verbatim. Distinct from `intent` (which is a derived
    # paraphrase: `QueryPlan.operation + temporal.intent`). Validators that
    # need to detect user-stated structural constraints — e.g.
    # `respects_user_limit` looking for a "top N" phrase — must read this
    # field, not `intent`, because the planner does not surface "top 5" into
    # operation/temporal. Populated by `verdict_pipeline._build_context`
    # when the caller passes `user_message=...`; defaults to "" so older
    # constructors don't break.

    plan: QueryPlan | None = None
    result: ResultSet | None = None

    route: PlannerRoute | None = None
    # ↑ planner-decided route for this turn ("direct" | "clarify" | "execute" |
    # "policy_guidance"). Populated by `verdict_pipeline._build_context` from
    # `response.turn.route`. Used by the verdict-pipeline pre-check to skip
    # criteria whose `payload.applicable_routes` excludes the current route —
    # e.g., a span_assertion criterion that only applies on execute turns
    # would carry `applicable_routes=["execute"]` and skip on clarify turns
    # rather than failing on "no execution span in trace".

    trace_id: str | None = None
    span_attributes: dict[str, Any] = field(default_factory=dict)
    # ↑ deliberately stays loose: span_attributes is an opaque metadata bag
    # for Logfire/observability. No key-by-key access exists in the codebase;
    # it's only ever `dict(ctx.span_attributes)` in evidence.py. Typing it
    # would force callers to enumerate keys we deliberately don't enumerate.

    triggered_by: Literal["user_turn", "sweep_run", "manual_replay"] = "user_turn"
    sweep_run_id: str | None = None

    prior_result_ref_count: int = 0
    # ↑ count of ResultMemoryRef entries already in ConversationMemory.result_refs
    # at evaluation time. Populated by `verdict_pipeline._build_context` from
    # `response.session.memory.result_refs`. The `_rule_preserves_prior_context`
    # prefilter reads this so it fires on follow-up refusals (rescue clarifies,
    # validation-failed manifests) when there IS prior context to preserve —
    # the failure mode the M3 rubric must catch but couldn't see through the
    # narrow sort/rank-only gate.
    recent_routes: list[str] = field(default_factory=list)
    # ↑ snapshot of ConversationMemory.recent_routes (bounded list of prior
    # turn routes). Used alongside `prior_result_ref_count` so the prefilter
    # recognises "this is a follow-up to a real exchange" without needing
    # the planner agent's full message_history.

    expected_output: ScenarioExpectation | None = None
    # ↑ per-case arc-level expectation from compass.cases.expected_output, hydrated by
    # pa-eval's runner before calling evaluate_and_write. None on the live user_turn
    # path (fire_and_forget never receives an expected_output). The scenario_fit
    # evaluator (Task 5.3) reads this to compare the actual answer against the
    # expected_behaviour + per-step expectations. Defaults to None so all live-path
    # callers require no changes.

    case_transcript: list[dict[str, str]] = field(default_factory=list)
    # ↑ full user/assistant transcript for one pa-eval case trial. Only populated
    # for case-level scenario_fit evaluation; ordinary per-turn verdicts leave it
    # empty and continue to judge ctx.answer_text.

    case_metadata: dict[str, Any] = field(default_factory=dict)
    # ↑ compass.cases.metadata for the case under evaluation. Populated by
    # verdict_pipeline on the case-level scenario_fit path (loaded by case_id at
    # enrollment); empty on the live user_turn path. The scenario_fit evaluator
    # reads `category` / `structural_diagnostic_dimension` from here to decide
    # applicability: structural-diagnostic FOUNDATION cases (where category equals
    # the structural_diagnostic_dimension) carry route/result_type/artifact/verdict
    # contract expectations the answer-text judge structurally cannot evaluate, so
    # the evaluator records a not_applicable verdict instead of judging them (#939).

    references_turn_indices: list[int] = field(default_factory=list)

    clarification_rescue_origin: bool = False
    # ↑ True when the clarify turn ORIGINATED from the rescue-fallback path
    # (UnexpectedModelBehavior caught in orchestration — the catalog pipeline
    # blocked on an unresolved phrase — whether or not the shape-guard
    # enrichment stage later rebuilt the turn). Populated by
    # verdict_pipeline._build_context from response.turn.clarification.rescue_origin
    # (the DURABLE marker), NOT from is_rescue_fallback (which enrichment clears
    # to False, #1613). Populated on EVERY path including the live user_turn
    # path; it is False only when the turn has no clarification (i.e. on
    # execute / direct / policy_guidance routes).


__all__ = [
    "ArtifactSnapshot",
    "ArtifactTableRow",
    "CompassEvaluatorContext",
    "ScenarioExpectation",
    "SelectionExpected",
]
