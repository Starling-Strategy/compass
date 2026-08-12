"""Private helpers + constants for the deterministic-result validators.

Constants (dimension lists, regex patterns, forbidden wording) and utility
functions (state normalization, direction extraction, value formatting,
finding construction) used by validators in `quality.validators.*`.

Extracted from the previously-monolithic `quality/validation.py` so that
each validator module imports a single private helpers module without
touching the public `validation.py` entry point (avoiding circular
imports between `validation.py` and `validators/*`).
"""

from __future__ import annotations

import re

from compass_backend.artifacts import CountRow
from compass_backend.contracts.planning import QueryPlan
from compass_backend.contracts.validation import (
    ValidationDimension,
    ValidationFinding,
    ValidationSeverity,
)
from compass_backend.execution.filters import filter_statement

# ─── Dimension sets used by the public dispatch ──────────────────────────────

_FIRST_PASS_DIMENSIONS: list[ValidationDimension] = [
    "selection",
    "metric",
    "sort_order",
    "coverage_state",
    "citation_coverage",
]
_COUNT_DIMENSIONS: list[ValidationDimension] = [
    "selection",
    "metric",
    "filter",
    "denominator",
    "coverage_state",
    "citation_coverage",
]

# ─── Regexes + wording patterns used by validators ───────────────────────────

_NUMERIC_TOKEN_RE = re.compile(r"(?<![A-Za-z])\$?-?\d[\d,]*(?:\.\d+)?%?(?![A-Za-z])")
_FORBIDDEN_COVERAGE_WORDING: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("no data available", re.compile(r"\bno data available\b", re.IGNORECASE)),
    ("not released yet", re.compile(r"\bnot released yet\b", re.IGNORECASE)),
    (
        "currently unavailable",
        re.compile(r"\bcurrently unavailable\b", re.IGNORECASE),
    ),
    (
        "we don't have data on",
        re.compile(r"\bwe do(?:n't| not) have data on\b", re.IGNORECASE),
    ),
    (
        "the data isn't available",
        re.compile(r"\bthe data is(?:n't| not) available\b", re.IGNORECASE),
    ),
    ("Not available", re.compile(r"(?<!\w)not available(?!\w)", re.IGNORECASE)),
    # #1514 D11 — retired short labels (artifacts/coverage.py no longer emits
    # them; the canonical narrative sentences replaced them). NOTE: the
    # canonical out-of-universe sentence "... is not in the District Policy
    # Pathfinder." must NOT be banned — the word-boundary "Out of Pathfinder"
    # pattern deliberately does not match it.
    ("Older year only", re.compile(r"\bolder year only\b", re.IGNORECASE)),
    ("Out of Pathfinder", re.compile(r"\bout of pathfinder\b", re.IGNORECASE)),
    # #1514 — tables hold answer rows only (value / INA / N-A); a bare
    # "Not reviewed" markdown table CELL is always a policy violation. The
    # pattern is cell-scoped (pipe-delimited, whole cell) so the words
    # "not reviewed" remain usable in prose sentences.
    (
        "Not reviewed (table cell)",
        re.compile(r"\|\s*not reviewed\s*\|", re.IGNORECASE),
    ),
)
_REASON_BREAKDOWN_FIELDS = {
    "answer_value": "answer_value_count",
    "issue_not_addressed": "issue_not_addressed_count",
    "not_applicable": "not_applicable_count",
    "metric_not_reviewed": "metric_not_reviewed_count",
    "district_not_reviewed": "district_not_reviewed_count",
    "stale_recent_answer": "stale_recent_answer_count",
    "non_numeric_rank_exclusion": "non_numeric_rank_exclusion_count",
    "out_of_universe": "out_of_universe_count",
    "profile_field_value": "profile_field_value_count",
    "covered_universe_count": "covered_universe_count",
    "count_summary": "count_summary_count",
    "categorical_value_count": "categorical_value_count",
    "unavailable": "unavailable_count",
}


def _normalize_numeric_token(value: str) -> str:
    normalized = value.strip().replace("$", "").replace(",", "").replace("%", "")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized


# _direction lives in compass_backend.execution.selection.direction — promoted to
# the execution layer so validators and execution paths share one definition (see
# audit finding #13). Validators import it as ``_direction`` to preserve the
# in-module call-site shorthand.
#
# _lookup_direction lives in validators/coverage.py because it depends on
# `_lookup_sort_kind` which is itself part of the coverage validator group.
# Validators that need it import it directly from `validators.coverage`.


def _expected_count_filter_statement(plan: QueryPlan, row: CountRow) -> str:
    if row.count_kind == "categorical_value_count":
        return "group by reviewed value"
    if row.source == "coverage_state" and row.coverage_reason == "covered_universe_count":
        return "covered district universe"
    return filter_statement(plan.filters)


def _finding(
    *,
    code: str,
    dimension: ValidationDimension,
    message: str,
    metadata: dict[str, object],
    severity: ValidationSeverity = "error",
) -> ValidationFinding:
    return ValidationFinding(
        code=code,
        dimension=dimension,
        message=message,
        severity=severity,
        metadata=metadata,
    )
