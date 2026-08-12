"""Reports-queue reads over ``compass.case_reports``.

The reports queue (``/compass/quality/reports``) is the reviewer's triage list:
every human pass/partial/fail report filed from the debug bar lands in
``compass.case_reports``, and this module reads it back — read-only, straight
from the canonical schema via ``run_sql``, the same posture as
``compass_conversations.py``. nctqai never writes ``compass.*``; the status
mutation is routed through the backend (see ``routes/compass/quality/reports.py``).

Public API:
    list_reports(...)            -> list[CaseReport]   (filterable queue)
    status_counts()             -> dict[str, int]     (summary by status)

The default view leans on the purpose-built ``case_reports_queue_idx`` index
``(outcome, status, created_at DESC)``: ``outcome IN ('fail','partial')`` AND
``status NOT IN ('resolved','wontfix')``, newest first.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from nctqai.db import COMPASS_SCHEMA, run_sql

logger = logging.getLogger(__name__)

# The triage statuses the DB CHECK allows. Mirrors migration 099 + 168 and the
# backend ``CaseReportStatusUpdate`` Literal so the queue filter and the
# per-row select offer exactly the set the column accepts. ``reviewed_no_followup``
# ("Reviewed – no follow-up", #1806) is a terminal/closed state distinct from
# resolved/wontfix.
STATUSES: tuple[str, ...] = (
    "open",
    "triaged",
    "promoted",
    "resolved",
    "wontfix",
    "reviewed_no_followup",
)

# The three reviewer verdicts the debug widget offers (migration 099 CHECK).
OUTCOMES: tuple[str, ...] = ("pass", "partial", "fail")

# Statuses that drop a report out of the open queue (terminal/closed states).
_CLOSED_STATUSES: tuple[str, ...] = ("resolved", "wontfix", "reviewed_no_followup")


@dataclass(frozen=True)
class CaseReport:
    """One human review report, projected for the queue table."""

    id: UUID
    case_id: int | None
    session_id: UUID
    turn_index: int | None
    trace_id: str | None
    dimension: str | None
    outcome: str
    comments: str | None
    status: str
    reviewer: str | None
    linked_issue: int | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> CaseReport:
        return cls(
            id=row["id"],
            case_id=row["case_id"],
            session_id=row["session_id"],
            turn_index=row["turn_index"],
            trace_id=row["trace_id"],
            dimension=row["dimension"],
            outcome=row["outcome"],
            comments=row["comments"],
            status=row["status"],
            reviewer=row["reviewer"],
            linked_issue=row["linked_issue"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def list_reports(
    *,
    status: str = "",
    dimension: str = "",
    outcome: str = "",
    open_only: bool = True,
    limit: int = 200,
) -> list[CaseReport]:
    """Return review reports newest-first, narrowed by the given filters.

    Args:
        status: Exact status match (one of ``STATUSES``). Empty = no filter.
        dimension: Exact dimension match. Empty = no filter.
        outcome: Exact outcome match (one of ``OUTCOMES``). Empty = no filter.
        open_only: When True (the default queue view) and no explicit status is
            given, restrict to the actionable queue —
            ``outcome IN ('fail','partial')`` AND
            ``status NOT IN ('resolved','wontfix')``. An explicit ``status`` or
            ``outcome`` filter takes precedence over this default so a reviewer
            can inspect e.g. resolved or passing reports on demand.
        limit: Max rows returned.

    Unknown filter values are silently ignored (treated as "no filter") rather
    than erroring — the query params come straight off the URL.
    """
    where: list[str] = []
    binds: list[Any] = []

    if status in STATUSES:
        where.append("status = %s")
        binds.append(status)

    if outcome in OUTCOMES:
        where.append("outcome = %s")
        binds.append(outcome)

    if dimension:
        where.append("dimension = %s")
        binds.append(dimension)

    # Default open-queue narrowing only applies when the reviewer hasn't pinned
    # a specific status/outcome — those explicit filters mean "show me exactly
    # this", including closed or passing reports.
    if open_only and status not in STATUSES and outcome not in OUTCOMES:
        where.append("outcome IN ('fail','partial')")
        where.append("status <> ALL(%s)")
        binds.append(list(_CLOSED_STATUSES))

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = f"""
        SELECT id, case_id, session_id, turn_index, trace_id, dimension,
               outcome, comments, status, reviewer, linked_issue,
               created_at, updated_at
        FROM {COMPASS_SCHEMA}.case_reports
        {where_sql}
        ORDER BY created_at DESC
        LIMIT %s
    """
    rows = run_sql(sql, (*binds, limit))
    return [CaseReport.from_row(r) for r in rows]


def status_counts(
    *,
    dimension: str = "",
    outcome: str = "",
    open_only: bool = True,
) -> dict[str, int]:
    """Return a ``{status: count}`` rollup over the population the list shows.

    Mirrors ``list_reports``' outcome/dimension filtering and default
    actionable-queue outcome scope so the summary pills reconcile with the rows
    on screen: in the default view both restrict to ``outcome IN
    ('fail','partial')``, so the ``open`` pill equals the number of open rows
    (previously the pills counted *all* reports, so an open ``outcome='pass'``
    flag the queue hides still inflated ``open`` — pill said 4, list showed 3).

    The status facet itself is never filtered by status (every ``STATUSES``
    value is zero-filled), so the lifecycle breakdown — including ``resolved`` /
    ``wontfix`` — stays visible regardless of the actionable-outcome scope.
    """
    where: list[str] = []
    binds: list[Any] = []

    if outcome in OUTCOMES:
        where.append("outcome = %s")
        binds.append(outcome)

    if dimension:
        where.append("dimension = %s")
        binds.append(dimension)

    # Same default actionable-outcome scope as list_reports. Only the outcome
    # facet is mirrored — NOT the queue's status exclusion — so closed statuses
    # still appear in the breakdown the pills render.
    if open_only and outcome not in OUTCOMES:
        where.append("outcome IN ('fail','partial')")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    rows = run_sql(
        f"""
        SELECT status, COUNT(*) AS n
        FROM {COMPASS_SCHEMA}.case_reports
        {where_sql}
        GROUP BY status
        """,
        tuple(binds),
    )
    counts = {s: 0 for s in STATUSES}
    for row in rows:
        # Defensive: a status outside the known set (shouldn't happen given the
        # CHECK) still gets surfaced rather than dropped.
        counts[row["status"]] = int(row["n"])
    return counts


def distinct_dimensions() -> list[str]:
    """Return the distinct non-null dimensions present, for the filter select."""
    rows = run_sql(
        f"""
        SELECT DISTINCT dimension
        FROM {COMPASS_SCHEMA}.case_reports
        WHERE dimension IS NOT NULL AND dimension <> ''
        ORDER BY dimension
        """
    )
    return [r["dimension"] for r in rows]
