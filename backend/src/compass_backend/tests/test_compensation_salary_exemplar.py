"""Tests for the compensation/teacher-pay exemplar planner-guidance snippet (#1613 U2).

The snippet routes a *bare subjective superlative* about teacher pay ("Who has
the best teacher pay?", "leading the way on salaries", "make good money") to
``general-salary`` exemplars instead of a which-salary-metric clarification —
the #1613 Mode-3 over-clarify family (cases 99/107/113/146-151), confirmed
still live on main via local replay. It mirrors HEALTH_BENEFIT_EXEMPLAR and is
``exclusive`` so the broader TEACHER_COMPENSATION_SALARY snippet does not also
fire and pull the turn back toward an execute/clarify shape. A request with
explicit ranking structure ("top 10", "rank") or a concrete metric/lane stays
on the execute path.

Run with:
    PYTHONPATH=src uv run pytest \
        src/compass_backend/tests/test_compensation_salary_exemplar.py --tb=short
"""

from __future__ import annotations

from types import SimpleNamespace

from compass_backend.planning.instruction_snippets import (
    COMPENSATION_SALARY_EXEMPLAR,
    HEALTH_BENEFIT_EXEMPLAR,
    TEACHER_COMPENSATION_SALARY,
    select_planner_instruction_snippets,
)


def _names(message: str) -> list[str]:
    deps = SimpleNamespace(message=message, query_context=None, recent_routes=())
    return [s.name for s in select_planner_instruction_snippets(deps)]


# ─── Fires for the bare-superlative best-pay family ───────────────────────────


def test_snippet_fires_for_best_teacher_pay() -> None:
    # Anchor case 99/107/113/146.
    assert COMPENSATION_SALARY_EXEMPLAR.name in _names("Who has the best teacher pay?")


def test_snippet_fires_for_leading_on_salaries() -> None:
    # Case 151.
    assert COMPENSATION_SALARY_EXEMPLAR.name in _names(
        "Who's leading the way on teacher salaries?"
    )


def test_snippet_fires_for_make_good_money() -> None:
    # Case 148.
    assert COMPENSATION_SALARY_EXEMPLAR.name in _names(
        "Where should I teach if I want to make good money?"
    )


def test_snippet_fires_for_pay_teachers_the_most() -> None:
    # Case 147 — "the most" reads as a superlative, not a ranking, when there
    # is no explicit ranking structure.
    assert COMPENSATION_SALARY_EXEMPLAR.name in _names(
        "Which districts pay teachers the most?"
    )


# ─── Exclusive: the broad salary snippet must not also fire ───────────────────


def test_snippet_is_exclusive_salary_snippet_does_not_also_fire() -> None:
    names = _names("Who has the best teacher pay?")
    assert names == [COMPENSATION_SALARY_EXEMPLAR.name]
    assert TEACHER_COMPENSATION_SALARY.name not in names


# ─── Does NOT hijack concrete rankings / lookups / counts ─────────────────────


def test_snippet_does_not_fire_for_explicit_top_n_ranking() -> None:
    # Explicit ranking structure + named lane → execute, not exemplars.
    assert COMPENSATION_SALARY_EXEMPLAR.name not in _names(
        "Show the top 10 districts by BA starting salary."
    )


def test_snippet_does_not_fire_for_named_district_salary_lookup() -> None:
    # Concrete metric ("starting salary") + no superlative → execute.
    assert COMPENSATION_SALARY_EXEMPLAR.name not in _names(
        "What is BA starting salary in Chicago and Denver?"
    )


def test_snippet_does_not_fire_for_count_request() -> None:
    assert COMPENSATION_SALARY_EXEMPLAR.name not in _names(
        "How many districts pay teachers more than $60k?"
    )


# ─── Does NOT collide with the health-benefit exemplar (different topic) ──────


def test_snippet_does_not_fire_for_best_health_coverage() -> None:
    # "best health coverage" is HEALTH_BENEFIT_EXEMPLAR's shape (different
    # required group); the compensation snippet requires a salary/pay phrase.
    names = _names("Which districts have the best teacher health coverage?")
    assert COMPENSATION_SALARY_EXEMPLAR.name not in names
    assert HEALTH_BENEFIT_EXEMPLAR.name in names


# ─── Body must not narrow the exemplar bundle to a single district ───────────


def test_snippet_body_does_not_narrow_exemplars_with_a_focus_term() -> None:
    """A hardcoded ``focus_terms`` value collapses the 6 general-salary
    exemplars down to one (rendering/policy_guidance.py — "when only one
    exemplar survives bundle filters (focus_terms, ...)"). A bare "best pay"
    superlative wants the full curated set across multiple lenses, so the
    snippet body must instruct an *empty* focus_terms, never a narrowing one.
    Validated on a local main backend: empty focus_terms surfaced 6 exemplars
    (performance pay, early-career growth, ...) vs. 1 (DCPS only) with a focus
    term. Guards against re-introducing the narrowing value."""
    body = COMPENSATION_SALARY_EXEMPLAR.body
    assert 'focus_terms=["' not in body, (
        "snippet must not hardcode a narrowing focus_terms value"
    )
    assert "focus_terms` empty" in body
