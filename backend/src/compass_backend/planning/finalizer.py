"""Deterministic plan finalization from reconciler evidence.

Step 4 of the catalog-resolution-unification plan
(``docs/plans/2026-05-28-catalog-resolution-unification-plan.md``).
:func:`finalize_plan` is a pure function: same
:class:`~compass_backend.contracts.planning.QueryPlan` and
:class:`~compass_backend.contracts.catalog_resolution.CatalogReconciliationReport`
in produces the same :class:`~compass_backend.contracts.finalization.FinalizedPlan`
out. No I/O. No catalog calls. No prose inspection.

The finalizer's responsibilities:

- Walk every catalog-resolvable phrase in the plan; collect typed
  blockers for unsupported / unresolved / non-rankable /
  ambiguous-district cases.
- Move :class:`MetricSpec` items the reconciler resolved to a profile
  drawer (``profile_field`` or ``profile_rank_field``) into
  ``profile_fields``, and symmetric for ``profile_fields`` items the
  reconciler resolved to a metric drawer.
- Collapse legacy :class:`SortSpec` into :class:`SortStepSpec` with the
  ``key_type`` chosen from the reconciler's entity type.
- Correct ``key_type`` on existing :class:`SortStepSpec` entries when
  the reconciler disagrees with the planner-emitted type.

Design invariant: every :class:`SlotMove` records the
:class:`ResolvedEntity` that justified the move. The finalizer never
invents an entity — it just sequences what the reconciler already
decided. This is the rule that keeps the finalizer from drifting into
another opaque classifier the way ``_looks_like_frpl`` did.
"""

from __future__ import annotations

from compass_backend.catalog.profile_rank_fields import profile_rank_field_by_key
from compass_backend.contracts.catalog_resolution import (
    CatalogReconciliationReport,
    ResolvedEntity,
    ResolvedEntityType,
)
from compass_backend.contracts.finalization import (
    AmbiguousDistrictBlocker,
    FinalizedPlan,
    FinalizerBlocker,
    NonRankableProfileFieldBlocker,
    SlotMove,
    UnresolvedPhraseBlocker,
    UnsupportedConceptBlocker,
)
from compass_backend.contracts.planning import (
    FilterSpec,
    MetricSpec,
    ProfileFieldSpec,
    QueryPlan,
    SortKeyType,
    SortSpec,
    SortStepSpec,
)

_PROFILE_DRAWERS: frozenset[ResolvedEntityType] = frozenset(
    {"profile_field", "profile_rank_field"}
)
_METRIC_DRAWERS: frozenset[ResolvedEntityType] = frozenset({"metric", "metric_bundle"})

def _filter_field_is_reserved(filter_spec: FilterSpec) -> bool:
    """Return True for filter fields the catalog reconciler must skip.

    Delegates to the single-source
    :func:`compass_backend.execution.filters.filter_field_is_reserved` (#1373):
    the selection-phase ``state``/``region``/``enrollment`` kinds plus the legacy
    ``"value"`` count token; only metric-value filter phrases stay
    catalog-checkable (``region`` reserved as of #1216 — its ``field`` is the
    structural token ``"region"``, not a catalog phrase).
    """

    from compass_backend.execution.filters import filter_field_is_reserved

    return filter_field_is_reserved(filter_spec)


def finalize_plan(
    plan: QueryPlan,
    report: CatalogReconciliationReport,
) -> FinalizedPlan:
    """Return the slot-corrected plan plus typed blockers.

    Pure function. The same ``plan`` and ``report`` always produce the
    same :class:`FinalizedPlan`.

    Step-5 callers responsibilities:

    - Pull ``plan`` out of the planner turn's ``query_plan`` before
      calling; this function does not inspect routes or sessions.
    - Use :attr:`FinalizedPlan.is_ready` to dispatch: ``True`` means
      hand ``plan`` to the executor; ``False`` means consume
      ``blockers`` to choose clarify/refuse routing.
    - Treat ``slot_moves`` as observability — no decisions key off it.
    """

    blockers: list[FinalizerBlocker] = []
    slot_moves: list[SlotMove] = []

    _collect_unsupported_and_unresolved_blockers(
        plan,
        report,
        blockers,
    )

    new_metrics, new_profile_fields, drawer_moves = _rebalance_metric_drawers(
        plan,
        report,
    )
    slot_moves.extend(drawer_moves)

    new_sort_steps = list(plan.sort_steps)
    if plan.sort is not None:
        appended_step, sort_blocker, sort_move = _collapse_legacy_sort(
            plan.sort,
            report,
        )
        if sort_blocker is not None:
            blockers.append(sort_blocker)
        if appended_step is not None:
            new_sort_steps.append(appended_step)
        if sort_move is not None:
            slot_moves.append(sort_move)

    corrected_sort_steps: list[SortStepSpec] = []
    for index, step in enumerate(new_sort_steps):
        corrected, step_blocker, step_move = _correct_sort_step(
            step,
            index,
            report,
        )
        if step_blocker is not None:
            blockers.append(step_blocker)
        if step_move is not None:
            slot_moves.append(step_move)
        corrected_sort_steps.append(corrected)

    _collect_ambiguous_district_blockers(plan, report, blockers)

    new_metrics, blockers, deferred_metric_gap = _apply_metric_gap(
        plan, new_metrics, blockers
    )

    finalized_plan = plan.model_copy(
        update={
            "metrics": new_metrics,
            "profile_fields": new_profile_fields,
            "sort": None,
            "sort_steps": corrected_sort_steps,
            # #1613 U4: carry the deferred metric gap on the plan itself — only
            # ``finalized.plan`` flows to execution (planner.py drops the rest of
            # the FinalizedPlan), so the executor reads it from here.
            "deferred_metric_gap": deferred_metric_gap,
        }
    )
    return FinalizedPlan(
        plan=finalized_plan,
        blockers=tuple(blockers),
        slot_moves=tuple(slot_moves),
    )


def _apply_metric_gap(
    plan: QueryPlan,
    new_metrics: list[MetricSpec],
    blockers: list[FinalizerBlocker],
) -> tuple[list[MetricSpec], list[FinalizerBlocker], tuple[str, ...]]:
    """#1613 U4: a multi-metric lookup where one metric phrase has no catalog
    metric answers the resolvable metric(s) and discloses the gap, instead of
    failing the whole plan (over-clarify) or letting the planner silently drop
    the phrase.

    Anchor: case 1193 ("BA, MA, and average teacher salary compare across the
    largest 10 districts" — "average teacher salary" has no metric). Per
    ``instructions/model_instructions/planner.md`` this is a multi-metric
    *lookup* (side-by-side), not a composite ranking.

    Fires only when **>=1** metric still resolves and the *only* finalizer
    blockers are ``confidence="none"`` metric-phrase blockers. Strips the
    unresolvable phrases from ``plan.metrics`` (so the executor's re-resolution
    loop never re-hits them) and returns them as the typed ``deferred_metric_gap``
    for the executor → renderer to disclose (the renderer reuses #1653's
    ``_compose_lookup`` lead-line + caveat-marker pattern).

    **Gated on ``operation == "lookup"``** so the gap path only opens for the
    shape the renderer discloses (#1613 U6). A composite ("do all N separately")
    request keeps its blocker and still clarifies until the composite render path
    covers it (deferred). ``requires_all_metrics`` ("both A AND B") is excluded —
    dropping one metric there would silently change the AND semantics. Any
    non-metric-gap blocker (sort, district, unsupported concept) also keeps the
    plan clarifying. Default-safe: returns the inputs unchanged when it does not
    fire, so non-lookup and fully-resolvable plans are untouched.
    """

    if plan.operation != "lookup" or plan.requires_all_metrics:
        return new_metrics, blockers, ()

    metric_gap_blockers = [
        blocker
        for blocker in blockers
        if isinstance(blocker, UnresolvedPhraseBlocker)
        and (blocker.source_field or "").startswith("metrics[")
    ]
    if not metric_gap_blockers:
        return new_metrics, blockers, ()
    # Only strip when the metric-phrase gaps are the SOLE blockers; any other
    # blocker (sort/district/unsupported) means the turn must still clarify.
    if len(metric_gap_blockers) != len(blockers):
        return new_metrics, blockers, ()

    gap_phrases = {blocker.phrase for blocker in metric_gap_blockers}
    resolvable = [metric for metric in new_metrics if metric.name not in gap_phrases]
    if not resolvable:
        # Every metric was unresolvable -> no subset to answer; keep clarifying.
        return new_metrics, blockers, ()

    return resolvable, [], tuple(sorted(gap_phrases))


def _collect_unsupported_and_unresolved_blockers(
    plan: QueryPlan,
    report: CatalogReconciliationReport,
    blockers: list[FinalizerBlocker],
) -> None:
    """Walk every catalog-resolvable phrase and record refusal/gap blockers."""

    for source_field, phrase in _resolvable_phrases(plan):
        resolution = report.for_phrase(phrase)
        if resolution is None:
            continue
        primary = resolution.primary
        if primary is not None and primary.entity_type == "unsupported_concept":
            blockers.append(
                UnsupportedConceptBlocker(
                    phrase=phrase,
                    source_field=source_field,
                    catalog_id=primary.catalog_id,
                    message=str(primary.metadata.get("message") or primary.label),
                )
            )
            continue
        if resolution.confidence == "none":
            blockers.append(
                UnresolvedPhraseBlocker(
                    phrase=phrase, source_field=source_field
                )
            )


def _resolvable_phrases(plan: QueryPlan) -> list[tuple[str, str]]:
    """Return ``(source_field, phrase)`` for every catalog-checkable slot."""

    phrases: list[tuple[str, str]] = []
    for index, metric in enumerate(plan.metrics):
        phrases.append((f"metrics[{index}].name", metric.name))
    for index, profile in enumerate(plan.profile_fields):
        phrases.append((f"profile_fields[{index}].name", profile.name))
    for index, filter_spec in enumerate(plan.filters):
        if _filter_field_is_reserved(filter_spec):
            continue
        phrases.append((f"filters[{index}].field", filter_spec.field))
    if plan.sort is not None:
        phrases.append(("sort.field", plan.sort.field))
    for index, step in enumerate(plan.sort_steps):
        phrases.append((f"sort_steps[{index}].field", step.field))
    # #959: selection.districts are intentionally NOT verifier-checked here. The
    # reconciler has no district drawer, so a real district always resolves to
    # confidence="none" and would falsely block every named-district plan.
    # Districts are enforced at execution (resolve_selection) and the result
    # boundary (intended_geography Gate-3). Mirrors the already-dormant
    # _collect_ambiguous_district_blockers.
    return phrases


def _rebalance_metric_drawers(
    plan: QueryPlan,
    report: CatalogReconciliationReport,
) -> tuple[list[MetricSpec], list[ProfileFieldSpec], list[SlotMove]]:
    """Move metrics ↔ profile_fields based on reconciler entity_type.

    ``MetricSpec`` items the reconciler resolved to a profile drawer
    leave the ``metrics`` list and arrive in ``profile_fields`` keyed
    by the resolved field_key. Symmetric for ``profile_fields`` items
    the reconciler resolved to a metric drawer (those keep the original
    user phrase since the executor's metric bundle resolver works from
    prose).
    """

    new_metrics: list[MetricSpec] = []
    new_profile_fields: list[ProfileFieldSpec] = list(plan.profile_fields)
    moves: list[SlotMove] = []
    existing_profile_keys = {field.name for field in new_profile_fields}

    for index, metric in enumerate(plan.metrics):
        resolution = report.for_phrase(metric.name)
        primary = resolution.primary if resolution is not None else None
        if (
            primary is not None
            and primary.entity_type in _PROFILE_DRAWERS
        ):
            target_key = _profile_field_key_for(primary, metric.name)
            if target_key not in existing_profile_keys:
                new_profile_fields.append(ProfileFieldSpec(name=target_key))
                existing_profile_keys.add(target_key)
            moves.append(
                SlotMove(
                    kind="metric_into_profile_fields",
                    phrase=metric.name,
                    from_slot=f"metrics[{index}]",
                    to_slot=f"profile_fields[{len(new_profile_fields) - 1}]",
                    resolved_entity=primary,
                )
            )
            continue
        new_metrics.append(metric)

    final_profile_fields: list[ProfileFieldSpec] = []
    seen_metric_phrases = {metric.name for metric in new_metrics}
    for index, profile in enumerate(new_profile_fields):
        resolution = report.for_phrase(profile.name)
        primary = resolution.primary if resolution is not None else None
        if (
            primary is not None
            and primary.entity_type in _METRIC_DRAWERS
        ):
            if profile.name not in seen_metric_phrases:
                new_metrics.append(MetricSpec(name=profile.name, role="primary"))
                seen_metric_phrases.add(profile.name)
            moves.append(
                SlotMove(
                    kind="profile_field_into_metrics",
                    phrase=profile.name,
                    from_slot=f"profile_fields[{index}]",
                    to_slot=f"metrics[{len(new_metrics) - 1}]",
                    resolved_entity=primary,
                )
            )
            continue
        final_profile_fields.append(profile)

    return new_metrics, final_profile_fields, moves


def _profile_field_key_for(primary: ResolvedEntity, fallback_phrase: str) -> str:
    """Return the profile-field key the executor should look up by.

    The reconciler stores the resolved key in ``metadata["field_key"]``
    for both ``profile_field`` and ``profile_rank_field`` candidates.
    Fall back to the original phrase only when metadata is unexpectedly
    missing — the executor's profile-field resolver also accepts prose,
    so worst case is a small extra alias lookup.
    """

    candidate_key = primary.metadata.get("field_key")
    if isinstance(candidate_key, str) and candidate_key:
        return candidate_key
    return fallback_phrase


def _collapse_legacy_sort(
    sort: SortSpec,
    report: CatalogReconciliationReport,
) -> tuple[SortStepSpec | None, FinalizerBlocker | None, SlotMove | None]:
    """Collapse :class:`SortSpec` into a presentation-phase
    :class:`SortStepSpec`.

    The decision the plan retires :class:`SortSpec` outright in step 8.
    Step 4 stops populating it and always emits a sort_step.

    Returns ``(step, blocker, slot_move)`` where step is the new
    sort_step to append (or ``None`` when blocked) and blocker is the
    refusal reason (or ``None`` when clean).
    """

    resolution = report.for_phrase(sort.field)
    primary = resolution.primary if resolution is not None else None

    if primary is None:
        # Unresolved sort field already produced an UnresolvedPhraseBlocker
        # from the upstream scan; emit the legacy-shaped sort_step anyway so
        # the audit trail captures the collapse, and let the validator
        # decide route.
        step = SortStepSpec(
            field=sort.field,
            direction=sort.direction,
            nulls=sort.nulls,
            key_type="policy_metric",
            phase="presentation",
        )
        move = SlotMove(
            kind="sort_into_sort_steps",
            phrase=sort.field,
            from_slot="sort",
            to_slot="sort_steps[-1]",
            resolved_entity=ResolvedEntity(
                entity_type="metric",
                catalog_id=f"metric:{sort.field}",
                label=sort.field,
                score=0.0,
                source="finalizer.legacy_sort_collapse",
            ),
        )
        # Note the synthesized resolved_entity above has score=0; it is an
        # audit placeholder, not a catalog vouching. The unresolved
        # blocker from the upstream scan is the authoritative signal.
        return step, None, move

    key_type, blocker = _key_type_for_sort_primary(
        primary,
        phrase=sort.field,
        source_field="sort.field",
    )
    if blocker is not None:
        return None, blocker, None

    field_name = _sort_step_field_name(primary, sort.field)
    step = SortStepSpec(
        field=field_name,
        direction=sort.direction,
        nulls=sort.nulls,
        key_type=key_type,
        phase="presentation",
    )
    move = SlotMove(
        kind="sort_into_sort_steps",
        phrase=sort.field,
        from_slot="sort",
        to_slot="sort_steps[-1]",
        resolved_entity=primary,
    )
    return step, None, move


def _correct_sort_step(
    step: SortStepSpec,
    index: int,
    report: CatalogReconciliationReport,
) -> tuple[SortStepSpec, FinalizerBlocker | None, SlotMove | None]:
    """Flip a sort step's ``key_type`` (and field) when the reconciler
    disagrees with the planner."""

    resolution = report.for_phrase(step.field)
    primary = resolution.primary if resolution is not None else None
    if primary is None:
        return step, None, None

    expected_key_type, blocker = _key_type_for_sort_primary(
        primary,
        phrase=step.field,
        source_field=f"sort_steps[{index}].field",
    )
    if blocker is not None:
        return step, blocker, None
    expected_field = _sort_step_field_name(primary, step.field)
    if expected_key_type == step.key_type and expected_field == step.field:
        return step, None, None

    corrected = step.model_copy(
        update={"key_type": expected_key_type, "field": expected_field}
    )
    move = SlotMove(
        kind="sort_step_key_type_corrected",
        phrase=step.field,
        from_slot=(
            f"sort_steps[{index}].(field={step.field},"
            f"key_type={step.key_type})"
        ),
        to_slot=(
            f"sort_steps[{index}].(field={expected_field},"
            f"key_type={expected_key_type})"
        ),
        resolved_entity=primary,
    )
    return corrected, None, move


def _key_type_for_sort_primary(
    primary: ResolvedEntity,
    *,
    phrase: str,
    source_field: str,
) -> tuple[SortKeyType, FinalizerBlocker | None]:
    """Decide the :data:`SortKeyType` for a sort-context resolution.

    - ``profile_rank_field`` → ``"profile_field"`` (the executor's rank
      dispatch).
    - ``profile_field`` (i.e. NCES allowlist member but NOT in the
      rank-fields enum) → :class:`NonRankableProfileFieldBlocker`. The
      catalog says we know the phrase but can't sort by it.
    - ``metric`` / ``metric_bundle`` → ``"policy_metric"``.
    - Anything else (district, peer_set, topic) → blocker via the
      non-rankable branch. The catalog identified the phrase as
      something that isn't a sortable column.
    """

    if primary.entity_type == "profile_rank_field":
        return "profile_field", None
    if primary.entity_type in _METRIC_DRAWERS:
        return "policy_metric", None
    if primary.entity_type == "profile_field":
        return "profile_field", NonRankableProfileFieldBlocker(
            phrase=phrase,
            source_field=source_field,
            resolved_field_key=_profile_field_key_for(primary, phrase),
            rankable_candidates=_rankable_alternatives(),
        )
    # Any non-sortable resolution (district, peer_set, topic) is treated
    # as a non-rankable blocker so the validator gives the user the same
    # rankable-fields menu instead of a generic refusal.
    return "policy_metric", NonRankableProfileFieldBlocker(
        phrase=phrase,
        source_field=source_field,
        resolved_field_key=_profile_field_key_for(primary, phrase),
        rankable_candidates=_rankable_alternatives(),
    )


def _sort_step_field_name(primary: ResolvedEntity, fallback_phrase: str) -> str:
    """Return the ``SortStepSpec.field`` value the executor expects.

    For profile-field steps the executor calls
    ``rank_districts_by_profile_field(step.field, ...)``, which keys on
    the field_key from the rank-fields enum. For metric steps the
    executor resolves through the metric bundle resolver, which accepts
    prose, so we preserve the original phrase.
    """

    if primary.entity_type in {"profile_rank_field", "profile_field"}:
        return _profile_field_key_for(primary, fallback_phrase)
    return fallback_phrase


def _rankable_alternatives() -> tuple[ResolvedEntity, ...]:
    """Return the governed rank-field set as :class:`ResolvedEntity` candidates.

    Used as the ``rankable_candidates`` payload on
    :class:`NonRankableProfileFieldBlocker` so the step-5 validator can
    show the user real alternatives (NCES enrollment, FRPL %).
    """

    rankable: list[ResolvedEntity] = []
    for field in profile_rank_field_by_key().values():
        rankable.append(
            ResolvedEntity(
                entity_type="profile_rank_field",
                catalog_id=f"profile_rank_field:{field.field_key}",
                label=field.label,
                score=1.0,
                vintage_label=field.vintage_label,
                source="finalizer.rankable_alternatives",
                metadata={
                    "field_key": field.field_key,
                    "column_name": field.column_name,
                    "display": field.display,
                },
            )
        )
    return tuple(rankable)


def _collect_ambiguous_district_blockers(
    plan: QueryPlan,
    report: CatalogReconciliationReport,
    blockers: list[FinalizerBlocker],
) -> None:
    """Flag districts the catalog resolved to multiple covered candidates.

    Today the step-1 reconciler does not fan out to ``resolve_districts``,
    so this scan is a no-op in production. It is wired now so the
    forward-compat case lights up automatically when district resolution
    lands in a later step.
    """

    for index, name in enumerate(plan.selection.districts):
        resolution = report.for_phrase(name)
        if resolution is None or resolution.confidence != "ambiguous":
            continue
        district_candidates = tuple(
            candidate
            for candidate in resolution.candidates
            if candidate.entity_type == "district"
        )
        if len(district_candidates) < 2:
            continue
        blockers.append(
            AmbiguousDistrictBlocker(
                phrase=name,
                source_field=f"selection.districts[{index}]",
                candidates=district_candidates,
            )
        )


__all__ = ["finalize_plan"]
