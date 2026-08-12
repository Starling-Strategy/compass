"""Validator group split from quality/validation.py.

Each function returns a list of ValidationFinding. The module is private
in spirit (consumed only by quality/validation.py); names retain their
underscore prefix from the original monolith.
"""

from __future__ import annotations

import re

from compass_backend.artifacts import (
    COUNT_ROW_TYPES,
    ResultSet,
)
from compass_backend.contracts.planning import MetricSpec, QueryPlan
from compass_backend.contracts.validation import (
    ValidationAuthority,
    ValidationFinding,
)

from .._validation_helpers import (
    _expected_count_filter_statement,
    _finding,
)
from .coverage import _validate_metric_authority


def _validate_metric(
    plan: QueryPlan,
    result: ResultSet,
    authority: ValidationAuthority | None,
) -> list[ValidationFinding]:
    # #1242 WS-3: a similarity (peer-set DISCOVERY) result carries no policy
    # metric — every row uses the metric_id=0 sentinel
    # (PeerComparisonResult.is_similarity_result) and the renderer dispatches it
    # to _render_similarity. Metric validation assumes a metric-bearing result
    # and would false-fire `unsupported_metric_plan` on a valid peer set
    # (S101/S94 manifest_validation_failed). Selection grounding and peer order
    # are validated elsewhere, so the peer set is still fully checked.
    if getattr(result, "is_similarity_result", False):
        return []
    if result.result_type in {"metric_lookup", "peer_comparison"}:
        return _validate_lookup_metrics(plan, result, authority)
    if result.result_type == "metric_trend":
        return _validate_trend_metrics(plan, result, authority)
    if result.result_type == "metric_count":
        return _validate_count_metrics(plan, result, authority)
    if result.result_type == "profile_lookup":
        return _validate_profile_fields(plan, result, authority)

    findings: list[ValidationFinding] = _validate_profile_sort_artifact(
        plan, result, authority
    )
    result_metrics = _ranking_result_metric_specs(plan, result, authority)
    # #1212: a cross-topic ranking may carry one primary policy metric plus
    # comparison columns (e.g. rank by starting salary, show years of experience
    # side by side). Mirror the lookup branch — accept one primary plus
    # comparisons — instead of demanding exactly one metric. Row-level
    # consistency is still enforced below by ``metric_row_mismatch``, so a
    # genuinely broken multi-metric ranking does not slip through.
    if (
        not result_metrics
        or result_metrics[0].role != "primary"
        or any(metric.role != "comparison" for metric in result_metrics[1:])
    ):
        findings.append(
            _finding(
                code="unsupported_metric_plan",
                dimension="metric",
                message="Validation supports one primary metric plus comparisons.",
                metadata={"metric_count": len(result_metrics)},
            )
        )
    if not result.rows:
        return findings

    first_metric_id = result.rows[0].metric_id
    first_metric_name = result.rows[0].metric_name
    for row in result.rows[1:]:
        if row.metric_id != first_metric_id or row.metric_name != first_metric_name:
            findings.append(
                _finding(
                    code="metric_row_mismatch",
                    dimension="metric",
                    message="Ranking rows do not all reference the same metric.",
                    metadata={
                        "expected_metric_id": first_metric_id,
                        "actual_metric_id": row.metric_id,
                        "district_id": row.district_id,
                    },
                )
            )
            break
    findings.extend(_validate_metric_authority(result, authority))
    return findings


def _validate_trend_metrics(
    plan: QueryPlan,
    result: ResultSet,
    authority: ValidationAuthority | None,
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    result_metrics = _lookup_result_metric_specs(plan)
    if len(result_metrics) != 1 or result_metrics[0].role != "primary":
        findings.append(
            _finding(
                code="unsupported_metric_plan",
                dimension="metric",
                message="Trend validation supports exactly one primary metric.",
                metadata={"metric_count": len(result_metrics)},
            )
        )

    seen_cells: set[tuple[int, int, str]] = set()
    duplicate_cells: list[tuple[int, int, str]] = []
    for row in result.rows:
        if row.district_id is None:
            continue
        cell = (row.district_id, row.metric_id, row.academic_year)
        if cell in seen_cells:
            duplicate_cells.append(cell)
        seen_cells.add(cell)

    if duplicate_cells:
        findings.append(
            _finding(
                code="metric_trend_duplicate",
                dimension="metric",
                message="Trend rows contain duplicate district/metric/year cells.",
                metadata={"cells": duplicate_cells},
            )
        )

    findings.extend(_validate_metric_authority(result, authority))
    return findings


def _validate_lookup_metrics(
    plan: QueryPlan,
    result: ResultSet,
    authority: ValidationAuthority | None,
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    result_metrics = _lookup_result_metric_specs(plan)
    if (
        not result_metrics
        or result_metrics[0].role != "primary"
        or any(
            metric.role not in {"primary", "comparison"}
            for metric in result_metrics[1:]
        )
    ):
        findings.append(
            _finding(
                code="unsupported_metric_plan",
                dimension="metric",
                message="Lookup validation supports one primary metric plus comparisons.",
                metadata={"metric_count": len(result_metrics)},
            )
        )

    seen_pairs: set[tuple[int, int]] = set()
    duplicate_pairs: list[tuple[int, int]] = []
    for row in result.rows:
        if row.district_id is None:
            continue
        pair = (row.district_id, row.metric_id)
        if pair in seen_pairs:
            duplicate_pairs.append(pair)
        seen_pairs.add(pair)

    if duplicate_pairs:
        findings.append(
            _finding(
                code="metric_lookup_duplicate",
                dimension="metric",
                message="Lookup rows contain duplicate district/metric pairs.",
                metadata={"pairs": duplicate_pairs},
            )
        )

    findings.extend(_validate_metric_authority(result, authority))
    return findings


def _validate_profile_fields(
    plan: QueryPlan,
    result: ResultSet,
    authority: ValidationAuthority | None,
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    if not plan.profile_fields:
        findings.append(
            _finding(
                code="unsupported_profile_field_plan",
                dimension="metric",
                message="Profile lookup validation requires a planned profile field.",
                metadata={},
            )
        )
    field_keys = {
        getattr(row, "field_key", None)
        for row in result.rows
        if getattr(row, "field_key", None)
    }
    # Authority is only available on the chat-time validation path (passed by
    # `validate_and_render`); the L2 dimension-bridge dispatch in
    # `quality/validators/registry.py` invokes `validate_result()` without
    # threading authority through. Treat "no authority" as "this validator
    # cannot perform the authority comparison from this call site" — emit no
    # finding rather than failing every profile-lookup turn. The chat-time
    # call still enforces authority because it does pass an authority object.
    if authority is None:
        return findings
    authorized_field_keys = {field.field_key for field in authority.profile_fields}
    if not authorized_field_keys:
        findings.append(
            _finding(
                code="missing_profile_field_authority",
                dimension="metric",
                message="Profile lookup validation requires resolved field authority.",
                metadata={},
            )
        )
    elif not field_keys <= authorized_field_keys:
        findings.append(
            _finding(
                code="profile_field_authority_mismatch",
                dimension="metric",
                message="Profile rows do not match resolved profile-field authority.",
                metadata={
                    "expected_field_keys": sorted(authorized_field_keys),
                    "actual_field_keys": sorted(field_keys),
                },
            )
        )
    return findings


def _lookup_result_metric_specs(plan: QueryPlan):
    return [
        metric
        for metric in plan.metrics
        if metric.role not in {"filter", "grouping"}
    ]


def _ranking_result_metric_specs(
    plan: QueryPlan,
    result: ResultSet,
    authority: ValidationAuthority | None = None,
):
    # #933: a metric surfaced as several variant clarification candidates can
    # land in the plan as duplicate comparison specs that all resolve to the
    # same metric. Collapse a comparison that duplicates an already-kept
    # result-role metric (same normalized name + same degree_lane), keeping the
    # first occurrence. The degree_lane equality keeps #874's explicit BA/MA
    # variants distinct (different lanes never collapse).
    kept: list[MetricSpec] = []
    seen_keys: set[tuple[str, str | None]] = set()
    for metric in plan.metrics:
        if metric.role in {"filter", "grouping"}:
            continue
        if _is_profile_sort_comparison_metric(metric, plan, result, authority):
            continue
        key = (_normalize_profile_field_phrase(metric.name), metric.degree_lane)
        if metric.role == "comparison" and key in seen_keys:
            continue
        seen_keys.add(key)
        kept.append(metric)
    return kept


def _is_profile_sort_comparison_metric(
    metric: MetricSpec,
    plan: QueryPlan,
    result: ResultSet,
    authority: ValidationAuthority | None,
) -> bool:
    if metric.role != "comparison":
        return False
    if not _profile_sort_steps(plan):
        return False
    return _profile_sort_artifact_matches_expected(plan, result, authority)


def _validate_profile_sort_artifact(
    plan: QueryPlan,
    result: ResultSet,
    authority: ValidationAuthority | None,
) -> list[ValidationFinding]:
    if not _profile_sort_steps(plan) or not result.rows:
        return []

    findings: list[ValidationFinding] = []
    missing_rows = [
        {
            "district_id": row.district_id,
            "missing_fields": _missing_profile_sort_fields(row),
        }
        for row in result.rows
        if _missing_profile_sort_fields(row)
    ]
    if missing_rows:
        findings.append(
            _finding(
                code="profile_sort_artifact_missing",
                dimension="metric",
                message=(
                    "Profile-field ranking rows must carry profile sort "
                    "artifact fields."
                ),
                metadata={"rows": missing_rows},
            )
        )
        return findings

    expected_labels = _profile_sort_expected_artifacts(plan, authority)
    if not expected_labels:
        findings.append(
            _finding(
                code="missing_profile_sort_authority",
                dimension="metric",
                message="Profile-field sort validation requires resolved authority.",
                metadata={},
            )
        )
        return findings

    mismatched_rows = []
    for row in result.rows:
        if not _profile_sort_row_matches(row, expected_labels):
            mismatched_rows.append(
                {
                    "district_id": row.district_id,
                    "sort_metric_name": getattr(row, "sort_metric_name", None),
                }
            )
    if mismatched_rows:
        findings.append(
            _finding(
                code="profile_sort_artifact_mismatch",
                dimension="metric",
                message=(
                    "Profile-field ranking rows do not match the requested "
                    "profile sort field."
                ),
                metadata={
                    "expected_profile_fields": sorted(expected_labels),
                    "rows": mismatched_rows,
                },
            )
        )
    return findings


def _profile_sort_steps(plan: QueryPlan) -> list:
    profile_steps = [
        step for step in plan.sort_steps if step.key_type == "profile_field"
    ]
    presentation_steps = [
        step for step in profile_steps if step.phase == "presentation"
    ]
    if presentation_steps:
        return presentation_steps
    # Selection-phase profile sorts prove the candidate pool. When a later
    # policy-metric presentation sort exists, row sort artifacts describe the
    # displayed order instead.
    if any(
        step.phase == "presentation" and step.key_type == "policy_metric"
        for step in plan.sort_steps
    ):
        return []
    return profile_steps


def _profile_sort_expected_artifacts(
    plan: QueryPlan,
    authority: ValidationAuthority | None = None,
) -> set[str]:
    if authority is not None:
        authority_fields = _profile_sort_authority_fields(plan, authority)
        return {
            _normalize_profile_field_phrase(value)
            for field in authority_fields
            for value in (field.label, field.input_phrase, field.field_key)
        }

    return {
        _normalize_profile_field_phrase(step.field)
        for step in _profile_sort_steps(plan)
        if step.field
    }


def _profile_sort_authority_fields(
    plan: QueryPlan,
    authority: ValidationAuthority,
):
    step_names = {
        _normalize_profile_field_phrase(step.field) for step in _profile_sort_steps(plan)
    }
    return [
        field
        for field in authority.profile_fields
        if _authority_field_matches_sort_step(field, step_names)
    ]


def _authority_field_matches_sort_step(
    field,
    step_names: set[str],
) -> bool:
    return any(
        _normalize_profile_field_phrase(value) in step_names
        for value in (field.field_key, field.label, field.input_phrase)
    )


def _profile_sort_artifact_matches_expected(
    plan: QueryPlan,
    result: ResultSet,
    authority: ValidationAuthority | None,
) -> bool:
    if not result.rows or any(_missing_profile_sort_fields(row) for row in result.rows):
        return False
    expected_labels = _profile_sort_expected_artifacts(plan, authority)
    if not expected_labels:
        return False
    return all(_profile_sort_row_matches(row, expected_labels) for row in result.rows)


def _profile_sort_row_matches(
    row: object,
    expected_labels: set[str],
) -> bool:
    sort_metric_name = _normalize_profile_field_phrase(
        str(getattr(row, "sort_metric_name", "") or "")
    )
    return sort_metric_name in expected_labels


def _missing_profile_sort_fields(row: object) -> list[str]:
    missing = []
    if getattr(row, "sort_value", None) is None:
        missing.append("sort_value")
    if not getattr(row, "sort_metric_name", None):
        missing.append("sort_metric_name")
    if not getattr(row, "sort_display_value", None):
        missing.append("sort_display_value")
    return missing


def _normalize_profile_field_phrase(value: str) -> str:
    normalized = value.casefold().replace("&", " and ")
    normalized = normalized.replace("-", " ").replace("_", " ")
    compact = re.sub(r"[^a-z0-9]+", " ", normalized).strip()
    tokens = set(compact.split())
    if "frpl" in tokens or {"free", "reduced", "lunch"}.issubset(tokens):
        return "frpl_pct"
    return compact


def _validate_count_metrics(
    plan: QueryPlan,
    result: ResultSet,
    authority: ValidationAuthority | None,
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    # covered_universe_count counts the covered-district universe itself and
    # carries no metric (enforced by the planner-side validator). Skip the
    # "needs a metric" finding for this typed count shape.
    requires_planned_metric = plan.count_kind != "covered_universe_count"
    if requires_planned_metric and not plan.metrics:
        findings.append(
            _finding(
                code="unsupported_metric_plan",
                dimension="metric",
                message="Count validation requires at least one planned metric.",
                metadata={"metric_count": 0},
            )
        )
    if any(not isinstance(row, COUNT_ROW_TYPES) for row in result.rows):
        findings.append(
            _finding(
                code="count_row_type_mismatch",
                dimension="metric",
                message="Metric count results must contain CountRow artifacts.",
                metadata={"result_type": result.result_type},
            )
        )
    findings.extend(_validate_metric_authority(result, authority))
    return findings


def _validate_filter(
    plan: QueryPlan,
    result: ResultSet,
) -> list[ValidationFinding]:
    if result.result_type != "metric_count":
        return []

    mismatched_rows = []
    for row in result.rows:
        if not isinstance(row, COUNT_ROW_TYPES):
            continue
        expected_statement = _expected_count_filter_statement(plan, row)
        if row.filter_statement != expected_statement:
            mismatched_rows.append(
                {
                    "metric_id": row.metric_id,
                    "expected": expected_statement,
                    "actual": row.filter_statement,
                }
            )

    if not mismatched_rows:
        return []
    return [
        _finding(
            code="filter_statement_mismatch",
            dimension="filter",
            message="Count filter statements must match the deterministic query plan.",
            metadata={"rows": mismatched_rows},
        )
    ]
