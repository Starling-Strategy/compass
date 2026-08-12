"""Planner-side diagnostic for `QueryPlan` shapes the executor cannot compile.

Background
==========

The deterministic executor accepts six ``QueryPlan`` operations
(``rank``, ``lookup``, ``count``, ``trend``, ``profile_lookup``,
``peer_comparison``). Each path has a ``*_shape_is_supported(plan) -> bool``
predicate that gates entry. When the LLM-emitted plan fails any of them, the
executor returns ``ExecutionRefusal`` with the constant
``_UNSUPPORTED_SHAPE_MESSAGE``.

This module moves the same check one step upstream — into the planner's
``output_validator`` — so the planner LLM gets a chance to re-roll inside
the same agent call (via ``ModelRetry``) instead of dead-ending in chat.

The authoritative invariants still live in each operation's
``*_shape_is_supported`` predicate; this module's per-operation diagnose
functions add *retry hints* on top of them. If a predicate ever rejects a
plan for a reason the diagnose function does not enumerate, we fall back to
a generic per-operation hint so the LLM still knows which operation is at
fault.
"""

from __future__ import annotations

from compass_backend.contracts.planning import QueryPlan

from .count import count_shape_is_supported
from .lookup import lookup_shape_is_supported
from .peer import peer_comparison_shape_is_supported, similarity_shape_is_supported
from .profile import profile_lookup_shape_is_supported
from .ranking import ranking_shape_is_supported
from .selection import selection_filters_are_supported, state_filters_are_supported
from .trend import trend_shape_is_supported

_SUPPORTED_OPERATION_SCOPES = {
    "all_covered_districts",
    "state",
    "named_districts",
    "largest_districts",
}


def ranking_delegates_to_multi_metric_lookup(plan: QueryPlan) -> bool:
    """Return True when a ``rank`` plan has 2+ execution metrics that the
    executor will delegate to a lookup path instead of pure ranking.

    See ``src/compass_backend/execution/operations.py::_execute_ranking``
    lines 636-646 — multi-metric rank dispatches to ``_execute_lookup`` (no
    limit) or ``_execute_limited_ranked_lookup`` (with limit). Both produce
    a multi-column table ordered by the first primary metric.

    The planner-time validator (``_diagnose_rank``) and the orchestrator-
    level shape guard (``orchestration/chat.py::_plan_likely_dispatchable``)
    must both honor this delegation, or they will pre-empt plans the
    executor would have handled and surface a user-facing refusal instead.
    Sharing the predicate keeps the two layers in lock-step.

    Execution metrics exclude ``filter`` and ``grouping`` roles; those are
    not displayed alongside the rank-by column, so a plan with one primary
    plus one grouping metric is effectively single-metric for ranking.
    """
    if plan.operation != "rank":
        return False
    execution_metrics = [
        metric for metric in plan.metrics if metric.role not in {"filter", "grouping"}
    ]
    return len(execution_metrics) > 1


def unsupported_shape_hint(plan: QueryPlan) -> str | None:
    """Return a retry hint when ``plan`` cannot be executed, else ``None``.

    The hint is intended for the planner's ``ModelRetry`` feedback string —
    actionable enough that the LLM can rewrite a single field or two and
    succeed on its next attempt.
    """

    if plan.operation == "rank":
        return _diagnose_rank(plan)
    if plan.operation == "lookup":
        return _diagnose_lookup(plan)
    if plan.operation == "count":
        return _diagnose_count(plan)
    if plan.operation == "trend":
        return _diagnose_trend(plan)
    if plan.operation == "profile_lookup":
        return _diagnose_profile_lookup(plan)
    if plan.operation == "peer_comparison":
        return _diagnose_peer_comparison(plan)
    if plan.operation == "similarity":
        return _diagnose_similarity(plan)
    # Pydantic's Literal on QueryOperation makes this branch structurally
    # unreachable; the predicate dispatch in execution/executor.py treats it
    # the same way.
    return None  # pragma: no cover


def _diagnose_rank(plan: QueryPlan) -> str | None:
    if ranking_shape_is_supported(plan):
        return None
    # Multi-metric rank is dispatchable via the executor's lookup delegation
    # (see ranking_delegates_to_multi_metric_lookup); skip the planner-time
    # rejection so the executor can produce a multi-column table instead of
    # the user seeing a structure-failure clarification (M3c-1 / #733).
    if ranking_delegates_to_multi_metric_lookup(plan):
        return None
    reasons: list[str] = []
    if plan.selection.scope not in _SUPPORTED_OPERATION_SCOPES:
        reasons.append(
            f"selection.scope `{plan.selection.scope}` is not supported — "
            "use `named_districts`, `state`, `largest_districts`, or "
            "`all_covered_districts`."
        )
    primary_metrics = [m for m in plan.metrics if m.role == "primary"]
    if len(plan.metrics) != 1 or (
        len(primary_metrics) != 1 or primary_metrics[0].role != "primary"
    ):
        reasons.append(
            "ranking requires exactly one primary metric — choose the single "
            "metric to rank by, or switch to `lookup` if you need multiple "
            f"metrics (received {len(plan.metrics)} metric(s))."
        )
    if not state_filters_are_supported(plan):
        reasons.append(
            "ranking supports state-scoped and metric-value threshold filters; "
            "enrollment-bounds filters are not supported for rank — move "
            "districts into `selection.districts` with "
            "`scope='named_districts'` or switch to `lookup`."
        )
    if not reasons:
        return (
            "Operation `rank` cannot be compiled with the current plan "
            "shape. Re-check selection, metric role/count, and filters."
        )
    return "Operation `rank` cannot be compiled: " + " ".join(reasons)


def _diagnose_lookup(plan: QueryPlan) -> str | None:
    if lookup_shape_is_supported(plan):
        return None
    reasons: list[str] = []
    if plan.selection.scope not in _SUPPORTED_OPERATION_SCOPES:
        reasons.append(
            f"selection.scope `{plan.selection.scope}` is not supported — "
            "use `named_districts`, `state`, `largest_districts`, or "
            "`all_covered_districts`."
        )
    if plan.limit is not None and plan.selection.scope != "largest_districts":
        # `kind="all"` IS valid for all_covered_districts / state / named_districts
        # (chart and CSV-export use cases, plus the multi-turn inheritance path
        # where prior_result_rows materializes named_districts and preserves
        # the planner's `kind="all"`). Numeric counts are only valid for
        # largest_districts.
        if plan.limit.kind == "all" and plan.selection.scope in {
            "all_covered_districts",
            "state",
            "named_districts",
        }:
            pass  # supported shape; no diagnostic
        else:
            reasons.append(
                "lookup with `selection.scope='"
                f"{plan.selection.scope}'` accepts only `LimitSpec(kind=\"all\")` "
                "(returns every matching row, e.g. for charts) — switch the "
                "limit to `kind=\"all\"` with `count=None`, or use "
                "`selection.scope='largest_districts'` with a numeric count."
            )
    if not selection_filters_are_supported(plan):
        reasons.append(
            "lookup filters must be state-scoped, enrollment-bounds, or "
            "metric-value threshold filters; remove unsupported filter kinds "
            "or move district names into selection."
        )
    primary_metrics = [m for m in plan.metrics if m.role == "primary"]
    if not primary_metrics:
        reasons.append(
            "lookup requires at least one metric with role `primary`."
        )
    if not reasons:
        return (
            "Operation `lookup` cannot be compiled with the current plan "
            "shape. Re-check selection, metric roles, limit, sort, and filters."
        )
    return "Operation `lookup` cannot be compiled: " + " ".join(reasons)


def _diagnose_count(plan: QueryPlan) -> str | None:
    if count_shape_is_supported(plan):
        return None
    reasons: list[str] = []
    if plan.selection.scope not in _SUPPORTED_OPERATION_SCOPES:
        reasons.append(
            f"selection.scope `{plan.selection.scope}` is not supported — "
            "use `named_districts`, `state`, `largest_districts`, or "
            "`all_covered_districts`."
        )
    if not plan.metrics:
        reasons.append(
            "count requires at least one metric — name the metric whose "
            "values you want to count."
        )
    # The count predicate also gates on filter operators; collapse the
    # message rather than re-implementing per-operator logic.
    if not reasons:
        return (
            "Operation `count` cannot be compiled — check that filters use "
            "supported operators (state-scope filters, or equals/not_equals/"
            "comparison operators on metric value)."
        )
    return "Operation `count` cannot be compiled: " + " ".join(reasons)


def _diagnose_trend(plan: QueryPlan) -> str | None:
    if trend_shape_is_supported(plan):
        return None
    reasons: list[str] = []
    if plan.selection.scope not in _SUPPORTED_OPERATION_SCOPES:
        reasons.append(
            f"selection.scope `{plan.selection.scope}` is not supported — "
            "use `named_districts`, `state`, `largest_districts`, or "
            "`all_covered_districts`."
        )
    if plan.sort is not None or plan.sort_steps:
        reasons.append(
            "trend does not accept manual sort — remove `sort` and "
            "`sort_steps`; trends are returned in year order automatically."
        )
    if plan.limit is not None and plan.selection.scope != "largest_districts":
        reasons.append(
            "trend only accepts `limit` when `selection.scope='largest_districts'`."
        )
    primary_metrics = [m for m in plan.metrics if m.role == "primary"]
    if len(primary_metrics) != 1:
        reasons.append(
            "trend requires exactly one primary metric to plot over time "
            f"(received {len(primary_metrics)} primary metric(s))."
        )
    if not selection_filters_are_supported(plan):
        reasons.append(
            "trend filters must be state-scoped, enrollment-bounds, or "
            "metric-value threshold filters."
        )
    if not reasons:
        return (
            "Operation `trend` cannot be compiled with the current plan "
            "shape. Re-check selection, sort, limit, metric count, and filters."
        )
    return "Operation `trend` cannot be compiled: " + " ".join(reasons)


def _diagnose_profile_lookup(plan: QueryPlan) -> str | None:
    if profile_lookup_shape_is_supported(plan):
        return None
    reasons: list[str] = []
    if plan.selection.scope != "named_districts":
        reasons.append(
            "profile_lookup only supports `selection.scope='named_districts'` "
            "with one or more districts listed."
        )
    if not plan.profile_fields:
        reasons.append(
            "profile_lookup requires at least one entry in `profile_fields` — "
            "name the NCES/demographic field you want to look up."
        )
    if plan.sort is not None or plan.sort_steps:
        reasons.append(
            "profile_lookup does not accept manual sort — remove `sort` and "
            "`sort_steps`."
        )
    if plan.limit is not None:
        reasons.append(
            "profile_lookup does not accept `limit` — list the districts you "
            "want directly in `selection.districts`."
        )
    if not selection_filters_are_supported(plan):
        reasons.append(
            "profile_lookup filters must be state-scoped, enrollment-bounds, "
            "or metric-value threshold filters."
        )
    if not reasons:
        return (
            "Operation `profile_lookup` cannot be compiled with the current "
            "plan shape."
        )
    return "Operation `profile_lookup` cannot be compiled: " + " ".join(reasons)


def _diagnose_peer_comparison(plan: QueryPlan) -> str | None:
    if peer_comparison_shape_is_supported(plan):
        return None
    reasons: list[str] = []
    if plan.selection.scope != "named_districts":
        reasons.append(
            "peer_comparison only supports `selection.scope='named_districts'` "
            "with a single anchor district."
        )
    if len(plan.selection.districts) != 1:
        reasons.append(
            "peer_comparison takes exactly one anchor district "
            f"(received {len(plan.selection.districts)}); split the "
            "comparison into one peer_comparison per anchor."
        )
    if not plan.metrics:
        reasons.append(
            "peer_comparison requires at least one metric to compare."
        )
    if plan.sort is not None or plan.sort_steps:
        reasons.append(
            "peer_comparison does not accept manual sort — remove `sort` and "
            "`sort_steps`."
        )
    if not selection_filters_are_supported(plan):
        reasons.append(
            "peer_comparison filters must be state-scoped, enrollment-bounds, "
            "or metric-value threshold filters."
        )
    if not reasons:
        return (
            "Operation `peer_comparison` cannot be compiled with the current "
            "plan shape."
        )
    return "Operation `peer_comparison` cannot be compiled: " + " ".join(reasons)


def _diagnose_similarity(plan: QueryPlan) -> str | None:
    if similarity_shape_is_supported(plan):
        return None
    reasons: list[str] = []
    if plan.similarity is None:
        reasons.append(
            "operation='similarity' requires a `similarity` payload with "
            "`anchor_name`. Add a `SimilarityQuerySpec` to the plan."
        )
    if plan.selection.scope != "named_districts":
        reasons.append(
            "similarity only supports `selection.scope='named_districts'` "
            "with a single anchor district."
        )
    if len(plan.selection.districts) != 1:
        reasons.append(
            "similarity takes exactly one anchor district "
            f"(received {len(plan.selection.districts)}); provide a single "
            "district in `selection.districts`."
        )
    if plan.sort is not None or plan.sort_steps:
        reasons.append(
            "similarity does not accept manual sort — remove `sort` and "
            "`sort_steps`; peers are returned in NCES similarity score order."
        )
    if not selection_filters_are_supported(plan):
        reasons.append(
            "similarity filters must be state-scoped, enrollment-bounds, "
            "or metric-value threshold filters; use `similarity.exclude_states` "
            "to exclude candidates by state."
        )
    if not reasons:
        return (
            "Operation `similarity` cannot be compiled with the current "
            "plan shape. Re-check selection, sort, and similarity payload."
        )
    return "Operation `similarity` cannot be compiled: " + " ".join(reasons)
