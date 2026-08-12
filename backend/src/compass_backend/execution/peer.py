"""Deterministic NCES-peer policy comparison operation path."""

from __future__ import annotations

from decimal import ROUND_HALF_UP

from compass_backend.artifacts import (
    CitationRef,
    MethodologyRef,
    PeerComparisonResult,
    PeerComparisonRow,
    ResultSelection,
    SelectedDistrict,
    coverage_frame_from_labels,
    coverage_label_for_answer,
    coverage_label_for_missing_answer,
    coverage_label_for_stale_answer,
)
from compass_backend.artifacts.currency import (
    currency_decimal,
    haystack_has_currency_token,
)
from compass_backend.catalog import MetricCandidate, PeerSetResolution
from compass_backend.catalog.peers import peer_reason
from compass_backend.contracts.planning import PeerComparisonOverrides, QueryPlan
from compass_backend.planning.temporal import default_current_academic_year

from .evidence import citation_markers_for_row
from .selection import selection_filters_are_supported
from .types import MetricAnswerRow

def peer_comparison_shape_is_supported(plan: QueryPlan) -> bool:
    """Return whether the peer comparison path supports this query plan."""

    return (
        plan.operation == "peer_comparison"
        and plan.selection.scope == "named_districts"
        and len(plan.selection.districts) == 1
        and bool(plan.metrics)
        and plan.sort is None
        and not plan.sort_steps
        and selection_filters_are_supported(plan)
    )


def similarity_shape_is_supported(plan: QueryPlan) -> bool:
    """Return whether the similarity peer-discovery path supports this query plan."""

    return (
        plan.operation == "similarity"
        and plan.similarity is not None
        and plan.selection.scope == "named_districts"
        and len(plan.selection.districts) == 1
        and plan.sort is None
        and not plan.sort_steps
        and selection_filters_are_supported(plan)
    )


def build_peer_comparison_result(
    plan: QueryPlan,
    metric_rows: list[
        tuple[MetricCandidate, list[MetricAnswerRow], list[MetricAnswerRow]]
    ],
    *,
    peer_set: PeerSetResolution,
    reviewed_district_ids: set[int],
    academic_year: str,
    peer_overrides: PeerComparisonOverrides | None = None,
) -> PeerComparisonResult:
    """Build policy metric cells for an anchor plus deterministic peer set."""

    selected_districts = [
        SelectedDistrict(
            district_id=peer_set.anchor.district_id,
            district_name=peer_set.anchor.district_name,
            state=peer_set.anchor.state,
        ),
        *[
            SelectedDistrict(
                district_id=peer.district_id,
                district_name=peer.district_name,
                state=peer.state,
            )
            for peer in peer_set.peers
        ],
    ]
    peer_metadata = {
        peer_set.anchor.district_id: {
            "role": "anchor",
            "rank": None,
            "score": None,
            "reason": "Anchor district selected by the user.",
            "enrollment": peer_set.anchor.enrollment,
            "urbanicity": peer_set.anchor.locale_text,
        }
    }
    peer_metadata.update(
        {
            peer.district_id: {
                "role": "peer",
                "rank": peer.rank,
                "score": peer.similarity_score,
                "reason": peer_reason(peer),
                "enrollment": peer.enrollment,
                "urbanicity": peer.locale_text,
            }
            for peer in peer_set.peers
        }
    )

    citation_refs: list[CitationRef] = []
    citation_marker_by_identity: dict[tuple[object, ...], int] = {}
    result_rows: list[PeerComparisonRow] = []
    coverage_labels = []

    for district in selected_districts:
        metadata = peer_metadata[district.district_id]
        for metric, rows, recent_rows in metric_rows:
            row = _row_for_district(rows, district.district_id)
            recent_row = _row_for_district(recent_rows, district.district_id)
            if row is None and recent_row is not None:
                coverage = coverage_label_for_stale_answer(
                    recent_row.value,
                    district_name=district.district_name,
                    metric_name=metric.name,
                    current_academic_year=academic_year,
                    prior_academic_year=recent_row.academic_year,
                    metric_topic=_metric_topic(metric),
                )
                value = None
                row_year = academic_year
                source = "coverage_state"
                citation_markers: list[int] = []
            elif row is None:
                coverage = coverage_label_for_missing_answer(
                    district_name=district.district_name,
                    metric_name=metric.name,
                    academic_year=academic_year,
                    district_has_current_year_rows=(
                        district.district_id in reviewed_district_ids
                    ),
                    metric_topic=_metric_topic(metric),
                )
                value = None
                row_year = academic_year
                source = "coverage_state"
                citation_markers = []
            else:
                coverage = coverage_label_for_answer(
                    row.value,
                    district_name=row.district_name,
                    metric_name=metric.name,
                    academic_year=row.academic_year,
                )
                value = row.value
                if coverage.state == "covered":
                    normalized = _whole_dollar_peer_value(metric, row.value)
                    if normalized is not None:
                        value, display_value = normalized
                        coverage = coverage.model_copy(
                            update={
                                "display": display_value,
                                "raw_value": value,
                            }
                        )
                row_year = row.academic_year
                source = "policy_answer"
                citation_markers = citation_markers_for_row(
                    row,
                    citation_refs,
                    citation_marker_by_identity,
                )

            coverage_labels.append(coverage)
            result_rows.append(
                PeerComparisonRow(
                    district_id=district.district_id,
                    district_name=district.district_name,
                    state=district.state,
                    metric_id=metric.metric_id,
                    metric_name=metric.name,
                    value=value,
                    display_value=coverage.display,
                    academic_year=row_year,
                    source=source,
                    citation_markers=citation_markers,
                    coverage_state=coverage.state,
                    coverage_display=coverage.display,
                    coverage_reason=coverage.reason,
                    coverage_qualifier=coverage.qualifier,
                    # #1514 D13 — carry the stale label's prior-year fields so
                    # a stale anchor/peer can be voiced as the canonical
                    # "last reviewed" sentence (None for non-stale labels).
                    coverage_prior_academic_year=coverage.prior_academic_year,
                    coverage_prior_display_value=coverage.prior_display_value,
                    peer_role=metadata["role"],
                    peer_rank=metadata["rank"],
                    peer_score=metadata["score"],
                    peer_reason=metadata["reason"],
                    peer_selection_method=peer_set.selection_method,
                    peer_enrollment=metadata["enrollment"],
                    peer_urbanicity=metadata["urbanicity"],
                )
            )

    # Base methodology codes — always emitted for peer_comparison.
    methodology_codes = [
        MethodologyRef(code="peer_selection_nces_profiles"),
        MethodologyRef(code="peer_score_method"),
        MethodologyRef(
            code="peer_scoring_policy_disclosure",
            metadata={"policy_version": peer_set.policy_version},
            audience="internal",
        ),
        MethodologyRef(code="peer_policy_cells_with_citations"),
    ]

    # Optional override codes — reuse similarity_* codes; prose is operation-neutral
    # (the rendered text "peer set was scored using FRPL-biased weights" is equally
    # correct for peer_comparison and similarity operations).
    if peer_overrides is not None:
        if peer_overrides.feature_set != "all":
            methodology_codes.append(
                MethodologyRef(
                    code="similarity_feature_set_override",
                    metadata={"feature_set": peer_overrides.feature_set},
                )
            )
        if peer_overrides.exclude_states:
            methodology_codes.append(
                MethodologyRef(
                    code="similarity_exclude_states_applied",
                    metadata={"exclude_states": ",".join(peer_overrides.exclude_states)},
                )
            )

    return PeerComparisonResult(
        selection=ResultSelection(
            scope="named_districts",
            districts=selected_districts,
            states=plan.selection.states,
        ),
        rows=result_rows,
        citations=citation_refs,
        coverage_frame=coverage_frame_from_labels(coverage_labels),
        total_considered=len(result_rows),
        excluded_count=0,
        order_statement=(
            "Compared selected policy metrics for the anchor district and "
            f"{len(peer_set.peers)} deterministic NCES-similar peer districts."
        ),
        methodology_codes=methodology_codes,
    )


def _row_for_district(
    rows: list[MetricAnswerRow],
    district_id: int,
) -> MetricAnswerRow | None:
    for row in rows:
        if row.district_id == district_id:
            return row
    return None


def _metric_topic(metric: MetricCandidate) -> str | None:
    topic = (metric.topic or "").strip()
    if topic:
        return topic
    metadata_topic = metric.metadata.get("topic")
    return str(metadata_topic).strip() if metadata_topic else None


def _whole_dollar_peer_value(
    metric: MetricCandidate,
    value: object,
) -> tuple[int, str] | None:
    if not _metric_is_currency(metric):
        return None
    amount = currency_decimal(value)
    if amount is None:
        return None
    whole_dollars = int(amount.to_integral_value(rounding=ROUND_HALF_UP))
    return whole_dollars, f"${whole_dollars:,}"


def _metric_is_currency(metric: MetricCandidate) -> bool:
    haystack = " ".join(
        part
        for part in (
            metric.name,
            metric.topic or "",
            str(metric.metadata.get("topic") or ""),
        )
        if part
    )
    return haystack_has_currency_token(haystack)


# ---------------------------------------------------------------------------
# PR 2B — similarity / peer-set discovery builder
# ---------------------------------------------------------------------------

# Sentinel metric_id for similarity-only rows (no policy metric).
# PeerComparisonRow.metric_id is int (non-optional); 0 signals "no metric."
_SIMILARITY_SENTINEL_METRIC_ID = 0


def build_similarity_result(
    plan: QueryPlan,
    *,
    peer_set: PeerSetResolution,
    pre_filter_peer_count: int | None = None,
) -> PeerComparisonResult:
    """Build a peer-set discovery artifact from a resolved NCES peer set.

    Reuses ``PeerComparisonResult`` / ``PeerComparisonRow`` so the renderer
    can dispatch on ``result_type='peer_comparison'`` and handle both shapes.
    Similarity rows have ``metric_id=0`` (sentinel), ``metric_name='similarity'``,
    and ``source='coverage_state'`` (``PeerComparisonRow.source`` is
    ``Literal["policy_answer", "coverage_state"]``; similarity rows are non-policy-answer
    rows derived from NCES profile data, not policy answers).

    Args:
        plan: The ``similarity`` query plan.
        peer_set: Resolved NCES peer set (anchor + scored peers).
        pre_filter_peer_count: When a metric-value filter was applied after
            peer-set discovery, pass the pre-filter peer count here so the
            ``similarity_post_filter_narrowed_peer_set`` methodology code
            can disclose how much the filter narrowed the set.
    """

    assert plan.similarity is not None  # guarded by shape check before this call

    selected_districts = [
        SelectedDistrict(
            district_id=peer_set.anchor.district_id,
            district_name=peer_set.anchor.district_name,
            state=peer_set.anchor.state,
        ),
        *[
            SelectedDistrict(
                district_id=peer.district_id,
                district_name=peer.district_name,
                state=peer.state,
            )
            for peer in peer_set.peers
        ],
    ]
    peer_metadata: dict[int, dict[str, object]] = {
        peer_set.anchor.district_id: {
            "role": "anchor",
            "rank": None,
            "score": None,
            "reason": "Anchor district selected by the user.",
            "enrollment": peer_set.anchor.enrollment,
            "urbanicity": peer_set.anchor.locale_text,
        }
    }
    peer_metadata.update(
        {
            peer.district_id: {
                "role": "peer",
                "rank": peer.rank,
                "score": peer.similarity_score,
                "reason": peer_reason(peer),
                "enrollment": peer.enrollment,
                "urbanicity": peer.locale_text,
            }
            for peer in peer_set.peers
        }
    )

    result_rows: list[PeerComparisonRow] = []
    for district in selected_districts:
        metadata = peer_metadata[district.district_id]
        result_rows.append(
            PeerComparisonRow(
                district_id=district.district_id,
                district_name=district.district_name,
                state=district.state,
                # Sentinel metric_id=0: similarity rows carry no policy metric.
                metric_id=_SIMILARITY_SENTINEL_METRIC_ID,
                metric_name="similarity",
                value=metadata["score"],
                display_value=(
                    f"{metadata['score']:.2f}"
                    if metadata["score"] is not None
                    else "anchor"
                ),
                academic_year=(
                    plan.temporal.academic_year
                    or default_current_academic_year()
                ),
                source="coverage_state",
                citation_markers=[],
                coverage_state="covered",
                coverage_display=(
                    f"Similarity score: {metadata['score']:.2f}"
                    if metadata["score"] is not None
                    else "Anchor district"
                ),
                coverage_reason=metadata["reason"],
                coverage_qualifier=None,
                peer_role=metadata["role"],
                peer_rank=metadata["rank"],
                peer_score=metadata["score"],
                peer_reason=metadata["reason"],
                peer_selection_method=peer_set.selection_method,
                peer_enrollment=metadata["enrollment"],
                peer_urbanicity=metadata["urbanicity"],
            )
        )

    # Compose methodology codes.
    # ``peer_selection_rationale`` describes scoring "across all factors"; it is
    # only emitted for ``feature_set="all"`` because the
    # ``similarity_feature_set_override`` code already conveys the rationale when
    # a subset is used — emitting both would contradict ("scored across all factors"
    # + "factors were excluded").  See PR 2B reviewer Fix 2.
    feature_set = plan.similarity.feature_set
    methodology_codes: list[MethodologyRef] = [
        MethodologyRef(code="peer_selection_nces_profiles"),
        MethodologyRef(code="peer_score_method"),
        MethodologyRef(
            code="peer_scoring_policy_disclosure",
            metadata={"policy_version": peer_set.policy_version},
            audience="internal",
        ),
    ]
    if feature_set == "all":
        methodology_codes.append(MethodologyRef(code="peer_selection_rationale"))
    else:
        methodology_codes.append(
            MethodologyRef(
                code="similarity_feature_set_override",
                metadata={"feature_set": feature_set},
            )
        )
    if plan.similarity.exclude_states:
        methodology_codes.append(
            MethodologyRef(
                code="similarity_exclude_states_applied",
                metadata={"excluded": ",".join(sorted(plan.similarity.exclude_states))},
            )
        )
    if pre_filter_peer_count is not None and pre_filter_peer_count > len(peer_set.peers):
        methodology_codes.append(
            MethodologyRef(
                code="similarity_post_filter_narrowed_peer_set",
                metadata={
                    "pre_filter_count": str(pre_filter_peer_count),
                    "post_filter_count": str(len(peer_set.peers)),
                },
            )
        )

    peer_count = len(peer_set.peers)
    order_statement = (
        f"Found {peer_count} NCES-similar peer district"
        f"{'s' if peer_count != 1 else ''} for {peer_set.anchor.district_name}."
    )

    return PeerComparisonResult(
        selection=ResultSelection(
            scope="named_districts",
            districts=selected_districts,
            states=plan.selection.states,
        ),
        rows=result_rows,
        citations=[],
        coverage_frame=coverage_frame_from_labels([]),
        total_considered=len(result_rows),
        excluded_count=0,
        order_statement=order_statement,
        methodology_codes=methodology_codes,
    )
