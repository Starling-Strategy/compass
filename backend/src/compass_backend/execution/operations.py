"""DeterministicQueryExecutor operation methods (mixin).

Extracted from `executor.py` to split the 1,852-LOC file along its natural
internal section boundary. The methods access executor instance state
(`self._catalog`, `self._repository`, `self._current_academic_year`,
`self._default_limit`) and free helpers from `._helpers`.

This is a mixin (not a standalone class) — the methods only make sense
when mixed into `DeterministicQueryExecutor`. Behavior is unchanged
from the pre-split version.
"""

from __future__ import annotations

import dataclasses

from compass_backend.artifacts import (
    CompositeRankingResult,
    MetricLookupResult,
    MethodologyRef,
    MetricRankingResult,
    ResultSelection,
    SelectedDistrict,
    coverage_label_for_answer,
)
from compass_backend.catalog import (
    CatalogResolutionCandidate,
    CatalogResolutionEntity,
    ContextualMetricDefault,
    DistrictCandidate,
    MetricCandidate,
    NcesDistrictMatch,
    NCESFieldCandidate,
    PeerDistrictCandidate,
    ProfileFieldRankRow,
    ProfileFieldResolution,
)
from compass_backend.catalog.reporting import report_from_entities
from compass_backend.catalog.resolution import PeerSetResolution
from compass_backend.reference.text import normalize_whitespace_casefold
from compass_backend.rendering.district_disambiguation import (
    grounded_uncovered_district_labels,
    grounded_uncovered_district_question,
)
from compass_backend.contracts.planning import (
    ClarificationRequest,
    FilterSpec,
    MetricSpec,
    QueryPlan,
    SortStepSpec,
)

from compass_backend.answer_layer.clarify import compose_clarify_question_async
from ._helpers import (
    _UNSUPPORTED_SHAPE_MESSAGE,
    _execution_metric_specs,
    _lookup_academic_year,
    _lookup_allows_recent_fallback,
    _lookup_chart_requires_numeric_metric_resolution,
    _metric_resolution_report,
    _policy_presentation_step,
    _primary_metric_is_policy_metric,
    _primary_metric_spec,
    _profile_presentation_step,
    _profile_ranking_limit,
    _profile_selection_step,
    _flatten_metric_groups,
    _selection_from_profile_rows,
    _validation_authority,
)
from .count import (
    build_categorical_value_count_result,
    build_covered_universe_count_result,
    build_filter_prevalence_summary,
    build_metric_count_result,
    build_topic_coverage_count_result,
    count_plan_is_categorical_value,
    count_plan_is_covered_universe,
    count_plan_is_topic_coverage,
    count_shape_is_supported,
    covered_universe_metric,
    metric_supports_categorical_value_count,
)
from .lookup import (
    build_metric_intersection_lookup_result,
    build_metric_lookup_result,
    lookup_shape_is_supported,
)
from .peer import (
    build_peer_comparison_result,
    build_similarity_result,
    peer_comparison_shape_is_supported,
    similarity_shape_is_supported,
)
from .profile import build_profile_lookup_result, profile_lookup_shape_is_supported
from .referent_resolution import resolve_referent_district, split_state_suffix
from ._text_utils import (
    _BOOLEAN_FALSE_VALUES,
    _BOOLEAN_TRUE_VALUES,
    parse_compass_numeric_value,
)
from .ranking import (
    build_metric_ranking_result,
    build_profile_ordered_metric_ranking_result,
    build_profile_ranking_result,
    ranking_shape_is_supported,
)
from .filters import ResolvedMetricFilter, filter_kind
from .selection import (
    _CATEGORICAL_TEXT_OPERATORS,
    _filter_is_metric_value,
    direction,
    direction_phrase,
    requested_metric_filters,
    requested_states,
    resolve_selection,
)
from .trend import (
    MAX_TREND_CELLS,
    build_metric_trend_result,
    trend_academic_years,
    trend_cell_count,
    trend_shape_is_supported,
)
from .types import (
    EXECUTION_OUTCOME_TYPES,
    ExecutionClarification,
    ExecutionOutcome,
    ExecutionRefusal,
    ExecutionSuccess,
    MetricAnswerRow,
)

# Peer-comparison rule constants. _PEER_DEFAULT_SAME_STATE_CAP is the
# stated product rule (Refs docs/plans/2026-05-26-peer-comparison-cap-rule.md):
# every peer-comparison response includes the anchor plus up to 2 of the most
# similar same-state districts, with the rest drawn from outside the anchor's
# state. The rule is disclosed in user-visible prose, not enforced silently.
_PEER_CANDIDATE_POOL_MIN = 30
_PEER_CANDIDATE_POOL_MULTIPLIER = 6
_PEER_DEFAULT_SAME_STATE_CAP = 2
_PEER_DEFAULT_DIVERSITY_SCORE_DELTA = 0.08
# Default peer count (anchor + similar districts) when the plan requests no
# explicit limit; the executor's own default limit can still lower it.
_PEER_COMPARISON_DEFAULT_LIMIT = 5
# Candidate cap passed to metric resolution: how many name-matched metric
# candidates to surface for clarification/disambiguation before one is chosen.
_METRIC_CLARIFICATION_CANDIDATE_LIMIT = 5


@dataclasses.dataclass(frozen=True)
class _PeerMetricCoverage:
    covered_count: int
    metric_count: int

    @property
    def complete(self) -> bool:
        return self.metric_count > 0 and self.covered_count == self.metric_count

    @property
    def partial(self) -> bool:
        return 0 < self.covered_count < self.metric_count

    @property
    def usable_for_peer_comparison(self) -> bool:
        return self.complete


@dataclasses.dataclass(frozen=True)
class _PeerCoverageScreenResult:
    peer_set: PeerSetResolution
    candidate_peer_count: int
    excluded_unavailable_count: int
    same_state_cap_applied: bool
    diversity_replacement_count: int


def _profile_field_resolution_report(
    plan: QueryPlan,
    phrase: str,
    resolution: ProfileFieldResolution,
    *,
    status: str,
    message: str,
):
    """Build the catalog resolution report for a profile-field dead-end,
    carrying the real candidate fields the catalog surfaced."""

    return report_from_entities(
        question=plan.question,
        operation=plan.operation,
        entities=[
            CatalogResolutionEntity(
                input_phrase=phrase,
                entity_type="profile_field",
                status=status,
                resolution_method=(
                    "catalog_search" if resolution.candidates else "unresolved"
                ),
                candidates=[
                    CatalogResolutionCandidate(
                        candidate_id=candidate.field_key,
                        label=candidate.label,
                        entity_type="profile_field",
                        metadata={"data_type": candidate.data_type},
                    )
                    for candidate in resolution.candidates
                ],
                provenance="CatalogResolver.resolve_profile_field",
                message=message,
            )
        ],
    )


def _profile_field_dead_end(
    plan: QueryPlan,
    phrase: str,
    resolution: ProfileFieldResolution,
) -> ExecutionRefusal | ExecutionClarification:
    """Resolve a profile-field dead-end into the right typed outcome.

    When the catalog surfaced candidate fields, clarify with their labels
    (SELECT-R4 / #1248) instead of discarding them in a generic refusal. A
    genuine no-candidate miss stays a LOW-severity refusal.
    """

    if resolution.candidates:
        question = (
            f'I could not pin "{phrase}" to a single covered NCES/profile '
            "field. Which field do you mean?"
        )
        clarification = ClarificationRequest(
            question=question,
            missing_fields=["profile_field"],
            candidates=[candidate.label for candidate in resolution.candidates],
        )
        return ExecutionClarification(
            clarification=clarification,
            message=question,
            resolution_report=_profile_field_resolution_report(
                plan,
                phrase,
                resolution,
                status="ambiguous",
                message=(
                    "Multiple approved NCES/profile fields matched this phrase."
                ),
            ),
        )
    return ExecutionRefusal(
        message=(
            "I could not resolve that NCES/profile field for deterministic "
            "execution yet."
        ),
        resolution_report=_profile_field_resolution_report(
            plan,
            phrase,
            resolution,
            status="unresolved",
            message="No approved NCES/profile field resolved for this phrase.",
        ),
    )


def _has_unresolved_filter_metrics(plan: QueryPlan) -> bool:
    return any(metric.role in {"filter", "grouping"} for metric in plan.metrics)


def _has_multiple_metric_candidates(candidates: list[MetricCandidate]) -> bool:
    return len({candidate.metric_id for candidate in candidates}) > 1


def _candidates_are_self_referential(
    phrase: str, candidates: list[MetricCandidate]
) -> bool:
    """Whether a clarify built from these candidates would just re-ask the input.

    #1830: when the user has already chosen a metric string and it resolves to a
    candidate set whose only member restates that same string, offering it back
    as "which did you mean?" cannot make progress — it is a guaranteed clarify
    loop with no exit. Detected on the TYPED candidate ``name`` (never prose):
    non-empty candidates that all normalize to the input phrase. Empty candidate
    sets are not self-referential — that is a genuine dead-end, handled by the
    caller's refusal.
    """

    if not candidates:
        return False
    normalized_phrase = normalize_whitespace_casefold(phrase)
    distinct = {normalize_whitespace_casefold(c.name) for c in candidates}
    return distinct == {normalized_phrase}


def _cannot_rank_non_numeric_message(phrase: str) -> str:
    """Plain, honest message when a rank op is handed a non-numeric field."""

    return (
        f'"{phrase}" is a yes/no or category field, not a number, so I cannot '
        "rank districts by it. Ask me to rank by a numeric metric and I will "
        "continue."
    )


def _is_state_filter(filter_spec: FilterSpec) -> bool:
    return filter_kind(filter_spec) == "state"


def _filter_field_can_be_profile_field(filter_spec: FilterSpec) -> bool:
    if filter_kind(filter_spec) == "metric_value":
        # A metric-value filter (resolved or not) is never a profile field.
        # Post-#1373 Stage 2 the field stays the free-form metric phrase rather
        # than a "metric:<id>" token, so the metric-value *classification* — not
        # a string prefix — is what keeps profile-field resolution from
        # re-resolving an already-resolved threshold filter.
        return False
    field = filter_spec.field.casefold().strip()
    return field not in {
        "answer_value",
        "district",
        "district_name",
        "display_value",
        "enrollment",
        "metric_value",
        "name",
        "region",
        "state",
        "value",
    }


# ---------------------------------------------------------------------------
# Metric-value filter resolution (PR 2A)
# ---------------------------------------------------------------------------


def _row_passes_metric_filter(row: MetricAnswerRow, f: ResolvedMetricFilter) -> bool:
    """Return True when ``row``'s value satisfies the resolved filter predicate."""
    if row.metric_id != f.metric_id:
        return False
    if f.value_kind == "numeric":
        # Parse-once: read the typed numeric the row carries from its boundary
        # (MetricAnswerRow.numeric_value), not a fresh parse of `value`.
        row_num = row.numeric_value
        if row_num is None:
            # Missing or non-numeric data: excluded (neither passes nor fails).
            return False
        # #1772: parse the filter value with the SAME comma-stripping canonical
        # parser as the row value. A raw float() crashed the turn whenever f.value
        # carried a thousands separator (an equality-anchor on a currency metric
        # pulls the anchor's rendered cell, e.g. "45,602"). Unparseable -> exclude,
        # never raise (mirrors the row_num guard above).
        filter_value = parse_compass_numeric_value(f.value)
        if filter_value is None:
            return False
        if f.operator == "greater_than":
            return row_num > filter_value
        if f.operator == "greater_than_or_equal":
            return row_num >= filter_value
        if f.operator == "less_than":
            return row_num < filter_value
        if f.operator == "less_than_or_equal":
            return row_num <= filter_value
        if f.operator == "equals":
            return row_num == filter_value
        if f.operator == "not_equals":
            return row_num != filter_value
        return False
    if f.value_kind == "boolean":
        row_bool = _parse_boolean(row.value)
        if row_bool is None:
            return False
        filter_value = bool(f.value)
        if f.operator == "equals":
            return row_bool is filter_value
        if f.operator == "not_equals":
            return row_bool is not filter_value
        return False
    if f.operator in ("contains", "in", "not_in"):
        return _row_passes_categorical_filter(row, f)
    row_text = _normalize_filter_text(row.value)
    filter_text = _normalize_filter_text(f.value)
    if row_text is None or filter_text is None:
        return False
    if f.operator == "equals":
        return row_text == filter_text
    if f.operator == "not_equals":
        return row_text != filter_text
    return False


def _row_passes_categorical_filter(
    row: MetricAnswerRow, f: ResolvedMetricFilter
) -> bool:
    """Match a multi-valued categorical cell against ``contains``/``in``/``not_in``.

    Categorical metrics (e.g. "Who is eligible for paid parental leave?") store
    a comma-joined cell of category tokens.  A target matches when any token in
    the cell equals it or begins with it at a token boundary, so the filter
    token "Non-birthing parent" matches the data token "Non-birthing parent
    (gender not specified)" while "Birthing parent" does NOT match it.

    A row with no reviewed value never satisfies a categorical predicate —
    including ``not_in`` — so missing / blank cells drop out rather than slip
    through an exclusion filter.
    """
    raw_targets = f.value if isinstance(f.value, list) else [f.value]
    targets = [t for t in (_normalize_filter_text(v) for v in raw_targets) if t]
    if not targets:
        return False
    if _normalize_filter_text(row.value) in (None, ""):
        return False
    has_any = any(_value_has_category(row.value, target) for target in targets)
    if f.operator == "not_in":
        return not has_any
    return has_any  # contains / in


def _value_has_category(row_value: object, normalized_target: str) -> bool:
    """True when a comma-joined categorical cell contains ``normalized_target``
    as a category token, matched at a token boundary.

    Equality or a prefix followed by a boundary char (space, ``(``, ``:``)
    counts; a bare substring does not.  This keeps "Birthing parent" from
    matching the "Non-birthing parent ..." token while letting "Other" match
    "Other: Family Caregivers".
    """
    if row_value is None:
        return False
    for token in str(row_value).split(","):
        normalized = _normalize_filter_text(token)
        if not normalized:
            continue
        if normalized == normalized_target:
            return True
        if normalized.startswith(normalized_target):
            boundary = normalized[len(normalized_target) : len(normalized_target) + 1]
            if boundary in (" ", "(", ":"):
                return True
    return False


def _normalize_filter_text(value: object) -> str | None:
    if value is None:
        return None
    return str(value).casefold().strip()


def _parse_boolean(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, int | float):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    normalized = str(value).casefold().replace(",", "").strip().strip(".")
    if normalized in _BOOLEAN_TRUE_VALUES:
        return True
    if normalized in _BOOLEAN_FALSE_VALUES:
        return False
    return None


def _apply_metric_value_filters_to_selection(
    selection: ResultSelection,
    rows: list[MetricAnswerRow],
    resolved_filters: list[ResolvedMetricFilter],
) -> ResultSelection:
    """Return a new ResultSelection containing only districts that pass all filters.

    For each resolved metric-value filter, a district passes if it has at least
    one row for that metric whose value satisfies the predicate.  Districts with
    no row (or a non-numeric row) for the filter metric are excluded.

    The ``rows`` list must contain rows for all filter metrics — typically the
    fetched rows for the primary metric, which coincides with the filter metric
    when the filter field mirrors MetricSpec.name.
    """
    if not resolved_filters:
        return selection

    # Build a per-metric-id lookup: district_id → passes filter?
    passing_by_filter: list[set[int]] = []
    for f in resolved_filters:
        filter_rows = [r for r in rows if r.metric_id == f.metric_id]
        passing_district_ids: set[int] = {
            r.district_id for r in filter_rows if _row_passes_metric_filter(r, f)
        }
        passing_district_ids -= set(f.exclude_district_ids)
        passing_by_filter.append(passing_district_ids)

    # A district must pass ALL filter predicates.
    if not passing_by_filter:
        return selection
    passing_all = passing_by_filter[0]
    for subsequent in passing_by_filter[1:]:
        passing_all = passing_all & subsequent

    filtered_districts = [
        d for d in selection.districts if d.district_id in passing_all
    ]
    return selection.model_copy(update={"districts": filtered_districts})


_NUMERIC_ONLY_FILTER_OPERATORS = frozenset(
    {
        "greater_than",
        "greater_than_or_equal",
        "less_than",
        "less_than_or_equal",
    }
)


def _filter_requires_numeric_metric(filter_spec: FilterSpec) -> bool:
    """Only the ordered comparators force numeric catalog resolution.

    ``equals``/``not_equals`` already work on text (Yes/No) metrics, and the
    categorical operators (``contains``/``in``/``not_in``) target text cells —
    none of these may demand a numeric metric (issue #1339).
    """
    return filter_spec.operator in _NUMERIC_ONLY_FILTER_OPERATORS


def _filter_value_kind(
    value: object,
    *,
    metric_answer_type: str | None,
) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, list):
        # in / not_in carry a list of category labels — always categorical text.
        return "text"
    if isinstance(value, int | float):
        return "numeric"
    if (
        (metric_answer_type or "").casefold() == "numeric"
        and parse_compass_numeric_value(value) is not None
    ):
        return "numeric"
    if _parse_boolean(value) is not None and str(value).casefold().strip() in {
        "true",
        "false",
    }:
        return "boolean"
    return "text"


def _best_guess_methodology_codes(
    metrics: list[MetricCandidate],
    alternate_candidates: list[MetricCandidate],
) -> list[MethodologyRef]:
    """Fix 4B disclosure: when ``_resolve_plan_metrics`` best-guessed a
    materially-ambiguous metric phrase, name the metric it chose plus the
    alternates it set aside so the best-guess is never silent. Empty when
    nothing was best-guessed (``alternate_candidates`` empty). The chosen metric
    is ``metrics[0]`` — the promoted primary for the single-metric ambiguous
    shape this fires on."""

    if not alternate_candidates or not metrics:
        return []
    return [
        MethodologyRef(
            code="metric_best_guess_disclosure",
            metadata={
                "chosen_metric": metrics[0].name,
                "alternate_metrics": "; ".join(
                    candidate.name for candidate in alternate_candidates
                ),
            },
        )
    ]


def _metric_filter_methodology_codes(
    resolved_filters: list[ResolvedMetricFilter],
) -> list[MethodologyRef]:
    refs: list[MethodologyRef] = []
    if any(filter_spec.exclude_district_ids for filter_spec in resolved_filters):
        refs.append(MethodologyRef(code="anchor_value_filter_applied"))
    if any(
        filter_spec.operator in _CATEGORICAL_TEXT_OPERATORS
        for filter_spec in resolved_filters
    ):
        refs.append(MethodologyRef(code="categorical_value_filter_applied"))
    return refs


def _candidate_pool_limit(peer_limit: int) -> int:
    return max(_PEER_CANDIDATE_POOL_MIN, peer_limit * _PEER_CANDIDATE_POOL_MULTIPLIER)


def _peer_anchor_states(anchor_text: str, plan: QueryPlan) -> set[str] | None:
    """States to pass into ``resolve_peer_set`` after #1363 anchor pre-resolution.

    The anchor is already pre-resolved to one canonical district by
    ``resolve_referent_district`` (which split any trailing ``"ME"`` qualifier).
    ``resolve_peer_set`` still re-resolves the *name* internally, and its
    ``states`` argument double-duties as both the anchor-resolution filter AND
    the positive peer-pool filter (``build_peer_set(include_states=states)``).

    To keep the canonical name unambiguous for that internal re-resolution
    without silently restricting the peer pool, we pass:

    - the plan's ``requested_states`` (the historic peer-pool filter — empty for
      the Denver case 403, so peers stay cross-state and behavior is unchanged),
      plus
    - the state the user *explicitly bundled into the anchor string*
      (``"Portland ME"``). That qualifier is the user's own anchor
      disambiguation, so honoring it on the peer pool is intended; without it
      the bare ``"Portland"`` would re-trigger ambiguity inside
      ``resolve_peer_set`` and dead-end to ``None``.
    """

    _, suffix_states = split_state_suffix(anchor_text)
    return (requested_states(plan) | suffix_states) or None


def _peer_selected_districts(peer_set: PeerSetResolution) -> list[SelectedDistrict]:
    return [
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


def _filter_peer_set_by_metric_value_filters(
    peer_set: PeerSetResolution,
    rows: list[MetricAnswerRow],
    resolved_filters: list[ResolvedMetricFilter],
) -> PeerSetResolution:
    """Return peer candidates that satisfy all resolved metric-value filters."""
    if not resolved_filters:
        return peer_set

    peer_selection = ResultSelection(
        scope="named_districts",
        districts=_peer_selected_districts(peer_set),
    )
    filtered_selection = _apply_metric_value_filters_to_selection(
        peer_selection,
        rows,
        resolved_filters,
    )
    passing_peer_ids = {
        district.district_id
        for district in filtered_selection.districts
        if district.district_id != peer_set.anchor.district_id
    }
    return PeerSetResolution(
        anchor=peer_set.anchor,
        peers=[
            peer for peer in peer_set.peers if peer.district_id in passing_peer_ids
        ],
        selection_method=peer_set.selection_method,
        policy_version=peer_set.policy_version,
    )


def _screen_peer_set_for_comparison_ready_rows(
    plan: QueryPlan,
    peer_set: PeerSetResolution,
    metric_rows: list[
        tuple[MetricCandidate, list[MetricAnswerRow], list[MetricAnswerRow]]
    ],
    *,
    reviewed_district_ids: set[int],
    peer_limit: int,
) -> _PeerCoverageScreenResult:
    peer_coverages = [
        (
            peer,
            _metric_coverage_for_district(
                peer.district_id,
                metric_rows,
                reviewed_district_ids,
            ),
        )
        for peer in peer_set.peers
    ]
    comparison_ready = [
        peer
        for peer, coverage in peer_coverages
        if coverage.usable_for_peer_comparison
    ]
    unavailable_count = len(peer_set.peers) - len(comparison_ready)

    selected_peers, cap_applied, replacement_count = _apply_default_peer_diversity(
        plan,
        peer_set,
        comparison_ready,
        peer_limit=peer_limit,
    )
    reranked = [
        peer.model_copy(update={"rank": rank})
        for rank, peer in enumerate(selected_peers, start=1)
    ]
    return _PeerCoverageScreenResult(
        peer_set=PeerSetResolution(
            anchor=peer_set.anchor,
            peers=reranked,
            selection_method=peer_set.selection_method,
            policy_version=peer_set.policy_version,
        ),
        candidate_peer_count=len(peer_set.peers),
        excluded_unavailable_count=unavailable_count,
        same_state_cap_applied=cap_applied,
        diversity_replacement_count=replacement_count,
    )


def _metric_coverage_for_district(
    district_id: int,
    metric_rows: list[
        tuple[MetricCandidate, list[MetricAnswerRow], list[MetricAnswerRow]]
    ],
    reviewed_district_ids: set[int],
) -> _PeerMetricCoverage:
    if district_id not in reviewed_district_ids:
        return _PeerMetricCoverage(
            covered_count=0,
            metric_count=len(metric_rows),
        )

    covered_count = 0
    for metric, rows, _recent_rows in metric_rows:
        row = _row_for_current_metric(rows, district_id)
        if row is None:
            continue
        label = coverage_label_for_answer(
            row.value,
            district_name=row.district_name,
            metric_name=metric.name,
            academic_year=row.academic_year,
        )
        if label.state == "covered":
            covered_count += 1
    return _PeerMetricCoverage(
        covered_count=covered_count,
        metric_count=len(metric_rows),
    )


def _row_for_current_metric(
    rows: list[MetricAnswerRow],
    district_id: int,
) -> MetricAnswerRow | None:
    for row in rows:
        if row.district_id == district_id:
            return row
    return None


def _apply_default_peer_diversity(
    plan: QueryPlan,
    peer_set: PeerSetResolution,
    comparison_ready: list[PeerDistrictCandidate],
    *,
    peer_limit: int,
) -> tuple[list[PeerDistrictCandidate], bool, int]:
    selected = list(comparison_ready[:peer_limit])
    if not _default_peer_diversity_applies(plan, peer_set):
        return selected, False, 0

    anchor_state = (peer_set.anchor.state or "").upper()
    if not anchor_state:
        return selected, False, 0

    same_state_count = _same_state_peer_count(selected, anchor_state)
    if same_state_count <= _PEER_DEFAULT_SAME_STATE_CAP:
        return selected, False, 0

    selected_ids = {peer.district_id for peer in selected}
    replacements = [
        peer
        for peer in comparison_ready[peer_limit:]
        if peer.district_id not in selected_ids
        and (peer.state or "").upper() != anchor_state
    ]
    replacement_index = 0
    replacement_count = 0
    next_selected = list(selected)

    for index in range(len(next_selected) - 1, -1, -1):
        if same_state_count <= _PEER_DEFAULT_SAME_STATE_CAP:
            break
        displaced = next_selected[index]
        if (displaced.state or "").upper() != anchor_state:
            continue
        replacement = None
        while replacement_index < len(replacements):
            candidate = replacements[replacement_index]
            replacement_index += 1
            if (
                candidate.similarity_score
                >= displaced.similarity_score - _PEER_DEFAULT_DIVERSITY_SCORE_DELTA
            ):
                replacement = candidate
                break
        if replacement is None:
            continue
        next_selected[index] = replacement
        same_state_count -= 1
        replacement_count += 1

    next_selected.sort(
        key=lambda peer: (-peer.similarity_score, peer.district_name.casefold())
    )
    return next_selected[:peer_limit], replacement_count > 0, replacement_count


def _default_peer_diversity_applies(
    plan: QueryPlan,
    peer_set: PeerSetResolution,
) -> bool:
    """The same-state cap applies whenever the user hasn't requested an
    in-state-only comparison and we have peers to consider. It is the
    stated default rule for peer comparisons (see module docstring on
    _PEER_DEFAULT_SAME_STATE_CAP), not a conditional diversity guard — it
    holds for any ``peer_limit``, which is why the caller no longer passes one.
    """
    return (
        plan.peer_overrides is None
        and not requested_states(plan)
        and bool(peer_set.peers)
    )


def _same_state_peer_count(
    peers: list[PeerDistrictCandidate],
    anchor_state: str,
) -> int:
    return sum(1 for peer in peers if (peer.state or "").upper() == anchor_state)


class _OperationsMixin:
    """Provides operation dispatch methods to DeterministicQueryExecutor."""

    async def _execute_ranking(self, plan: QueryPlan) -> ExecutionOutcome:
        # Resolve metric-value filters FIRST.
        metric_filter_result = await self._resolve_metric_value_filters(plan)
        if isinstance(metric_filter_result, EXECUTION_OUTCOME_TYPES):
            return metric_filter_result
        plan, resolved_metric_filters = metric_filter_result

        profile_filter_result = await self._resolve_profile_filter_fields(plan)
        if isinstance(profile_filter_result, EXECUTION_OUTCOME_TYPES):
            return profile_filter_result
        plan, profile_filter_field_authority = profile_filter_result
        plan, profile_filter_metric_authority = await self._strip_profile_filter_metrics(
            plan
        )
        profile_filter_authority = [
            *profile_filter_field_authority,
            *profile_filter_metric_authority,
        ]
        if _has_unresolved_filter_metrics(plan):
            return ExecutionRefusal(message=_UNSUPPORTED_SHAPE_MESSAGE)

        # W2-M3-01 phase 3 (#859): when the user accepted "do all N
        # separately" in an N-metric clarification, the planner sets
        # ``requires_composite_ranking=True``. Intercept BEFORE the
        # profile-selection/profile-presentation branches and BEFORE the
        # multi-metric fallbacks (``_execute_lookup`` /
        # ``_execute_limited_ranked_lookup``) so the executor emits a
        # ``CompositeRankingResult`` envelope of N ``MetricRankingResult``
        # children instead. The phase 2 validator already guarantees
        # ``operation == "rank"`` and ``2 <= len(metrics) <= 8``.
        if plan.requires_composite_ranking:
            return await self._execute_composite_ranking(
                plan,
                profile_filter_authority=profile_filter_authority,
            )

        profile_selection_step = _profile_selection_step(plan)
        if (
            profile_selection_step is not None
            and _primary_metric_is_policy_metric(plan)
            and _policy_presentation_step(plan) is None
        ):
            return await self._execute_profile_ordered_metric_ranking(
                plan,
                profile_selection_step,
            )

        profile_presentation_step = _profile_presentation_step(plan)
        if profile_presentation_step is not None:
            return await self._execute_profile_ranking(plan, profile_presentation_step)

        execution_metric_specs = _execution_metric_specs(plan)
        if len(execution_metric_specs) > 1 and plan.limit is None:
            return await self._execute_lookup(
                plan.model_copy(update={"operation": "lookup", "sort": None})
            )
        if len(execution_metric_specs) > 1 and plan.limit is not None:
            return await self._execute_limited_ranked_lookup(
                plan,
                resolved_metric_filters=resolved_metric_filters,
                profile_filter_authority=profile_filter_authority,
            )

        if not ranking_shape_is_supported(plan):
            return ExecutionRefusal(message=_UNSUPPORTED_SHAPE_MESSAGE)

        selection_result = await self._resolve_ranking_selection(plan)
        if isinstance(selection_result, EXECUTION_OUTCOME_TYPES):
            return selection_result
        selection, profile_selection_authority = selection_result

        if (
            await self._catalog.is_multi_metric_bundle(plan.metrics[0].name)
            and plan.limit is None
        ):
            return await self._execute_lookup(
                plan.model_copy(update={"operation": "lookup"}),
                ordering_plan=plan,
            )

        # Resolve the primary metric through the shared rank helper so this
        # plain single-metric ranking path applies the governed degree-lane
        # default (e.g. bare "starting teacher salary" → bachelor's lane +
        # disclosure note) and clarifies with the real candidates, exactly like
        # `_execute_limited_ranked_lookup` and the profile-sort paths already
        # do. Resolving inline here dodged into a BA-vs-MA clarify even when the
        # governed default applied (#1248 WS-1; case_id=470 regression).
        primary_result = await self._resolve_rank_primary_metric_with_default(
            plan,
            plan.metrics[0],
        )
        if isinstance(primary_result, EXECUTION_OUTCOME_TYPES):
            return primary_result
        metric, source_notes = primary_result

        raw_rows = await self._repository.fetch_metric_answer_rows(
            metric_id=metric.metric_id,
            academic_year=self._current_academic_year,
        )
        scope_districts = await self._scope_districts(selection)
        scope_district_ids = {district.district_id for district in scope_districts}

        # Apply metric-value filters AFTER scope_districts are resolved so we
        # have a concrete district list to filter against.
        if resolved_metric_filters:
            filter_rows: list[MetricAnswerRow] = list(raw_rows)
            for f in resolved_metric_filters:
                if f.metric_id != metric.metric_id:
                    extra = await self._repository.fetch_metric_answer_rows(
                        metric_id=f.metric_id,
                        academic_year=self._current_academic_year,
                    )
                    filter_rows.extend(extra)
            # Build a temporary ResultSelection with concrete districts so the
            # filter helper has something to narrow.
            concrete_selection = selection.model_copy(
                update={"districts": scope_districts}
            )
            concrete_selection = _apply_metric_value_filters_to_selection(
                concrete_selection, filter_rows, resolved_metric_filters
            )
            scope_districts = concrete_selection.districts
            scope_district_ids = {district.district_id for district in scope_districts}
            selection = concrete_selection

        recent_rows = await self._repository.fetch_recent_metric_answer_rows(
            metric_id=metric.metric_id,
            before_academic_year=self._current_academic_year,
            district_ids=scope_district_ids,
        )
        reviewed_district_ids = await self._reviewed_district_ids(scope_districts)
        result = build_metric_ranking_result(
            plan,
            metric,
            raw_rows,
            recent_rows=recent_rows,
            selection=selection,
            selected_districts=scope_districts,
            default_limit=self._default_limit,
            reviewed_district_ids=reviewed_district_ids,
            academic_year=self._current_academic_year,
            source_notes=source_notes,
        )
        # Emit methodology code when user's explicit degree_lane shaped metric resolution.
        if plan.metrics[0].degree_lane is not None:
            result = result.model_copy(
                update={
                    "methodology_codes": [
                        *result.methodology_codes,
                        MethodologyRef(
                            code="degree_lane_applied",
                            metadata={"degree_lane": plan.metrics[0].degree_lane},
                        ),
                    ]
                }
            )
        filter_methodology = _metric_filter_methodology_codes(resolved_metric_filters)
        if filter_methodology:
            result = result.model_copy(
                update={
                    "methodology_codes": [
                        *result.methodology_codes,
                        *filter_methodology,
                    ]
                }
            )
        return ExecutionSuccess(
            result=result,
            authority=_validation_authority(
                plan,
                selection,
                [metric],
                profile_fields=[
                    *profile_filter_authority,
                    *(
                        [profile_selection_authority]
                        if profile_selection_authority is not None
                        else []
                    ),
                ],
            ),
            message=result.order_statement,
        )

    async def _execute_composite_ranking(
        self,
        plan: QueryPlan,
        *,
        profile_filter_authority: list[tuple[str, NCESFieldCandidate, str]],
    ) -> ExecutionOutcome:
        """W2-M3-01 phase 3 (#859): build N ``MetricRankingResult`` children
        and wrap them in a ``CompositeRankingResult`` envelope.

        Triggered by ``plan.requires_composite_ranking=True``. Phase 2's
        ``QueryPlan`` validator enforces ``operation == "rank"`` and
        ``2 <= len(metrics) <= 8`` at construction; this method preserves
        that invariant at emit time (it returns ``ExecutionRefusal`` rather
        than violating the envelope's ``min_length=2`` bound if metric
        de-duplication collapses the child list).

        Selection resolution runs ONCE (the composite is one answer shape
        with shared scope across N tables). Per-metric row-fetch + child
        build is in a loop that mirrors the existing per-metric pattern in
        ``_execute_limited_ranked_lookup`` (operations.py around line 943).

        Phase 3 explicitly scopes the executor only — renderer composer
        wiring (phase 4) handles formatting the envelope into N tables.
        """

        selection_result = await self._resolve_ranking_selection(plan)
        if isinstance(selection_result, EXECUTION_OUTCOME_TYPES):
            return selection_result
        selection, profile_selection_authority = selection_result

        scope_districts = await self._scope_districts(selection)
        scope_district_ids = {district.district_id for district in scope_districts}
        reviewed_district_ids = await self._reviewed_district_ids(scope_districts)

        children: list[MetricRankingResult] = []
        authority_metrics: list[MetricCandidate] = []
        seen_metric_ids: set[int] = set()

        # W2-M5-04 / #748: share citation dedup state across composite
        # siblings so the same document URL cited from multiple children
        # consolidates into one CitationRef and one inline marker number.
        # Pydantic v2 frozen=True does not deep-copy list/dict containers,
        # so every child's ``citations`` field ends up referencing the same
        # final deduped list once all children have been built.
        shared_citation_refs: list = []
        shared_citation_marker_by_identity: dict[tuple[object, ...], int] = {}

        for metric_spec in _execution_metric_specs(plan):
            child_source_notes: list[str] = []
            metric_resolution = await self._catalog.resolve_metric_bundle(
                metric_spec.name,
                numeric_only=False,
                limit=_METRIC_CLARIFICATION_CANDIDATE_LIMIT,
                degree_lane=metric_spec.degree_lane,
            )
            if metric_resolution.ambiguous:
                # Commit the governed degree-lane default (bare "starting
                # salary" → bachelor's lane + disclosure note) before
                # clarifying, mirroring the primary rank path (#1442) and
                # the lookup metric-groups loop (#1248 WS-1; #1452). Each
                # composite child is a full ranking, so a bare-lane salary
                # child must not dodge the whole composite into a clarify.
                contextual_default = await self._contextual_metric_default(
                    metric_spec, numeric_only=False
                )
                if contextual_default is None:
                    clarification = await compose_clarify_question_async(
                        metric_spec.name,
                        operation=plan.operation,
                        candidates=metric_resolution.candidates,
                        adjudicator_hint=metric_resolution.clarification_hint,
                    )
                    return ExecutionClarification(
                        clarification=clarification,
                        message=clarification.question,
                        resolution_report=_metric_resolution_report(
                            plan,
                            metric_spec.name,
                            metric_resolution,
                            entity_type="metric_bundle",
                        ),
                    )
                metric = contextual_default.metric
                child_source_notes = [
                    note.note_text for note in contextual_default.renderer_notes
                ]
            elif len(metric_resolution.resolved) != 1:
                # Surface the real candidates the catalog found instead of a
                # generic dead-end (SELECT-R4 / #1248). The bundle resolver
                # can return candidates without flagging ``ambiguous`` (e.g.
                # an ambiguous alias whose canonical IDs no longer fetch a
                # metric, or a lane filter that left several lane-matching
                # candidates). #1363 WS-B: harmonize the clarify gate to the
                # single >1-distinct rule scoping.py uses — a single distinct
                # candidate keeps the refusal; never clarify on zero.
                if _has_multiple_metric_candidates(metric_resolution.candidates):
                    clarification = await compose_clarify_question_async(
                        metric_spec.name,
                        operation=plan.operation,
                        candidates=metric_resolution.candidates,
                        adjudicator_hint=metric_resolution.clarification_hint,
                    )
                    return ExecutionClarification(
                        clarification=clarification,
                        message=clarification.question,
                        resolution_report=_metric_resolution_report(
                            plan,
                            metric_spec.name,
                            metric_resolution,
                            entity_type="metric_bundle",
                        ),
                    )
                return ExecutionRefusal(
                    message=(
                        "I could not resolve every requested metric for "
                        f"this separate ranking: {metric_spec.name}."
                    ),
                    resolution_report=_metric_resolution_report(
                        plan,
                        metric_spec.name,
                        metric_resolution,
                        entity_type="metric_bundle",
                    ),
                )
            else:
                metric = metric_resolution.resolved[0]
            if metric.metric_id in seen_metric_ids:
                continue
            seen_metric_ids.add(metric.metric_id)
            authority_metrics.append(metric)

            raw_rows = await self._repository.fetch_metric_answer_rows(
                metric_id=metric.metric_id,
                academic_year=self._current_academic_year,
            )
            recent_rows = await self._repository.fetch_recent_metric_answer_rows(
                metric_id=metric.metric_id,
                before_academic_year=self._current_academic_year,
                district_ids=scope_district_ids,
            )

            # Clone the plan with this child's single metric so
            # ``build_metric_ranking_result`` reads the right metric spec
            # (and drop the composite flag — each child is a leaf rank,
            # not itself composite). ``inherit_from_session`` stays on the
            # plan only if it was set originally; we preserve other plan
            # state (sort, filters resolved earlier, selection) so the
            # child's order_statement and methodology match.
            child_plan = plan.model_copy(
                update={
                    "metrics": [metric_spec],
                    "requires_composite_ranking": False,
                }
            )
            child = build_metric_ranking_result(
                child_plan,
                metric,
                raw_rows,
                recent_rows=recent_rows,
                selection=selection,
                selected_districts=scope_districts,
                default_limit=self._default_limit,
                reviewed_district_ids=reviewed_district_ids,
                academic_year=self._current_academic_year,
                source_notes=child_source_notes,
                shared_citation_refs=shared_citation_refs,
                shared_citation_marker_by_identity=(
                    shared_citation_marker_by_identity
                ),
            )
            # Emit methodology code when user's explicit degree_lane shaped
            # this child's metric resolution (#1454 composite gap).
            if metric_spec.degree_lane is not None:
                child = child.model_copy(
                    update={
                        "methodology_codes": [
                            *child.methodology_codes,
                            MethodologyRef(
                                code="degree_lane_applied",
                                metadata={"degree_lane": metric_spec.degree_lane},
                            ),
                        ]
                    }
                )
            children.append(child)

        if len(children) < 2:
            # Phase 2 validator guarantees the plan has 2..8 metrics, but
            # metric-id de-duplication after resolution may collapse the
            # list below 2 (e.g., two aliases that resolve to the same
            # metric_id). Refuse with a typed message rather than violate
            # ``CompositeRankingResult``'s ``min_length=2`` bound.
            return ExecutionRefusal(
                message=(
                    "Composite ranking requires at least 2 distinct resolved "
                    f"metrics; got {len(children)} after de-duplication."
                ),
            )

        composite = CompositeRankingResult(
            selection=selection,
            children=children,
            citations=list(shared_citation_refs),
            total_considered=0,  # validator rolls up from children
            excluded_count=0,  # validator rolls up from children
            order_statement=(
                f"{len(children)} metrics, ranked highest to lowest by each."
            ),
        )

        return ExecutionSuccess(
            result=composite,
            authority=_validation_authority(
                plan,
                selection,
                authority_metrics,
                profile_fields=[
                    *profile_filter_authority,
                    *(
                        [profile_selection_authority]
                        if profile_selection_authority is not None
                        else []
                    ),
                ],
            ),
            message=composite.order_statement,
        )

    async def _execute_limited_ranked_lookup(
        self,
        plan: QueryPlan,
        *,
        resolved_metric_filters: list[ResolvedMetricFilter],
        profile_filter_authority: list[tuple[str, NCESFieldCandidate, str]],
    ) -> ExecutionOutcome:
        primary_metric_spec = _primary_metric_spec(plan)
        if primary_metric_spec is None:
            return ExecutionRefusal(message=_UNSUPPORTED_SHAPE_MESSAGE)

        selection_result = await self._resolve_ranking_selection(plan)
        if isinstance(selection_result, EXECUTION_OUTCOME_TYPES):
            return selection_result
        selection, profile_selection_authority = selection_result

        primary_result = await self._resolve_rank_primary_metric_with_default(
            plan,
            primary_metric_spec,
        )
        if isinstance(primary_result, EXECUTION_OUTCOME_TYPES):
            return primary_result
        primary_metric, source_notes = primary_result

        raw_rows = await self._repository.fetch_metric_answer_rows(
            metric_id=primary_metric.metric_id,
            academic_year=self._current_academic_year,
        )
        scope_districts = await self._scope_districts(selection)
        scope_district_ids = {district.district_id for district in scope_districts}

        if resolved_metric_filters:
            filter_rows: list[MetricAnswerRow] = list(raw_rows)
            for filter_spec in resolved_metric_filters:
                if filter_spec.metric_id != primary_metric.metric_id:
                    extra = await self._repository.fetch_metric_answer_rows(
                        metric_id=filter_spec.metric_id,
                        academic_year=self._current_academic_year,
                    )
                    filter_rows.extend(extra)
            concrete_selection = selection.model_copy(
                update={"districts": scope_districts}
            )
            concrete_selection = _apply_metric_value_filters_to_selection(
                concrete_selection,
                filter_rows,
                resolved_metric_filters,
            )
            scope_districts = concrete_selection.districts
            scope_district_ids = {district.district_id for district in scope_districts}
            selection = concrete_selection

        recent_rows = await self._repository.fetch_recent_metric_answer_rows(
            metric_id=primary_metric.metric_id,
            before_academic_year=self._current_academic_year,
            district_ids=scope_district_ids,
        )
        reviewed_district_ids = await self._reviewed_district_ids(scope_districts)
        ranking_result = build_metric_ranking_result(
            plan,
            primary_metric,
            raw_rows,
            recent_rows=recent_rows,
            selection=selection,
            selected_districts=scope_districts,
            default_limit=self._default_limit,
            reviewed_district_ids=reviewed_district_ids,
            academic_year=self._current_academic_year,
            source_notes=source_notes,
        )
        ranked_ids = [row.district_id for row in ranking_result.rows]
        districts_by_id = {district.district_id: district for district in scope_districts}
        ranked_districts = [
            districts_by_id[district_id]
            for district_id in ranked_ids
            if district_id in districts_by_id
        ]
        ranked_selection = selection.model_copy(
            update={
                "scope": "named_districts",
                "districts": ranked_districts,
                "unresolved_districts": [],
            }
        )
        ranked_district_ids = {
            district.district_id for district in ranked_selection.districts
        }

        metric_rows: list[
            tuple[MetricCandidate, list[MetricAnswerRow], list[MetricAnswerRow]]
        ] = []
        authority_metrics: list[MetricCandidate] = []
        seen_metric_ids: set[int] = set()
        for index, metric_spec in enumerate(_execution_metric_specs(plan)):
            if index == 0:
                resolved_metrics = [primary_metric]
            else:
                metric_resolution = await self._catalog.resolve_metric_bundle(
                    metric_spec.name,
                    numeric_only=False,
                    limit=_METRIC_CLARIFICATION_CANDIDATE_LIMIT,
                    degree_lane=metric_spec.degree_lane,
                )
                if metric_resolution.ambiguous:
                    # Commit the governed degree-lane default (bare
                    # "starting salary" → bachelor's lane + disclosure note)
                    # before clarifying, mirroring the primary rank path
                    # (#1442) and the lookup metric-groups loop (#1248 WS-1;
                    # #1452) — a bare-lane salary comparison column must not
                    # dodge the whole ranked lookup into a clarify.
                    contextual_default = await self._contextual_metric_default(
                        metric_spec, numeric_only=False
                    )
                    if contextual_default is None:
                        clarification = await compose_clarify_question_async(
                            metric_spec.name,
                            operation=plan.operation,
                            candidates=metric_resolution.candidates,
                            adjudicator_hint=metric_resolution.clarification_hint,
                        )
                        return ExecutionClarification(
                            clarification=clarification,
                            message=clarification.question,
                            resolution_report=_metric_resolution_report(
                                plan,
                                metric_spec.name,
                                metric_resolution,
                                entity_type="metric_bundle",
                            ),
                        )
                    resolved_metrics = [contextual_default.metric]
                    source_notes.extend(
                        note.note_text
                        for note in contextual_default.renderer_notes
                    )
                elif not metric_resolution.resolved:
                    # Surface the real candidates the catalog found instead
                    # of a generic dead-end that discards them. #1363 round
                    # 2: mirror the :1166 separate-ranking template + the
                    # single >1-distinct rule scoping.py uses — a single
                    # distinct candidate keeps the per-metric refusal; never
                    # clarify on zero.
                    if _has_multiple_metric_candidates(
                        metric_resolution.candidates
                    ):
                        clarification = await compose_clarify_question_async(
                            metric_spec.name,
                            operation=plan.operation,
                            candidates=metric_resolution.candidates,
                            adjudicator_hint=metric_resolution.clarification_hint,
                        )
                        return ExecutionClarification(
                            clarification=clarification,
                            message=clarification.question,
                            resolution_report=_metric_resolution_report(
                                plan,
                                metric_spec.name,
                                metric_resolution,
                                entity_type="metric_bundle",
                            ),
                        )
                    return ExecutionRefusal(
                        message=(
                            "I could not match every requested metric. Please "
                            f"choose the intended metric for: {metric_spec.name}."
                        ),
                        resolution_report=_metric_resolution_report(
                            plan,
                            metric_spec.name,
                            metric_resolution,
                            entity_type="metric_bundle",
                        ),
                    )
                else:
                    resolved_metrics = metric_resolution.resolved

            for metric in resolved_metrics:
                if metric.metric_id in seen_metric_ids:
                    continue
                seen_metric_ids.add(metric.metric_id)
                authority_metrics.append(metric)
                current_rows = await self._fetch_metric_rows_for_year(
                    metric_id=metric.metric_id,
                    academic_year=self._current_academic_year,
                    district_ids=ranked_district_ids,
                )
                recent_metric_rows = await self._repository.fetch_recent_metric_answer_rows(
                    metric_id=metric.metric_id,
                    before_academic_year=self._current_academic_year,
                    district_ids=ranked_district_ids,
                )
                metric_rows.append((metric, current_rows, recent_metric_rows))

        lookup_reviewed_ids = await self._reviewed_district_ids(
            ranked_selection.districts,
        )
        result = build_metric_lookup_result(
            plan,
            metric_rows,
            selection=ranked_selection,
            reviewed_district_ids=lookup_reviewed_ids,
            academic_year=self._current_academic_year,
            preserve_selection_order=True,
            source_notes=source_notes,
        )
        # direction() reads the finalized plan (sort_steps), not the legacy
        # plan.sort the finalizer always clears to None — otherwise this label
        # defaults to "desc" and contradicts the asc rows build_metric_ranking_result
        # produces for "ranked lowest first" (same seam as the direction() fix).
        order = direction_phrase(direction(plan))
        result = result.model_copy(
            update={
                "order_statement": (
                    f"Ranked by {primary_metric.name}, {order}; displayed "
                    "requested comparison metrics for the ranked districts."
                ),
                # #1220: name the metric the rows are ordered by so the
                # comparison renderer can mark that column as the ranked one —
                # a BA-vs-MA salary table must make plain which figure drives
                # the order instead of blending both into one confusing order.
                "ranked_by_metric_name": primary_metric.name,
            }
        )
        # Emit methodology code when user's explicit degree_lane shaped the
        # primary metric resolution (#1454 GAP 2).  Mirror the plain rank
        # path (_execute_ranking) and the lookup path (_execute_lookup).
        lane_applied = next(
            (ms.degree_lane for ms in _execution_metric_specs(plan)
             if ms.degree_lane is not None),
            None,
        )
        if lane_applied is not None:
            result = result.model_copy(
                update={
                    "methodology_codes": [
                        *result.methodology_codes,
                        MethodologyRef(
                            code="degree_lane_applied",
                            metadata={"degree_lane": lane_applied},
                        ),
                    ]
                }
            )
        filter_methodology = _metric_filter_methodology_codes(resolved_metric_filters)
        if filter_methodology:
            result = result.model_copy(
                update={
                    "methodology_codes": [
                        *result.methodology_codes,
                        *filter_methodology,
                    ]
                }
            )
        return ExecutionSuccess(
            result=result,
            authority=_validation_authority(
                plan,
                ranked_selection,
                authority_metrics,
                profile_fields=[
                    *profile_filter_authority,
                    *(
                        [profile_selection_authority]
                        if profile_selection_authority is not None
                        else []
                    ),
                ],
            ),
            message=result.order_statement,
        )

    async def _resolve_rank_primary_metric_with_default(
        self,
        plan: QueryPlan,
        metric_spec: MetricSpec,
    ) -> tuple[MetricCandidate, list[str]] | ExecutionOutcome:
        metric_resolution = await self._catalog.resolve_primary_metric(
            metric_spec.name,
            numeric_only=True,
            limit=_METRIC_CLARIFICATION_CANDIDATE_LIMIT,
            degree_lane=metric_spec.degree_lane,
        )
        if metric_resolution.resolved is not None and not metric_resolution.ambiguous:
            return metric_resolution.resolved, []

        if metric_spec.degree_lane is None:
            contextual_default = (
                await self._catalog.resolve_contextual_metric_default(
                    metric_spec.name,
                    context_key="launch_starting_salary_default",
                    numeric_only=True,
                )
            )
            if contextual_default is not None:
                return contextual_default.metric, [
                    note.note_text for note in contextual_default.renderer_notes
                ]

        if _candidates_are_self_referential(
            metric_spec.name, metric_resolution.candidates
        ):
            # #1830 loop-breaker: the user already chose this exact string and it
            # is a non-numeric (categorical/policy) field that ``numeric_only``
            # filtered out of the rank. Re-offering it as the sole clarify
            # candidate is a guaranteed self-referential loop with no exit — the
            # production failure this fix closes. Refuse plainly instead.
            return ExecutionRefusal(
                message=_cannot_rank_non_numeric_message(metric_spec.name),
                resolution_report=_metric_resolution_report(
                    plan,
                    metric_spec.name,
                    metric_resolution,
                    entity_type="metric",
                ),
            )

        if metric_resolution.ambiguous or _has_multiple_metric_candidates(
            metric_resolution.candidates
        ):
            # Catalog flagged ambiguity, OR it returned >1 distinct candidate
            # without confidently resolving one — clarify with the real
            # candidates instead of the generic "I found several possible
            # Compass metrics. Please choose one" refusal that listed NONE.
            # #1363 round 2: mirror the :961 template + the single >1-distinct
            # rule scoping.py uses. A single distinct candidate (or zero) keeps
            # the deterministic refusal below.
            clarification = await compose_clarify_question_async(
                metric_spec.name,
                operation=plan.operation,
                candidates=metric_resolution.candidates,
                adjudicator_hint=metric_resolution.clarification_hint,
            )
            return ExecutionClarification(
                clarification=clarification,
                message=clarification.question,
                resolution_report=_metric_resolution_report(
                    plan,
                    metric_spec.name,
                    metric_resolution,
                    entity_type="metric",
                ),
            )
        return ExecutionRefusal(
            message=(
                "I found several possible Compass metrics. Please choose one "
                "metric to rank and I will continue."
            ),
            resolution_report=_metric_resolution_report(
                plan,
                metric_spec.name,
                metric_resolution,
                entity_type="metric",
            ),
        )

    async def _execute_profile_ranking(
        self,
        plan: QueryPlan,
        sort_step: SortStepSpec,
    ) -> ExecutionOutcome:
        resolved_step = await self._resolve_profile_sort_step(plan, sort_step)
        if isinstance(resolved_step, EXECUTION_OUTCOME_TYPES):
            return resolved_step
        sort_step, profile_field_authority = resolved_step
        rows = await self._rank_profile_rows(
            plan,
            sort_step,
            limit=_profile_ranking_limit(
                plan,
                sort_step,
                default_limit=self._default_limit,
            ),
        )
        if isinstance(rows, ExecutionRefusal):
            return rows
        selection = _selection_from_profile_rows(plan, rows, states=requested_states(plan))
        result = build_profile_ranking_result(
            plan,
            rows,
            selection=selection,
            direction=sort_step.direction,
        )
        return ExecutionSuccess(
            result=result,
            authority=_validation_authority(
                plan,
                selection,
                [],
                profile_fields=[profile_field_authority],
            ),
            message=result.order_statement,
        )

    async def _resolve_profile_sort_display_default(
        self,
        metric_spec: MetricSpec,
        sort_step: SortStepSpec,
        *,
        numeric_only: bool,
    ) -> ContextualMetricDefault | None:
        """Return a governed display-metric default for profile-sorted results."""

        if metric_spec.degree_lane is not None:
            return None
        context_keys = ["launch_starting_salary_default"]
        if sort_step.field == "frpl_pct":
            context_keys.insert(0, "frpl_profile_sort_display")
        for context_key in context_keys:
            contextual_default = await self._catalog.resolve_contextual_metric_default(
                metric_spec.name,
                context_key=context_key,
                numeric_only=numeric_only,
            )
            if contextual_default is not None:
                return contextual_default
        return None

    async def _execute_profile_ordered_metric_ranking(
        self,
        plan: QueryPlan,
        sort_step: SortStepSpec,
    ) -> ExecutionOutcome:
        resolved_step = await self._resolve_profile_sort_step(plan, sort_step)
        if isinstance(resolved_step, EXECUTION_OUTCOME_TYPES):
            return resolved_step
        sort_step, profile_field_authority = resolved_step
        profile_rows = await self._rank_profile_rows(
            plan,
            sort_step,
            limit=_profile_ranking_limit(
                plan,
                sort_step,
                default_limit=self._default_limit,
            ),
        )
        if isinstance(profile_rows, ExecutionRefusal):
            return profile_rows
        if not profile_rows:
            return ExecutionRefusal(
                message=(
                    "I could not resolve profile-field districts for deterministic "
                    "execution yet."
                ),
            )
        selection = _selection_from_profile_rows(
            plan,
            profile_rows,
            states=requested_states(plan),
        )

        primary_metric = _primary_metric_spec(plan)
        if primary_metric is None:
            return ExecutionRefusal(message=_UNSUPPORTED_SHAPE_MESSAGE)

        metric_resolution = await self._catalog.resolve_primary_metric(
            primary_metric.name,
            numeric_only=False,
            limit=_METRIC_CLARIFICATION_CANDIDATE_LIMIT,
            degree_lane=primary_metric.degree_lane,
        )
        metric = metric_resolution.resolved
        source_notes: list[str] = []
        if metric_resolution.ambiguous:
            contextual_default = await self._resolve_profile_sort_display_default(
                primary_metric,
                sort_step,
                numeric_only=False,
            )
            if contextual_default is None:
                clarification = await compose_clarify_question_async(
                    primary_metric.name,
                    operation=plan.operation,
                    candidates=metric_resolution.candidates,
                    adjudicator_hint=metric_resolution.clarification_hint,
                )
                return ExecutionClarification(
                    clarification=clarification,
                    message=clarification.question,
                    resolution_report=_metric_resolution_report(
                        plan,
                        primary_metric.name,
                        metric_resolution,
                        entity_type="metric",
                    ),
                )
            metric = contextual_default.metric
            source_notes = [
                note.note_text for note in contextual_default.renderer_notes
            ]
            metric_resolution = metric_resolution.model_copy(
                update={"resolved": metric, "ambiguous": False}
            )
        if metric is None:
            contextual_default = await self._resolve_profile_sort_display_default(
                primary_metric,
                sort_step,
                numeric_only=False,
            )
            if contextual_default is not None:
                metric = contextual_default.metric
                source_notes = [
                    note.note_text for note in contextual_default.renderer_notes
                ]
                metric_resolution = metric_resolution.model_copy(
                    update={"resolved": metric, "ambiguous": False}
                )
        if metric is None:
            # The contextual-default fallbacks above did not resolve a display
            # metric. Surface the real candidates the catalog found instead of a
            # generic dead-end that discards them. #1363 round 2: mirror the :961
            # template + the single >1-distinct rule scoping.py uses — a single
            # distinct candidate keeps the refusal; never clarify on zero.
            if _has_multiple_metric_candidates(metric_resolution.candidates):
                clarification = await compose_clarify_question_async(
                    primary_metric.name,
                    operation=plan.operation,
                    candidates=metric_resolution.candidates,
                    adjudicator_hint=metric_resolution.clarification_hint,
                )
                return ExecutionClarification(
                    clarification=clarification,
                    message=clarification.question,
                    resolution_report=_metric_resolution_report(
                        plan,
                        primary_metric.name,
                        metric_resolution,
                        entity_type="metric",
                    ),
                )
            return ExecutionRefusal(
                message=(
                    "I could not match the requested display metric. Please "
                    f"choose the intended metric for: {primary_metric.name}."
                ),
                resolution_report=_metric_resolution_report(
                    plan,
                    primary_metric.name,
                    metric_resolution,
                    entity_type="metric",
                ),
            )

        raw_rows = await self._repository.fetch_metric_answer_rows(
            metric_id=metric.metric_id,
            academic_year=self._current_academic_year,
        )
        selected_district_ids = {
            district.district_id for district in selection.districts
        }
        recent_rows = await self._repository.fetch_recent_metric_answer_rows(
            metric_id=metric.metric_id,
            before_academic_year=self._current_academic_year,
            district_ids=selected_district_ids,
        )
        reviewed_district_ids = await self._reviewed_district_ids(selection.districts)
        result = build_profile_ordered_metric_ranking_result(
            plan,
            metric,
            raw_rows,
            recent_rows,
            profile_rows,
            selection=selection,
            direction=sort_step.direction,
            reviewed_district_ids=reviewed_district_ids,
            academic_year=self._current_academic_year,
            source_notes=source_notes,
        )
        if primary_metric.degree_lane is not None:
            result = result.model_copy(
                update={
                    "methodology_codes": [
                        *result.methodology_codes,
                        MethodologyRef(
                            code="degree_lane_applied",
                            metadata={"degree_lane": primary_metric.degree_lane},
                        ),
                    ]
                }
            )
        return ExecutionSuccess(
            result=result,
            authority=_validation_authority(
                plan,
                selection,
                [metric],
                profile_fields=[profile_field_authority],
            ),
            message=result.order_statement,
        )

    async def _resolve_ranking_selection(
        self,
        plan: QueryPlan,
    ) -> tuple[ResultSelection, tuple[str, NCESFieldCandidate, str] | None] | ExecutionOutcome:
        selection_step = _profile_selection_step(plan)
        if selection_step is None:
            selection = await resolve_selection(
                plan,
                self._catalog,
                academic_year=self._current_academic_year,
                largest_limit=self._default_limit,
            )
            if isinstance(selection, EXECUTION_OUTCOME_TYPES):
                return selection
            return selection, None
        resolved_step = await self._resolve_profile_sort_step(plan, selection_step)
        if isinstance(resolved_step, EXECUTION_OUTCOME_TYPES):
            return resolved_step
        selection_step, profile_field_authority = resolved_step
        rows = await self._rank_profile_rows(
            plan,
            selection_step,
            limit=_profile_ranking_limit(
                plan,
                selection_step,
                default_limit=self._default_limit,
            ),
        )
        if isinstance(rows, ExecutionRefusal):
            return rows
        if not rows:
            return ExecutionRefusal(
                message=(
                    "I could not resolve profile-field districts for deterministic "
                    "execution yet."
                ),
            )
        selection = _selection_from_profile_rows(plan, rows, states=requested_states(plan))
        return selection, profile_field_authority

    async def _resolve_profile_sort_step(
        self,
        plan: QueryPlan,
        sort_step: SortStepSpec,
    ) -> (
        tuple[SortStepSpec, tuple[str, NCESFieldCandidate, str]]
        | ExecutionRefusal
        | ExecutionClarification
    ):
        resolution = await self._catalog.resolve_profile_field_authority(sort_step.field)
        if not resolution.ok or resolution.resolved is None:
            return _profile_field_dead_end(plan, sort_step.field, resolution)
        field = resolution.resolved
        return (
            sort_step.model_copy(update={"field": field.field_key}),
            (sort_step.field, field, resolution.resolution_method),
        )

    async def _strip_profile_filter_metrics(
        self,
        plan: QueryPlan,
    ) -> tuple[QueryPlan, list[tuple[str, NCESFieldCandidate, str]]]:
        """Remove only catalog-resolved profile-field filters from metric specs."""

        retained_metrics: list[MetricSpec] = []
        profile_fields: list[tuple[str, NCESFieldCandidate, str]] = []
        changed = False
        for metric in plan.metrics:
            if metric.role in {"filter", "grouping"}:
                resolution = await self._catalog.resolve_profile_field_authority(
                    metric.name
                )
                if resolution.ok and resolution.resolved is not None:
                    profile_fields.append(
                        (
                            metric.name,
                            resolution.resolved,
                            resolution.resolution_method,
                        )
                    )
                    changed = True
                    continue
            retained_metrics.append(metric)

        if not changed:
            return plan, profile_fields
        return plan.model_copy(update={"metrics": retained_metrics}), profile_fields

    async def _resolve_profile_filter_fields(
        self,
        plan: QueryPlan,
    ) -> (
        tuple[QueryPlan, list[tuple[str, NCESFieldCandidate, str]]]
        | ExecutionRefusal
        | ExecutionClarification
    ):
        """Canonicalize profile-field filters through catalog authority."""

        filters: list[FilterSpec] = []
        profile_fields: list[tuple[str, NCESFieldCandidate, str]] = []
        changed = False
        for filter_spec in plan.filters:
            if (
                _is_state_filter(filter_spec)
                or not _filter_field_can_be_profile_field(filter_spec)
            ):
                filters.append(filter_spec)
                continue

            resolution = await self._catalog.resolve_profile_field_authority(
                filter_spec.field
            )
            if not resolution.ok or resolution.resolved is None:
                return _profile_field_dead_end(plan, filter_spec.field, resolution)
            field = resolution.resolved
            profile_fields.append(
                (filter_spec.field, field, resolution.resolution_method)
            )
            filters.append(filter_spec.model_copy(update={"field": field.field_key}))
            changed = True

        if not changed:
            return plan, profile_fields
        return plan.model_copy(update={"filters": filters}), profile_fields

    async def _resolve_metric_value_filters(
        self,
        plan: QueryPlan,
    ) -> tuple[QueryPlan, list[ResolvedMetricFilter]] | ExecutionOutcome:
        """Resolve metric-value filter fields to catalog metric IDs.

        For each filter where ``_filter_is_metric_value()`` is True, the field
        phrase is resolved via ``CatalogResolver.resolve_primary_metric()``.

        Returns the plan unchanged (the metric *phrase* stays free-form in
        ``FilterSpec.field`` — #1373 Stage 2 removed the ``"metric:<id>"`` field
        rewrite) and the list of ``ResolvedMetricFilter`` objects for post-fetch
        application. ``_resolve_profile_filter_fields`` skips these by their
        ``metric_value`` classification, and the count path scopes per-metric by
        ``ResolvedMetricFilter.metric_id`` — neither re-parses a field token.

        Idempotent: a resolved metric-value filter still classifies as
        ``metric_value`` (free-form phrase under a metric-value operator), so
        re-running the resolver re-resolves to the same metric_id and leaves the
        plan untouched.

        On catalog ambiguity → ``ExecutionClarification``.
        On catalog failure   → ``ExecutionRefusal``.
        """
        metric_filters = requested_metric_filters(plan)
        if not metric_filters:
            return plan, []

        resolved_filters: list[ResolvedMetricFilter] = []

        for filter_spec in plan.filters:
            if not _filter_is_metric_value(filter_spec):
                continue

            # PR 2A composition: if a MetricSpec in the same plan shares the
            # same name (case-folded) as this filter field, inherit its
            # degree_lane so the filter resolves to the same metric variant.
            _filter_field_cf = filter_spec.field.casefold().strip()
            inherited_lane = next(
                (
                    ms.degree_lane
                    for ms in plan.metrics
                    if ms.name.casefold().strip() == _filter_field_cf
                ),
                None,
            )
            numeric_only = _filter_requires_numeric_metric(filter_spec)
            metric = None
            metric_resolution = await self._catalog.resolve_primary_metric(
                filter_spec.field,
                numeric_only=numeric_only,
                limit=_METRIC_CLARIFICATION_CANDIDATE_LIMIT,
                degree_lane=inherited_lane,
            )
            # Before clarifying, check whether the governed salary default
            # applies (#1454 GAP 1).  Filter predicates are invisible to
            # the user — no disclosure note is needed; commit silently.
            if (
                metric_resolution.ambiguous
                or metric_resolution.resolved is None
            ) and inherited_lane is None:
                _filter_metric_spec = MetricSpec(
                    name=filter_spec.field, degree_lane=None
                )
                _ctx_default = await self._contextual_metric_default(
                    _filter_metric_spec, numeric_only=numeric_only
                )
                if _ctx_default is not None:
                    metric = _ctx_default.metric

            if metric is None and (
                metric_resolution.ambiguous or (
                    not metric_resolution.ambiguous
                    and metric_resolution.resolved is None
                    and _has_multiple_metric_candidates(
                        metric_resolution.candidates
                    )
                )
            ):
                # Catalog signals ambiguity, OR it returned >1 distinct
                # candidate without flagging ``ambiguous`` — clarify with the
                # real candidates. #1363 round 2 (WS-B): harmonize the inner
                # branch to the single >1-distinct rule scoping.py uses
                # (``_metric_clarification_or_refusal``). A *single* distinct
                # candidate is not recoverable by asking "which of this one?",
                # so it falls through to the deterministic refusal below;
                # never clarify on zero.
                clarification = await compose_clarify_question_async(
                    filter_spec.field,
                    operation=plan.operation,
                    candidates=metric_resolution.candidates,
                    adjudicator_hint=metric_resolution.clarification_hint,
                )
                return ExecutionClarification(
                    clarification=clarification,
                    message=clarification.question,
                    ambiguous_metric_phrase=filter_spec.field,
                    resolution_report=_metric_resolution_report(
                        plan,
                        filter_spec.field,
                        metric_resolution,
                        entity_type="metric",
                    ),
                )
            if metric is None and metric_resolution.resolved is None:
                return ExecutionRefusal(
                    message=(
                        "I could not resolve the filter field "
                        f"'{filter_spec.field}' to a governed metric for "
                        "this request."
                    ),
                    resolution_report=_metric_resolution_report(
                        plan,
                        filter_spec.field,
                        metric_resolution,
                        entity_type="metric",
                    ),
                )

            if metric is None:
                metric = metric_resolution.resolved
            filter_value = filter_spec.value
            exclude_district_ids: frozenset[int] = frozenset()
            if filter_spec.anchor_value is not None:
                anchor_result = await self._resolve_anchor_filter_value(
                    filter_spec,
                    plan,
                    metric_id=metric.metric_id,
                )
                if isinstance(anchor_result, EXECUTION_OUTCOME_TYPES):
                    return anchor_result
                filter_value, exclude_district_ids = anchor_result

            value_kind = _filter_value_kind(
                filter_value,
                metric_answer_type=metric.answer_type,
            )
            # Only the ordered comparators (>, >=, <, <=) need a numeric value;
            # equals/not_equals and the categorical operators (contains/in/
            # not_in) apply to text cells (issue #1339).
            if _filter_requires_numeric_metric(filter_spec) and value_kind != "numeric":
                return ExecutionRefusal(
                    message=(
                        f"Filter on '{filter_spec.field}' requires a numeric "
                        "comparison value."
                    ),
                )
            if filter_value is None:
                return ExecutionRefusal(
                    message=(
                        f"Filter on '{filter_spec.field}' requires a comparison "
                        "value or anchor district value."
                    ),
                )

            # #1772: parse a numeric filter value with the comma-stripping
            # canonical parser, not a raw float(). An equality-anchor on a
            # currency metric resolves the anchor's rendered cell ("45,602") as
            # filter_value; a raw float() there crashed the turn at resolution
            # time (before _row_passes_metric_filter ever ran). Unparseable ->
            # refuse with the same numeric-value message, never raise.
            if value_kind == "numeric":
                numeric_filter_value = parse_compass_numeric_value(filter_value)
                if numeric_filter_value is None:
                    return ExecutionRefusal(
                        message=(
                            f"Filter on '{filter_spec.field}' requires a numeric "
                            "comparison value."
                        ),
                    )
                resolved_value: float | str = numeric_filter_value
            else:
                resolved_value = filter_value
            resolved_filters.append(
                ResolvedMetricFilter(
                    metric_id=metric.metric_id,
                    operator=filter_spec.operator,
                    value=resolved_value,
                    value_kind=value_kind,
                    exclude_district_ids=exclude_district_ids,
                )
            )

        # The plan is returned unchanged: the metric phrase stays in
        # FilterSpec.field, and the resolved metric_ids ride out on
        # resolved_filters (#1373 Stage 2 — no "metric:<id>" field rewrite).
        return plan, resolved_filters

    async def _resolve_anchor_filter_value(
        self,
        filter_spec: FilterSpec,
        plan: QueryPlan,
        *,
        metric_id: int,
    ) -> tuple[object, frozenset[int]] | ExecutionOutcome:
        anchor = filter_spec.anchor_value
        if anchor is None:
            return filter_spec.value, frozenset()

        anchor_metric_id = metric_id
        if anchor.metric:
            anchor_metric_resolution = await self._catalog.resolve_primary_metric(
                anchor.metric,
                numeric_only=False,
                limit=_METRIC_CLARIFICATION_CANDIDATE_LIMIT,
            )
            if anchor_metric_resolution.resolved is None:
                # Surface the real candidates the catalog found instead of a
                # generic dead-end (SELECT-R4 / #1248). #1363 WS-B: harmonize the
                # clarify gate to the single >1-distinct rule that scoping.py
                # uses (``_metric_clarification_or_refusal``) — a *single*
                # distinct candidate is not recoverable by asking "which of this
                # one?", so it keeps the refusal; never clarify on zero.
                if _has_multiple_metric_candidates(anchor_metric_resolution.candidates):
                    clarification = await compose_clarify_question_async(
                        anchor.metric,
                        operation=plan.operation,
                        candidates=anchor_metric_resolution.candidates,
                        adjudicator_hint=anchor_metric_resolution.clarification_hint,
                    )
                    return ExecutionClarification(
                        clarification=clarification,
                        message=clarification.question,
                        resolution_report=_metric_resolution_report(
                            plan,
                            anchor.metric,
                            anchor_metric_resolution,
                            entity_type="metric",
                        ),
                    )
                return ExecutionRefusal(
                    message=(
                        "I could not resolve the anchor metric "
                        f"'{anchor.metric}' to a governed Compass metric for "
                        "this request."
                    ),
                    resolution_report=_metric_resolution_report(
                        plan,
                        anchor.metric,
                        anchor_metric_resolution,
                        entity_type="metric",
                    ),
                )
            anchor_metric_id = anchor_metric_resolution.resolved.metric_id

        # Recoverable referent resolution: prefer the typed anchor.state (WS-D /
        # #1373), fall back to splitting a bundled state qualifier ("Portland
        # ME"), merge requested states, accept a single confident candidate, and
        # clarify with real candidates on genuine ambiguity (#1192 / #739)
        # instead of the generic dead-end.
        referent = await resolve_referent_district(
            self._catalog,
            anchor.district,
            plan,
            extra_states={anchor.state} if anchor.state else None,
        )
        if isinstance(referent, ExecutionClarification):
            return referent
        anchor_district = referent
        rows = await self._fetch_metric_rows_for_year(
            metric_id=anchor_metric_id,
            academic_year=self._current_academic_year,
            district_ids={anchor_district.district_id},
        )
        anchor_row = next(
            (
                row
                for row in rows
                if row.district_id == anchor_district.district_id
            ),
            None,
        )
        if anchor_row is None or anchor_row.value is None:
            return ExecutionRefusal(
                message=(
                    "I could not find a reviewed anchor value for deterministic "
                    "filter execution yet."
                ),
            )
        exclude_ids = (
            frozenset({anchor_district.district_id})
            if anchor.exclude_anchor
            else frozenset()
        )
        return anchor_row.value, exclude_ids

    async def _rank_profile_rows(
        self,
        plan: QueryPlan,
        sort_step: SortStepSpec,
        *,
        limit: int | None,
    ) -> list[ProfileFieldRankRow] | ExecutionRefusal:
        from compass_backend.catalog.profile_rank_fields import UnsupportedRankField

        try:
            return await self._catalog.rank_districts_by_profile_field(
                sort_step.field,
                limit=limit,
                direction=sort_step.direction,
                states=requested_states(plan) or None,
                academic_year=self._current_academic_year,
            )
        except UnsupportedRankField as exc:
            return ExecutionRefusal(
                message=(
                    f"I cannot rank districts by {exc.field_key!r}: that field "
                    "is not in the governed profile-rank allowlist."
                ),
            )

    async def _execute_lookup(
        self,
        plan: QueryPlan,
        *,
        ordering_plan: QueryPlan | None = None,
    ) -> ExecutionOutcome:
        # Resolve metric-value filters FIRST so _resolve_profile_filter_fields
        # skips them by their metric_value classification (the field stays the
        # free-form metric phrase — #1373 Stage 2 removed the "metric:<id>"
        # rewrite).
        metric_filter_result = await self._resolve_metric_value_filters(plan)
        if isinstance(metric_filter_result, EXECUTION_OUTCOME_TYPES):
            return metric_filter_result
        plan, resolved_metric_filters = metric_filter_result

        profile_filter_result = await self._resolve_profile_filter_fields(plan)
        if isinstance(profile_filter_result, EXECUTION_OUTCOME_TYPES):
            return profile_filter_result
        plan, profile_filter_field_authority = profile_filter_result
        plan, profile_filter_metric_authority = await self._strip_profile_filter_metrics(
            plan
        )
        profile_filter_authority = [
            *profile_filter_field_authority,
            *profile_filter_metric_authority,
        ]
        if ordering_plan is not None:
            ordering_plan = ordering_plan.model_copy(update={"metrics": plan.metrics})
        if _has_unresolved_filter_metrics(plan):
            return ExecutionRefusal(message=_UNSUPPORTED_SHAPE_MESSAGE)

        if not lookup_shape_is_supported(plan):
            return ExecutionRefusal(message=_UNSUPPORTED_SHAPE_MESSAGE)

        selection = await resolve_selection(
            plan,
            self._catalog,
            allow_unresolved=True,
            academic_year=self._current_academic_year,
            largest_limit=self._default_limit,
        )
        if isinstance(selection, EXECUTION_OUTCOME_TYPES):
            return selection

        selection = await self._materialize_scope_selection(selection)

        metric_group_result = await self._resolve_plan_metric_groups(
            plan,
            numeric_only=_lookup_chart_requires_numeric_metric_resolution(plan),
        )
        if isinstance(metric_group_result, EXECUTION_OUTCOME_TYPES):
            return metric_group_result
        metric_groups, adjacent_candidates, metric_source_notes = metric_group_result
        metrics = _flatten_metric_groups(metric_groups)

        lookup_academic_year = _lookup_academic_year(
            plan,
            current_academic_year=self._current_academic_year,
        )
        allow_recent_fallback = _lookup_allows_recent_fallback(plan)
        resolved_district_ids = {
            district.district_id for district in selection.districts
        }
        rows_by_metric_id = {}
        for metric in metrics:
            raw_rows = await self._fetch_metric_rows_for_year(
                metric_id=metric.metric_id,
                academic_year=lookup_academic_year,
                district_ids=resolved_district_ids,
            )
            recent_rows = []
            if allow_recent_fallback:
                recent_rows = await self._repository.fetch_recent_metric_answer_rows(
                    metric_id=metric.metric_id,
                    before_academic_year=lookup_academic_year,
                    district_ids=resolved_district_ids,
                )
            rows_by_metric_id[metric.metric_id] = (metric, raw_rows, recent_rows)

        # Apply metric-value filters: keep only districts that pass ALL predicates.
        # Snapshot the pre-narrow selection and filter rows before narrowing so we
        # can compute the policy-honest prevalence tally (U1/U2, #1337/FILT-88).
        pre_narrow_selection = selection
        filter_rows_for_prevalence: list[MetricAnswerRow] = []
        if resolved_metric_filters:
            all_rows_for_filter: list[MetricAnswerRow] = []
            for f in resolved_metric_filters:
                if f.metric_id in rows_by_metric_id:
                    _, metric_rows, _ = rows_by_metric_id[f.metric_id]
                    all_rows_for_filter.extend(metric_rows)
                else:
                    # Filter metric not in display metrics — fetch separately.
                    extra_rows = await self._fetch_metric_rows_for_year(
                        metric_id=f.metric_id,
                        academic_year=lookup_academic_year,
                        district_ids=resolved_district_ids,
                    )
                    all_rows_for_filter.extend(extra_rows)
            # Capture filter rows for single-filter prevalence (scoped to the
            # one filter's metric_id so the denominator is unambiguous).
            if len(resolved_metric_filters) == 1:
                filter_rows_for_prevalence = [
                    r
                    for r in all_rows_for_filter
                    if r.metric_id == resolved_metric_filters[0].metric_id
                ]
            selection = _apply_metric_value_filters_to_selection(
                selection, all_rows_for_filter, resolved_metric_filters
            )

        reviewed_district_ids = await self._reviewed_district_ids(
            selection.districts,
            academic_year=lookup_academic_year,
        )

        # #1613 U5: surface the deferred metric gap on the selection. The
        # finalizer (U4) stripped any requested metric phrase with no catalog
        # metric from plan.metrics so this lookup could answer the resolvable
        # subset; carry those phrases onto the selection so the renderer (U6)
        # discloses the gap instead of silently dropping it. plan.deferred_metric_gap
        # survives the intervening model_copy()s (each preserves unspecified fields).
        if plan.deferred_metric_gap:
            selection = selection.model_copy(
                update={"unresolved_metrics": list(plan.deferred_metric_gap)}
            )

        result_plan = ordering_plan or plan
        if result_plan.requires_all_metrics and len(metric_groups) >= 2:
            criteria_metric_rows = [
                (
                    criterion_id,
                    label,
                    [rows_by_metric_id[metric.metric_id] for metric in group_metrics],
                )
                for criterion_id, label, group_metrics in metric_groups
            ]
            result = build_metric_intersection_lookup_result(
                result_plan,
                criteria_metric_rows,
                selection=selection,
                reviewed_district_ids=reviewed_district_ids,
                academic_year=lookup_academic_year,
            )
        else:
            metric_rows = [rows_by_metric_id[metric.metric_id] for metric in metrics]
            result = build_metric_lookup_result(
                result_plan,
                metric_rows,
                selection=selection,
                reviewed_district_ids=reviewed_district_ids,
                academic_year=lookup_academic_year,
            )
        # Attach the pre-narrow prevalence tally for single-filter filtered lookups
        # (#1337 / FILT-88, U1+U2). Only when exactly one filter was applied —
        # multiple filters make the denominator ambiguous (which metric's covered
        # count?). model_copy does NOT re-run populate_artifact_surfaces, so the
        # supplied filter_prevalence value is preserved as-is.
        if (
            filter_rows_for_prevalence
            and isinstance(result, MetricLookupResult)
        ):
            prevalence = build_filter_prevalence_summary(
                filter_rows_for_prevalence,
                pre_narrow_selection=pre_narrow_selection,
                post_narrow_district_count=len(selection.districts),
            )
            # None = the matched>denominator invariant was violated (anchor-equality
            # on an NA value); suppress the incoherent prevalence lead rather than
            # 500 on the percent<=100 bound. The answer still lists the matches.
            if prevalence is not None:
                result = result.model_copy(
                    update={"filter_prevalence": prevalence}
                )
        # Emit methodology code for any metric with an explicit degree_lane.
        lane_applied = next(
            (ms.degree_lane for ms in plan.metrics if ms.degree_lane is not None), None
        )
        if lane_applied is not None:
            result = result.model_copy(
                update={
                    "methodology_codes": [
                        *result.methodology_codes,
                        MethodologyRef(
                            code="degree_lane_applied",
                            metadata={"degree_lane": lane_applied},
                        ),
                    ]
                }
            )
        filter_methodology = _metric_filter_methodology_codes(resolved_metric_filters)
        if filter_methodology:
            result = result.model_copy(
                update={
                    "methodology_codes": [
                        *result.methodology_codes,
                        *filter_methodology,
                    ]
                }
            )
        if metric_source_notes:
            result = result.model_copy(
                update={"source_notes": [*result.source_notes, *metric_source_notes]}
            )
        return ExecutionSuccess(
            result=result,
            authority=_validation_authority(
                result_plan,
                selection,
                metrics,
                profile_fields=profile_filter_authority,
            ),
            message=result.order_statement,
            adjacent_candidates=adjacent_candidates,
        )

    async def _execute_trend(self, plan: QueryPlan) -> ExecutionOutcome:
        # Resolve metric-value filters FIRST (rewrite field tokens so
        # _resolve_profile_filter_fields skips them). Trend cannot enforce
        # threshold filters safely without a typed year basis, so it refuses
        # before rendering instead of returning a filtered-looking result.
        metric_filter_result = await self._resolve_metric_value_filters(plan)
        if isinstance(metric_filter_result, EXECUTION_OUTCOME_TYPES):
            return metric_filter_result
        plan, resolved_metric_filters_trend = metric_filter_result
        if resolved_metric_filters_trend:
            return ExecutionRefusal(
                message=(
                    "I cannot apply metric-value threshold filters to trend "
                    "results yet because the filter needs a specific year basis. "
                    "Please ask for a lookup or ranking, or specify the year the "
                    "threshold should use."
                )
            )

        profile_filter_result = await self._resolve_profile_filter_fields(plan)
        if isinstance(profile_filter_result, EXECUTION_OUTCOME_TYPES):
            return profile_filter_result
        plan, profile_filter_field_authority = profile_filter_result
        plan, profile_filter_metric_authority = await self._strip_profile_filter_metrics(
            plan
        )
        profile_filter_authority = [
            *profile_filter_field_authority,
            *profile_filter_metric_authority,
        ]
        if _has_unresolved_filter_metrics(plan):
            return ExecutionRefusal(message=_UNSUPPORTED_SHAPE_MESSAGE)

        if not trend_shape_is_supported(plan):
            return ExecutionRefusal(message=_UNSUPPORTED_SHAPE_MESSAGE)

        selection = await resolve_selection(
            plan,
            self._catalog,
            allow_unresolved=True,
            academic_year=self._current_academic_year,
            largest_limit=self._default_limit,
        )
        if isinstance(selection, EXECUTION_OUTCOME_TYPES):
            return selection

        selection = await self._materialize_scope_selection(selection)
        metrics_result = await self._resolve_plan_metrics(plan, numeric_only=False)
        if isinstance(metrics_result, EXECUTION_OUTCOME_TYPES):
            return metrics_result
        metrics, metric_source_notes, metric_alternates = metrics_result
        if len(metrics) != 1:
            return ExecutionRefusal(
                message=(
                    "Longitudinal execution currently supports one resolved metric "
                    "at a time. Please narrow the metric."
                ),
            )

        years = trend_academic_years(
            plan,
            current_academic_year=self._current_academic_year,
        )
        district_cell_count = len(selection.districts) + len(selection.unresolved_districts)
        cells = trend_cell_count(
            district_count=district_cell_count,
            metric_count=len(metrics),
            year_count=len(years),
        )
        if cells > MAX_TREND_CELLS:
            return ExecutionRefusal(
                message=(
                    "That trend would require too many district-year cells for the "
                    f"current deterministic row budget ({cells} requested, "
                    f"{MAX_TREND_CELLS} allowed). Please narrow the districts, "
                    "metric, or year window."
                ),
            )

        metric = metrics[0]
        resolved_district_ids = {
            district.district_id for district in selection.districts
        }
        rows = await self._repository.fetch_metric_answer_rows_for_years(
            metric_id=metric.metric_id,
            academic_years=years,
            district_ids=resolved_district_ids,
        )
        reviewed_district_ids_by_year = {
            year: await self._repository.fetch_reviewed_district_ids(
                academic_year=year,
                district_ids=resolved_district_ids,
            )
            for year in years
        }
        result = build_metric_trend_result(
            plan,
            metric,
            rows,
            selection=selection,
            academic_years=years,
            reviewed_district_ids_by_year=reviewed_district_ids_by_year,
        )
        # Emit methodology code when user's explicit degree_lane shaped metric resolution.
        if plan.metrics and plan.metrics[0].degree_lane is not None:
            result = result.model_copy(
                update={
                    "methodology_codes": [
                        *result.methodology_codes,
                        MethodologyRef(
                            code="degree_lane_applied",
                            metadata={"degree_lane": plan.metrics[0].degree_lane},
                        ),
                    ]
                }
            )
        best_guess_codes = _best_guess_methodology_codes(metrics, metric_alternates)
        if best_guess_codes:
            result = result.model_copy(
                update={
                    "methodology_codes": [*result.methodology_codes, *best_guess_codes]
                }
            )
        if metric_source_notes:
            result = result.model_copy(
                update={"source_notes": [*result.source_notes, *metric_source_notes]}
            )
        return ExecutionSuccess(
            result=result,
            authority=_validation_authority(
                plan,
                selection,
                metrics,
                profile_fields=profile_filter_authority,
            ),
            message=result.order_statement,
        )

    async def _execute_profile_lookup(self, plan: QueryPlan) -> ExecutionOutcome:
        if not profile_lookup_shape_is_supported(plan):
            return ExecutionRefusal(message=_UNSUPPORTED_SHAPE_MESSAGE)

        # Route district names through the AI-aware CatalogResolver (exact-match
        # → alias-table → AI adjudication). Same pattern as _execute_lookup.
        # `allow_unresolved=True` lets NCES-only names fall through via
        # `selection.unresolved_districts` so the downstream lookup can still
        # return profile data for districts that exist in compass.nces_districts
        # but not compass.district_profiles. Closes #738: a user phrase like
        # "philadelphia school district" canonicalizes to "School District of
        # Philadelphia" before the keyword-normalized NCES lookup, which would
        # otherwise drop it via suffix-stripping normalization.
        selection = await resolve_selection(
            plan,
            self._catalog,
            allow_unresolved=True,
            academic_year=self._current_academic_year,
            largest_limit=self._default_limit,
        )
        if isinstance(selection, EXECUTION_OUTCOME_TYPES):
            return selection

        canonical_names = [d.district_name for d in selection.districts]
        fallback_names = list(selection.unresolved_districts or [])
        rows = []
        fields: list[NCESFieldCandidate] = []
        profile_field_authority: list[tuple[str, NCESFieldCandidate, str]] = []
        for profile_field in plan.profile_fields:
            field_phrase = profile_field.name
            resolution = await self._catalog.resolve_profile_field_authority(
                field_phrase
            )
            if not resolution.ok or resolution.resolved is None:
                return _profile_field_dead_end(plan, field_phrase, resolution)
            field = resolution.resolved
            resolution_method = resolution.resolution_method
            fields.append(field)
            profile_field_authority.append(
                (field_phrase, field, resolution_method)
            )
            field_rows = await self._catalog.lookup_nces_profile_values(
                canonical_names + fallback_names,
                field_key=field.field_key,
                states=requested_states(plan) or None,
                academic_year=self._current_academic_year,
            )
            rows.extend(field_rows)
        if not rows:
            return await self._handle_profile_lookup_empty_rows(selection)

        result = build_profile_lookup_result(
            plan,
            fields,
            rows,
            academic_year=self._current_academic_year,
            resolved_selection=selection,
        )
        return ExecutionSuccess(
            result=result,
            authority=_validation_authority(
                plan,
                result.selection,
                [],
                profile_fields=profile_field_authority,
            ),
            message=result.order_statement,
        )

    async def _handle_profile_lookup_empty_rows(
        self,
        selection: ResultSelection,
    ) -> ExecutionClarification | ExecutionRefusal:
        """Split the zero-row profile-lookup branch by *why* it is empty (#1758).

        Two distinct cases used to share one opaque refusal:

        * **Out-of-universe district** — the name resolved to nothing covered
          and fell through as an ``unresolved_district`` (e.g. a fictional or
          non-Pathfinder name). Here we route to the existing grounded
          uncovered-district clarification ("…none are currently in the District
          Policy Pathfinder… if you meant a covered district, let me know which
          one"), the same honest message the disambiguation path already uses.
          The candidate bullets come from a grounded NCES search — never
          invented — and when NCES has nothing either (the reported "Bravo
          School District" case), the question degrades to a truthful,
          candidate-free "I couldn't match that to a district in the District
          Policy Pathfinder. Which district did you mean?".
        * **Real covered district with a genuine data gap** — at least one
          district resolved (``selection.districts`` non-empty) but the requested
          profile field returned no rows. That keeps the original "no profile
          data yet" message; it is a true data gap, not an unknown district.
        """

        if not selection.districts and selection.unresolved_districts:
            matches: list[NcesDistrictMatch] = []
            seen_leaids: set[str] = set()
            for name in selection.unresolved_districts:
                for match in await self._catalog.search_nces_districts(name):
                    if match.leaid in seen_leaids:
                        continue
                    seen_leaids.add(match.leaid)
                    matches.append(match)
            uncovered = [match for match in matches if not match.covered]
            labels = grounded_uncovered_district_labels(uncovered)
            question = grounded_uncovered_district_question(uncovered)
            clarification = ClarificationRequest(
                question=question,
                missing_fields=["district"],
                candidates=labels,
            )
            return ExecutionClarification(
                clarification=clarification,
                message=question,
            )

        return ExecutionRefusal(
            message=(
                "I could not resolve sourced NCES/profile values for those "
                "districts yet."
            ),
        )

    async def _resolve_peer_anchor(
        self,
        anchor_text: str,
        plan: QueryPlan,
    ) -> DistrictCandidate | ExecutionClarification | None:
        """Pre-resolve a peer/similarity anchor with recoverable referent rules.

        #1363: split a bundled state qualifier ("Portland ME"), merge requested
        states, accept a confident single candidate, and clarify with the real
        candidates on genuine multi-candidate ambiguity (#1192 / #739) — instead
        of letting ``resolve_peer_set`` dead-end to ``None`` and emitting a
        generic refusal.

        Returns:
        - a :class:`DistrictCandidate` for a confident/qualified single match,
        - an :class:`ExecutionClarification` ONLY when there are *real* candidate
          labels to disambiguate (so we never ask "which one?" with an empty
          list), or
        - ``None`` for a genuine zero-candidate miss, letting the caller keep its
          existing deterministic peer-set refusal (preserves the unresolvable
          anchor contract).
        """

        anchor = await resolve_referent_district(self._catalog, anchor_text, plan)
        if isinstance(anchor, ExecutionClarification):
            if anchor.clarification.candidates:
                return anchor
            return None
        return anchor

    async def _execute_peer_comparison(self, plan: QueryPlan) -> ExecutionOutcome:
        # Resolve metric-value filters FIRST (rewrite field tokens so the
        # profile-field path doesn't try to resolve them). Peer comparison
        # applies those filters to peer candidates before rendering.
        metric_filter_result = await self._resolve_metric_value_filters(plan)
        if isinstance(metric_filter_result, EXECUTION_OUTCOME_TYPES):
            return metric_filter_result
        plan, resolved_metric_filters_peer = metric_filter_result

        if not peer_comparison_shape_is_supported(plan):
            return ExecutionRefusal(message=_UNSUPPORTED_SHAPE_MESSAGE)

        peer_limit = (
            min(_PEER_COMPARISON_DEFAULT_LIMIT, self._default_limit)
            if plan.limit is None or plan.limit.kind == "all"
            else plan.limit.count
        )
        metric_result = await self._resolve_plan_metrics(plan, numeric_only=False)
        if isinstance(metric_result, EXECUTION_OUTCOME_TYPES):
            return metric_result
        metrics, metric_source_notes, metric_alternates = metric_result

        # Recoverable anchor resolution (#1363): clarify on genuine ambiguity,
        # resolve a state-qualified single, else fall through to the
        # deterministic peer-set refusal (zero-candidate miss).
        anchor_text = plan.selection.districts[0]
        anchor = await self._resolve_peer_anchor(anchor_text, plan)
        if isinstance(anchor, ExecutionClarification):
            return anchor
        anchor_name = anchor.district_name if anchor is not None else anchor_text

        peer_set = await self._catalog.resolve_peer_set(
            anchor_name,
            states=_peer_anchor_states(anchor_text, plan),
            limit=_candidate_pool_limit(peer_limit),
            academic_year=self._current_academic_year,
            feature_set=plan.peer_overrides.feature_set if plan.peer_overrides else "all",
            exclude_states=plan.peer_overrides.exclude_states if plan.peer_overrides else None,
        )
        if peer_set is None:
            return ExecutionRefusal(
                message=(
                    "I could not resolve a deterministic covered NCES peer set "
                    "for that anchor district yet."
                ),
                resolution_report=report_from_entities(
                    question=plan.question,
                    operation=plan.operation,
                    entities=[
                        CatalogResolutionEntity(
                            input_phrase=anchor_name,
                            entity_type="peer_method",
                            status="unresolved",
                            resolution_method="unresolved",
                            provenance="CatalogResolver.resolve_peer_set",
                            message=(
                                "No deterministic covered NCES peer set resolved "
                                "for this anchor district."
                            ),
                        )
                    ],
                ),
            )

        selected_district_ids = {
            peer_set.anchor.district_id,
            *(peer.district_id for peer in peer_set.peers),
        }
        metric_rows = []
        for metric in metrics:
            raw_rows = await self._repository.fetch_metric_answer_rows(
                metric_id=metric.metric_id,
                academic_year=self._current_academic_year,
            )
            recent_rows = await self._repository.fetch_recent_metric_answer_rows(
                metric_id=metric.metric_id,
                before_academic_year=self._current_academic_year,
                district_ids=selected_district_ids,
            )
            metric_rows.append((metric, raw_rows, recent_rows))

        if resolved_metric_filters_peer:
            filter_rows: list[MetricAnswerRow] = []
            for metric_filter in resolved_metric_filters_peer:
                rows_for_filter = await self._repository.fetch_metric_answer_rows(
                    metric_id=metric_filter.metric_id,
                    academic_year=self._current_academic_year,
                )
                filter_rows.extend(
                    row
                    for row in rows_for_filter
                    if row.district_id in selected_district_ids
                )
            peer_set = _filter_peer_set_by_metric_value_filters(
                peer_set,
                filter_rows,
                resolved_metric_filters_peer,
            )
            if not peer_set.peers:
                return ExecutionRefusal(
                    message=(
                        "No deterministic peer districts matched the requested "
                        "metric-value filter, so I cannot produce a faithful "
                        "peer comparison for that constraint."
                    )
                )

        selected_districts = _peer_selected_districts(peer_set)
        reviewed_district_ids = await self._reviewed_district_ids(selected_districts)
        screen = _screen_peer_set_for_comparison_ready_rows(
            plan,
            peer_set,
            metric_rows,
            reviewed_district_ids=reviewed_district_ids,
            peer_limit=peer_limit,
        )
        peer_set = screen.peer_set
        result = build_peer_comparison_result(
            plan,
            metric_rows,
            peer_set=peer_set,
            reviewed_district_ids=reviewed_district_ids,
            academic_year=self._current_academic_year,
            peer_overrides=plan.peer_overrides,
        )
        result = result.model_copy(
            update={
                "excluded_count": screen.excluded_unavailable_count,
                "methodology_codes": [
                    *result.methodology_codes,
                    MethodologyRef(
                        code="peer_metric_coverage_screen_applied",
                        metadata={
                            "candidate_count": str(screen.candidate_peer_count),
                            "candidate_pool_limit": str(
                                _candidate_pool_limit(peer_limit)
                            ),
                            "excluded_unavailable_count": str(
                                screen.excluded_unavailable_count
                            ),
                            "final_peer_count": str(len(peer_set.peers)),
                            "same_state_cap": str(_PEER_DEFAULT_SAME_STATE_CAP),
                            "diversity_score_delta": str(
                                _PEER_DEFAULT_DIVERSITY_SCORE_DELTA
                            ),
                            "same_state_cap_applied": str(
                                screen.same_state_cap_applied
                            ).lower(),
                            "diversity_replacement_count": str(
                                screen.diversity_replacement_count
                            ),
                        },
                    ),
                ],
            }
        )
        if resolved_metric_filters_peer:
            result = result.model_copy(
                update={
                    "methodology_codes": [
                        *result.methodology_codes,
                        MethodologyRef(code="metric_value_filter_applied"),
                    ]
                }
            )
        # Emit methodology code when user's explicit degree_lane shaped metric resolution.
        if plan.metrics and plan.metrics[0].degree_lane is not None:
            result = result.model_copy(
                update={
                    "methodology_codes": [
                        *result.methodology_codes,
                        MethodologyRef(
                            code="degree_lane_applied",
                            metadata={"degree_lane": plan.metrics[0].degree_lane},
                        ),
                    ]
                }
            )
        best_guess_codes = _best_guess_methodology_codes(metrics, metric_alternates)
        if best_guess_codes:
            result = result.model_copy(
                update={
                    "methodology_codes": [*result.methodology_codes, *best_guess_codes]
                }
            )
        if metric_source_notes:
            result = result.model_copy(
                update={"source_notes": [*result.source_notes, *metric_source_notes]}
            )
        return ExecutionSuccess(
            result=result,
            authority=_validation_authority(plan, result.selection, metrics),
            message=result.order_statement,
        )

    async def _execute_similarity(self, plan: QueryPlan) -> ExecutionOutcome:
        """Execute the similarity peer-set discovery operation.

        Step 1: Resolve any metric-value filters (PR 2A reuse).
        Step 2: Validate the shape predicate.
        Step 3: Resolve the peer set with feature_set + exclude_states overrides.
        Step 4: Apply metric-value filters post-peer-set (composition with PR 2A).
        Step 5: Build and return a PeerComparisonResult (peers-only rows).
        """

        assert plan.similarity is not None  # guarded by shape check before this call

        # Step 1: Resolve metric-value filters (may rewrite filter field tokens).
        metric_filter_result = await self._resolve_metric_value_filters(plan)
        if isinstance(metric_filter_result, EXECUTION_OUTCOME_TYPES):
            return metric_filter_result
        plan, resolved_metric_filters = metric_filter_result

        if not similarity_shape_is_supported(plan):
            return ExecutionRefusal(message=_UNSUPPORTED_SHAPE_MESSAGE)

        peer_limit = plan.similarity.limit

        # Recoverable anchor resolution (#1363): same as peer_comparison —
        # clarify on genuine ambiguity, resolve a state-qualified single, else
        # fall through to the deterministic peer-set refusal (zero-candidate).
        anchor_text = plan.similarity.anchor_name
        anchor = await self._resolve_peer_anchor(anchor_text, plan)
        if isinstance(anchor, ExecutionClarification):
            return anchor
        anchor_name = anchor.district_name if anchor is not None else anchor_text

        # Step 3: Resolve the NCES peer set with feature_set + exclude_states.
        peer_set = await self._catalog.resolve_peer_set(
            anchor_name,
            states=_peer_anchor_states(anchor_text, plan),
            limit=peer_limit,
            academic_year=self._current_academic_year,
            feature_set=plan.similarity.feature_set,
            exclude_states=list(plan.similarity.exclude_states) or None,
        )
        if peer_set is None:
            return ExecutionRefusal(
                message=(
                    "I could not resolve a deterministic covered NCES peer set "
                    "for that anchor district yet."
                ),
                resolution_report=report_from_entities(
                    question=plan.question,
                    operation=plan.operation,
                    entities=[
                        CatalogResolutionEntity(
                            input_phrase=anchor_name,
                            entity_type="peer_method",
                            status="unresolved",
                            resolution_method="unresolved",
                            provenance="CatalogResolver.resolve_peer_set",
                            message=(
                                "No deterministic covered NCES peer set resolved "
                                "for this anchor district."
                            ),
                        )
                    ],
                ),
            )

        # Step 4: Apply metric-value filters post-peer-set (PR 2A composition).
        # If filters are present, fetch filter metric rows for peer-set districts.
        pre_filter_peer_count: int | None = None
        if resolved_metric_filters:
            pre_filter_peer_count = len(peer_set.peers)
            peer_district_ids: set[int] = {
                peer_set.anchor.district_id,
                *(peer.district_id for peer in peer_set.peers),
            }
            # Build a ResultSelection covering all peer districts for filter application.
            peer_selection = ResultSelection(
                scope="named_districts",
                districts=[
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
                ],
                states=plan.selection.states,
            )
            # Fetch rows for all resolved filter metrics.
            all_filter_rows: list[MetricAnswerRow] = []
            for f in resolved_metric_filters:
                filter_rows = await self._repository.fetch_metric_answer_rows(
                    metric_id=f.metric_id,
                    academic_year=self._current_academic_year,
                )
                all_filter_rows.extend(
                    r for r in filter_rows if r.district_id in peer_district_ids
                )
            filtered_selection = _apply_metric_value_filters_to_selection(
                peer_selection,
                all_filter_rows,
                resolved_metric_filters,
            )
            # Rebuild peer_set from filtered selection (exclude anchor from filter check).
            passing_peer_ids = {d.district_id for d in filtered_selection.districts}
            filtered_peers = [
                peer for peer in peer_set.peers
                if peer.district_id in passing_peer_ids
            ]
            peer_set = PeerSetResolution(
                anchor=peer_set.anchor,
                peers=filtered_peers,
                policy_version=peer_set.policy_version,
            )

        # Step 5: Build result artifact.
        result = build_similarity_result(
            plan,
            peer_set=peer_set,
            pre_filter_peer_count=pre_filter_peer_count,
        )
        # Emit methodology code when user's explicit degree_lane shaped metric resolution.
        # Similarity does not require metrics, so guard against empty plan.metrics.
        if plan.metrics and plan.metrics[0].degree_lane is not None:
            result = result.model_copy(
                update={
                    "methodology_codes": [
                        *result.methodology_codes,
                        MethodologyRef(
                            code="degree_lane_applied",
                            metadata={"degree_lane": plan.metrics[0].degree_lane},
                        ),
                    ]
                }
            )
        return ExecutionSuccess(
            result=result,
            authority=_validation_authority(plan, result.selection, []),
            message=result.order_statement,
        )

    async def _execute_count(self, plan: QueryPlan) -> ExecutionOutcome:
        # Resolve metric-value filters FIRST. Their fields stay the free-form
        # metric phrase (#1373 Stage 2 removed the "metric:<id>" rewrite);
        # _resolve_profile_filter_fields skips them by their metric_value
        # classification, and build_metric_count_result scopes each threshold to
        # its metric_id via the resolved-filter list below. The legacy "value"
        # token (classified None) is still matched against every metric's rows by
        # _row_matches_count_filters.
        metric_filter_result = await self._resolve_metric_value_filters(plan)
        if isinstance(metric_filter_result, EXECUTION_OUTCOME_TYPES):
            return metric_filter_result
        plan, resolved_metric_filters = metric_filter_result

        profile_filter_result = await self._resolve_profile_filter_fields(plan)
        if isinstance(profile_filter_result, EXECUTION_OUTCOME_TYPES):
            return profile_filter_result
        plan, profile_filter_authority = profile_filter_result

        if not count_shape_is_supported(plan):
            return ExecutionRefusal(message=_UNSUPPORTED_SHAPE_MESSAGE)

        selection = await resolve_selection(
            plan,
            self._catalog,
            allow_unresolved=True,
            academic_year=self._current_academic_year,
            largest_limit=self._default_limit,
        )
        if isinstance(selection, EXECUTION_OUTCOME_TYPES):
            return selection

        selection = await self._materialize_scope_selection(selection)
        if count_plan_is_covered_universe(plan):
            result = build_covered_universe_count_result(
                plan,
                selection=selection,
                academic_year=self._current_academic_year,
            )
            # Emit methodology code when user's explicit degree_lane shaped metric resolution.
            if plan.metrics and plan.metrics[0].degree_lane is not None:
                result = result.model_copy(
                    update={
                        "methodology_codes": [
                            *result.methodology_codes,
                            MethodologyRef(
                                code="degree_lane_applied",
                                metadata={"degree_lane": plan.metrics[0].degree_lane},
                            ),
                        ]
                    }
                )
            return ExecutionSuccess(
                result=result,
                authority=_validation_authority(
                    plan,
                    selection,
                    [covered_universe_metric()],
                    profile_fields=profile_filter_authority,
                ),
                message=result.order_statement,
            )

        if count_plan_is_categorical_value(plan):
            metric_result = await self._resolve_categorical_count_metrics(plan)
            if isinstance(metric_result, EXECUTION_OUTCOME_TYPES):
                return metric_result

            metric_rows = []
            for metric in metric_result:
                raw_rows = await self._repository.fetch_metric_answer_rows(
                    metric_id=metric.metric_id,
                    academic_year=self._current_academic_year,
                )
                if not metric_supports_categorical_value_count(
                    plan,
                    metric,
                    raw_rows,
                    selection=selection,
                ):
                    continue
                metric_rows.append((metric, raw_rows))

            if not metric_rows:
                return ExecutionRefusal(
                    message=(
                        "I could not resolve an approved categorical field for "
                        "deterministic count execution yet."
                    ),
                )

            result = build_categorical_value_count_result(
                plan,
                metric_rows,
                selection=selection,
                academic_year=self._current_academic_year,
            )
            # Emit methodology code when user's explicit degree_lane shaped metric resolution.
            if plan.metrics and plan.metrics[0].degree_lane is not None:
                result = result.model_copy(
                    update={
                        "methodology_codes": [
                            *result.methodology_codes,
                            MethodologyRef(
                                code="degree_lane_applied",
                                metadata={"degree_lane": plan.metrics[0].degree_lane},
                            ),
                        ]
                    }
                )
            metrics = [metric for metric, _rows in metric_rows]
            return ExecutionSuccess(
                result=result,
                authority=_validation_authority(
                    plan,
                    selection,
                    metrics,
                    profile_fields=profile_filter_authority,
                ),
                message=result.order_statement,
            )

        if count_plan_is_topic_coverage(plan):
            topic_result = await self._resolve_topic_coverage_metrics(plan)
            if isinstance(topic_result, EXECUTION_OUTCOME_TYPES):
                return topic_result
            topic_metrics, topic_label = topic_result
            topic_metric_rows = []
            for metric in topic_metrics:
                raw_rows = await self._repository.fetch_metric_answer_rows(
                    metric_id=metric.metric_id,
                    academic_year=self._current_academic_year,
                )
                topic_metric_rows.append((metric, raw_rows))
            result = build_topic_coverage_count_result(
                plan,
                topic_metric_rows,
                selection=selection,
                academic_year=self._current_academic_year,
                topic_label=topic_label,
            )
            # The output is a single synthetic aggregate row (metric_id=0, the
            # covered-universe authority) — like covered_universe_count — not a
            # per-metric answer, so it grounds against covered_universe_metric()
            # rather than the topic's 24 source metrics.
            return ExecutionSuccess(
                result=result,
                authority=_validation_authority(
                    plan,
                    selection,
                    [covered_universe_metric()],
                    profile_fields=profile_filter_authority,
                ),
                message=result.order_statement,
            )

        metrics_result = await self._resolve_plan_metrics(plan, numeric_only=False)
        if isinstance(metrics_result, EXECUTION_OUTCOME_TYPES):
            return metrics_result
        metrics, metric_source_notes, metric_alternates = metrics_result

        metric_rows = []
        for metric in metrics:
            raw_rows = await self._repository.fetch_metric_answer_rows(
                metric_id=metric.metric_id,
                academic_year=self._current_academic_year,
            )
            metric_rows.append((metric, raw_rows))

        result = build_metric_count_result(
            plan,
            metric_rows,
            selection=selection,
            academic_year=self._current_academic_year,
            resolved_metric_filters=resolved_metric_filters,
        )
        # Emit methodology code when user's explicit degree_lane shaped metric resolution.
        if plan.metrics and plan.metrics[0].degree_lane is not None:
            result = result.model_copy(
                update={
                    "methodology_codes": [
                        *result.methodology_codes,
                        MethodologyRef(
                            code="degree_lane_applied",
                            metadata={"degree_lane": plan.metrics[0].degree_lane},
                        ),
                    ]
                }
            )
        best_guess_codes = _best_guess_methodology_codes(metrics, metric_alternates)
        if best_guess_codes:
            result = result.model_copy(
                update={
                    "methodology_codes": [*result.methodology_codes, *best_guess_codes]
                }
            )
        if metric_source_notes:
            result = result.model_copy(
                update={"source_notes": [*result.source_notes, *metric_source_notes]}
            )
        return ExecutionSuccess(
            result=result,
            authority=_validation_authority(
                plan,
                selection,
                metrics,
                profile_fields=profile_filter_authority,
            ),
            message=result.order_statement,
        )
