"""Selection helpers shared by deterministic query operations."""

from __future__ import annotations

from compass_backend.artifacts import ResultSelection, SelectedDistrict
from compass_backend.catalog import (
    CatalogResolutionCandidate,
    CatalogResolutionEntity,
    CatalogResolver,
    DistrictCandidate,
)
from compass_backend.catalog.reporting import report_from_entities
from compass_backend.contracts.planning import (
    ClarificationOption,
    ClarificationRequest,
    FilterSpec,
    QueryPlan,
    SortStepSpec,
)
from compass_backend.reference import (
    expand_state_or_region_values,
    normalize_state,
    split_state_suffix,
)

from .types import (
    ExecutionClarification,
    ExecutionOutcome,
    ExecutionRefusal,
    MetricAnswerRow,
)


def split_unresolved_district_state(
    unresolved_name: str,
    *,
    selection: ResultSelection,
) -> tuple[str, str | None]:
    """Derive the state behind an out-of-universe requested name (#1514 D6).

    Order: a recognized trailing state qualifier typed in the name itself
    (split off, so the canonical sentence renders "{name}, {state} is not
    in the District Policy Pathfinder." without doubling it), else the
    selection's lone requested state, else ``None`` (stateless sentence —
    the data-honest fallback). Returns ``(display_name, state)``.

    The one home for this derivation — lookup, trend, count, and the
    renderer all import it from here so the canonical sentence gets the
    same ", ST" everywhere.
    """

    base_name, suffix_states = split_state_suffix(unresolved_name)
    if suffix_states:
        return base_name, next(iter(suffix_states))
    if len(selection.states) == 1:
        return unresolved_name, selection.states[0]
    return unresolved_name, None


async def resolve_selection(
    plan: QueryPlan,
    catalog: CatalogResolver,
    *,
    allow_unresolved: bool = False,
    academic_year: str | None = None,
    largest_limit: int = 10,
) -> ResultSelection | ExecutionOutcome:
    """Resolve planner selection text into artifact selection metadata."""

    if academic_year is None:
        # W1-06 (#844): centralized default lives in planning.temporal.
        from compass_backend.planning.temporal import default_current_academic_year

        academic_year = default_current_academic_year()
    states = requested_states(plan)
    enrollment_bounds = requested_enrollment_bounds(plan)
    if (
        enrollment_bounds is not None
        and plan.selection.scope in {"all_covered_districts", "state"}
    ):
        min_enrollment, max_enrollment = enrollment_bounds
        districts = await catalog.select_districts_by_enrollment_range(
            states=states or None,
            min_enrollment=min_enrollment,
            max_enrollment=max_enrollment,
            academic_year=academic_year,
        )
        return ResultSelection(
            scope=plan.selection.scope,
            districts=[
                SelectedDistrict(
                    district_id=district.district_id,
                    district_name=district.district_name,
                    state=district.state,
                )
                for district in districts
            ],
            states=sorted(states),
        )

    if plan.selection.scope == "all_covered_districts":
        return ResultSelection(scope="all_covered_districts", states=sorted(states))
    if plan.selection.scope == "state":
        return ResultSelection(scope="state", states=sorted(states))
    if plan.selection.scope == "largest_districts":
        limit = (
            None
            if plan.limit is not None and plan.limit.kind == "all"
            else plan.limit.count if plan.limit is not None else largest_limit
        )
        districts = await catalog.select_largest_districts(
            states=states or None,
            limit=limit,
            academic_year=academic_year,
        )
        if not districts:
            scope_label = (
                f" in {', '.join(sorted(states))}" if states else ""
            )
            return ExecutionRefusal(
                message=(
                    "I could not resolve largest covered districts"
                    f"{scope_label}. Try naming a state, region, or district group."
                ),
            )
        return ResultSelection(
            scope="largest_districts",
            districts=[
                SelectedDistrict(
                    district_id=district.district_id,
                    district_name=district.district_name,
                    state=district.state,
                )
                for district in districts
            ],
            states=sorted(states),
        )

    resolution = await catalog.resolve_districts(
        plan.selection.districts,
        states=states or None,
    )
    if resolution.unresolved and not allow_unresolved:
        entities = [
            CatalogResolutionEntity(
                input_phrase=phrase,
                entity_type="district",
                status="unresolved",
                resolution_method="unresolved",
                provenance="CatalogResolver.resolve_districts",
                message="No covered Compass district resolved for this phrase.",
            )
            for phrase in resolution.unresolved
        ]
        return ExecutionRefusal(
            message=(
                "I could not resolve these covered districts for deterministic "
                f"execution: {', '.join(resolution.unresolved)}."
            ),
            resolution_report=report_from_entities(
                question=plan.question,
                operation=plan.operation,
                entities=entities,
            ),
        )
    if resolution.ambiguous:
        ambiguous_names = ", ".join(sorted(resolution.ambiguous))
        ambiguous_candidates = [
            candidate
            for candidates in resolution.ambiguous.values()
            for candidate in candidates
        ]
        candidate_labels = [
            _district_candidate_label(candidate) for candidate in ambiguous_candidates
        ]
        clarification = ClarificationRequest(
            question=(
                f'I found more than one covered district matching "{ambiguous_names}". '
                "Which district do you mean?"
            ),
            missing_fields=["district"],
            candidates=candidate_labels,
            # #1348: structured, clickable counterparts to the prose labels —
            # same grounded DistrictCandidates, carrying each district_id so a
            # click resumes deterministically.
            candidate_options=[
                _district_clarification_option(candidate)
                for candidate in ambiguous_candidates
            ],
        )
        entities = [
            CatalogResolutionEntity(
                input_phrase=phrase,
                entity_type="district",
                status="ambiguous",
                resolution_method="catalog_search",
                candidates=[
                    CatalogResolutionCandidate(
                        candidate_id=str(candidate.district_id),
                        label=(
                            f"{candidate.district_name}, {candidate.state}"
                            if candidate.state
                            else candidate.district_name
                        ),
                        entity_type="district",
                        metadata={"state": candidate.state},
                    )
                    for candidate in candidates
                ],
                provenance="CatalogResolver.resolve_districts",
                message="Multiple covered Compass districts matched this phrase.",
            )
            for phrase, candidates in resolution.ambiguous.items()
        ]
        return ExecutionClarification(
            clarification=clarification,
            message=clarification.question,
            resolution_report=report_from_entities(
                question=plan.question,
                operation=plan.operation,
                entities=entities,
            ),
        )

    return ResultSelection(
        scope="named_districts",
        districts=[
            SelectedDistrict(
                district_id=district.district_id,
                district_name=district.district_name,
                state=district.state,
            )
            for district in resolution.resolved
        ],
        unresolved_districts=resolution.unresolved,
        states=sorted(states),
    )


def _district_candidate_label(candidate: DistrictCandidate) -> str:
    """Return a user-facing district option label."""

    if candidate.state:
        return f"{candidate.district_name}, {candidate.state}"
    return candidate.district_name


def _district_clarification_option(candidate: DistrictCandidate) -> ClarificationOption:
    """Build a grounded, clickable option from a covered district candidate.

    Born-from-data (#1348): ``value`` is the real ``district_id``, ``label``
    reuses the shared district label, and ``detail`` is an optional secondary
    line drawn straight from the candidate (enrollment, city) — never invented.
    """

    detail_parts: list[str] = []
    if candidate.enrollment is not None:
        detail_parts.append(f"{candidate.enrollment:,} students")
    if candidate.city:
        detail_parts.append(candidate.city)
    return ClarificationOption(
        value=str(candidate.district_id),
        label=_district_candidate_label(candidate),
        detail=" · ".join(detail_parts) or None,
    )


def select_rows(
    plan: QueryPlan,
    rows: list[MetricAnswerRow],
    selection: ResultSelection,
) -> list[MetricAnswerRow]:
    """Filter answer rows to the resolved selection."""

    if selection.districts:
        district_ids = {district.district_id for district in selection.districts}
        rows = [row for row in rows if row.district_id in district_ids]

    states = requested_states(plan)
    if not states:
        return rows
    return [row for row in rows if normalize_state(row.state) in states]


def selection_filters_are_supported(plan: QueryPlan) -> bool:
    """Return whether filters are limited to supported selection filters.

    Supported filter kinds:
    - state-scoped filters (field="state")
    - enrollment bounds (field="enrollment")
    - metric-value filters (any other field — resolved at executor time via
      CatalogResolver; see _filter_is_metric_value)

    Any combination of the above is accepted. Unsupported: operators or
    field patterns that don't fit any of the three kinds.
    """

    non_metric_filter_states = _requested_filter_states_ignoring_metric_value(plan)
    if non_metric_filter_states is None:
        return False
    # All non-metric-value filters are valid (state or enrollment).
    # Metric-value filters are unconditionally accepted here; executor
    # resolution will reject unresolvable fields at runtime.
    return True


_STRUCTURAL_FILTER_FIELDS = frozenset(
    {
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
)
"""Field names that are structural artifact/selection fields, not metric phrases.

Any ``FilterSpec.field`` in this set is NOT a metric-value filter.  Fields
outside this set are treated as metric-value filter candidates resolved at
executor time (the phrase stays free-form even after resolution — #1373
Stage 2 removed the ``"metric:<id>"`` field rewrite).
"""

_NUMERIC_COMPARATOR_OPERATORS = frozenset(
    {
        "equals",
        "not_equals",
        "greater_than",
        "greater_than_or_equal",
        "less_than",
        "less_than_or_equal",
    }
)
"""Operators that are valid for numeric metric-value threshold comparison."""

_CATEGORICAL_TEXT_OPERATORS = frozenset(
    {
        "contains",
        "in",
        "not_in",
    }
)
"""Operators for categorical (multi-valued text) metric-value filters.

The eligibility-style metrics (e.g. "Who is eligible for paid parental
leave?") store a comma-joined cell of categories.  ``contains``/``in``/
``not_in`` match by category token rather than numeric comparison (issue
#1339).  Kept distinct from the numeric set so only numeric comparators
force ``numeric_only`` catalog resolution.
"""

_METRIC_VALUE_FILTER_OPERATORS = (
    _NUMERIC_COMPARATOR_OPERATORS | _CATEGORICAL_TEXT_OPERATORS
)
"""Every operator that targets a metric value (numeric or categorical)."""


def _filter_is_metric_value(filter_spec: FilterSpec) -> bool:
    """Return True when this filter targets a metric value (not state or enrollment).

    True for a metric phrase (e.g. "teacher workdays") under a metric-value
    operator, whether or not the executor has already resolved it — the phrase
    stays free-form in ``field`` either way (#1373 Stage 2 removed the
    ``"metric:<id>"`` field rewrite, so resolved filters still classify here and
    ``_resolve_profile_filter_fields`` keeps skipping them by this classification).

    The decision now lives in :func:`execution.filters.filter_kind` (#1373) —
    this predicate is the thin "is the kind ``metric_value``?" view of it, kept
    so existing call sites read unchanged. The metric-value rule (non-structural
    field under a metric-value operator; ``is_missing``/``is_not_missing`` and
    the legacy ``"value"`` token are not metric-value) is defined once there.

    Imported lazily to avoid an import cycle: ``filters`` imports the derivation
    primitives (``_STRUCTURAL_FILTER_FIELDS`` / ``_METRIC_VALUE_FILTER_OPERATORS``)
    from this module.
    """
    from .filters import filter_kind

    return filter_kind(filter_spec) == "metric_value"


def requested_metric_filters(plan: QueryPlan) -> list[FilterSpec]:
    """Return filters whose field phrase targets a metric value (not state or enrollment).

    These are resolved at executor time via CatalogResolver.  The returned
    list may be empty (no metric-value filters in this plan).  The list is
    never None — use bool(result) to test for presence.
    """
    return [f for f in plan.filters if _filter_is_metric_value(f)]


def state_filters_are_supported(plan: QueryPlan) -> bool:
    """Return whether filters are limited to supported state (and metric-value) filters.

    Accepts state-scoped filters and metric-value filters (which are resolved
    at executor time).  Rejects enrollment bounds and any unrecognized filter kind.
    """

    from .filters import filter_kind

    if requested_filter_states(plan) is None:
        return False
    # All filters are either state-scoped or metric-value filters.
    return all(
        filter_kind(f) in {"state", "region", "metric_value"} for f in plan.filters
    )


def direction(plan: QueryPlan) -> str:
    """Return the canonical sort direction (``"asc"`` or ``"desc"``) for a plan.

    Resolution order, picking the first signal present:

    1. ``plan.sort.direction`` if a ``SortSpec`` is attached.
    2. The first ``selection``-phase ``SortStepSpec`` direction, if any. This
       handles plans that emit ``sort_steps`` without a top-level ``plan.sort``.
    3. ``"asc"`` when ``plan.limit.kind == "bottom"`` (a bottom-N limit
       implicitly asks for ascending order).
    4. The first remaining ``SortStepSpec`` direction (any phase). The
       finalizer (:func:`planning.finalizer._collapse_legacy_sort`) folds a
       top-level ``plan.sort`` into a ``presentation``-phase sort step and
       clears ``plan.sort``, so a "ranked lowest first" plan reaches execution
       with its direction living *only* on a presentation-phase step. Without
       this rule the explicit ``asc`` is dropped here and rendered ``desc``
       (S41/S107: "compare … ranked lowest first").
    5. ``"desc"`` as the conventional ranking default.

    Promoted from four divergent copies that disagreed on rules 2–5:

    - ``execution.ranking._direction`` (rules 1, 3, 5 — skipped sort_steps)
    - ``execution._helpers._plan_direction`` (rules 1, 3, 5 — skipped sort_steps)
    - ``execution.lookup._lookup_direction`` (rule 1 then ``"asc"`` — never called)
    - ``quality._validation_helpers._direction`` (rules 1–3, 5)

    The sort_steps-aware variant is the most permissive: a plan with
    ``sort_steps`` populated but no ``plan.sort`` gets a meaningful answer
    instead of silently falling back to the conventional default.
    """

    if plan.sort is not None:
        return plan.sort.direction
    for step in plan.sort_steps:
        if step.phase == "selection":
            return step.direction
    if plan.limit is not None and plan.limit.kind == "bottom":
        return "asc"
    if plan.sort_steps:
        return plan.sort_steps[0].direction
    return "desc"


def direction_phrase(direction: str) -> str:
    """Render a sort direction as its canonical value-ordering phrase.

    ``"desc"`` -> ``"highest to lowest"``; anything else -> ``"lowest to
    highest"``. The single home for the value-order wording that the lookup,
    ranking, and operations order statements each re-derived. Sort resolution
    already lives here (see :func:`direction`), so the phrase for a resolved
    direction belongs beside it.
    """

    return "highest to lowest" if direction == "desc" else "lowest to highest"


_VALUE_SORT_FIELD_TOKENS = frozenset(
    {"value", "display_value", "metric_value", "<unknown>", "unknown"}
)
"""Artifact-field tokens that name a metric value rather than a district axis."""


def primary_sort_step(plan: QueryPlan) -> SortStepSpec | None:
    """Return the canonical ``SortStepSpec`` carrying the plan's sort intent.

    Mirrors :func:`direction`'s step resolution so every reader of a finalized
    plan agrees on the *same* step. The finalizer
    (:func:`planning.finalizer._collapse_legacy_sort`) folds a top-level
    ``plan.sort`` into a ``presentation``-phase ``SortStepSpec`` and clears
    ``plan.sort``, so on a finalized plan the user's sort field/direction/kind
    live *only* on a sort step.

    Resolution order: the first ``selection``-phase step, else the first step of
    any phase, else ``None`` (no requested sort).
    """

    for step in plan.sort_steps:
        if step.phase == "selection":
            return step
    if plan.sort_steps:
        return plan.sort_steps[0]
    return None


def sort_field(plan: QueryPlan) -> str | None:
    """Return the canonical sort field token, or ``None`` when no sort applies.

    Reads ``plan.sort.field`` when a legacy ``SortSpec`` is still attached
    (pre-finalize draft), else the canonical :func:`primary_sort_step` field.
    """

    if plan.sort is not None:
        return plan.sort.field
    step = primary_sort_step(plan)
    return step.field if step is not None else None


def sort_kind(plan: QueryPlan, *, metric_names: frozenset[str] = frozenset()) -> str:
    """Classify the active sort as ``"value"`` or ``"district"``.

    Reads only typed sort fields (``plan.sort`` pre-finalize, the canonical
    folded ``SortStepSpec`` post-finalize) — never raw user prose. A step whose
    ``key_type`` is ``"district"`` (or whose field is a district axis) is a
    district sort; a metric/profile-keyed step, a value-token field, or a field
    matching a plan metric name is a value sort. Absent any sort, defaults to
    ``"district"`` (the conventional lookup order).

    ``metric_names`` lets callers fold in result-derived metric names (the
    validator twin) so a step field that matches a rendered metric column is
    classified as a value sort.
    """

    field = sort_field(plan)
    if field is None:
        return "district"
    normalized = field.casefold().strip()
    if plan.sort is None:
        step = primary_sort_step(plan)
        if step is not None and step.key_type == "district":
            return "district"
    if normalized in {"district", "district_name"}:
        return "district"
    if normalized in _VALUE_SORT_FIELD_TOKENS:
        return "value"
    names = {metric.name.casefold().strip() for metric in plan.metrics} | set(
        metric_names
    )
    if normalized in names:
        return "value"
    if plan.sort is None:
        step = primary_sort_step(plan)
        if step is not None and step.key_type in {"policy_metric", "profile_field"}:
            return "value"
    return "district"


def requested_states(plan: QueryPlan) -> set[str]:
    """Return normalized state values requested by selection or filters."""

    states: set[str] = set()
    if plan.selection.states:
        states.update(expand_state_or_region_values(plan.selection.states))

    if not plan.filters:
        return states
    filter_states = requested_filter_states(plan)
    if filter_states is None:
        return states
    if states:
        if not filter_states:
            return states
        return states & filter_states
    return filter_states


def requested_filter_states(plan: QueryPlan) -> set[str] | None:
    """Return normalized state filters, or None for unsupported filters.

    Metric-value filters (those targeting a metric phrase rather than "state"
    or "enrollment") are skipped — they are not selection-phase constraints and
    are handled by the executor's _resolve_metric_value_filters step.
    """

    return _requested_filter_states_ignoring_metric_value(plan)


def _requested_filter_states_ignoring_metric_value(plan: QueryPlan) -> set[str] | None:
    """Compute state filter set, skipping metric-value filters.

    Returns None when any non-metric-value, non-state, non-enrollment filter
    is present (i.e., genuinely unsupported filter kinds).
    """

    from .filters import filter_kind

    states: set[str] = set()
    for filter_spec in plan.filters:
        kind = filter_kind(filter_spec)
        if kind == "metric_value":
            continue
        if kind == "enrollment":
            continue
        if kind not in {"state", "region"}:
            return None
        if filter_spec.operator == "equals" and isinstance(filter_spec.value, str):
            states.update(expand_state_or_region_values([filter_spec.value]))
            continue
        if filter_spec.operator == "in" and isinstance(filter_spec.value, list):
            if not all(isinstance(value, str) for value in filter_spec.value):
                return None
            states.update(expand_state_or_region_values(filter_spec.value))
            continue
        return None
    return states


def requested_enrollment_bounds(plan: QueryPlan) -> tuple[int | None, int | None] | None:
    """Return enrollment filter bounds or None when filters are unsupported."""

    from .filters import filter_kind

    min_enrollment: int | None = None
    max_enrollment: int | None = None
    saw_enrollment = False
    for filter_spec in plan.filters:
        # Metric-value filters are handled separately by _resolve_metric_value_filters;
        # they must be skipped here so an enrollment filter is not silently dropped
        # when a plan combines enrollment bounds with a metric-value threshold.
        kind = filter_kind(filter_spec)
        if kind == "metric_value":
            continue
        if kind == "state":
            continue
        if kind != "enrollment":
            return None
        if not isinstance(filter_spec.value, int | float):
            return None
        value = int(filter_spec.value)
        saw_enrollment = True
        if filter_spec.operator == "greater_than_or_equal":
            min_enrollment = max(min_enrollment or value, value)
            continue
        if filter_spec.operator == "greater_than":
            min_enrollment = max(min_enrollment or (value + 1), value + 1)
            continue
        if filter_spec.operator == "less_than_or_equal":
            max_enrollment = min(max_enrollment or value, value)
            continue
        if filter_spec.operator == "less_than":
            max_enrollment = min(max_enrollment or (value - 1), value - 1)
            continue
        return None
    if not saw_enrollment:
        return None
    return (min_enrollment, max_enrollment)
