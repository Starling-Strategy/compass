"""Tests for the WS0 Selection "definition of good" validators (#1095).

`clarify_spiral_route_warrant` and `district_phrase_resolves_to_expected` are the
deterministic, route+result-anchored checks that gate the Selection sprint — the
executable half of #1248's ends-vs-means thesis. These pin the three traps from
the design: (1) the clarify-spiral turn has `plan is None`, so the check reads
`ctx.route`, never a plan shape; (2) the validator returns `fail` (not a skip) on
the buggy clarify turn; (3) routes are validated against real `PlannerRoute`
members, so a typo'd route surfaces as an error.

Run with:
    PYTHONPATH=src uv run pytest \
        src/compass_backend/tests/test_selection_definition_of_good_validators.py --tb=short
"""

from __future__ import annotations

import asyncio

from compass_backend.contracts.planning import PlannerRoute
from compass_backend.quality._evaluator_context import CompassEvaluatorContext
from compass_backend.quality.validators.registry import known_validators, lookup
from compass_backend.tests.conftest import make_evaluator_context


def _ctx(
    *,
    route: PlannerRoute | None = None,
    user_message: str = "",
    table: list[dict] | None = None,
) -> CompassEvaluatorContext:
    return make_evaluator_context(
        session_id="11111111-1111-1111-1111-111111111111",
        turn_index=0,
        route=route,
        user_message=user_message,
        artifact_snapshot={"table": table} if table is not None else {},
    )


def _run(ctx: CompassEvaluatorContext, name: str, payload: dict):
    fn = lookup(name)
    assert fn is not None, f"validator {name!r} is not registered"
    return asyncio.run(fn(ctx, payload))


# ─── Registration ─────────────────────────────────────────────────────────────


def test_both_validators_are_registered() -> None:
    known = known_validators()
    assert "clarify_spiral_route_warrant" in known
    assert "district_phrase_resolves_to_expected" in known


# ─── clarify_spiral_route_warrant ─────────────────────────────────────────────


def test_clarify_on_warranted_turn_fails() -> None:
    # The Denver bug: a data-bearing turn that should have executed clarified.
    out = _run(
        _ctx(route="clarify", user_message="Compare DCPS and Houston starting salaries"),
        "clarify_spiral_route_warrant",
        {"expected_route": "execute"},
    )
    assert out.outcome == "fail"
    assert "punted" in out.reason


def test_execute_when_execute_expected_passes() -> None:
    out = _run(
        _ctx(route="execute"),
        "clarify_spiral_route_warrant",
        {"expected_route": "execute"},
    )
    assert out.outcome == "pass"


def test_clarify_fails_against_default_warranted_routes() -> None:
    # No expected_route in payload → default {execute, direct, policy_guidance};
    # clarify is not warranted.
    out = _run(_ctx(route="clarify"), "clarify_spiral_route_warrant", {})
    assert out.outcome == "fail"


def test_direct_passes_against_default_warranted_routes() -> None:
    out = _run(_ctx(route="direct"), "clarify_spiral_route_warrant", {})
    assert out.outcome == "pass"


def test_missing_route_is_error() -> None:
    out = _run(_ctx(route=None), "clarify_spiral_route_warrant", {})
    assert out.outcome == "error"
    assert out.reason.startswith("missing artifact:")


def test_unknown_route_in_payload_is_error_not_silent_pass() -> None:
    # Trap 3: a hand-typed bogus route must surface, not silently never-match.
    out = _run(
        _ctx(route="execute"),
        "clarify_spiral_route_warrant",
        {"expected_route": "comparison"},  # not a PlannerRoute (it's an operation)
    )
    assert out.outcome == "error"
    assert "unknown route" in out.reason


def test_expected_routes_list_form() -> None:
    out = _run(
        _ctx(route="direct"),
        "clarify_spiral_route_warrant",
        {"expected_routes": ["execute", "direct"]},
    )
    assert out.outcome == "pass"


# ─── district_phrase_resolves_to_expected ─────────────────────────────────────


def _row(name: str) -> dict:
    return {"district_name": name, "display_value": "$60,000"}


def test_expected_districts_present_passes() -> None:
    out = _run(
        _ctx(
            route="execute",
            table=[
                _row("District of Columbia Public Schools"),
                _row("Houston Independent School District"),
            ],
        ),
        "district_phrase_resolves_to_expected",
        {
            "expected_districts": [
                "District of Columbia Public Schools",
                "Houston Independent School District",
            ]
        },
    )
    assert out.outcome == "pass"


def test_valid_but_wrong_district_fails() -> None:
    # The DCPS/Houston bug: a real-but-wrong district took DCPS's place.
    out = _run(
        _ctx(
            route="execute",
            table=[
                _row("Washington County Public Schools"),
                _row("Houston Independent School District"),
            ],
        ),
        "district_phrase_resolves_to_expected",
        {
            "expected_districts": [
                "District of Columbia Public Schools",
                "Houston Independent School District",
            ]
        },
    )
    assert out.outcome == "fail"
    assert "District of Columbia Public Schools" in out.evidence["missing_expected"]
    assert "Washington County Public Schools" in out.evidence["unexpected_districts"]


def test_shorthand_matches_canonical() -> None:
    out = _run(
        _ctx(route="execute", table=[_row("DCPS (District of Columbia Public Schools)")]),
        "district_phrase_resolves_to_expected",
        {"expected_districts": ["DCPS"]},
    )
    assert out.outcome == "pass"


def test_missing_expected_districts_payload_is_error() -> None:
    out = _run(
        _ctx(route="execute", table=[_row("Houston Independent School District")]),
        "district_phrase_resolves_to_expected",
        {},
    )
    assert out.outcome == "error"


def test_empty_table_is_error() -> None:
    out = _run(
        _ctx(route="execute", table=[]),
        "district_phrase_resolves_to_expected",
        {"expected_districts": ["Houston Independent School District"]},
    )
    assert out.outcome == "error"
    assert out.reason.startswith("missing artifact:")
