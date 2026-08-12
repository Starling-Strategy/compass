"""Recoverable referent (secondary-district) resolution for execution.

Several execution call sites resolve a *referent* district — a district named
inside a typed plan field rather than the primary selection (the anchor in an
``anchor_value`` filter, the anchor in a peer/similarity spec, etc.). The
catalog returns rich resolution results (``DistrictResolution.ambiguous`` /
``.unresolved``), but the historic call sites discarded that candidate info and
emitted a generic dead-end ``ExecutionRefusal`` ("could not resolve the anchor
district"). That violates SELECT-R4 / RFC #1248: clarify with the real
candidates, not a generic refusal — and it dropped two recoverable signals:

1. A *state qualifier bundled into the typed string* (``"Portland ME"``). The
   planner emits ``AnchorValueSpec.district`` as one plain string; nobody split
   off the trailing state, so the wrong same-name district could be picked (or
   nothing resolved). See #1192 / #739 ("same value as me" for Portland ME vs
   Portland OR; Detroit fuzzy match).
2. A *state-qualifier-narrowed candidate* arriving via ``ambiguous`` (the live
   CatalogResolver fuzzy path puts a lone fuzzy hit there). The old code only
   accepted ``len(resolved) == 1`` and treated a single-fuzzy result as a
   dead-end. The helper now accepts a lone fuzzy candidate only when an
   explicit state qualifier narrowed it (the user's own disambiguation); an
   *ungated* lone fuzzy hit could be a real-but-wrong district, so it clarifies
   instead (mirroring ``execution.selection``).

This module centralizes that recovery so the anchor paths — and, in Phase 2,
the other dead-end sites — share one implementation:

- :func:`split_state_suffix` — pure: strip a trailing recognized US-state
  qualifier from a typed district string. Its authority now lives in
  :mod:`compass_backend.reference.states` (so the catalog boundary can use it
  without importing execution); it is re-exported here for the existing
  execution/orchestration call sites.
- :func:`resolve_referent_district` — async: split the suffix, merge with the
  plan's requested states, resolve, accept a lone candidate (resolved *or* a
  single total across ``ambiguous``), and on genuine multi-candidate ambiguity
  return an :class:`ExecutionClarification` built from the real candidates
  (mirroring ``execution.selection``), not a generic dead-end.

Grounding is preserved: every accepted candidate is a real covered
``DistrictCandidate`` from the catalog. The helper only improves *which*
recoverable signal is honored, never invents a district.
"""

from __future__ import annotations

from compass_backend.catalog import (
    CatalogResolutionCandidate,
    CatalogResolutionEntity,
    CatalogResolver,
    DistrictCandidate,
)
from compass_backend.catalog.reporting import report_from_entities
from compass_backend.contracts.planning import ClarificationRequest, QueryPlan
from compass_backend.reference import split_state_suffix

from .selection import (
    _district_candidate_label,
    _district_clarification_option,
    requested_states,
)
from .types import ExecutionClarification


async def resolve_referent_district(
    catalog: CatalogResolver,
    district_text: str,
    plan: QueryPlan,
    *,
    extra_states: set[str] | None = None,
) -> DistrictCandidate | ExecutionClarification:
    """Resolve a typed referent district string to one covered district.

    Splits a bundled trailing state qualifier, merges it with ``extra_states``
    (e.g. a typed ``AnchorValueSpec.state`` — preferred over bundling) and the
    plan's requested states, resolves through the catalog, and:

    - returns the single resolved (exact/alias) candidate, OR
    - accepts a single candidate that arrived via ``ambiguous`` (the live
      fuzzy-match path) ONLY when an explicit state qualifier narrowed it, OR
    - on genuine multi-candidate ambiguity (or an ungated lone fuzzy hit)
      returns an
      :class:`ExecutionClarification` listing the real candidate labels
      (mirroring :mod:`compass_backend.execution.selection`).

    A genuine unresolved (no candidate at all) is reported the same way as
    multi-candidate ambiguity — a clarification — so the caller can decide how
    to degrade. Callers that cannot plumb a clarification fall back to a
    candidate-listing refusal (see operations._resolve_anchor_filter_value).
    """

    name, suffix_states = split_state_suffix(district_text)
    # The user's OWN disambiguation: a trailing state on the referent string
    # ("Portland, ME") or a typed AnchorValueSpec.state. Kept separate from the
    # plan's broad requested_states so a lone fuzzy hit can be checked against
    # the state the user actually named (#1556 / B05).
    explicit_states = suffix_states | (extra_states or set())
    states = requested_states(plan) | explicit_states

    resolution = await catalog.resolve_districts([name], states=states or None)

    if len(resolution.resolved) == 1 and not resolution.ambiguous:
        return resolution.resolved[0]

    all_candidates: list[DistrictCandidate] = list(resolution.resolved)
    for candidates in resolution.ambiguous.values():
        all_candidates.extend(candidates)

    # A lone candidate that only surfaced via the fuzzy `ambiguous` bucket is
    # accepted ONLY when an explicit state qualifier (a trailing state on the
    # referent string, or the plan's requested states) was applied that uniquely
    # selected it — that qualifier is the user's own disambiguation. An ungated
    # lone fuzzy hit could be a real-but-wrong district, so without a qualifier
    # we clarify, mirroring `execution.selection` (which never auto-accepts an
    # ambiguous candidate). See #1192 / #739.
    #
    # #1556 / B05 grounding guard: the live fuzzy/recall path does not strictly
    # honor the state filter, so the lone bucket candidate can be the WRONG
    # state ("Portland, ME" surfacing Portland Public Schools, OR). When the
    # user gave an explicit state qualifier, the lone candidate's state MUST be
    # one of those states — otherwise the qualifier contradicts the candidate
    # and a silent substitution would answer about the wrong district. On a
    # contradiction we clarify, never auto-accept. (The geography invariant: a
    # value must trace to the district the user actually named.)
    if states and len(all_candidates) == 1:
        candidate = all_candidates[0]
        candidate_state = (candidate.state or "").upper()
        if not explicit_states or candidate_state in explicit_states:
            return candidate

    return _referent_clarification(plan, name, all_candidates)


def _referent_clarification(
    plan: QueryPlan,
    phrase: str,
    candidates: list[DistrictCandidate],
) -> ExecutionClarification:
    """Build a candidate-listing clarification for a referent district.

    Mirrors ``execution.selection``'s district ambiguity branch so the user
    sees the real covered districts, not a generic dead-end.
    """

    candidate_labels = [_district_candidate_label(candidate) for candidate in candidates]
    clarification = ClarificationRequest(
        question=(
            f'I found more than one covered district matching "{phrase}". '
            "Which district do you mean?"
        ),
        missing_fields=["district"],
        candidates=candidate_labels,
        # #1348: structured, clickable counterparts so referent/anchor
        # disambiguation is as resumable as primary-selection ambiguity.
        candidate_options=[
            _district_clarification_option(candidate) for candidate in candidates
        ],
    )
    entity = CatalogResolutionEntity(
        input_phrase=phrase,
        entity_type="district",
        status="ambiguous" if candidates else "unresolved",
        resolution_method="catalog_search" if candidates else "unresolved",
        candidates=[
            CatalogResolutionCandidate(
                candidate_id=str(candidate.district_id),
                label=_district_candidate_label(candidate),
                entity_type="district",
                metadata={"state": candidate.state},
            )
            for candidate in candidates
        ],
        provenance="CatalogResolver.resolve_districts",
        message=(
            "Multiple covered Compass districts matched this referent phrase."
            if candidates
            else "No covered Compass district resolved for this referent phrase."
        ),
    )
    return ExecutionClarification(
        clarification=clarification,
        message=clarification.question,
        resolution_report=report_from_entities(
            question=plan.question,
            operation=plan.operation,
            entities=[entity],
        ),
    )


__all__ = ["resolve_referent_district", "split_state_suffix"]
