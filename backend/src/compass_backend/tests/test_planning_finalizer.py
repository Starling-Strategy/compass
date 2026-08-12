"""Tests for the deterministic plan finalizer.

Step 4 of the catalog-resolution-unification plan
(``docs/plans/2026-05-28-catalog-resolution-unification-plan.md``).

The finalizer is a pure function — every test here constructs a
:class:`QueryPlan` + a :class:`CatalogReconciliationReport`, calls
:func:`finalize_plan`, and asserts on the returned
:class:`FinalizedPlan`. No catalog. No fakes. No I/O.

The closure cases the plan pins:

- SORT-NEW-02 / case 8 — ``enrollment`` in ``sort.field`` with
  ``operation=rank`` moves to ``sort_steps`` with
  ``key_type="profile_field"``; ``sort`` cleared; zero blockers.
- Row 20 / case 13 — ``free and reduced lunch share`` in ``sort_steps``
  with the wrong ``key_type`` is corrected and the field rewritten to
  the rank-field key.
- Family-A drawer rebalancing — ``enrollment`` in ``metrics`` moves to
  ``profile_fields`` keyed by the resolved field_key.
- Non-rankable profile field in a sort context surfaces as
  :class:`NonRankableProfileFieldBlocker` with the rankable
  alternatives from the governed rank-fields enum.
- Unsupported concept anywhere → refuse blocker; unresolved phrase →
  clarify-shaped blocker.
"""

from __future__ import annotations


from compass_backend.contracts.catalog_resolution import (
    CatalogReconciliationReport,
    CatalogResolution,
    PhraseRoleHint,
    ResolutionConfidence,
    ResolvedEntity,
    ResolvedEntityType,
)
from compass_backend.contracts.finalization import (
    AmbiguousDistrictBlocker,
    NonRankableProfileFieldBlocker,
    SlotMove,
    UnresolvedPhraseBlocker,
    UnsupportedConceptBlocker,
)
from compass_backend.contracts.planning import (
    FilterSpec,
    MetricSpec,
    OutputSpec,
    ProfileFieldSpec,
    QueryPlan,
    SelectionSpec,
    SortSpec,
    SortStepSpec,
)
from compass_backend.planning.finalizer import finalize_plan


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _entity(
    entity_type: ResolvedEntityType,
    catalog_id: str,
    *,
    label: str | None = None,
    score: float = 0.95,
    field_key: str | None = None,
    metric_id: int | None = None,
    message: str | None = None,
) -> ResolvedEntity:
    metadata: dict[str, object] = {}
    if field_key:
        metadata["field_key"] = field_key
    if metric_id:
        metadata["metric_id"] = metric_id
    if message:
        metadata["message"] = message
    return ResolvedEntity(
        entity_type=entity_type,
        catalog_id=catalog_id,
        label=label or catalog_id,
        score=score,
        source="test",
        metadata=metadata,
    )


def _resolution(
    phrase: str,
    primary: ResolvedEntity | None,
    *,
    role_hint: PhraseRoleHint = "unspecified",
    confidence: ResolutionConfidence | None = None,
    extra_candidates: tuple[ResolvedEntity, ...] = (),
) -> CatalogResolution:
    candidates = (primary,) if primary is not None else ()
    candidates = candidates + extra_candidates
    if confidence is None:
        confidence = "high" if primary is not None else "none"
    return CatalogResolution(
        phrase=phrase,
        role_hint=role_hint,
        candidates=candidates,
        confidence=confidence,
    )


def _report(*resolutions: CatalogResolution) -> CatalogReconciliationReport:
    return CatalogReconciliationReport(resolutions=tuple(resolutions))


def _rank_plan(*, sort_field: str = "enrollment") -> QueryPlan:
    return QueryPlan(
        operation="rank",
        question="Rank districts by enrollment, largest first.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name=sort_field, role="primary")],
        sort=SortSpec(field=sort_field, direction="desc"),
        output=OutputSpec(format="table"),
    )


def _lookup_plan(*, metric_name: str = "enrollment") -> QueryPlan:
    return QueryPlan(
        operation="lookup",
        question=f"What is the {metric_name}?",
        selection=SelectionSpec(
            scope="named_districts", districts=["Philadelphia"]
        ),
        metrics=[MetricSpec(name=metric_name, role="primary")],
        output=OutputSpec(format="text"),
    )


# ---------------------------------------------------------------------------
# SORT-NEW-02 / case 8 closure
# ---------------------------------------------------------------------------


def test_sort_new_02_closure_enrollment_in_sort_field() -> None:
    """North-star case: rank-by-enrollment finalizes to sort_steps."""

    plan = _rank_plan()
    report = _report(
        _resolution(
            "enrollment",
            _entity(
                "profile_rank_field",
                "profile_rank_field:enrollment",
                label="NCES enrollment",
                field_key="enrollment",
            ),
            role_hint="sort_key",
        )
    )

    finalized = finalize_plan(plan, report)

    assert finalized.is_ready
    assert finalized.plan.sort is None, "legacy SortSpec must be cleared"
    assert len(finalized.plan.sort_steps) == 1
    step = finalized.plan.sort_steps[0]
    assert step.key_type == "profile_field"
    assert step.field == "enrollment"
    assert step.direction == "desc"
    assert step.phase == "presentation"

    # The metrics slot still has enrollment as a MetricSpec — that's a
    # rebalancing move which we assert next; for this test we just
    # confirm no blockers obstruct the SORT-NEW-02 closure.
    assert finalized.blockers == ()


def test_sort_new_02_metric_phrase_rebalances_to_profile_fields() -> None:
    """The same plan has ``enrollment`` in ``metrics``; the reconciler
    resolves it to a profile drawer, so the finalizer moves it."""

    plan = _rank_plan()
    report = _report(
        _resolution(
            "enrollment",
            _entity(
                "profile_rank_field",
                "profile_rank_field:enrollment",
                label="NCES enrollment",
                field_key="enrollment",
            ),
            role_hint="sort_key",
        )
    )

    finalized = finalize_plan(plan, report)

    assert finalized.plan.metrics == []
    assert len(finalized.plan.profile_fields) == 1
    assert finalized.plan.profile_fields[0].name == "enrollment"

    drawer_moves = [
        move
        for move in finalized.slot_moves
        if move.kind == "metric_into_profile_fields"
    ]
    assert len(drawer_moves) == 1
    assert drawer_moves[0].phrase == "enrollment"
    assert drawer_moves[0].resolved_entity.catalog_id == (
        "profile_rank_field:enrollment"
    )


def test_named_district_with_none_resolution_does_not_block() -> None:
    """#959: a named district must NOT produce an UnresolvedPhraseBlocker even
    when the reconciler returns confidence="none" for it.

    The reconciler has no district drawer, so a real district always resolves to
    "none"; before this fix that falsely blocked every named-district plan
    (rescue-clarify — the #959 clarify-over-answer cluster). District existence
    is enforced at execution (``resolve_selection`` refuses an unresolved
    district) and the right-district check at the result boundary
    (``intended_geography`` Gate-3), not at the planner-output verifier.
    """

    plan = QueryPlan(
        operation="lookup",
        question="Starting teacher salary in Philadelphia",
        selection=SelectionSpec(
            scope="named_districts", districts=["Philadelphia"]
        ),
        metrics=[MetricSpec(name="starting teacher salary", role="primary")],
        output=OutputSpec(format="text"),
    )
    report = _report(
        _resolution(
            "starting teacher salary",
            _entity("metric", "metric:89", label="BA starting salary", metric_id=89),
        ),
        # The district resolves to confidence="none" (no district drawer); the
        # finalizer must not treat that as an unresolved-phrase refusal.
        _resolution("Philadelphia", None),
    )

    finalized = finalize_plan(plan, report)

    district_blockers = [
        blocker
        for blocker in finalized.blockers
        if isinstance(blocker, UnresolvedPhraseBlocker)
        and "district" in blocker.source_field.lower()
    ]
    assert district_blockers == [], finalized.blockers


# ---------------------------------------------------------------------------
# Row 20 / case 13 — FRPL share in sort_steps with wrong key_type
# ---------------------------------------------------------------------------


def test_frpl_share_in_sort_steps_corrects_key_type_and_field() -> None:
    """The planner emitted ``key_type=policy_metric``; the finalizer
    flips it to ``profile_field`` and rewrites the field to the
    rank-field key (``frpl_pct``) so the executor's
    ``rank_districts_by_profile_field`` finds it."""

    plan = QueryPlan(
        operation="rank",
        question=(
            "Show me starting teacher salaries for districts with the "
            "highest free-and-reduced lunch share."
        ),
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary", role="primary")],
        sort_steps=[
            SortStepSpec(
                field="free and reduced lunch share",
                direction="desc",
                key_type="policy_metric",
                phase="presentation",
            )
        ],
        output=OutputSpec(format="table"),
    )
    report = _report(
        _resolution(
            "free and reduced lunch share",
            _entity(
                "profile_rank_field",
                "profile_rank_field:frpl_pct",
                label="FRPL %",
                field_key="frpl_pct",
            ),
            role_hint="sort_key",
        ),
        _resolution(
            "starting salary",
            _entity(
                "metric",
                "metric:89",
                label="BA starting salary",
                metric_id=89,
            ),
            role_hint="metric_subject",
        ),
    )

    finalized = finalize_plan(plan, report)

    assert finalized.is_ready
    assert len(finalized.plan.sort_steps) == 1
    step = finalized.plan.sort_steps[0]
    assert step.field == "frpl_pct"
    assert step.key_type == "profile_field"

    corrections = [
        move
        for move in finalized.slot_moves
        if move.kind == "sort_step_key_type_corrected"
    ]
    assert len(corrections) == 1


# ---------------------------------------------------------------------------
# Lookup case — drawer rebalance with no sort involvement
# ---------------------------------------------------------------------------


def test_enrollment_lookup_rebalances_metric_to_profile_field() -> None:
    """Lookup plan with ``enrollment`` in ``metrics`` moves it to
    ``profile_fields`` using the resolved field_key as the name.

    Plan has a second metric so the post-move shape still satisfies
    the ``operation=lookup`` validator's "at least one metric" rule.
    The finalizer does not flip operations (that's a step-5
    validator responsibility — finalizer is purely slot-mechanical).
    """

    plan = QueryPlan(
        operation="lookup",
        question="Show me enrollment and starting salary.",
        selection=SelectionSpec(
            scope="named_districts", districts=["Philadelphia"]
        ),
        metrics=[
            MetricSpec(name="enrollment", role="primary"),
            MetricSpec(name="starting salary", role="comparison"),
        ],
        output=OutputSpec(format="text"),
    )
    report = _report(
        _resolution(
            "enrollment",
            _entity(
                "profile_field",
                "profile_field:enrollment",
                label="NCES enrollment",
                field_key="enrollment",
            ),
            role_hint="profile_subject",
        ),
        _resolution(
            "starting salary",
            _entity(
                "metric",
                "metric:89",
                label="BA starting salary",
                metric_id=89,
            ),
            role_hint="metric_subject",
        ),
    )

    finalized = finalize_plan(plan, report)

    assert finalized.is_ready
    assert [m.name for m in finalized.plan.metrics] == ["starting salary"]
    assert [p.name for p in finalized.plan.profile_fields] == ["enrollment"]


def test_profile_field_phrase_resolves_to_metric_moves_back() -> None:
    """Symmetric case: a phrase the planner put in ``profile_fields``
    that the reconciler resolved to a metric drawer.

    Uses ``operation=profile_lookup`` and keeps a second profile_field
    so the post-move shape stays valid (profile_lookup requires
    non-empty ``profile_fields``).
    """

    plan = QueryPlan(
        operation="profile_lookup",
        question="Show me starting salary and enrollment.",
        selection=SelectionSpec(
            scope="named_districts", districts=["Philadelphia"]
        ),
        profile_fields=[
            ProfileFieldSpec(name="starting salary"),
            ProfileFieldSpec(name="enrollment"),
        ],
        output=OutputSpec(format="text"),
    )
    report = _report(
        _resolution(
            "starting salary",
            _entity(
                "metric_bundle",
                "metric_bundle:starting_salary",
                label="starting_salary",
            ),
            role_hint="profile_subject",
        ),
        _resolution(
            "enrollment",
            _entity(
                "profile_field",
                "profile_field:enrollment",
                label="NCES enrollment",
                field_key="enrollment",
            ),
            role_hint="profile_subject",
        ),
    )

    finalized = finalize_plan(plan, report)

    assert finalized.is_ready
    assert [m.name for m in finalized.plan.metrics] == ["starting salary"]
    assert [p.name for p in finalized.plan.profile_fields] == ["enrollment"]


# ---------------------------------------------------------------------------
# Blocker cases
# ---------------------------------------------------------------------------


def test_non_rankable_profile_field_blocks_with_rankable_alternatives() -> None:
    """``pupil_teacher_ratio`` is in the NCES allowlist but NOT in the
    governed rank-fields enum. Asking to rank by it must surface a
    blocker carrying the rankable alternatives (enrollment + frpl_pct)."""

    plan = _rank_plan(sort_field="pupil teacher ratio")
    report = _report(
        _resolution(
            "pupil teacher ratio",
            _entity(
                "profile_field",
                "profile_field:pupil_teacher_ratio",
                label="Pupil-teacher ratio",
                field_key="pupil_teacher_ratio",
            ),
            role_hint="sort_key",
        )
    )

    finalized = finalize_plan(plan, report)

    assert not finalized.is_ready
    blockers = [
        b for b in finalized.blockers
        if isinstance(b, NonRankableProfileFieldBlocker)
    ]
    assert len(blockers) == 1
    blocker = blockers[0]
    assert blocker.phrase == "pupil teacher ratio"
    assert blocker.resolved_field_key == "pupil_teacher_ratio"
    candidate_keys = {
        c.metadata.get("field_key") for c in blocker.rankable_candidates
    }
    assert candidate_keys == {"enrollment", "frpl_pct"}


def test_unsupported_concept_in_metrics_blocks_refuse() -> None:
    """Phrase resolved to ``unsupported_concept`` produces a refusal blocker."""

    plan = QueryPlan(
        operation="lookup",
        question="What about union release time?",
        selection=SelectionSpec(
            scope="named_districts", districts=["Philadelphia"]
        ),
        metrics=[MetricSpec(name="union release time", role="primary")],
        output=OutputSpec(format="text"),
    )
    report = _report(
        _resolution(
            "union release time",
            _entity(
                "unsupported_concept",
                "unsupported_concept:union_release_time",
                label="Union release time",
                message=(
                    "Compass does not yet have a governed metric for "
                    "union release time."
                ),
            ),
            role_hint="metric_subject",
        )
    )

    finalized = finalize_plan(plan, report)

    assert not finalized.is_ready
    blockers = [
        b for b in finalized.blockers
        if isinstance(b, UnsupportedConceptBlocker)
    ]
    assert len(blockers) == 1
    blocker = blockers[0]
    assert blocker.catalog_id == "unsupported_concept:union_release_time"
    assert "governed metric" in blocker.message


def test_unresolved_phrase_in_metrics_blocks_clarify() -> None:
    """Catalog gap surfaces as an :class:`UnresolvedPhraseBlocker`
    distinct from refusal."""

    plan = QueryPlan(
        operation="lookup",
        question="What is the salary schedule for Denver?",
        selection=SelectionSpec(
            scope="named_districts", districts=["Denver"]
        ),
        metrics=[MetricSpec(name="salary schedule", role="primary")],
        output=OutputSpec(format="text"),
    )
    report = _report(_resolution("salary schedule", None))

    finalized = finalize_plan(plan, report)

    blockers = [
        b for b in finalized.blockers
        if isinstance(b, UnresolvedPhraseBlocker)
    ]
    assert len(blockers) == 1
    assert blockers[0].phrase == "salary schedule"
    assert blockers[0].source_field == "metrics[0].name"


def test_ambiguous_district_blocker_when_multiple_district_candidates() -> None:
    """Forward-compat: ``selection.districts`` phrase resolves to many
    governed districts → :class:`AmbiguousDistrictBlocker`."""

    plan = QueryPlan(
        operation="lookup",
        question="Show me Philadelphia's starting salary.",
        selection=SelectionSpec(
            scope="named_districts", districts=["Philadelphia"]
        ),
        metrics=[MetricSpec(name="starting salary", role="primary")],
        output=OutputSpec(format="text"),
    )
    report = _report(
        _resolution(
            "Philadelphia",
            _entity("district", "district:6700", label="Philadelphia, PA"),
            role_hint="selection_district",
            confidence="ambiguous",
            extra_candidates=(
                _entity("district", "district:2901", label="Philadelphia, MS"),
            ),
        ),
        _resolution(
            "starting salary",
            _entity("metric", "metric:89", label="BA starting salary", metric_id=89),
            role_hint="metric_subject",
        ),
    )

    finalized = finalize_plan(plan, report)

    blockers = [
        b for b in finalized.blockers
        if isinstance(b, AmbiguousDistrictBlocker)
    ]
    assert len(blockers) == 1
    blocker = blockers[0]
    assert blocker.phrase == "Philadelphia"
    assert {c.catalog_id for c in blocker.candidates} == {
        "district:6700",
        "district:2901",
    }


# ---------------------------------------------------------------------------
# Pass-through and invariants
# ---------------------------------------------------------------------------


def test_already_correct_plan_passes_through_with_no_moves() -> None:
    """A plan already in the canonical sort_steps shape stays unchanged."""

    plan = QueryPlan(
        operation="rank",
        question="Rank by BA salary.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary", role="primary")],
        sort_steps=[
            SortStepSpec(
                field="starting salary",
                direction="desc",
                key_type="policy_metric",
                phase="presentation",
            )
        ],
        output=OutputSpec(format="table"),
    )
    report = _report(
        _resolution(
            "starting salary",
            _entity(
                "metric",
                "metric:89",
                label="BA starting salary",
                metric_id=89,
            ),
            role_hint="metric_subject",
        )
    )

    finalized = finalize_plan(plan, report)

    assert finalized.is_ready
    assert finalized.plan.sort is None
    assert finalized.plan.metrics == plan.metrics
    assert finalized.plan.sort_steps == plan.sort_steps
    assert finalized.slot_moves == ()


def test_finalize_plan_is_deterministic() -> None:
    """Pure function: same inputs produce equal outputs across calls."""

    plan = _rank_plan()
    report = _report(
        _resolution(
            "enrollment",
            _entity(
                "profile_rank_field",
                "profile_rank_field:enrollment",
                label="NCES enrollment",
                field_key="enrollment",
            ),
            role_hint="sort_key",
        )
    )

    first = finalize_plan(plan, report)
    second = finalize_plan(plan, report)

    assert first.model_dump() == second.model_dump()


def test_every_slot_move_references_a_resolved_entity() -> None:
    """The design invariant: the finalizer never invents an entity.

    Every :class:`SlotMove` in a passing finalization must carry a
    :class:`ResolvedEntity` that came from the reconciliation report.
    This test exercises three moves at once (drawer rebalance, sort
    collapse, key_type correction) and walks each move's
    ``resolved_entity`` to assert it shows up in the report.
    """

    plan = QueryPlan(
        operation="rank",
        question="Rank teacher salaries by FRPL share.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="enrollment", role="primary")],
        profile_fields=[ProfileFieldSpec(name="starting salary")],
        sort=SortSpec(field="enrollment", direction="desc"),
        sort_steps=[
            SortStepSpec(
                field="free and reduced lunch share",
                direction="desc",
                key_type="policy_metric",
                phase="presentation",
            )
        ],
        output=OutputSpec(format="table"),
    )
    report = _report(
        _resolution(
            "enrollment",
            _entity(
                "profile_rank_field",
                "profile_rank_field:enrollment",
                label="NCES enrollment",
                field_key="enrollment",
            ),
            role_hint="sort_key",
        ),
        _resolution(
            "starting salary",
            _entity(
                "metric_bundle",
                "metric_bundle:starting_salary",
                label="starting_salary",
            ),
            role_hint="profile_subject",
        ),
        _resolution(
            "free and reduced lunch share",
            _entity(
                "profile_rank_field",
                "profile_rank_field:frpl_pct",
                label="FRPL %",
                field_key="frpl_pct",
            ),
            role_hint="sort_key",
        ),
    )

    finalized = finalize_plan(plan, report)

    report_entity_ids = {
        candidate.catalog_id
        for resolution in report.resolutions
        for candidate in resolution.candidates
    }

    seeded_ids = {
        candidate.catalog_id
        for candidate in (
            _entity(
                "profile_rank_field",
                "profile_rank_field:enrollment",
                field_key="enrollment",
            ),
            _entity(
                "profile_rank_field",
                "profile_rank_field:frpl_pct",
                field_key="frpl_pct",
            ),
        )
    }

    for move in finalized.slot_moves:
        assert isinstance(move, SlotMove)
        assert move.resolved_entity.catalog_id in (
            report_entity_ids | seeded_ids
        ), (
            "every slot move must reference a ResolvedEntity from the "
            f"report; found stray {move.resolved_entity.catalog_id}"
        )


def test_reserved_filter_fields_do_not_produce_unresolved_blockers() -> None:
    """``filters[].field`` values of ``state``/``enrollment``/``value``
    are planner keywords, not user phrases. The finalizer must skip
    catalog lookups for them so they don't become spurious blockers.
    """

    plan = QueryPlan(
        operation="rank",
        question="Top salaries in California.",
        selection=SelectionSpec(scope="state", states=["CA"]),
        metrics=[MetricSpec(name="starting salary", role="primary")],
        filters=[
            __import__(
                "compass_backend.contracts.planning",
                fromlist=["FilterSpec"],
            ).FilterSpec(field="state", operator="equals", value="CA")
        ],
        output=OutputSpec(format="table"),
    )
    report = _report(
        _resolution(
            "starting salary",
            _entity("metric", "metric:89", label="BA starting salary", metric_id=89),
            role_hint="metric_subject",
        )
    )

    finalized = finalize_plan(plan, report)

    assert all(
        not (
            isinstance(b, UnresolvedPhraseBlocker) and b.phrase == "state"
        )
        for b in finalized.blockers
    )


def test_region_filter_field_does_not_block(_=None) -> None:
    """#1216/#1722: a ``kind="region"`` filter's ``field`` token is the literal
    word ``"region"`` (the region phrase rides in ``value``, resolved by
    reference.regions, never the catalog). The finalizer must NOT raise an
    ``UnresolvedPhraseBlocker`` on ``"region"`` — that landmine turned every
    region-filtered lookup/count/existence plan into a rescue
    ("Do any districts in the South offer differentiated pay?").
    """

    plan = QueryPlan(
        operation="lookup",
        question="Do any of the districts in the South offer differentiated pay?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(
                name="Performance pay based on evaluation ratings, "
                "unrelated to salary schedule",
                role="primary",
            )
        ],
        filters=[
            FilterSpec(
                field="region",
                operator="equals",
                value="the South",
                kind="region",
            )
        ],
        output=OutputSpec(format="table"),
    )
    report = _report(
        _resolution(
            "Performance pay based on evaluation ratings, unrelated to salary schedule",
            _entity(
                "metric",
                "metric:171",
                label="Performance pay based on evaluation ratings",
                metric_id=171,
            ),
            role_hint="metric_subject",
        )
    )

    finalized = finalize_plan(plan, report)

    assert not any(
        isinstance(b, UnresolvedPhraseBlocker) and b.phrase == "region"
        for b in finalized.blockers
    )
