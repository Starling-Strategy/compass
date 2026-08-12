"""Tests for peer_overrides on operation='peer_comparison' (Phase 3 Track 3.2).

TDD sequence (from track-3-2-plan.md):
  Step 1 — contract: reject peer_overrides on non-peer_comparison operations
  Step 2 — contract: PeerComparisonOverrides spec validates state normalisation
  Step 3 — contract: _normalize_exclude_states_list helper works for both specs
  Step 4 — execution: default plan does NOT emit override methodology codes
  Step 5 — execution: feature_set='frpl' emits similarity_feature_set_override code
  Step 6 — execution: exclude_states drops in-state peers
  Step 7 — execution: exclude_states emits similarity_exclude_states_applied code
  Step 8 — execution: PR 2A composition (filter + overrides both emit codes)
  Step 9 — execution: anchor unchanged when peer_overrides present
  Step 10 — multi-turn: peer_overrides carried in PendingQueryContext

All tests are written BEFORE implementation (TDD). They fail until the implementation
lands.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from compass_backend.catalog import (
    CatalogAliasRecord,
    DistrictCandidate,
    MetricCandidate,
)
from compass_backend.contracts import (
    FilterSpec,
    MetricSpec,
    QueryPlan,
    SelectionSpec,
)
from compass_backend.contracts.planning import (
    ClarificationRequest,
    PeerComparisonOverrides,
    PendingQueryContext,
    SimilarityQuerySpec,
)
from compass_backend.execution import DeterministicQueryExecutor, MetricAnswerRow
from compass_backend.planning.planner import (
    _merge_pending_context,
    pending_context_from_execution_clarification,
)
from compass_backend.tests.test_mvp_execution import (  # type: ignore[attr-defined]
    FakePolicyAnswerRepository,
)


# ---------------------------------------------------------------------------
# Step 1 — Contract: peer_overrides must not appear on other operations
# ---------------------------------------------------------------------------


def test_query_plan_rejects_peer_overrides_on_rank_operation() -> None:
    """peer_overrides set on operation='rank' raises ValidationError."""
    with pytest.raises(ValidationError, match="peer_comparison"):
        QueryPlan(
            operation="rank",
            question="Rank districts by max salary.",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="max salary")],
            peer_overrides=PeerComparisonOverrides(feature_set="frpl"),
        )


def test_query_plan_rejects_peer_overrides_on_similarity_operation() -> None:
    """peer_overrides set on operation='similarity' raises ValidationError."""
    from compass_backend.contracts import SimilarityQuerySpec

    with pytest.raises(ValidationError, match="peer_comparison"):
        QueryPlan(
            operation="similarity",
            question="Find peers to Denver.",
            selection=SelectionSpec(scope="named_districts", districts=["Denver Public Schools"]),
            similarity=SimilarityQuerySpec(
                anchor_name="Denver Public Schools",
                feature_set="all",
            ),
            peer_overrides=PeerComparisonOverrides(feature_set="frpl"),
        )


def test_query_plan_accepts_peer_overrides_on_peer_comparison() -> None:
    """peer_overrides is valid on operation='peer_comparison'."""
    plan = QueryPlan(
        operation="peer_comparison",
        question="Compare Denver and its peers on max salary, weighted by FRPL share.",
        selection=SelectionSpec(scope="named_districts", districts=["Denver Public Schools"]),
        metrics=[MetricSpec(name="max salary")],
        peer_overrides=PeerComparisonOverrides(feature_set="frpl"),
    )
    assert plan.peer_overrides is not None
    assert plan.peer_overrides.feature_set == "frpl"


def test_query_plan_peer_overrides_defaults_to_none() -> None:
    """Existing peer_comparison plans without peer_overrides still work."""
    plan = QueryPlan(
        operation="peer_comparison",
        question="Compare Denver and its peers on sick leave.",
        selection=SelectionSpec(scope="named_districts", districts=["Denver Public Schools"]),
        metrics=[MetricSpec(name="sick leave")],
    )
    assert plan.peer_overrides is None


# ---------------------------------------------------------------------------
# Step 2 — Contract: PeerComparisonOverrides spec validates state normalization
# ---------------------------------------------------------------------------


def test_peer_comparison_overrides_defaults() -> None:
    """PeerComparisonOverrides defaults to feature_set='all' and exclude_states=[]."""
    spec = PeerComparisonOverrides()
    assert spec.feature_set == "all"
    assert spec.exclude_states == []


def test_peer_comparison_overrides_accepts_valid_feature_set() -> None:
    """PeerComparisonOverrides accepts valid feature_set values."""
    for fs in ["enrollment", "frpl", "locale", "all"]:
        spec = PeerComparisonOverrides(feature_set=fs)
        assert spec.feature_set == fs


def test_peer_comparison_overrides_rejects_invalid_feature_set() -> None:
    """PeerComparisonOverrides rejects unknown feature_set values."""
    with pytest.raises(ValidationError):
        PeerComparisonOverrides(feature_set="geography")  # type: ignore[arg-type]


def test_peer_comparison_overrides_normalizes_state_abbreviations() -> None:
    """Two-letter state abbreviations are uppercased."""
    spec = PeerComparisonOverrides(exclude_states=["ca", "tx"])
    assert spec.exclude_states == ["CA", "TX"]


def test_peer_comparison_overrides_normalizes_full_state_names() -> None:
    """Full state names are mapped to 2-letter abbreviations."""
    spec = PeerComparisonOverrides(exclude_states=["California"])
    assert spec.exclude_states == ["CA"]


def test_peer_comparison_overrides_rejects_unparseable_state() -> None:
    """Strings that cannot be normalized to 2-letter abbreviations are rejected."""
    with pytest.raises(ValidationError, match="2-letter"):
        PeerComparisonOverrides(exclude_states=["NotAState"])


def test_peer_comparison_overrides_rejects_extra_fields() -> None:
    """PeerComparisonOverrides has extra='forbid'."""
    with pytest.raises(ValidationError):
        PeerComparisonOverrides(anchor_name="Denver")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Step 3 — Contract: _normalize_exclude_states_list shared helper works for
#          both SimilarityQuerySpec and PeerComparisonOverrides
# ---------------------------------------------------------------------------


def test_similarity_spec_exclude_states_still_normalizes_after_refactor() -> None:
    """PR 2B SimilarityQuerySpec normalizer still works after helper extraction."""
    spec = SimilarityQuerySpec(
        anchor_name="Denver Public Schools",
        exclude_states=["california"],
    )
    assert spec.exclude_states == ["CA"]


def test_similarity_spec_exclude_states_still_rejects_bad_entry_after_refactor() -> None:
    """PR 2B SimilarityQuerySpec still rejects bad entries after helper extraction."""
    with pytest.raises(ValidationError, match="2-letter"):
        SimilarityQuerySpec(
            anchor_name="Denver Public Schools",
            exclude_states=["NotAState"],
        )


# ---------------------------------------------------------------------------
# Step 4 — PendingQueryContext carries peer_overrides for multi-turn inheritance
# ---------------------------------------------------------------------------


def test_pending_query_context_accepts_peer_overrides() -> None:
    """PendingQueryContext can hold a PeerComparisonOverrides for multi-turn use."""
    ctx = PendingQueryContext(
        operation="peer_comparison",
        peer_overrides=PeerComparisonOverrides(feature_set="frpl"),
    )
    assert ctx.peer_overrides is not None
    assert ctx.peer_overrides.feature_set == "frpl"


def test_pending_query_context_peer_overrides_defaults_to_none() -> None:
    """PendingQueryContext.peer_overrides defaults to None."""
    ctx = PendingQueryContext()
    assert ctx.peer_overrides is None


# ---------------------------------------------------------------------------
# Execution test helpers
# ---------------------------------------------------------------------------


def _peer_comparison_metric() -> MetricCandidate:
    return MetricCandidate(
        metric_id=112,
        name="Maximum base salary",
        answer_type="numeric",
        topic="Salary",
    )


def _peer_comparison_alias() -> CatalogAliasRecord:
    return CatalogAliasRecord(
        alias="max salary",
        normalized_alias="max salary",
        entity_type="metric_bundle",
        resolution_status="approved",
        canonical_ids=["112"],
        source="test",
        provenance="Test alias.",
        review_status="approved",
    )


def _peer_profiles() -> list[dict[str, object]]:
    """Denver (anchor, CO) + Jeffco (CO peer) + Aurora (CO peer) + LA (CA peer)."""
    return [
        {
            "district_id": 26,
            "district_name": "Denver Public Schools",
            "state": "CO",
            "city": "DENVER",
            "enrollment": 87883,
            "locale_text": "City: Large",
            "latitude": 39.745750,
            "longitude": -104.985751,
            "total_rev_pp": 18381.59,
            "total_exp_pp": 18137.07,
            "pupil_teacher_ratio": 14.8,
            "frpl_pct": 65.0,
        },
        {
            "district_id": 29,
            "district_name": "Jeffco Public Schools",
            "state": "CO",
            "city": "GOLDEN",
            "enrollment": 75327,
            "locale_text": "Suburb: Large",
            "latitude": 39.740,
            "longitude": -105.220,
            "total_rev_pp": 23900.00,
            "total_exp_pp": 21000.00,
            "pupil_teacher_ratio": 16.6,
            "frpl_pct": 38.0,
        },
        {
            "district_id": 50,
            "district_name": "Los Angeles Unified",
            "state": "CA",
            "city": "LOS ANGELES",
            "enrollment": 600000,
            "locale_text": "City: Large",
            "latitude": 34.05,
            "longitude": -118.24,
            "total_rev_pp": 15000.00,
            "total_exp_pp": 14000.00,
            "pupil_teacher_ratio": 22.5,
            "frpl_pct": 79.0,
        },
    ]


def _salary_rows_for_peer_comparison() -> list[MetricAnswerRow]:
    return [
        MetricAnswerRow(
            district_id=26,
            district_name="Denver Public Schools",
            state="CO",
            metric_id=112,
            metric_name="Maximum base salary",
            value="90000",
            academic_year="2024-2025",
            answer_id=1,
            citations=[],
        ),
        MetricAnswerRow(
            district_id=29,
            district_name="Jeffco Public Schools",
            state="CO",
            metric_id=112,
            metric_name="Maximum base salary",
            value="85000",
            academic_year="2024-2025",
            answer_id=2,
            citations=[],
        ),
        MetricAnswerRow(
            district_id=50,
            district_name="Los Angeles Unified",
            state="CA",
            metric_id=112,
            metric_name="Maximum base salary",
            value="105000",
            academic_year="2024-2025",
            answer_id=3,
            citations=[],
        ),
    ]


def _make_repo(include_ca: bool = True) -> FakePolicyAnswerRepository:
    """Build a FakePolicyAnswerRepository for peer_comparison tests.

    Reuses the FakePolicyAnswerRepository from test_mvp_execution so the executor
    interface stays in sync without reimplementing every method.
    """
    repo = FakePolicyAnswerRepository(
        metrics=[_peer_comparison_metric()],
        rows=_salary_rows_for_peer_comparison(),
        districts=[
            DistrictCandidate(
                district_id=26,
                district_name="Denver Public Schools",
                state="CO",
            )
        ],
        aliases=[_peer_comparison_alias()],
    )
    profiles = _peer_profiles()
    if not include_ca:
        profiles = [p for p in profiles if p["state"] != "CA"]
    repo.peer_profile_rows = profiles
    return repo


def _make_executor(include_ca: bool = True) -> DeterministicQueryExecutor:
    """Build a DeterministicQueryExecutor backed by a fake repository."""
    return DeterministicQueryExecutor(_make_repo(include_ca=include_ca), default_limit=5)


def _peer_comparison_plan(
    *,
    feature_set: str | None = None,
    exclude_states: list[str] | None = None,
) -> QueryPlan:
    """Build a peer_comparison plan with optional peer_overrides."""
    overrides = None
    if feature_set is not None or exclude_states is not None:
        overrides = PeerComparisonOverrides(
            feature_set=feature_set or "all",
            exclude_states=exclude_states or [],
        )
    return QueryPlan(
        operation="peer_comparison",
        question="Compare Denver and its peers on max salary.",
        selection=SelectionSpec(scope="named_districts", districts=["Denver Public Schools"]),
        metrics=[MetricSpec(name="max salary")],
        peer_overrides=overrides,
    )


def _methodology_codes(result: object) -> list[str]:
    return [ref.code for ref in result.methodology_codes]  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Step 5 — Execution: default plan does NOT emit override codes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_peer_comparison_default_does_not_emit_override_codes() -> None:
    """Default peer_comparison (no peer_overrides) must not emit similarity_* codes."""
    executor = _make_executor()
    outcome = await executor.execute(_peer_comparison_plan())

    assert outcome.result is not None
    codes = _methodology_codes(outcome.result)
    assert "similarity_feature_set_override" not in codes, (
        "similarity_feature_set_override must NOT fire when peer_overrides is None"
    )
    assert "similarity_exclude_states_applied" not in codes, (
        "similarity_exclude_states_applied must NOT fire when peer_overrides is None"
    )
    # Base codes still present
    assert "peer_selection_nces_profiles" in codes
    assert "peer_policy_cells_with_citations" in codes


# ---------------------------------------------------------------------------
# Step 6 — Execution: feature_set='frpl' emits similarity_feature_set_override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_peer_comparison_with_feature_set_frpl_emits_override_code() -> None:
    """feature_set='frpl' on peer_comparison emits similarity_feature_set_override."""
    executor = _make_executor()
    outcome = await executor.execute(_peer_comparison_plan(feature_set="frpl"))

    assert outcome.result is not None
    codes = _methodology_codes(outcome.result)
    assert "similarity_feature_set_override" in codes, (
        "similarity_feature_set_override must fire when peer_overrides.feature_set != 'all'"
    )
    # Base codes still present
    assert "peer_selection_nces_profiles" in codes
    assert "peer_policy_cells_with_citations" in codes


# ---------------------------------------------------------------------------
# Step 7 — Execution: exclude_states drops in-state peers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_peer_comparison_exclude_states_drops_ca_peers() -> None:
    """exclude_states=['CA'] removes CA districts from the peer set."""
    executor = _make_executor(include_ca=True)
    # With CA excluded, only CO districts remain as peers
    outcome = await executor.execute(_peer_comparison_plan(exclude_states=["CA"]))

    assert outcome.result is not None
    peer_states = {
        row.state for row in outcome.result.rows if row.peer_role == "peer"
    }
    assert "CA" not in peer_states, (
        "exclude_states=['CA'] must prevent CA districts from appearing as peers"
    )


# ---------------------------------------------------------------------------
# Step 8 — Execution: exclude_states emits similarity_exclude_states_applied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_peer_comparison_exclude_states_emits_methodology_code() -> None:
    """exclude_states non-empty emits similarity_exclude_states_applied code."""
    executor = _make_executor(include_ca=True)
    outcome = await executor.execute(_peer_comparison_plan(exclude_states=["CA"]))

    assert outcome.result is not None
    codes = _methodology_codes(outcome.result)
    assert "similarity_exclude_states_applied" in codes, (
        "similarity_exclude_states_applied must fire when peer_overrides.exclude_states is non-empty"
    )


# ---------------------------------------------------------------------------
# Step 9 — Execution: anchor is unchanged when peer_overrides present
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_peer_comparison_anchor_unchanged_with_peer_overrides() -> None:
    """The anchor district is always selection.districts[0], even with peer_overrides."""
    executor = _make_executor()
    outcome = await executor.execute(_peer_comparison_plan(feature_set="frpl"))

    assert outcome.result is not None
    anchor_rows = [row for row in outcome.result.rows if row.peer_role == "anchor"]
    assert len(anchor_rows) >= 1
    assert all(row.district_name == "Denver Public Schools" for row in anchor_rows)


# ---------------------------------------------------------------------------
# Step 10 — PR 2A composition: filter + overrides both emit codes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_peer_comparison_filter_and_overrides_both_emit_codes() -> None:
    """metric_value_filter + peer_overrides compose: both methodology codes emitted."""
    executor = _make_executor()
    plan = QueryPlan(
        operation="peer_comparison",
        question="Compare Denver and peers on max salary weighted by FRPL, only >80k.",
        selection=SelectionSpec(scope="named_districts", districts=["Denver Public Schools"]),
        metrics=[MetricSpec(name="max salary")],
        filters=[
            FilterSpec(field="max salary", operator="greater_than_or_equal", value=80000),
        ],
        peer_overrides=PeerComparisonOverrides(feature_set="frpl"),
    )
    outcome = await executor.execute(plan)

    assert outcome.result is not None
    codes = _methodology_codes(outcome.result)
    # PR 2A code
    assert "metric_value_filter_applied" in codes, (
        "metric_value_filter_applied must fire after filtering peer candidates"
    )
    assert "metric_value_filter_not_applied" not in codes, (
        "successful peer comparison must not disclose an unapplied user filter"
    )
    # Track 3.2 code
    assert "similarity_feature_set_override" in codes, (
        "similarity_feature_set_override must fire (feature_set='frpl')"
    )


# ---------------------------------------------------------------------------
# Phase 3 Item 4 — Multi-turn carry-through: _merge_pending_context + pending_context_from_execution_clarification
# ---------------------------------------------------------------------------


def _minimal_clarification() -> ClarificationRequest:
    """Minimal ClarificationRequest for use in pending-context tests."""
    return ClarificationRequest(
        question="Which district are you asking about?",
        missing_fields=["district"],
    )


def test_merge_pending_context_carries_forward_similarity() -> None:
    """Track 3.1 + 3.2 composition: PendingQueryContext.similarity survives merge.

    When incoming context has no similarity (user provided partial update),
    the existing similarity must not be discarded.
    """
    anchor = SimilarityQuerySpec(
        anchor_name="Denver Public Schools",
        feature_set="frpl",
    )
    existing = PendingQueryContext(
        operation="similarity",
        similarity=anchor,
    )
    incoming = PendingQueryContext(
        operation="similarity",
        similarity=None,  # user update didn't re-specify similarity
    )
    merged = _merge_pending_context(
        existing,
        incoming,
        turn_index=2,
        fallback_missing_fields=["district"],
        clarification_question="Which metric?",
    )
    assert merged.similarity is not None, (
        "_merge_pending_context must preserve existing.similarity when incoming.similarity is None"
    )
    assert merged.similarity.anchor_name == "Denver Public Schools"
    assert merged.similarity.feature_set == "frpl"


def test_merge_pending_context_carries_forward_peer_overrides() -> None:
    """Track 3.2 multi-turn: PendingQueryContext.peer_overrides survives merge.

    When incoming context has no peer_overrides, the existing overrides must
    not be discarded.
    """
    overrides = PeerComparisonOverrides(feature_set="frpl", exclude_states=["CA"])
    existing = PendingQueryContext(
        operation="peer_comparison",
        peer_overrides=overrides,
    )
    incoming = PendingQueryContext(
        operation="peer_comparison",
        peer_overrides=None,  # user update didn't re-specify peer_overrides
    )
    merged = _merge_pending_context(
        existing,
        incoming,
        turn_index=2,
        fallback_missing_fields=["district"],
        clarification_question="Which district?",
    )
    assert merged.peer_overrides is not None, (
        "_merge_pending_context must preserve existing.peer_overrides when incoming.peer_overrides is None"
    )
    assert merged.peer_overrides.feature_set == "frpl"
    assert merged.peer_overrides.exclude_states == ["CA"]


def test_merge_pending_context_incoming_similarity_wins() -> None:
    """When incoming has similarity, it takes precedence over existing."""
    existing_anchor = SimilarityQuerySpec(anchor_name="Alpha", feature_set="all")
    incoming_anchor = SimilarityQuerySpec(anchor_name="Beta", feature_set="frpl")
    existing = PendingQueryContext(operation="similarity", similarity=existing_anchor)
    incoming = PendingQueryContext(operation="similarity", similarity=incoming_anchor)
    merged = _merge_pending_context(
        existing,
        incoming,
        turn_index=3,
        fallback_missing_fields=["district"],
        clarification_question="Which metric?",
    )
    assert merged.similarity is not None
    assert merged.similarity.anchor_name == "Beta"


def test_pending_context_from_execution_clarification_captures_similarity() -> None:
    """When execution clarification is built from a similarity plan, similarity carries forward."""
    plan = QueryPlan(
        operation="similarity",
        question="Find peers to Denver.",
        selection=SelectionSpec(scope="named_districts", districts=["Denver Public Schools"]),
        similarity=SimilarityQuerySpec(
            anchor_name="Denver Public Schools",
            feature_set="frpl",
        ),
    )
    ctx = pending_context_from_execution_clarification(
        plan,
        _minimal_clarification(),
        turn_index=1,
    )
    assert ctx.similarity is not None, (
        "pending_context_from_execution_clarification must carry plan.similarity forward"
    )
    assert ctx.similarity.anchor_name == "Denver Public Schools"
    assert ctx.similarity.feature_set == "frpl"


def test_pending_context_from_execution_clarification_captures_peer_overrides() -> None:
    """When execution clarification is built from a peer_comparison plan, peer_overrides carries forward."""
    plan = QueryPlan(
        operation="peer_comparison",
        question="Compare Denver and peers on max salary weighted by FRPL.",
        selection=SelectionSpec(scope="named_districts", districts=["Denver Public Schools"]),
        metrics=[MetricSpec(name="max salary")],
        peer_overrides=PeerComparisonOverrides(feature_set="frpl", exclude_states=["CA"]),
    )
    ctx = pending_context_from_execution_clarification(
        plan,
        _minimal_clarification(),
        turn_index=1,
    )
    assert ctx.peer_overrides is not None, (
        "pending_context_from_execution_clarification must carry plan.peer_overrides forward"
    )
    assert ctx.peer_overrides.feature_set == "frpl"
    assert ctx.peer_overrides.exclude_states == ["CA"]
