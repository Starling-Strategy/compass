"""Unified reconcile → adjudicate → finalize orchestrator.

Step 5 of the catalog-resolution-unification plan
(``docs/plans/2026-05-28-catalog-resolution-unification-plan.md``). The
:class:`CatalogPlanPipeline` is the single async entry point the
planner's output validator always calls (#1248). It owns the sequencing:

1. Extract phrases from the planner draft's
   :class:`~compass_backend.contracts.planning.QueryPlan`.
2. Run :class:`~compass_backend.catalog.reconciliation.CatalogReconciler`
   in parallel across every phrase, with the per-phrase
   :data:`PhraseRoleHint` derived from the source slot.
3. For each :class:`~compass_backend.contracts.catalog_resolution.CatalogResolution`
   with ``confidence`` in ``{"medium", "ambiguous"}`` AND more than one
   candidate, call the catalog adjudicator agent to pick a primary.
   ``confidence="high"`` resolutions skip the adjudicator entirely
   (deterministic path) — see plan decision 3.
4. Call :func:`~compass_backend.planning.finalizer.finalize_plan` on
   the (possibly adjudicated) report to produce the slot-corrected
   :class:`FinalizedPlan` with typed blockers for clarify/refuse
   routing.

The pipeline is stateless beyond its constructor inputs and safe to
share across requests. Construct once at startup; pass into
:class:`PlannerDeps` so the planner output validator can consume it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

import logfire

from compass_backend.catalog.adjudication import (
    CatalogAdjudicationCandidate,
    CatalogAdjudicationDecision,
    CatalogAdjudicationDeps,
    validate_catalog_adjudication_decision,
)
from compass_backend.catalog.reconciliation import (
    CatalogReconciler,
    PhraseToReconcile,
)
from compass_backend.contracts.catalog_resolution import (
    CatalogReconciliationReport,
    CatalogResolution,
    ResolvedEntity,
)
from compass_backend.contracts.finalization import FinalizedPlan
from compass_backend.contracts.planning import QueryPlan
from compass_backend.observability import compass_span, set_span_attributes
from compass_backend.planning.finalizer import finalize_plan
from compass_backend.recognition.extraction import mentions_from_plan
from compass_backend.recognition.reconcile_shadow import role_hint_for_source_field


class _AdjudicatorAgent(Protocol):
    """Minimal protocol the orchestrator needs from the adjudicator agent.

    Matches the Pydantic AI Agent's ``run`` shape so tests can supply a
    fake without instantiating a real agent.
    """

    async def run(
        self,
        user_prompt: str,
        *,
        deps: CatalogAdjudicationDeps,
    ) -> "_AdjudicatorRunResult": ...


class _AdjudicatorRunResult(Protocol):
    output: CatalogAdjudicationDecision


@dataclass(frozen=True)
class _AdjudicatedPhrase:
    """Internal helper carrying the per-phrase outcome for span attrs."""

    phrase: str
    skipped: bool
    decision: CatalogAdjudicationDecision | None
    error: str | None


class CatalogPlanPipeline:
    """Reconcile → adjudicate → finalize the planner's draft plan."""

    def __init__(
        self,
        reconciler: CatalogReconciler,
        *,
        adjudicator: _AdjudicatorAgent | None = None,
        adjudicator_factory: Callable[[], _AdjudicatorAgent] | None = None,
    ) -> None:
        self._reconciler = reconciler
        self._adjudicator = adjudicator
        self._adjudicator_factory = adjudicator_factory

    def _resolve_adjudicator(self) -> _AdjudicatorAgent | None:
        if self._adjudicator is not None:
            return self._adjudicator
        if self._adjudicator_factory is None:
            return None
        self._adjudicator = self._adjudicator_factory()
        return self._adjudicator

    async def reconcile_and_finalize(self, plan: QueryPlan) -> FinalizedPlan:
        """Produce the :class:`FinalizedPlan` for ``plan``.

        Pure-async wrapper: the same ``plan`` produces the same
        :class:`FinalizedPlan` shape given the same catalog state. The
        only non-determinism is the adjudicator's LLM call when
        ambiguity drives it.
        """

        # #959: districts do NOT flow through this verifier. The reconciler has
        # metric / profile-field / unsupported-concept drawers but no district
        # drawer (see finalizer._collect_ambiguous_district_blockers, which is
        # already dormant for the same reason). Reconciling a district phrase
        # therefore always yields confidence="none" → a false
        # UnresolvedPhraseBlocker on EVERY named-district plan. District
        # existence/ambiguity is enforced at execution (resolve_selection refuses
        # an unresolved district) and the right-district check at the result
        # boundary (intended_geography Gate-3), per the #1248 "harden the ends at
        # the result boundary" design. Drop district mentions before reconciling.
        mentions = [
            mention
            for mention in mentions_from_plan(plan)
            if mention.source_field != "selection.districts"
        ]
        if not mentions:
            return finalize_plan(plan, CatalogReconciliationReport())

        phrases = [
            PhraseToReconcile(
                phrase=mention.phrase,
                role_hint=role_hint_for_source_field(mention.source_field),  # type: ignore[arg-type]
            )
            for mention in mentions
        ]
        report = await self._reconciler.reconcile_all(phrases)
        adjudicated_report = await self._maybe_adjudicate(report)
        return finalize_plan(plan, adjudicated_report)

    async def _maybe_adjudicate(
        self, report: CatalogReconciliationReport
    ) -> CatalogReconciliationReport:
        """Adjudicate non-``high``-confidence resolutions in parallel.

        Returns the report unchanged when no adjudicator is configured
        (test setups) or every resolution is already deterministic.
        """

        adjudicator = self._resolve_adjudicator()
        if adjudicator is None:
            for resolution in report.resolutions:
                _emit_skip(resolution, reason="no_adjudicator")
            return report

        # #1587: gather(return_exceptions=True) + reraise, not TaskGroup.
        # _maybe_adjudicate runs inside the planner agent's output validator
        # (planning/planner.py → reconcile_and_finalize), where Pydantic AI's
        # retry control flow and orchestration's ``except UnexpectedModelBehavior``
        # (orchestration/chat.py) key off exact exception types -- an
        # ExceptionGroup from a TaskGroup would change the surfaced type and
        # bypass them. _adjudicate_one already traps its own ``except Exception``
        # and returns the resolution, so a child normally cannot fail; but a
        # bare BaseException (e.g. CancelledError) would still, under the old
        # bare gather(), unwind the parent while siblings keep running detached
        # against the per-turn DB connection (db/_turn_connection.py).
        # Collecting every result before reraising the first exception
        # guarantees all siblings settled before the parent unwinds -- closing
        # that hazard -- while the reraise preserves the original exception type.
        results = await asyncio.gather(
            *(
                self._adjudicate_one(resolution, adjudicator)
                for resolution in report.resolutions
            ),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return CatalogReconciliationReport(resolutions=tuple(results))

    async def _adjudicate_one(
        self,
        resolution: CatalogResolution,
        adjudicator: _AdjudicatorAgent,
    ) -> CatalogResolution:
        if _should_skip_adjudication(resolution):
            _emit_skip(resolution, reason="confidence_high")
            return resolution
        if not resolution.candidates:
            _emit_skip(resolution, reason="no_candidates")
            return resolution

        candidates = tuple(
            CatalogAdjudicationCandidate(
                entity_type=candidate.entity_type,
                candidate_id=candidate.catalog_id,
                label=candidate.label,
                metadata=candidate.metadata,
            )
            for candidate in resolution.candidates
        )
        with compass_span(
            "compass.catalog.adjudicate.run",
            phrase=resolution.phrase,
            role_hint=resolution.role_hint,
            candidate_count=len(candidates),
            confidence=resolution.confidence,
        ) as run_span:
            try:
                result = await adjudicator.run(
                    resolution.phrase,
                    deps=CatalogAdjudicationDeps(
                        query=resolution.phrase, candidates=candidates
                    ),
                )
                decision = validate_catalog_adjudication_decision(
                    result.output, candidates
                )
            except Exception as exc:  # pragma: no cover - defensive
                # Swallow-with-marker (src/compass_backend/AGENTS.md error-handling
                # posture): adjudication is a non-blocking refinement, so on failure
                # we keep the un-adjudicated resolution but leave the loss observable
                # -- a span marker on this run span plus the dedicated skipped span.
                set_span_attributes(
                    run_span,
                    adjudication_skipped=True,
                    adjudication_skip_reason="adjudicator_error",
                    adjudication_error=type(exc).__name__,
                )
                logfire.warning(
                    "compass.catalog.adjudicate.error",
                    phrase=resolution.phrase,
                    error=type(exc).__name__,
                )
                _emit_skip(resolution, reason="adjudicator_error")
                return resolution

        return _apply_adjudication(resolution, decision)


def _should_skip_adjudication(resolution: CatalogResolution) -> bool:
    """Skip the adjudicator on deterministic resolutions.

    Plan decision 3: ``confidence="high"`` resolutions already have a
    clear primary; calling the adjudicator wastes a gateway request.
    Single-candidate resolutions are also trivially decided.

    PR1b defense-in-depth (#1248): a single district NAME that more than
    one real geography shares (Washington, DC vs Washington state;
    Houston, TX vs a same-named district elsewhere) must NOT take the
    ``confidence="high"`` deterministic skip. A naive single-name match
    can score one candidate high enough to look clean while landing on a
    real-but-WRONG geography — the DC/Houston bug. When the widened
    district candidate list spans more than one geography, force the
    adjudicator to break the ambiguity rather than rubber-stamping the
    top score. Single-candidate resolutions are still trivially decided.
    """

    if _is_geographically_ambiguous_district(resolution):
        return False
    return resolution.confidence == "high" or len(resolution.candidates) <= 1


def _is_geographically_ambiguous_district(resolution: CatalogResolution) -> bool:
    """Return True when a district phrase's candidates span >1 geography.

    Geography here is the candidate ``state`` (carried in
    :attr:`ResolvedEntity.metadata`). Two or more distinct states among
    the district candidates means the single name is ambiguous and the
    adjudicator must choose — even if one candidate scored high. The
    check is inert for non-district resolutions and for district phrases
    whose candidates all share one state.
    """

    states = {
        state
        for candidate in resolution.candidates
        if candidate.entity_type == "district"
        and (state := _candidate_state(candidate))
    }
    return len(states) > 1


def _candidate_state(candidate: ResolvedEntity) -> str | None:
    raw = candidate.metadata.get("state")
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    return stripped.casefold() or None


def _apply_adjudication(
    resolution: CatalogResolution,
    decision: CatalogAdjudicationDecision,
) -> CatalogResolution:
    """Promote the adjudicator's pick to primary in a new
    :class:`CatalogResolution`."""

    if decision.action in {"select_one", "select_bundle", "select_with_alternates"}:
        primary = _find_candidate(resolution.candidates, decision.selected_ids)
        if primary is not None:
            reordered = (primary,) + tuple(
                candidate
                for candidate in resolution.candidates
                if candidate.catalog_id != primary.catalog_id
            )
            return resolution.model_copy(
                update={"candidates": reordered, "confidence": "high"}
            )

    if decision.action in {"clarify", "no_match"}:
        # A clarify/no_match adjudication must not silently keep candidates[0].
        # What it SHOULD do depends on the typed drawer (``entity_type``):
        #
        # DISTRICT drawer (PR1b defense-in-depth, #1248): a real-but-WRONG
        # district (Washington DC vs Washington state; the DC/Houston bug) must
        # BLOCK. Collapse to ``confidence="none"`` so the finalizer's
        # UnresolvedPhrase path produces a typed blocker and the live path pivots
        # to clarify/refuse instead of executing on an unverified geography. The
        # candidates are dropped so ``primary`` cannot resurface them. (Districts
        # are filtered out before this pipeline in production — see
        # reconcile_and_finalize — so this is the forward-compat district guard.)
        if _has_district_candidate(resolution):
            return resolution.model_copy(
                update={"candidates": (), "confidence": "none"}
            )
        # METRIC drawer (Fix 4B, #1 refusal family): a clarify/no_match on a
        # metric tie ("paid sick days in the first year" → 5 leave metrics at
        # 0.6; or an ambiguous [BA, MA] bundle) is a materially-ambiguous but
        # ANSWERABLE phrase, not a wrong-entity risk. Per the product decision,
        # best-guess rather than collapse to the canned rescue: keep the
        # top-ranked candidate (``candidates[0]``) as primary so ``finalize_plan``
        # sees a real primary (``is_ready=True``) and the plan reaches execution.
        # The concrete metric and its alternates disclosure are re-derived at
        # execution (``_resolve_plan_metrics`` applies the same best-guess rule
        # and names the alternates); this gate only unblocks the plan. TYPED-field
        # only (``entity_type``) — no prose dispatch.
        return resolution.model_copy(update={"confidence": "high"})

    return resolution


def _has_district_candidate(resolution: CatalogResolution) -> bool:
    """True when any candidate is a district drawer (the DC/Houston guard scope)."""

    return any(
        candidate.entity_type == "district" for candidate in resolution.candidates
    )


def _find_candidate(
    candidates: Sequence[ResolvedEntity], selected_ids: Sequence[str]
) -> ResolvedEntity | None:
    if not selected_ids:
        return None
    target = selected_ids[0]
    for candidate in candidates:
        if candidate.catalog_id == target:
            return candidate
    return None


def _emit_skip(
    resolution: CatalogResolution,
    *,
    reason: str,
) -> None:
    """Span the deterministic-path bypass so audits can count them.

    Step 6's retire-the-workarounds review uses these counts to gauge
    how often the catalog-only path resolves cleanly without LLM help.
    """

    with compass_span(
        "compass.catalog.adjudicate.skipped",
        phrase=resolution.phrase,
        role_hint=resolution.role_hint,
        confidence=resolution.confidence,
        candidate_count=len(resolution.candidates),
        reason=reason,
    ):
        pass


__all__ = ["CatalogPlanPipeline"]
