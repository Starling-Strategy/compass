"""Tests for the step-5 unified catalog pipeline + validator flip.

Step 5 of the catalog-resolution-unification plan
(``docs/plans/2026-05-28-catalog-resolution-unification-plan.md``).

Two halves:

- :class:`CatalogPlanPipeline` orchestration — adjudicator skip on
  high-confidence resolutions, adjudication of ambiguous resolutions,
  finalizer dispatch.
- :func:`validate_planner_turn_quality_async` — pass-through when no
  pipeline is configured (production today, flag off); on the SORT-NEW-02
  shape with a pipeline attached, succeeds with the slot-corrected
  plan; on every :class:`FinalizerBlocker` shape, raises ``ModelRetry``
  with catalog evidence carried in the hint.
"""

from __future__ import annotations

import pytest
from pydantic_ai import ModelRetry

from compass_backend.catalog import (
    CatalogResolutionEntity,
    MetricBundleResolution,
    NCESFieldCandidate,
    ProfileFieldResolution,
)
from compass_backend.catalog.adjudication import (
    CatalogAdjudicationDecision,
    CatalogAdjudicationDeps,
)
from compass_backend.catalog.reconciliation import (
    _SCORE_CANDIDATE,
    _SCORE_RESOLVED_SINGLE,
    CatalogReconciler,
)
from compass_backend.contracts.catalog_resolution import (
    CatalogResolution,
    ResolvedEntity,
)
from compass_backend.contracts.planning import (
    MetricSpec,
    OutputSpec,
    PlannerTurn,
    QueryPlan,
    SelectionSpec,
    SortSpec,
)
from compass_backend.planning.catalog_pipeline import (
    CatalogPlanPipeline,
    _apply_adjudication,
    _should_skip_adjudication,
)
from compass_backend.planning.planner import (
    PlannerDeps,
    _model_retry_hint_from_finalizer_blockers,
    validate_planner_turn_quality_async,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeCatalog:
    """Catalog stub covering the reconciler's narrow Protocol surface."""

    async def resolve_unsupported_concepts(
        self, phrases: list[str]
    ) -> list[CatalogResolutionEntity]:
        results: list[CatalogResolutionEntity] = []
        for phrase in phrases:
            if phrase.casefold().strip() == "union release time":
                results.append(
                    CatalogResolutionEntity(
                        input_phrase=phrase,
                        entity_type="unsupported_concept",
                        status="unsupported",
                        resolution_method="unsupported_catalog",
                        approved_key="union_release_time",
                        label="Union release time",
                        provenance="test",
                        message=(
                            "Compass does not yet have a governed metric for "
                            "union release time."
                        ),
                    )
                )
        return results

    async def resolve_metric_bundle(
        self,
        query: str,
        *,
        numeric_only: bool = False,
        limit: int = 5,
        degree_lane: object | None = None,
    ) -> MetricBundleResolution:
        return MetricBundleResolution(query=query)

    async def resolve_profile_field_authority(
        self, query: str, *, limit: int = 5
    ) -> ProfileFieldResolution:
        normalized = query.casefold().strip()
        if normalized == "enrollment":
            return ProfileFieldResolution(
                query=query,
                resolved=NCESFieldCandidate(
                    field_key="enrollment",
                    label="NCES enrollment",
                    data_type="integer",
                ),
                resolution_method="exact",
            )
        if normalized == "pupil teacher ratio":
            return ProfileFieldResolution(
                query=query,
                resolved=NCESFieldCandidate(
                    field_key="pupil_teacher_ratio",
                    label="Pupil-teacher ratio",
                    data_type="number",
                ),
                resolution_method="exact",
            )
        return ProfileFieldResolution(query=query)


class _StubAdjudicatorAgent:
    """Adjudicator stub that records every call and returns a scripted decision.

    Step 5 only needs to verify the orchestrator's dispatch behavior
    (skip-on-high-confidence, call-on-ambiguous, apply decision); the
    adjudicator's own decision logic is exercised in
    ``test_catalog_adjudicator_entity_types.py``.
    """

    def __init__(self, decision: CatalogAdjudicationDecision) -> None:
        self._decision = decision
        self.calls: list[CatalogAdjudicationDeps] = []

    async def run(
        self, user_prompt: str, *, deps: CatalogAdjudicationDeps
    ) -> "_StubAdjudicatorResult":
        self.calls.append(deps)
        return _StubAdjudicatorResult(self._decision)


class _StubAdjudicatorResult:
    def __init__(self, decision: CatalogAdjudicationDecision) -> None:
        self.output = decision


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _sort_new_02_turn() -> PlannerTurn:
    """SORT-NEW-02 / case 8 north-star planner draft."""

    return PlannerTurn(
        route="execute",
        confidence=0.82,
        query_plan=QueryPlan(
            operation="rank",
            question="Rank districts by enrollment, largest first.",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="enrollment", role="primary")],
            sort=SortSpec(field="enrollment", direction="desc"),
            output=OutputSpec(format="table"),
        ),
    )


def _non_rankable_profile_turn() -> PlannerTurn:
    """Rank by pupil-teacher ratio — NCES allowlist member, not rankable."""

    return PlannerTurn(
        route="execute",
        confidence=0.5,
        query_plan=QueryPlan(
            operation="rank",
            question="Rank districts by pupil-teacher ratio.",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="pupil teacher ratio", role="primary")],
            sort=SortSpec(field="pupil teacher ratio", direction="asc"),
            output=OutputSpec(format="table"),
        ),
    )


def _unsupported_concept_turn() -> PlannerTurn:
    return PlannerTurn(
        route="execute",
        confidence=0.5,
        query_plan=QueryPlan(
            operation="lookup",
            question="What about union release time in Philadelphia?",
            selection=SelectionSpec(
                scope="named_districts", districts=["Philadelphia"]
            ),
            metrics=[MetricSpec(name="union release time", role="primary")],
            output=OutputSpec(format="text"),
        ),
    )


def _unresolved_phrase_turn() -> PlannerTurn:
    return PlannerTurn(
        route="execute",
        confidence=0.5,
        query_plan=QueryPlan(
            operation="lookup",
            question="Show me the salary schedule.",
            selection=SelectionSpec(
                scope="named_districts", districts=["Denver"]
            ),
            metrics=[MetricSpec(name="salary schedule", role="primary")],
            output=OutputSpec(format="text"),
        ),
    )


def _pipeline(catalog: _FakeCatalog, *, adjudicator=None) -> CatalogPlanPipeline:
    return CatalogPlanPipeline(
        CatalogReconciler(catalog), adjudicator=adjudicator
    )


def _deps(
    *, message: str = "...", catalog_pipeline: CatalogPlanPipeline | None = None
) -> PlannerDeps:
    return PlannerDeps(message=message, catalog_pipeline=catalog_pipeline)


# ---------------------------------------------------------------------------
# Pipeline orchestration — adjudicator skip / call dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_skips_adjudicator_on_high_confidence() -> None:
    """``confidence='high'`` resolutions never reach the adjudicator —
    plan decision 3 (deterministic path saves a gateway call)."""

    decision = CatalogAdjudicationDecision(
        action="select_one",
        selected_ids=["profile_rank_field:enrollment"],
        confidence=1.0,
        rationale="should not be called",
    )
    adjudicator = _StubAdjudicatorAgent(decision)
    pipeline = _pipeline(_FakeCatalog(), adjudicator=adjudicator)

    finalized = await pipeline.reconcile_and_finalize(
        _sort_new_02_turn().query_plan
    )

    assert finalized.is_ready
    assert adjudicator.calls == [], "adjudicator must be skipped"


@pytest.mark.asyncio
async def test_pipeline_calls_adjudicator_on_ambiguous_resolution() -> None:
    """When reconciliation returns ambiguous candidates, the adjudicator
    is invoked and its pick becomes the resolution's primary."""

    resolution = CatalogResolution(
        phrase="starting salary",
        role_hint="metric_subject",
        candidates=(
            ResolvedEntity(
                entity_type="metric",
                catalog_id="metric:89",
                label="BA starting salary",
                score=0.6,
                source="test",
                metadata={"metric_id": 89},
            ),
            ResolvedEntity(
                entity_type="metric",
                catalog_id="metric:96",
                label="MA starting salary",
                score=0.6,
                source="test",
                metadata={"metric_id": 96},
            ),
        ),
        confidence="ambiguous",
    )
    decision = CatalogAdjudicationDecision(
        action="select_one",
        selected_ids=["metric:96"],
        confidence=0.9,
        rationale="user phrasing prefers MA lane",
    )
    adjudicator = _StubAdjudicatorAgent(decision)
    pipeline = CatalogPlanPipeline(
        CatalogReconciler(_FakeCatalog()), adjudicator=adjudicator
    )

    adjudicated = await pipeline._adjudicate_one(resolution, adjudicator)

    assert len(adjudicator.calls) == 1
    assert adjudicated.candidates[0].catalog_id == "metric:96"
    assert adjudicated.confidence == "high"


@pytest.mark.asyncio
async def test_pipeline_skips_adjudicator_when_none_configured() -> None:
    """No adjudicator → orchestrator must not raise; returns the
    reconciler output unchanged."""

    pipeline = _pipeline(_FakeCatalog(), adjudicator=None)

    finalized = await pipeline.reconcile_and_finalize(
        _sort_new_02_turn().query_plan
    )

    # No adjudicator means we trust the reconciler's primary, which is
    # already the rank-fields entry for enrollment in sort_key context.
    assert finalized.is_ready
    step = finalized.plan.sort_steps[0]
    assert step.field == "enrollment"
    assert step.key_type == "profile_field"


# ---------------------------------------------------------------------------
# PR1b — district-geography adjudicator hardening (defense-in-depth, #1248)
# ---------------------------------------------------------------------------


def _district_entity(
    *, catalog_id: str, label: str, state: str, score: float = _SCORE_RESOLVED_SINGLE
) -> ResolvedEntity:
    """Build a district candidate the reconciler-widened search would emit."""

    return ResolvedEntity(
        entity_type="district",
        catalog_id=catalog_id,
        label=label,
        score=score,
        source="search_district_candidates",
        metadata={"state": state},
    )


def _ambiguous_district_resolution() -> CatalogResolution:
    """A single district NAME that two real geographies share.

    The reconciler's widened candidate search surfaces both the Washington, DC
    and the Washington-state match. A naive single-name match would score one
    high and skip the adjudicator — exactly the DC/Houston bug shape. The
    geography is ambiguous, so adjudication MUST run.
    """

    return CatalogResolution(
        phrase="Washington",
        role_hint="selection_district",
        candidates=(
            _district_entity(
                catalog_id="district:101",
                label="District of Columbia Public Schools",
                state="DC",
                score=_SCORE_RESOLVED_SINGLE,
            ),
            _district_entity(
                catalog_id="district:202",
                label="Washington School District",
                state="WA",
                score=_SCORE_CANDIDATE,
            ),
        ),
        confidence="high",
    )


def test_geographically_ambiguous_district_does_not_take_high_skip() -> None:
    """PR1b (a): a ``confidence='high'`` district resolution whose candidates
    span more than one geography MUST NOT take the deterministic skip — the
    multi-geography ambiguity has to reach the adjudicator."""

    resolution = _ambiguous_district_resolution()

    assert _should_skip_adjudication(resolution) is False


def test_single_geography_district_still_skips_on_high() -> None:
    """Guard the narrowing: a high-confidence district whose candidates all
    share one geography keeps the deterministic skip (no needless gateway
    call). Only multi-geography ambiguity forces adjudication."""

    resolution = CatalogResolution(
        phrase="Houston",
        role_hint="selection_district",
        candidates=(
            _district_entity(
                catalog_id="district:301",
                label="Houston Independent School District",
                state="TX",
                score=_SCORE_RESOLVED_SINGLE,
            ),
            _district_entity(
                catalog_id="district:302",
                label="North Houston Early College",
                state="TX",
                score=_SCORE_CANDIDATE,
            ),
        ),
        confidence="high",
    )

    assert _should_skip_adjudication(resolution) is True


@pytest.mark.asyncio
async def test_ambiguous_district_reaches_adjudicator() -> None:
    """End-to-end at the orchestrator: the geographically-ambiguous district
    resolution is dispatched to the adjudicator instead of being skipped."""

    resolution = _ambiguous_district_resolution()
    decision = CatalogAdjudicationDecision(
        action="clarify",
        clarification_question="Did you mean Washington, DC or Washington state?",
        confidence=0.5,
        rationale="two geographies share the name",
    )
    adjudicator = _StubAdjudicatorAgent(decision)

    adjudicated = await _pipeline(
        _FakeCatalog(), adjudicator=adjudicator
    )._adjudicate_one(resolution, adjudicator)

    assert len(adjudicator.calls) == 1, "ambiguous district must reach adjudicator"
    # clarify must BLOCK downstream — see _apply_adjudication test below.
    assert adjudicated.primary is None


def test_apply_adjudication_clarify_blocks_district() -> None:
    """PR1b (b): a ``clarify`` decision must actually change the resolution so
    the finalizer blocks. Today it no-ops and the original primary slips
    through. After PR1b the resolution carries no finalizable primary."""

    resolution = _ambiguous_district_resolution()
    decision = CatalogAdjudicationDecision(
        action="clarify",
        clarification_question="Washington, DC or Washington state?",
        confidence=0.5,
        rationale="ambiguous geography",
    )

    applied = _apply_adjudication(resolution, decision)

    # The pre-PR1b no-op kept candidates[0] as a real-but-maybe-wrong primary.
    assert applied.primary is None
    assert applied.confidence == "none"


def test_apply_adjudication_no_match_blocks_district() -> None:
    """PR1b (b): a ``no_match`` decision must also block — none of the real
    candidates is the district the user meant."""

    resolution = _ambiguous_district_resolution()
    decision = CatalogAdjudicationDecision(
        action="no_match",
        confidence=0.4,
        rationale="no candidate matches the named geography",
    )

    applied = _apply_adjudication(resolution, decision)

    assert applied.primary is None
    assert applied.confidence == "none"


def _ambiguous_metric_resolution() -> CatalogResolution:
    """A metric phrase whose candidates are several materially-different metrics.

    Mirrors "paid sick days in the first year" → 5 leave metrics tying at 0.6:
    the adjudicator returns ``clarify``, but these are all real, answerable
    metric drawers (not a wrong-entity risk like the district case), so Fix 4B
    best-guesses candidates[0] instead of collapsing to the canned rescue.
    """

    return CatalogResolution(
        phrase="paid sick days in the first year",
        role_hint="metric_subject",
        candidates=(
            ResolvedEntity(
                entity_type="metric",
                catalog_id="metric:198",
                label="Maximum number of annual paid sick days",
                score=_SCORE_CANDIDATE,
                source="search_metric_candidates",
            ),
            ResolvedEntity(
                entity_type="metric",
                catalog_id="metric:207",
                label="Number of paid personal leave days granted in first year",
                score=_SCORE_CANDIDATE,
                source="search_metric_candidates",
            ),
        ),
        confidence="ambiguous",
    )


def test_apply_adjudication_clarify_best_guesses_metric_drawer() -> None:
    """Fix 4B (#1 refusal family): a ``clarify`` on a METRIC tie must NOT collapse
    to ``confidence="none"`` (which rescues). It promotes the top candidate so
    the finalizer sees a real primary and the plan reaches execution; the
    concrete metric + alternates disclosure are re-derived at execution."""

    resolution = _ambiguous_metric_resolution()
    decision = CatalogAdjudicationDecision(
        action="clarify",
        clarification_question="Which sick-leave metric did you mean?",
        confidence=0.5,
        rationale="five leave metrics tie",
    )

    applied = _apply_adjudication(resolution, decision)

    # Unblocked: a real primary survives (candidates[0]) so is_ready=True downstream.
    assert applied.primary is not None
    assert applied.primary.catalog_id == "metric:198"
    assert applied.confidence != "none"


def test_apply_adjudication_no_match_best_guesses_metric_drawer() -> None:
    """Fix 4B: ``no_match`` on a metric tie also best-guesses (answerable phrase,
    not a wrong-entity risk) rather than collapsing to the canned rescue."""

    resolution = _ambiguous_metric_resolution()
    decision = CatalogAdjudicationDecision(
        action="no_match",
        confidence=0.4,
        rationale="no single clean winner",
    )

    applied = _apply_adjudication(resolution, decision)

    assert applied.primary is not None
    assert applied.primary.catalog_id == "metric:198"
    assert applied.confidence != "none"


def test_apply_adjudication_select_one_still_promotes() -> None:
    """Regression guard: PR1b must not break the select_one promotion path."""

    resolution = _ambiguous_district_resolution()
    decision = CatalogAdjudicationDecision(
        action="select_one",
        selected_ids=["district:202"],
        confidence=0.9,
        rationale="user meant Washington state",
    )

    applied = _apply_adjudication(resolution, decision)

    assert applied.primary is not None
    assert applied.primary.catalog_id == "district:202"
    assert applied.confidence == "high"


@pytest.mark.asyncio
async def test_pipeline_does_not_block_named_district_plan() -> None:
    """#1310 / #959: districts do NOT flow through this verifier. The reconciler
    has metric / profile-field / unsupported-concept drawers but no district
    drawer, so a named district must be DROPPED before reconciling — otherwise it
    reconciles to ``confidence="none"`` and raises a false
    ``UnresolvedPhraseBlocker`` that rescue-clarifies EVERY named-district plan.

    The "right district" defense-in-depth this test used to assert at the
    finalizer moved downstream and is covered there: execution
    ``resolve_selection`` clarifies a genuinely ambiguous district
    (``test_mvp_execution.py`` Portland → ``missing_fields == ["district"]``),
    and the result-boundary Gate-3 ``intended_geography`` validator flags a
    real-but-wrong geography (``test_intended_geography_validator.py`` —
    ``..._flagged_dc`` / ``..._flagged_houston``). Supersedes the old
    finalizer-level block (removed in #1310)."""

    # _sort_new_02_turn() is a proven is_ready=True plan; swapping only the
    # selection to a named district isolates the district-drop behavior — the
    # district is the only mention that could newly block.
    plan = _sort_new_02_turn().query_plan.model_copy(
        update={
            "selection": SelectionSpec(
                scope="named_districts", districts=["Washington"]
            )
        }
    )
    pipeline = _pipeline(_FakeCatalog(), adjudicator=None)

    finalized = await pipeline.reconcile_and_finalize(plan)

    assert finalized.is_ready
    assert not any(
        getattr(blocker, "phrase", None) == "Washington"
        for blocker in finalized.blockers
    )


# ---------------------------------------------------------------------------
# validate_planner_turn_quality_async — flag-off pass-through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validator_passes_through_when_no_pipeline() -> None:
    """``deps.catalog_pipeline is None`` → behavior identical to the
    legacy sync validator. Regression guard for production today.

    The SORT-NEW-02 turn passes the legacy shape gate (1 metric,
    1 primary, rank operation), so the legacy validator returns the
    turn unchanged. The v2 wrapper must do the same when pipeline
    is None — no plan rewrite, no ModelRetry.
    """

    turn = _sort_new_02_turn()

    validated = await validate_planner_turn_quality_async(turn, _deps())

    assert validated.route == turn.route
    assert validated.query_plan == turn.query_plan


# ---------------------------------------------------------------------------
# validate_planner_turn_quality_async — flag-on closure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sort_new_02_closes_when_pipeline_is_on() -> None:
    """The north-star case: with the pipeline attached, the validator
    accepts the SORT-NEW-02 turn and replaces ``query_plan.sort`` with
    a ``sort_steps`` entry keyed by ``profile_field``.

    Closes case 8 end-to-end at the planner-validator boundary.
    """

    pipeline = _pipeline(_FakeCatalog(), adjudicator=None)
    turn = _sort_new_02_turn()

    validated = await validate_planner_turn_quality_async(
        turn, _deps(catalog_pipeline=pipeline)
    )

    assert validated.route == "execute"
    plan = validated.query_plan
    assert plan is not None
    assert plan.sort is None
    assert len(plan.sort_steps) == 1
    step = plan.sort_steps[0]
    assert step.field == "enrollment"
    assert step.key_type == "profile_field"
    assert step.direction == "desc"


# ---------------------------------------------------------------------------
# validate_planner_turn_quality_async — blocker → ModelRetry hints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_rankable_blocker_raises_retry_with_rankable_alternatives() -> None:
    pipeline = _pipeline(_FakeCatalog(), adjudicator=None)
    turn = _non_rankable_profile_turn()

    with pytest.raises(ModelRetry) as exc:
        await validate_planner_turn_quality_async(
            turn, _deps(catalog_pipeline=pipeline)
        )

    hint = str(exc.value)
    assert "pupil teacher ratio" in hint
    assert "rankable alternatives" in hint.lower()
    assert "NCES enrollment" in hint or "enrollment" in hint.lower()
    assert "FRPL" in hint or "frpl" in hint.lower()


@pytest.mark.asyncio
async def test_unsupported_concept_raises_retry_with_refusal_language() -> None:
    pipeline = _pipeline(_FakeCatalog(), adjudicator=None)
    turn = _unsupported_concept_turn()

    with pytest.raises(ModelRetry) as exc:
        await validate_planner_turn_quality_async(
            turn, _deps(catalog_pipeline=pipeline)
        )

    hint = str(exc.value)
    assert "union release time" in hint
    assert "governed-unsupported" in hint
    assert "clarify" in hint or "refuse" in hint


@pytest.mark.asyncio
async def test_unresolved_phrase_raises_retry_with_rephrase_guidance() -> None:
    pipeline = _pipeline(_FakeCatalog(), adjudicator=None)
    turn = _unresolved_phrase_turn()

    with pytest.raises(ModelRetry) as exc:
        await validate_planner_turn_quality_async(
            turn, _deps(catalog_pipeline=pipeline)
        )

    hint = str(exc.value)
    assert "salary schedule" in hint
    assert "no candidates" in hint
    assert "clarify" in hint or "phrasing" in hint


# ---------------------------------------------------------------------------
# Hint formatter — priority ordering
# ---------------------------------------------------------------------------


def test_blocker_hint_prioritizes_unsupported_over_unresolved() -> None:
    """When multiple blockers are present, the most actionable hint
    comes first so the model focuses its retry on the right edit."""

    from compass_backend.contracts.finalization import (
        UnresolvedPhraseBlocker,
        UnsupportedConceptBlocker,
    )

    blockers = (
        UnresolvedPhraseBlocker(phrase="something obscure"),
        UnsupportedConceptBlocker(
            phrase="union release time",
            catalog_id="unsupported_concept:union_release_time",
            message="Compass does not yet have a governed metric for union release time.",
        ),
    )

    hint = _model_retry_hint_from_finalizer_blockers(blockers)
    lines = hint.split("\n")
    assert "union release time" in lines[0]
    assert "something obscure" in lines[-1]
