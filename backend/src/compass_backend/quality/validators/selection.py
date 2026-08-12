"""Validator group split from quality/validation.py.

Each function returns a list of ValidationFinding. The module is private
in spirit (consumed only by quality/validation.py); names retain their
underscore prefix from the original monolith.
"""

from __future__ import annotations


from compass_backend.artifacts import (
    COUNT_ROW_TYPES,
    ResultSet,
)
from compass_backend.contracts.planning import QueryPlan
from compass_backend.contracts.validation import (
    ValidationAuthority,
    ValidationFinding,
)
from compass_backend.execution.selection import requested_states as _requested_states
from compass_backend.reference import normalize_state as _normalize_state

from .._validation_helpers import _finding
from .intended_geography import _validate_intended_geography


def _validate_selection(
    plan: QueryPlan,
    result: ResultSet,
    authority: ValidationAuthority | None,
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    if result.result_type not in {
        "metric_ranking",
        "metric_lookup",
        "metric_count",
        "metric_trend",
        "profile_lookup",
        "peer_comparison",
    }:
        findings.append(
            _finding(
                code="unsupported_result_type",
                dimension="selection",
                message="Validation does not support this result type.",
                metadata={"result_type": result.result_type},
            )
        )
    if plan.selection.scope not in {
        "all_covered_districts",
        "state",
        "named_districts",
        "largest_districts",
    }:
        findings.append(
            _finding(
                code="unsupported_selection_scope",
                dimension="selection",
                message="The result uses a selection scope MVP 6 cannot validate.",
                metadata={"scope": plan.selection.scope},
            )
        )
        return findings

    if plan.selection.scope in {"named_districts", "largest_districts"}:
        findings.extend(
            _validate_resolved_district_selection(
                result,
                authority,
                expected_scope=plan.selection.scope,
            )
        )
        # Independent Gate-3 (#1248): the closed-loop check above compares the
        # result against authority.selection, but both derive from the same
        # resolver output. This check re-derives the user's intended geography
        # from the typed prose (independent oracle) so a real-but-wrong district
        # in the wrong state is caught, not rubber-stamped.
        findings.extend(_validate_intended_geography(plan, result))

    requested_states = _requested_states(plan)
    if result.result_type == "metric_count":
        findings.extend(_validate_count_selection(result, authority, requested_states))
    elif requested_states:
        mismatched_rows = [
            row.district_id
            for row in result.rows
            if _normalize_state(row.state) not in requested_states
        ]
        if mismatched_rows:
            findings.append(
                _finding(
                    code="selection_state_mismatch",
                    dimension="selection",
                    message=(
                        "Result rows include districts outside the requested "
                        "state scope."
                    ),
                    metadata={
                        "states": sorted(requested_states),
                        "district_ids": mismatched_rows,
                    },
                )
            )
    return findings


def _validate_count_selection(
    result: ResultSet,
    authority: ValidationAuthority | None,
    requested_states: set[str],
) -> list[ValidationFinding]:
    selection = result.selection
    if selection is None or (
        requested_states
        and not selection.states
        and not selection.districts
    ):
        return [
            _finding(
                code="selection_metadata_missing",
                dimension="selection",
                message=(
                    "Count results must carry resolved selection metadata before "
                    "validation can verify district membership."
                ),
                metadata={"states": sorted(requested_states)},
            )
        ]

    findings: list[ValidationFinding] = []
    if requested_states:
        selection_states = {
            _normalize_state(state)
            for state in selection.states
            if _normalize_state(state)
        }
        extra_selection_states = sorted(selection_states - requested_states)
        mismatched_selection_districts = [
            district.district_id
            for district in selection.districts
            if _normalize_state(district.state) not in requested_states
        ]
        if extra_selection_states or mismatched_selection_districts:
            findings.append(
                _finding(
                    code="selection_state_mismatch",
                    dimension="selection",
                    message=(
                        "Count selection metadata includes districts or states outside "
                        "the requested state scope."
                    ),
                    metadata={
                        "states": sorted(requested_states),
                        "selection_states": extra_selection_states,
                        "district_ids": mismatched_selection_districts,
                    },
                )
            )

    selected_district_ids = {
        district.district_id for district in selection.districts
    }
    qualifying_ids = {
        district_id
        for row in result.rows
        if isinstance(row, COUNT_ROW_TYPES)
        for district_id in row.qualifying_district_ids
    }
    if qualifying_ids and not selected_district_ids:
        findings.append(
            _finding(
                code="selection_metadata_missing",
                dimension="selection",
                message=(
                    "State-scoped count results with qualifying districts must "
                    "carry resolved district selection metadata."
                ),
                metadata={"qualifying_district_ids": sorted(qualifying_ids)},
            )
        )
    elif qualifying_ids:
        outside_selection_ids = sorted(qualifying_ids - selected_district_ids)
        if outside_selection_ids:
            findings.append(
                _finding(
                    code="selection_district_mismatch",
                    dimension="selection",
                    message=(
                        "Count qualifying districts include IDs outside the "
                        "resolved selection."
                    ),
                    metadata={
                        "selected_district_ids": sorted(selected_district_ids),
                        "district_ids": outside_selection_ids,
                    },
                )
            )

    authority_selection = authority.selection if authority is not None else None
    if authority_selection is not None:
        authority_selected_ids = set(authority_selection.district_ids)
        unauthorized_selection_ids = sorted(
            selected_district_ids - authority_selected_ids
        )
        if unauthorized_selection_ids:
            findings.append(
                _finding(
                    code="selection_authority_mismatch",
                    dimension="selection",
                    message=(
                        "Count selection metadata includes districts outside the "
                        "resolved catalog selection."
                    ),
                    metadata={
                        "resolved_district_ids": sorted(authority_selected_ids),
                        "selection_district_ids": unauthorized_selection_ids,
                    },
                )
            )
    return findings


def _validate_resolved_district_selection(
    result: ResultSet,
    authority: ValidationAuthority | None,
    *,
    expected_scope: str,
) -> list[ValidationFinding]:
    if (
        result.selection is None
        or result.selection.scope != expected_scope
        or (
            not result.selection.districts
            and not result.selection.unresolved_districts
        )
    ):
        return [
            _finding(
                code="selection_metadata_missing",
                dimension="selection",
                message=(
                    "Selected-district results must carry resolved selection metadata."
                ),
                metadata={"scope": expected_scope},
            )
        ]

    artifact_selected_ids = {
        district.district_id for district in result.selection.districts
    }
    mismatched_rows = [
        row.district_id
        for row in result.rows
        if row.district_id is not None
        and row.district_id not in artifact_selected_ids
    ]
    findings: list[ValidationFinding] = []
    if mismatched_rows:
        findings.append(
            _finding(
                code="selection_district_mismatch",
                dimension="selection",
                message="Result rows include districts outside the resolved selection.",
                metadata={
                    "selected_district_ids": sorted(artifact_selected_ids),
                    "district_ids": mismatched_rows,
                },
            )
        )

    authority_selection = authority.selection if authority is not None else None
    if authority_selection is None or authority_selection.scope != expected_scope:
        return findings

    authority_selected_ids = set(authority_selection.district_ids)
    unauthorized_selection_ids = sorted(artifact_selected_ids - authority_selected_ids)
    unauthorized_row_ids = sorted(
        {
            row.district_id
            for row in result.rows
            if row.district_id is not None
            and row.district_id not in authority_selected_ids
        }
    )
    if unauthorized_selection_ids or unauthorized_row_ids:
        findings.append(
            _finding(
                code="selection_authority_mismatch",
                dimension="selection",
                message=(
                    "Result rows or selection metadata include districts outside "
                    "the resolved catalog selection."
                ),
                metadata={
                    "resolved_district_ids": sorted(authority_selected_ids),
                    "selection_district_ids": unauthorized_selection_ids,
                    "row_district_ids": unauthorized_row_ids,
                },
            )
        )
    return findings

