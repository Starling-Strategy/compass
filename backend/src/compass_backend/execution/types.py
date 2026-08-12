"""Shared execution contracts for deterministic Compass operations."""

from __future__ import annotations

from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from compass_backend.artifacts import ResultSet
from compass_backend.catalog import CatalogResolutionReport, MetricCandidate, RecallReport
from compass_backend.contracts.planning import ClarificationRequest, QueryPlan
from compass_backend.contracts.validation import ValidationAuthority
from compass_backend.db.rows import MetricAnswerRow  # re-exported for back-compat

__all__ = [
    "EXECUTION_OUTCOME_TYPES",
    "ExecutionClarification",
    "ExecutionOutcome",
    "ExecutionRefusal",
    "ExecutionSuccess",
    "MetricAnswerRow",
    "PolicyAnswerRepository",
    "QueryExecutor",
]


class ExecutionSuccess(BaseModel):
    """Deterministic execution produced rows and a validation authority."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["success"] = "success"
    result: ResultSet
    authority: ValidationAuthority
    message: str
    resolution_report: CatalogResolutionReport | None = None
    recall_report: RecallReport | None = None
    # Metric candidates the adjudicator marked as adjacent variants of the
    # resolved primary metric (populated only for lookup when the adjudicator
    # emits action='select_with_alternates').  The answer stylist reads these
    # from manifest.metadata["adjacent_metrics"] after rendering.
    adjacent_candidates: list[MetricCandidate] = Field(default_factory=list)

    # Read-side compatibility: callers that use the legacy "is not None" idiom
    # (mostly tests) can still ask whether the outcome is a clarification.
    @property
    def clarification(self) -> None:
        return None


class ExecutionClarification(BaseModel):
    """Deterministic execution needs the user to disambiguate before proceeding."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["clarification"] = "clarification"
    clarification: ClarificationRequest
    message: str
    resolution_report: CatalogResolutionReport | None = None
    recall_report: RecallReport | None = None
    # #1348 (m1): the specific metric phrase this clarify is disambiguating, when
    # the open slot is one ambiguous metric. Lets the pending-context builder keep
    # the OTHER already-resolved metrics in a multi-metric plan (e.g. "salary AND
    # planning time") instead of dropping them when it captures the candidate set.
    # None for non-metric clarifies (district, scope, …).
    ambiguous_metric_phrase: str | None = None

    @property
    def result(self) -> None:
        return None

    @property
    def authority(self) -> None:
        return None


class ExecutionRefusal(BaseModel):
    """Deterministic execution declined to answer (unsupported shape or no data)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["refusal"] = "refusal"
    message: str
    resolution_report: CatalogResolutionReport | None = None
    recall_report: RecallReport | None = None

    @property
    def result(self) -> None:
        return None

    @property
    def authority(self) -> None:
        return None

    @property
    def clarification(self) -> None:
        return None


ExecutionOutcome = Annotated[
    ExecutionSuccess | ExecutionClarification | ExecutionRefusal,
    Field(discriminator="kind"),
]

# isinstance-checkable tuple of variant classes. The Annotated alias above is
# a typing construct, not a runtime class — `isinstance(x, ExecutionOutcome)`
# raises TypeError. Use this tuple in runtime guards instead.
EXECUTION_OUTCOME_TYPES: tuple[type, ...] = (
    ExecutionSuccess,
    ExecutionClarification,
    ExecutionRefusal,
)


class PolicyAnswerRepository(Protocol):
    """Read-only answer-row boundary used by deterministic executors."""

    async def fetch_metric_answer_rows(
        self,
        *,
        metric_id: int,
        academic_year: str,
    ) -> list[MetricAnswerRow]:
        """Return covered-district answer rows for one metric and year."""

    async def list_available_academic_years(
        self,
        *,
        metric_id: int,
        district_ids: set[int],
    ) -> list[str]:
        """Return reviewed academic years for one metric and selected districts."""

    async def fetch_metric_answer_rows_for_year(
        self,
        *,
        metric_id: int,
        academic_year: str,
        district_ids: set[int],
    ) -> list[MetricAnswerRow]:
        """Return selected-district answer rows for one metric and academic year."""

    async def fetch_metric_answer_rows_for_years(
        self,
        *,
        metric_id: int,
        academic_years: list[str],
        district_ids: set[int],
    ) -> list[MetricAnswerRow]:
        """Return selected-district answer rows for one metric and year list."""

    async def fetch_reviewed_district_ids(
        self,
        *,
        academic_year: str,
        district_ids: set[int],
    ) -> set[int]:
        """Return selected district IDs that have any row for one year."""

    async def fetch_recent_metric_answer_rows(
        self,
        *,
        metric_id: int,
        before_academic_year: str,
        district_ids: set[int],
    ) -> list[MetricAnswerRow]:
        """Return the latest prior metric rows for selected districts."""


class QueryExecutor(Protocol):
    """Execution boundary consumed by the chat route."""

    async def execute(self, plan: QueryPlan) -> ExecutionOutcome:
        """Execute a typed query plan when a deterministic shape is supported."""
