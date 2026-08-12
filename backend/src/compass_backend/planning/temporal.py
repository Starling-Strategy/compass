"""Deterministic temporal normalization for Compass query plans."""

from __future__ import annotations

import re

from compass_backend.contracts.planning import PlannerTurn, QueryPlan, TemporalSpec

_ACADEMIC_YEAR_RE = re.compile(
    r"\b((?:19|20)\d{2})\s*(?:-|/|–|—)\s*((?:19|20)?\d{2})\b"
)


def normalize_academic_year(value: str | None) -> str | None:
    """Normalize one academic-year phrase to the canonical DB form."""

    if value is None:
        return None
    match = _ACADEMIC_YEAR_RE.search(str(value).strip())
    if match is None:
        return None
    start = int(match.group(1))
    end_text = match.group(2)
    end = _expand_end_year(start, end_text)
    if end != start + 1:
        return None
    return f"{start} - {end}"


def academic_year_window_ending_at(
    current_academic_year: str,
    window_years: int,
) -> list[str]:
    """Return a consecutive academic-year window ending at current_academic_year."""

    if window_years < 1:
        raise ValueError("window_years must be at least 1")
    current_start = academic_year_start(current_academic_year)
    return [
        academic_year_for_start_year(start)
        for start in range(current_start - window_years + 1, current_start + 1)
    ]


def academic_year_for_start_year(start_year: int) -> str:
    """Return canonical academic-year text for one start year."""

    return f"{start_year} - {start_year + 1}"


# W1-06 (#844): single source of truth for the current academic year used
# by execution/config call sites as a fallback when a plan does not carry
# its own temporal. The CI grep test in test_no_year_literals_outside_temporal
# rejects any year literal under src/compass_backend/ outside this module.
_CURRENT_ACADEMIC_YEAR_DEFAULT = "2024 - 2025"


def default_current_academic_year() -> str:
    """Return the canonical \"current academic year\" used by executor/config defaults.

    All production-code fallbacks for the current academic year MUST route
    through this helper. To roll forward a year, change
    ``_CURRENT_ACADEMIC_YEAR_DEFAULT`` in this module — no other file
    should carry a year literal.
    """

    return _CURRENT_ACADEMIC_YEAR_DEFAULT


def academic_year_start(academic_year: str) -> int:
    """Return the integer start year for a canonical academic-year string."""

    normalized = normalize_academic_year(academic_year)
    if normalized is None:
        raise ValueError(f"Invalid academic year: {academic_year!r}")
    return int(normalized.split(" - ", 1)[0])


def normalize_turn_temporal_intent(
    turn: PlannerTurn,
    *,
    message: str,
    current_academic_year: str,
) -> PlannerTurn:
    """Normalize a planner turn's query-plan temporal intent, when present."""

    if turn.route != "execute" or turn.query_plan is None:
        return turn
    return turn.model_copy(
        update={
            "query_plan": normalize_plan_temporal_intent(
                turn.query_plan,
                message=message,
                current_academic_year=current_academic_year,
            )
        }
    )


def normalize_plan_temporal_intent(
    plan: QueryPlan,
    *,
    message: str,
    current_academic_year: str,
) -> QueryPlan:
    """Return a query plan with canonical, deterministic temporal intent."""

    temporal = _normalized_explicit_temporal(
        plan.temporal,
        current_academic_year=current_academic_year,
    )
    inferred = _infer_temporal_from_text(message or plan.question)
    if _explicit_temporal_has_intent(plan.temporal):
        normalized = _complete_temporal(temporal, current_academic_year)
    else:
        normalized = inferred

    operation = plan.operation
    if normalized.intent in {
        "relative_trend_window",
        "year_range",
        "comparison_years",
    }:
        operation = "trend"
    elif normalized.intent == "specific_year" and plan.operation == "trend":
        operation = "lookup"

    return plan.model_copy(update={"operation": operation, "temporal": normalized})


def _normalized_explicit_temporal(
    temporal: TemporalSpec,
    *,
    current_academic_year: str,
) -> TemporalSpec:
    academic_year = normalize_academic_year(temporal.academic_year)
    start_academic_year = normalize_academic_year(temporal.start_academic_year)
    end_academic_year = normalize_academic_year(temporal.end_academic_year)
    academic_years = _dedupe_years(
        normalize_academic_year(year)
        for year in temporal.academic_years
    )

    if temporal.intent == "relative_trend_window" and temporal.window_years:
        academic_years = academic_year_window_ending_at(
            current_academic_year,
            temporal.window_years,
        )

    if temporal.intent == "year_range" and start_academic_year and end_academic_year:
        academic_years = _academic_year_range(start_academic_year, end_academic_year)

    return temporal.model_copy(
        update={
            "academic_year": academic_year,
            "start_academic_year": start_academic_year,
            "end_academic_year": end_academic_year,
            "academic_years": academic_years,
        }
    )


def _infer_temporal_from_text(text: str) -> TemporalSpec:
    years = _extract_academic_years(text)

    if len(years) >= 2:
        return TemporalSpec(intent="comparison_years", academic_years=years[:2])

    if len(years) == 1:
        return TemporalSpec(intent="specific_year", academic_year=years[0])

    return TemporalSpec(intent="current")


def _complete_temporal(
    temporal: TemporalSpec,
    current_academic_year: str,
) -> TemporalSpec:
    if temporal.intent == "current":
        return temporal.model_copy(
            update={
                "academic_year": (
                    temporal.academic_year
                    or normalize_academic_year(current_academic_year)
                )
            }
        )
    if temporal.intent == "specific_year" and temporal.academic_year is None:
        return temporal.model_copy(
            update={"academic_year": normalize_academic_year(current_academic_year)}
        )
    if (
        temporal.intent == "relative_trend_window"
        and temporal.window_years
        and not temporal.academic_years
    ):
        return temporal.model_copy(
            update={
                "academic_years": academic_year_window_ending_at(
                    current_academic_year,
                    temporal.window_years,
                )
            }
        )
    if (
        temporal.intent == "year_range"
        and temporal.start_academic_year
        and temporal.end_academic_year
        and not temporal.academic_years
    ):
        return temporal.model_copy(
            update={
                "academic_years": _academic_year_range(
                    temporal.start_academic_year,
                    temporal.end_academic_year,
                )
            }
        )
    return temporal


def _explicit_temporal_has_intent(temporal: TemporalSpec) -> bool:
    return (
        temporal.intent != "current"
        or temporal.academic_year is not None
        or temporal.start_academic_year is not None
        or temporal.end_academic_year is not None
        or bool(temporal.academic_years)
        or temporal.window_years is not None
    )


def _extract_academic_years(text: str) -> list[str]:
    years: list[str] = []
    for match in _ACADEMIC_YEAR_RE.finditer(text):
        normalized = normalize_academic_year(match.group(0))
        if normalized is not None and normalized not in years:
            years.append(normalized)
    return years


def _academic_year_range(
    start_academic_year: str,
    end_academic_year: str,
) -> list[str]:
    start = academic_year_start(start_academic_year)
    end = academic_year_start(end_academic_year)
    if end < start:
        start, end = end, start
    return [academic_year_for_start_year(year) for year in range(start, end + 1)]


def _expand_end_year(start: int, end_text: str) -> int:
    if len(end_text) == 4:
        return int(end_text)
    start_century = (start // 100) * 100
    end = start_century + int(end_text)
    if end < start:
        end += 100
    return end


def _dedupe_years(values) -> list[str]:
    deduped: list[str] = []
    for value in values:
        if value is not None and value not in deduped:
            deduped.append(value)
    return deduped
