"""Shared deterministic-render helpers (#1419 single-source).

``selection_label`` and ``sort_metric_name`` were byte-identical (or, for
``sort_metric_name``, behavior-equivalent) across ``rendering.writer`` and
``rendering.composer``. They live here as the single source both modules import.

This module must not import ``writer`` or ``composer`` (cycle safety).
"""

from __future__ import annotations

from compass_backend.artifacts import ResultSet
from compass_backend.contracts.planning import QueryPlan
from compass_backend.execution.selection import requested_states as _requested_states


def selection_label(plan: QueryPlan) -> str:
    """Return the human phrase for the plan's district selection.

    ``"selected districts"`` for named/largest-district scopes; otherwise
    ``"covered districts"`` optionally narrowed to the requested states.
    """

    if plan.selection.scope in {"named_districts", "largest_districts"}:
        return "selected districts"
    states = sorted(_requested_states(plan))
    if not states:
        return "covered districts"
    return f"covered districts in {', '.join(states)}"


def sort_metric_name(result: ResultSet) -> str | None:
    """Return the human-facing sort-metric label for the lead and table header.

    Prefers ``sort_metric_label`` (the human title, e.g. ``"FRPL %"``) and falls
    back to ``sort_metric_name`` when no label is set. ``sort_metric_name`` on a
    profile-field ranking row carries the *machine* ``field_key`` (e.g.
    ``"frpl_pct"``) for the sort-proof validator, so reading it directly here
    leaked the database token into the answer lead and produced a duplicate
    table column (#1495). The policy-metric ranking path leaves
    ``sort_metric_label`` unset and stores the human name in
    ``sort_metric_name``, so the fallback preserves its prior behavior.

    Uses ``getattr`` defensively so result variants whose rows lack these
    attributes are treated as having no sort metric (the composer's prior
    behavior, now the single source).
    """

    for row in result.rows:
        value = getattr(row, "sort_metric_label", None) or getattr(
            row, "sort_metric_name", None
        )
        if value:
            return value
    return None
