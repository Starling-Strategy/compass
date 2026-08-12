"""Deterministic query executor for supported Compass operations.

The class shell lives here. Operation methods (`_execute_*`) are in the
mixin at `operations.py`, scoping/metric-resolution methods are in the
mixin at `scoping.py`, and module-level free helpers are in `_helpers.py`.
The split was done in 2026-05 to break a 1,852-LOC file along its natural
internal section boundaries; behavior is unchanged.
"""

from __future__ import annotations

from compass_backend.catalog import CatalogResolver
from compass_backend.contracts.planning import (
    ClarificationRequest,
    PlannerTurn,
    QueryPlan,
)
from compass_backend.observability import compass_span, set_span_attributes
from compass_backend.planning.cross_box import (
    review_cross_box_planner_turn as _review_cross_box_planner_turn,
)

from ._helpers import (
    _UNSUPPORTED_SHAPE_MESSAGE,
    _cap_list,
    _plan_concept_phrases,
    _resolution_report_from_outcome,
    _unsupported_resolution_report,
)
from .operations import _OperationsMixin
from .scoping import _ScopingMixin
from .types import (
    ExecutionClarification,
    ExecutionOutcome,
    ExecutionRefusal,
    ExecutionSuccess,
    PolicyAnswerRepository,
)


class DeterministicQueryExecutor(_OperationsMixin, _ScopingMixin):
    """Route supported typed query plans to deterministic operation paths."""

    def __init__(
        self,
        repository: PolicyAnswerRepository,
        *,
        catalog_resolver: CatalogResolver | None = None,
        current_academic_year: str | None = None,
        default_limit: int = 10,
    ) -> None:
        if current_academic_year is None:
            # W1-06 (#844): centralized default lives in planning.temporal.
            from compass_backend.planning.temporal import (
                default_current_academic_year,
            )

            current_academic_year = default_current_academic_year()
        self._repository = repository
        self._catalog = catalog_resolver or CatalogResolver(repository)
        self._current_academic_year = current_academic_year
        self._default_limit = default_limit

    @property
    def catalog(self) -> CatalogResolver:
        """Expose the catalog boundary for orchestration-level grounding (#1348).

        The clarification-option grounding step re-resolves a planner-emitted
        district clarify through the catalog so the offered options carry real
        ``district_id``s rather than model-authored strings.
        """

        return self._catalog

    async def review_cross_box_planner_turn(self, turn: PlannerTurn) -> PlannerTurn:
        """Run catalog-backed wrong-box review before deterministic execution."""

        return await _review_cross_box_planner_turn(turn, self._catalog)

    async def execute(self, plan: QueryPlan) -> ExecutionOutcome:
        """Execute supported deterministic query shapes."""

        unsupported_report = _unsupported_resolution_report(
            plan,
            await self._catalog.resolve_unsupported_concepts(
                _plan_concept_phrases(plan)
            ),
        )
        if unsupported_report.has_blockers:
            blocker = unsupported_report.unsupported_blockers[0]
            clarification = ClarificationRequest(
                question=blocker.message,
                missing_fields=["metric"],
                candidates=[],
            )
            return ExecutionClarification(
                clarification=clarification,
                message=blocker.message,
                resolution_report=unsupported_report,
                recall_report=self._catalog.drain_recall_report(),
            )

        with compass_span(
            f"compass.execution.{plan.operation}",
            operation=plan.operation,
            metric_phrases=_cap_list(
                [metric.name for metric in plan.metrics],
                5,
            ),
            district_phrases=_cap_list(plan.selection.districts, 5),
            states=_cap_list(plan.selection.states, 5),
            selection_scope=plan.selection.scope,
        ) as span:
            if plan.operation == "rank":
                outcome = await self._execute_ranking(plan)
            elif plan.operation == "lookup":
                outcome = await self._execute_lookup(plan)
            elif plan.operation == "count":
                outcome = await self._execute_count(plan)
            elif plan.operation == "trend":
                outcome = await self._execute_trend(plan)
            elif plan.operation == "profile_lookup":
                outcome = await self._execute_profile_lookup(plan)
            elif plan.operation == "peer_comparison":
                outcome = await self._execute_peer_comparison(plan)
            elif plan.operation == "similarity":
                outcome = await self._execute_similarity(plan)
            else:
                outcome = ExecutionRefusal(message=_UNSUPPORTED_SHAPE_MESSAGE)
            if isinstance(outcome, ExecutionSuccess):
                set_span_attributes(
                    span,
                    row_count=len(outcome.result.rows),
                    result_type=outcome.result.result_type,
                    resolved_metric_ids=_cap_list(
                        [metric.metric_id for metric in outcome.authority.metrics],
                        5,
                    ),
                    resolved_district_ids=_cap_list(
                        outcome.authority.selection.district_ids
                        if outcome.authority.selection is not None
                        else [],
                        5,
                    ),
                    clarification_emitted=False,
                )
            else:
                set_span_attributes(
                    span,
                    row_count=0,
                    result_type="none",
                    resolved_metric_ids=None,
                    resolved_district_ids=None,
                    clarification_emitted=isinstance(outcome, ExecutionClarification),
                )
        updates: dict[str, object] = {}
        if outcome.resolution_report is None:
            updates["resolution_report"] = _resolution_report_from_outcome(
                plan,
                outcome,
            )
        recall_report = self._catalog.drain_recall_report()
        if recall_report is not None:
            updates["recall_report"] = recall_report
        if not updates:
            return outcome
        return outcome.model_copy(update=updates)
