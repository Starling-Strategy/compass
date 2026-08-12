"""Tests for the deterministic validator registry.

Covers the four validators wired in PR `wire-deterministic-validator-registry`:
forbidden_temporal_phrases, sort_descending, respects_user_limit, citation_format,
plus RecordDeterministicEvaluator's dispatch behaviour (known / unknown /
validator-raises) end-to-end.

Run with:
    PYTHONPATH=src uv run pytest src/compass_backend/tests/test_validator_registry.py --tb=short
"""

from __future__ import annotations

import pytest

from compass_backend.artifacts import (
    CitationRef,
    MetricLookupResult,
    MetricValueRow,
    ResultSelection,
)
from compass_backend.contracts import MetricSpec, QueryPlan, SelectionSpec, SortSpec
from compass_backend.db.rows import CriterionRecord
from compass_backend.quality._evaluator_context import CompassEvaluatorContext
from compass_backend.quality.criteria import RecordDeterministicEvaluator
from compass_backend.quality.validators import registry
from compass_backend.quality.validators.registry import (
    ValidatorOutcome,
    known_validators,
    lookup,
)
from compass_backend.tests.conftest import (
    make_criterion_record,
    make_evaluator_context,
)


# ─── Shared fixtures ──────────────────────────────────────────────────────────


def _ctx(
    *,
    answer_text: str = "",
    intent: str | None = None,
    user_message: str = "",
    artifact_snapshot: dict | None = None,
    plan: QueryPlan | None = None,
    result=None,
    route: str | None = None,
) -> CompassEvaluatorContext:
    return make_evaluator_context(
        session_id="11111111-1111-1111-1111-111111111111",
        turn_index=0,
        answer_text=answer_text,
        intent=intent,
        user_message=user_message,
        artifact_snapshot=artifact_snapshot or {},
        plan=plan,
        result=result,
        route=route,
    )


def _single_cited_result() -> MetricLookupResult:
    return MetricLookupResult(
        selection=ResultSelection(scope="named_districts"),
        rows=[
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=89,
                metric_name="Starting teacher salary",
                value=50000,
                display_value="$50,000",
                academic_year="2024 - 2025",
                citation_markers=[1],
                coverage_state="covered",
                coverage_display="$50,000",
                coverage_reason="answer_value",
            )
        ],
        citations=[
            CitationRef(
                marker=1,
                title="Alpha Salary Schedule",
                url="https://example.org/alpha.pdf",
                academic_year="2024 - 2025",
                district_id=1,
            )
        ],
        total_considered=1,
        excluded_count=0,
        order_statement="Looked up starting teacher salary.",
    )


def _criterion(
    validator_name: str,
    *,
    id: int = 1,
    criterion_code: str = "test_criterion",
) -> CriterionRecord:
    return make_criterion_record(
        id=id,
        criterion_code=criterion_code,
        text=f"Test for {validator_name}",
        priority=50,
        payload={"validator_name": validator_name},
        version_hash=f"hash-{validator_name}-v1",
    )


# ─── 1. Registry mechanics ────────────────────────────────────────────────────


def test_known_validators_includes_all_four() -> None:
    """Importing the registry module registers all planned validators."""
    expected = {
        "forbidden_temporal_phrases",
        "sort_descending",
        "respects_user_limit",
        "citation_format",
        "selection_set_matches_request",
        "currency_format_canonical",
        "trace_id_present",
    }
    assert expected.issubset(known_validators())


def test_lookup_unknown_returns_none() -> None:
    assert lookup("nonexistent_validator_xyz") is None


def test_register_rejects_duplicate() -> None:
    """Re-registering an existing name must raise (prevents silent override)."""
    with pytest.raises(RuntimeError, match="already registered"):

        @registry.register("forbidden_temporal_phrases")
        async def _shadow(ctx, payload):  # type: ignore[no-untyped-def]
            return ValidatorOutcome(outcome="pass", reason="shadow")


# ─── 2. forbidden_temporal_phrases ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_forbidden_temporal_phrases_pass_on_clean_text() -> None:
    fn = lookup("forbidden_temporal_phrases")
    assert fn is not None
    ctx = _ctx(answer_text="LAUSD enrolled 600,000 students.")
    result = await fn(ctx, {})
    assert result.outcome == "pass"


@pytest.mark.asyncio
async def test_forbidden_temporal_phrases_fail_on_unanchored_phrase() -> None:
    fn = lookup("forbidden_temporal_phrases")
    assert fn is not None
    ctx = _ctx(answer_text="The salary schedule is currently unavailable for review.")
    result = await fn(ctx, {})
    assert result.outcome == "fail"
    assert "currently unavailable" in result.evidence["flagged_phrases"]


@pytest.mark.asyncio
async def test_forbidden_temporal_phrases_pass_when_anchored_to_ina() -> None:
    fn = lookup("forbidden_temporal_phrases")
    assert fn is not None
    # Phrase appears but within the INA window of "issue not addressed"
    text = (
        "For the 2024 review, this issue not addressed in the documents reviewed; "
        "the underlying salary schedule is currently unavailable for that year."
    )
    ctx = _ctx(answer_text=text)
    result = await fn(ctx, {})
    assert result.outcome == "pass"


@pytest.mark.asyncio
async def test_forbidden_temporal_phrases_error_on_empty_answer() -> None:
    fn = lookup("forbidden_temporal_phrases")
    assert fn is not None
    result = await fn(_ctx(answer_text=""), {})
    assert result.outcome == "error"
    assert "missing artifact: answer_text" in result.reason


# ─── 3. sort_descending ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sort_descending_pass_on_descending_table() -> None:
    fn = lookup("sort_descending")
    assert fn is not None
    ctx = _ctx(
        artifact_snapshot={
            "table": [
                {"district_name": "NYC", "display_value": "1,000,000"},
                {"district_name": "LAUSD", "display_value": "600,000"},
                {"district_name": "Chicago", "display_value": "330,000"},
            ]
        }
    )
    result = await fn(ctx, {})
    assert result.outcome == "pass", result.reason


@pytest.mark.asyncio
async def test_sort_descending_fail_on_ascending_table() -> None:
    fn = lookup("sort_descending")
    assert fn is not None
    ctx = _ctx(
        artifact_snapshot={
            "table": [
                {"district_name": "Chicago", "display_value": "330,000"},
                {"district_name": "LAUSD", "display_value": "600,000"},
                {"district_name": "NYC", "display_value": "1,000,000"},
            ]
        }
    )
    result = await fn(ctx, {})
    assert result.outcome == "fail"
    assert result.evidence["first_violation_index"] == 0


def _rank_plan(direction: str) -> QueryPlan:
    """Minimal ranking plan whose canonical sort direction is `direction`."""
    return QueryPlan(
        operation="rank",
        question="rank districts by starting salary",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting teacher salary")],
        sort=SortSpec(field="starting teacher salary", direction=direction),
    )


@pytest.mark.asyncio
async def test_sort_descending_passes_ascending_table_for_ascending_request() -> None:
    """#1691 (case 7): a 'lowest first' request whose table is correctly
    ascending must PASS. The criterion checks the *requested* direction
    (`selection.direction(plan) == "asc"`), not unconditionally descending.
    """
    fn = lookup("sort_descending")
    assert fn is not None
    ctx = _ctx(
        artifact_snapshot={
            "table": [
                {"district_name": "Chicago", "display_value": "$48,000"},
                {"district_name": "LAUSD", "display_value": "$60,000"},
                {"district_name": "NYC", "display_value": "$72,000"},
            ]
        },
        plan=_rank_plan("asc"),
        route="execute",
    )
    result = await fn(ctx, {})
    assert result.outcome == "pass", result.reason


@pytest.mark.asyncio
async def test_sort_descending_fails_descending_table_for_ascending_request() -> None:
    """#1691: an ascending request answered with a *descending* table is wrong
    and must FAIL — recalibration keeps ordering coverage for asc, it does not
    blanket-pass ascending requests.
    """
    fn = lookup("sort_descending")
    assert fn is not None
    ctx = _ctx(
        artifact_snapshot={
            "table": [
                {"district_name": "NYC", "display_value": "$72,000"},
                {"district_name": "LAUSD", "display_value": "$60,000"},
                {"district_name": "Chicago", "display_value": "$48,000"},
            ]
        },
        plan=_rank_plan("asc"),
        route="execute",
    )
    result = await fn(ctx, {})
    assert result.outcome == "fail", result.reason


@pytest.mark.asyncio
async def test_sort_descending_still_fails_ascending_table_for_descending_request() -> None:
    """#1691 guard (advisor): an explicit *descending* request answered with an
    ascending table must STILL FAIL — the descending check is not blunted.
    """
    fn = lookup("sort_descending")
    assert fn is not None
    ctx = _ctx(
        artifact_snapshot={
            "table": [
                {"district_name": "Chicago", "display_value": "$48,000"},
                {"district_name": "LAUSD", "display_value": "$60,000"},
                {"district_name": "NYC", "display_value": "$72,000"},
            ]
        },
        plan=_rank_plan("desc"),
        route="execute",
    )
    result = await fn(ctx, {})
    assert result.outcome == "fail", result.reason
    assert result.evidence["first_violation_index"] == 0


@pytest.mark.asyncio
async def test_sort_descending_pass_on_formatted_values() -> None:
    """display_value strings with $, %, commas parse correctly."""
    fn = lookup("sort_descending")
    assert fn is not None
    ctx = _ctx(
        artifact_snapshot={
            "table": [
                {"district_name": "A", "display_value": "$78,500"},
                {"district_name": "B", "display_value": "$72,000"},
                {"district_name": "C", "display_value": "$65,400"},
            ]
        }
    )
    result = await fn(ctx, {})
    assert result.outcome == "pass"


@pytest.mark.asyncio
async def test_sort_descending_uses_sort_display_value_when_present() -> None:
    fn = lookup("sort_descending")
    assert fn is not None
    ctx = _ctx(
        artifact_snapshot={
            "table": [
                {
                    "district_name": "A",
                    "display_value": "$46,500",
                    "sort_display_value": "100%",
                },
                {
                    "district_name": "B",
                    "display_value": "$48,150",
                    "sort_display_value": "90%",
                },
            ]
        }
    )
    result = await fn(ctx, {})
    assert result.outcome == "pass"


@pytest.mark.asyncio
async def test_sort_descending_pass_trivially_on_single_row() -> None:
    fn = lookup("sort_descending")
    assert fn is not None
    ctx = _ctx(
        artifact_snapshot={
            "table": [{"district_name": "A", "display_value": "1,000"}]
        }
    )
    result = await fn(ctx, {})
    assert result.outcome == "pass"


# ─── 3b. sort_descending: multi-metric per-column (#1460 defect 2) ─────────────


@pytest.mark.asyncio
async def test_sort_descending_multimetric_passes_on_ranked_column_only() -> None:
    """#1460 defect 2: a multi-metric ranked lookup interleaves columns in
    ``result.rows`` (one row per district-metric cell). The flat row scan mixes
    the ranked salary column with an unranked workday-count column and trips
    spuriously — even though the *ranked* column is correctly descending.

    Reproduced from a real ``compass.verdicts`` false-fail (case 1006/1032):
    ``values_sample = [79468, 184, 75444, 185, 73336, 184, 72908, 185, 71038.98,
    186]`` — salaries (descending) interleaved with workday counts. The checker
    must isolate the ranked metric's column (``ranked_by_metric_name``) and run
    the monotonicity check on it alone, ignoring the unranked column.
    """
    fn = lookup("sort_descending")
    assert fn is not None
    ctx = _ctx(
        artifact_snapshot={
            "ranked_by_metric_name": "Starting teacher salary",
            "table": [
                {"district_name": "A", "metric_name": "Starting teacher salary",
                 "display_value": "$79,468"},
                {"district_name": "A", "metric_name": "Teacher work days",
                 "display_value": "184"},
                {"district_name": "B", "metric_name": "Starting teacher salary",
                 "display_value": "$75,444"},
                {"district_name": "B", "metric_name": "Teacher work days",
                 "display_value": "185"},
                {"district_name": "C", "metric_name": "Starting teacher salary",
                 "display_value": "$73,336"},
                {"district_name": "C", "metric_name": "Teacher work days",
                 "display_value": "184"},
            ],
        },
        plan=_rank_plan("desc"),
        route="execute",
    )
    result = await fn(ctx, {})
    assert result.outcome == "pass", result.reason


@pytest.mark.asyncio
async def test_sort_descending_multimetric_still_fails_wrong_ranked_column() -> None:
    """#1460 guard: isolating the ranked column must NOT blunt the check. A
    genuinely wrong-direction *ranked* column (ascending salaries when descending
    was requested) must STILL FAIL even though an interleaved unranked column is
    present. The unranked column being unsorted is irrelevant; the ranked one is
    wrong.
    """
    fn = lookup("sort_descending")
    assert fn is not None
    ctx = _ctx(
        artifact_snapshot={
            "ranked_by_metric_name": "Starting teacher salary",
            "table": [
                {"district_name": "A", "metric_name": "Starting teacher salary",
                 "display_value": "$48,000"},
                {"district_name": "A", "metric_name": "Teacher work days",
                 "display_value": "184"},
                {"district_name": "B", "metric_name": "Starting teacher salary",
                 "display_value": "$60,000"},
                {"district_name": "B", "metric_name": "Teacher work days",
                 "display_value": "185"},
                {"district_name": "C", "metric_name": "Starting teacher salary",
                 "display_value": "$72,000"},
                {"district_name": "C", "metric_name": "Teacher work days",
                 "display_value": "184"},
            ],
        },
        plan=_rank_plan("desc"),
        route="execute",
    )
    result = await fn(ctx, {})
    assert result.outcome == "fail", result.reason
    # Violation index is measured within the isolated ranked column (3 salary
    # rows: 48k < 60k at position 0), not the flattened 6-row scan.
    assert result.evidence["first_violation_index"] == 0


@pytest.mark.asyncio
async def test_sort_descending_multimetric_ascending_request_passes() -> None:
    """#1460 defects 1 + 2 together: a 'lowest first' multi-metric ranked lookup
    whose ranked salary column is correctly ascending must PASS — direction read
    from the plan, column isolated by ``ranked_by_metric_name``.
    """
    fn = lookup("sort_descending")
    assert fn is not None
    ctx = _ctx(
        artifact_snapshot={
            "ranked_by_metric_name": "Starting teacher salary",
            "table": [
                {"district_name": "A", "metric_name": "Starting teacher salary",
                 "display_value": "$48,000"},
                {"district_name": "A", "metric_name": "Teacher work days",
                 "display_value": "184"},
                {"district_name": "B", "metric_name": "Starting teacher salary",
                 "display_value": "$60,000"},
                {"district_name": "B", "metric_name": "Teacher work days",
                 "display_value": "185"},
                {"district_name": "C", "metric_name": "Starting teacher salary",
                 "display_value": "$72,000"},
                {"district_name": "C", "metric_name": "Teacher work days",
                 "display_value": "184"},
            ],
        },
        plan=_rank_plan("asc"),
        route="execute",
    )
    result = await fn(ctx, {})
    assert result.outcome == "pass", result.reason


@pytest.mark.asyncio
async def test_sort_descending_error_on_missing_table() -> None:
    """Route unknown (None) + no table → genuine 'missing artifact:' error.

    The sweep lane always populates a route, so route=None only arises off the
    sweep path; treat it conservatively as a real failure so nothing is masked.
    """
    fn = lookup("sort_descending")
    assert fn is not None
    result = await fn(_ctx(artifact_snapshot={}), {})
    assert result.outcome == "error"
    assert "missing artifact: table" in result.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["clarify", "direct", "policy_guidance"])
async def test_sort_descending_not_applicable_on_non_execute_route(route: str) -> None:
    """U3 (c): a non-execute route produces no table → NOT_APPLICABLE, not error.

    clarify/direct/policy_guidance turns legitimately carry no ResultSet, so a
    table-requiring validator firing there has nothing to evaluate — a
    turn-shape mismatch (harness gap), not a pipeline failure. The
    'not applicable:' prefix excludes it from the Scorecard.
    """
    fn = lookup("sort_descending")
    assert fn is not None
    result = await fn(_ctx(artifact_snapshot={}, route=route), {})
    assert result.outcome == "error"
    assert result.reason.startswith("not applicable:")
    assert route in result.reason


@pytest.mark.asyncio
async def test_sort_descending_missing_table_on_execute_route_stays_error() -> None:
    """U3 (c): an EXECUTE turn with no table IS a genuine pipeline failure.

    This is the candidate-real-failure case the triage deliberately does NOT
    mask: route=execute means a ResultSet was expected, so its absence stays
    a 'missing artifact:' error visible in the error bucket.
    """
    fn = lookup("sort_descending")
    assert fn is not None
    result = await fn(_ctx(artifact_snapshot={}, route="execute"), {})
    assert result.outcome == "error"
    assert "missing artifact: table" in result.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["clarify", "direct"])
async def test_sources_block_not_applicable_on_non_execute_route(route: str) -> None:
    """A result-requiring validator NA's on clarify/direct (no ResultSet)."""
    fn = lookup("sources_block_is_deduplicated")
    assert fn is not None
    result = await fn(_ctx(result=None, route=route), {})
    assert result.outcome == "error"
    assert result.reason.startswith("not applicable:")


@pytest.mark.asyncio
async def test_sources_block_missing_result_on_execute_route_stays_error() -> None:
    """Execute turn with no ResultSet stays a genuine 'missing artifact:' error."""
    fn = lookup("sources_block_is_deduplicated")
    assert fn is not None
    result = await fn(_ctx(result=None, route="execute"), {})
    assert result.outcome == "error"
    assert "missing artifact: result" in result.reason


@pytest.mark.asyncio
async def test_sort_descending_error_on_non_numeric_display_value() -> None:
    """A truly non-numeric value (not a coverage-gap sentinel) still errors.

    "abracadabra" is neither a number nor a recognized coverage-gap sentinel,
    so the validator cannot place it in the sort and reports a real parse
    error — the genuine signal U3 (b) preserves. (Coverage-gap sentinels like
    "n/a" / "Issue not addressed" are skipped instead — see the dedicated
    tests below.)
    """
    fn = lookup("sort_descending")
    assert fn is not None
    ctx = _ctx(
        artifact_snapshot={
            "table": [
                {"district_name": "A", "display_value": "abracadabra"},
                {"district_name": "B", "display_value": "600,000"},
            ]
        }
    )
    result = await fn(ctx, {})
    assert result.outcome == "error"
    assert "non-numeric" in result.reason


@pytest.mark.asyncio
async def test_sort_descending_skips_coverage_gap_sentinel_rows() -> None:
    """U3 (b): coverage-gap sentinels are skipped, not treated as parse errors.

    Pre-fix, a row whose display_value was a coverage-gap sentinel (here the
    INA string the sweep surfaced) made the validator ERROR with
    "non-numeric display_value: 'Issue not addressed …'", masking real signal.
    Now the sentinel row is dropped from the numeric sequence and the
    remaining numeric rows are sort-checked normally.
    """
    fn = lookup("sort_descending")
    assert fn is not None
    ctx = _ctx(
        artifact_snapshot={
            "table": [
                {"district_name": "A", "display_value": "600,000"},
                {
                    "district_name": "B",
                    "display_value": "Issue not addressed in the documents reviewed.",
                },
                {"district_name": "C", "display_value": "330,000"},
            ]
        }
    )
    result = await fn(ctx, {})
    assert result.outcome == "pass", result.reason


@pytest.mark.asyncio
async def test_sort_descending_skips_na_and_not_reviewed_sentinels() -> None:
    """Sibling sentinels ("n/a", "Not reviewed", "Not applicable for …") skip too."""
    fn = lookup("sort_descending")
    assert fn is not None
    ctx = _ctx(
        artifact_snapshot={
            "table": [
                {"district_name": "A", "display_value": "$72,000"},
                {"district_name": "B", "display_value": "n/a"},
                {"district_name": "C", "display_value": "Not reviewed"},
                {"district_name": "D", "display_value": "Not applicable for D."},
                {"district_name": "E", "display_value": "$48,150"},
            ]
        }
    )
    result = await fn(ctx, {})
    # $72,000 then $48,150 — strictly descending after skipping the 3 gaps.
    assert result.outcome == "pass", result.reason


@pytest.mark.asyncio
async def test_sort_descending_not_applicable_when_all_rows_are_gaps() -> None:
    """An all-gap table has no numeric ordering — NOT_APPLICABLE, not a vacuous pass.

    The `not applicable:` reason prefix routes the trial out of the Scorecard's
    numerator AND denominator (quality/scorecard.py::_is_not_applicable).
    """
    fn = lookup("sort_descending")
    assert fn is not None
    ctx = _ctx(
        artifact_snapshot={
            "table": [
                {"district_name": "A", "display_value": "Not reviewed"},
                {
                    "district_name": "B",
                    "display_value": "Issue not addressed in the documents reviewed.",
                },
            ]
        }
    )
    result = await fn(ctx, {})
    assert result.outcome == "error"
    assert result.reason.startswith("not applicable:")
    assert result.evidence["skipped_gap_rows"] == 2


# ─── 4. respects_user_limit ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_respects_user_limit_pass_on_match() -> None:
    fn = lookup("respects_user_limit")
    assert fn is not None
    ctx = _ctx(
        intent="top 3 districts by enrollment",
        artifact_snapshot={"table": [{"district_name": "d1", "display_value": "100"}, {"district_name": "d2", "display_value": "90"}, {"district_name": "d3", "display_value": "80"}]},
    )
    result = await fn(ctx, {})
    assert result.outcome == "pass"
    assert result.evidence == {"row_count": 3, "user_limit": 3}


@pytest.mark.asyncio
async def test_respects_user_limit_fail_on_too_few_rows() -> None:
    fn = lookup("respects_user_limit")
    assert fn is not None
    ctx = _ctx(
        intent="top 10 by enrollment",
        artifact_snapshot={"table": [{"district_name": "d1", "display_value": "100"}, {"district_name": "d2", "display_value": "90"}, {"district_name": "d3", "display_value": "80"}]},
    )
    result = await fn(ctx, {})
    assert result.outcome == "fail"
    assert result.evidence == {"row_count": 3, "user_limit": 10}


@pytest.mark.asyncio
async def test_respects_user_limit_error_on_missing_table() -> None:
    fn = lookup("respects_user_limit")
    assert fn is not None
    result = await fn(_ctx(intent="top 5"), {})
    assert result.outcome == "error"
    assert "missing artifact: table" in result.reason


@pytest.mark.asyncio
async def test_respects_user_limit_not_applicable_when_intent_has_no_top_n() -> None:
    """When the user did not request a limit, the validator returns an
    `error` outcome with reason prefix `not applicable:`. The Scorecard's
    `_is_excluded_from_score` filter then drops these from both numerator
    and denominator. Distinct from a real `missing artifact:` error
    (which signals something broke and stays in the error bucket).
    """
    fn = lookup("respects_user_limit")
    assert fn is not None
    ctx = _ctx(intent="sort by enrollment", artifact_snapshot={"table": [{"district_name": "d1", "display_value": "100"}, {"district_name": "d2", "display_value": "90"}]})
    result = await fn(ctx, {})
    assert result.outcome == "error"
    assert result.reason.startswith("not applicable:")
    assert "top N" in result.reason


@pytest.mark.asyncio
async def test_respects_user_limit_missing_table_keeps_missing_artifact_prefix() -> None:
    """A genuine missing-table error must NOT use the `not applicable:`
    prefix — that case signals the validator wanted a table but got
    nothing, which is a real failure worth surfacing as an error in the
    Scorecard. Only "no constraint to evaluate" turns get the N/A prefix.
    """
    fn = lookup("respects_user_limit")
    assert fn is not None
    result = await fn(_ctx(intent="top 5"), {})
    assert result.outcome == "error"
    assert result.reason.startswith("missing artifact:")
    assert not result.reason.startswith("not applicable:")


@pytest.mark.asyncio
async def test_respects_user_limit_pass_reading_user_message() -> None:
    """When ctx.user_message contains a 'top N' phrase and the table has
    N rows, the validator passes — regardless of what ctx.intent says.

    This is the regression PR 2 fixed: the planner's derived intent drops
    user-stated structural constraints, so `respects_user_limit` must
    read the raw user message to grade real user requests.
    """
    fn = lookup("respects_user_limit")
    assert fn is not None
    ctx = _ctx(
        user_message="top 3 California districts by enrollment",
        intent="sort current",  # planner's paraphrase — no "top 3"
        artifact_snapshot={
            "table": [
                {"district_name": "LAUSD", "display_value": "600,000"},
                {"district_name": "San Diego", "display_value": "120,000"},
                {"district_name": "Long Beach", "display_value": "70,000"},
            ]
        },
    )
    result = await fn(ctx, {})
    assert result.outcome == "pass", result.reason
    assert result.evidence == {"row_count": 3, "user_limit": 3}


@pytest.mark.asyncio
async def test_respects_user_limit_fail_when_user_message_limit_exceeded() -> None:
    """When ctx.user_message says 'top 5' but the table has 3 rows, fail.

    This is the case that surfaced product gaps yesterday: requesting top 5
    but the executor returned only 3 ("3 of 9 in-scope cells").
    """
    fn = lookup("respects_user_limit")
    assert fn is not None
    ctx = _ctx(
        user_message="top 5 California districts ranked by salary",
        intent="sort current",
        artifact_snapshot={
            "table": [
                {"district_name": "A", "display_value": "$80,000"},
                {"district_name": "B", "display_value": "$75,000"},
                {"district_name": "C", "display_value": "$70,000"},
            ]
        },
    )
    result = await fn(ctx, {})
    assert result.outcome == "fail"
    assert result.evidence == {"row_count": 3, "user_limit": 5}


@pytest.mark.asyncio
async def test_respects_user_limit_user_message_takes_precedence_over_intent() -> None:
    """When BOTH user_message and intent contain a 'top N', user_message
    wins. This guards against the planner's intent string ever carrying
    its own "top N" that disagrees with what the user typed.
    """
    fn = lookup("respects_user_limit")
    assert fn is not None
    ctx = _ctx(
        user_message="top 2 districts",  # what the user actually said
        intent="top 10 sort current",  # would say something different
        artifact_snapshot={
            "table": [
                {"district_name": "A", "display_value": "100"},
                {"district_name": "B", "display_value": "90"},
            ]
        },
    )
    result = await fn(ctx, {})
    assert result.outcome == "pass"
    assert result.evidence["user_limit"] == 2, (
        f"user_message must win over intent; got user_limit={result.evidence['user_limit']}"
    )


@pytest.mark.asyncio
async def test_respects_user_limit_falls_back_to_intent_when_user_message_empty() -> None:
    """Backward compatibility: contexts that don't populate user_message
    (older callers, manual replays) still get a useful result if intent
    happens to have the top-N. Defaults of "" on user_message must not
    short-circuit the intent fallback.
    """
    fn = lookup("respects_user_limit")
    assert fn is not None
    ctx = _ctx(
        user_message="",  # not populated
        intent="top 3 enrollment",
        artifact_snapshot={
            "table": [
                {"district_name": "A", "display_value": "100"},
                {"district_name": "B", "display_value": "90"},
                {"district_name": "C", "display_value": "80"},
            ]
        },
    )
    result = await fn(ctx, {})
    assert result.outcome == "pass"
    assert result.evidence["user_limit"] == 3


# ─── 5. citation_format ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_citation_format_pass_on_canonical_citations() -> None:
    fn = lookup("citation_format")
    assert fn is not None
    text = (
        "LAUSD pays new teachers $58,000 [Los Angeles USD, 2023, Salary Schedule]. "
        "NYC pays $64,000 [New York City DOE, 2023, Collective Agreement]."
    )
    result = await fn(_ctx(answer_text=text), {})
    assert result.outcome == "pass"
    assert result.evidence["citation_count"] == 2


@pytest.mark.asyncio
async def test_citation_format_pass_on_artifact_backed_numeric_markers() -> None:
    fn = lookup("citation_format")
    assert fn is not None
    result = await fn(
        _ctx(
            answer_text="Alpha starts teachers at $50,000 [1].",
            result=_single_cited_result(),
        ),
        {},
    )
    assert result.outcome == "pass"
    assert result.evidence["citation_count"] == 1
    assert result.evidence["numeric_marker_count"] == 1


@pytest.mark.asyncio
async def test_citation_format_fails_orphan_numeric_marker() -> None:
    fn = lookup("citation_format")
    assert fn is not None
    result = await fn(
        _ctx(
            answer_text="Alpha starts teachers at $50,000 [2].",
            result=_single_cited_result(),
        ),
        {},
    )
    assert result.outcome == "fail"
    assert result.evidence["missing_numeric_markers"] == [2]


@pytest.mark.asyncio
async def test_citation_format_pass_when_no_citations_present() -> None:
    fn = lookup("citation_format")
    assert fn is not None
    result = await fn(_ctx(answer_text="LAUSD enrolls about 600,000 students."), {})
    assert result.outcome == "pass"


@pytest.mark.asyncio
async def test_citation_format_fail_on_malformed_citation() -> None:
    fn = lookup("citation_format")
    assert fn is not None
    text = "LAUSD pays $58,000 [LAUSD 2023 Salary]."  # missing commas
    result = await fn(_ctx(answer_text=text), {})
    assert result.outcome == "fail"
    assert result.evidence["malformed_count"] == 1


@pytest.mark.asyncio
async def test_citation_format_error_on_empty_answer() -> None:
    fn = lookup("citation_format")
    assert fn is not None
    result = await fn(_ctx(answer_text=""), {})
    assert result.outcome == "error"


@pytest.mark.asyncio
async def test_citation_format_skips_provisional_source_marker() -> None:
    """`[provisional source]` is the renderer's marker for policy-guidance
    bullets backed by an unverified source (see
    rendering/policy_guidance.PROVISIONAL_SOURCE_MARKER). It is not a
    citation token and must not be counted as malformed by this validator.
    """
    fn = lookup("citation_format")
    assert fn is not None
    text = (
        "[provisional source] Districts may offer NBCT stipends "
        "[Los Angeles USD, 2023, Salary Schedule]."
    )
    result = await fn(_ctx(answer_text=text), {})
    assert result.outcome == "pass"
    assert result.evidence["citation_count"] == 1
    assert result.evidence["canonical_count"] == 1


@pytest.mark.asyncio
async def test_citation_format_pass_on_governed_pathfinder_source() -> None:
    """`[NCTQ District Policy Pathfinder]` is a governed source-level citation
    (the covered-universe dataset name), not an invented bracket. It must pass
    alongside canonical [District, Year, Source] tokens. Regression for #1418
    (CITE-MIGRATED-254, case_id 433): 17 canonical + 1 governed token false-
    failed because the governed token isn't district/year scoped.
    """
    fn = lookup("citation_format")
    assert fn is not None
    text = (
        "Broward leads on health benefits [Broward County, 2023, Benefits "
        "Handbook]. Coverage is drawn from the [NCTQ District Policy Pathfinder]."
    )
    result = await fn(_ctx(answer_text=text), {})
    assert result.outcome == "pass"
    assert result.evidence["citation_count"] == 2
    assert result.evidence["canonical_count"] == 1
    assert result.evidence["governed_source_count"] == 1


@pytest.mark.asyncio
async def test_citation_format_pass_on_governed_nctq_research_source() -> None:
    """`[NCTQ Research: <title>]` references the policy-guidance research
    rationale ("NCTQ Research" is the library's only source_title). Regression
    for #1418 (C11-NO-STANCE-FRAMING, case_id 980).
    """
    fn = lookup("citation_format")
    assert fn is not None
    text = "The evidence is mixed [NCTQ Research: Research Is Mixed]."
    result = await fn(_ctx(answer_text=text), {})
    assert result.outcome == "pass"
    assert result.evidence["governed_source_count"] == 1


@pytest.mark.asyncio
async def test_citation_format_pass_on_lowercase_nctq_source() -> None:
    """The governed-source allowlist must be case-insensitive. Compass writes
    "[NCTQ research]" (lowercase r) in prose — the same legitimate source-level
    citation as "[NCTQ Research]" — but the case-sensitive regex false-failed it.
    Regression for the 2026-06-21 scorecard-trust-map finding (criterion 9):
    docs/audits/2026-06-21-scorecard-trust-map.md.
    """
    fn = lookup("citation_format")
    assert fn is not None
    text = "The evidence is mixed [NCTQ research]."
    result = await fn(_ctx(answer_text=text), {})
    assert result.outcome == "pass"
    assert result.evidence["governed_source_count"] == 1


@pytest.mark.asyncio
async def test_citation_format_still_fails_invented_bracket() -> None:
    """Loosening for governed sources must NOT admit arbitrary invented
    brackets. A free-text link-label bracket is still malformed.
    """
    fn = lookup("citation_format")
    assert fn is not None
    text = "Health benefits are strong [Check out Broward's healthcare costs.]."
    result = await fn(_ctx(answer_text=text), {})
    assert result.outcome == "fail"
    assert result.evidence["malformed_count"] == 1
    assert result.evidence["first_violation"] == "[Check out Broward's healthcare costs.]"


# ─── 6. selection_set_matches_request ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_selection_set_matches_request_pass_state_match() -> None:
    """scope=state with all rows inside requested set -> pass."""
    fn = lookup("selection_set_matches_request")
    assert fn is not None
    ctx = _ctx(
        artifact_snapshot={
            "table": [
                {"district_name": "Los Angeles USD", "state": "CA", "display_value": "600000"},
                {"district_name": "San Diego USD", "state": "CA", "display_value": "100000"},
            ],
            "selection_expected": {
                "scope": "state",
                "states": ["CA"],
                "districts": [],
            },
        },
    )
    result = await fn(ctx, {})
    assert result.outcome == "pass"
    assert result.evidence["scope"] == "state"
    assert result.evidence["expected_states"] == ["CA"]


@pytest.mark.asyncio
async def test_selection_set_matches_request_pass_named_districts_exact_match() -> None:
    """scope=named_districts where row names match expected names exactly."""
    fn = lookup("selection_set_matches_request")
    assert fn is not None
    ctx = _ctx(
        artifact_snapshot={
            "table": [
                {"district_name": "Houston ISD", "state": "TX", "display_value": "200000"},
                {"district_name": "Dallas ISD", "state": "TX", "display_value": "150000"},
            ],
            "selection_expected": {
                "scope": "named_districts",
                "states": [],
                "districts": ["Houston ISD", "Dallas ISD"],
            },
        },
    )
    result = await fn(ctx, {})
    assert result.outcome == "pass"
    assert result.evidence["scope"] == "named_districts"


@pytest.mark.asyncio
async def test_selection_set_matches_request_allows_peer_comparison_peers() -> None:
    fn = lookup("selection_set_matches_request")
    assert fn is not None
    ctx = _ctx(
        artifact_snapshot={
            "table": [
                {
                    "district_name": "San Bernardino City Unified School District",
                    "state": "CA",
                    "display_value": "$95,924",
                },
                {
                    "district_name": "Corona-Norco Unified School District",
                    "state": "CA",
                    "display_value": "not reviewed",
                },
            ],
            "selection_expected": {
                "scope": "named_districts",
                "states": [],
                "districts": ["San Bernardino City Unified"],
            },
        },
        plan=QueryPlan(
            operation="peer_comparison",
            question="Maximum teacher salary in San Bernardino and peers",
            selection=SelectionSpec(
                scope="named_districts",
                districts=["San Bernardino City Unified"],
            ),
            metrics=[MetricSpec(name="maximum teacher salary")],
        ),
    )

    result = await fn(ctx, {})

    assert result.outcome == "pass"
    assert result.evidence["operation"] == "peer_comparison"


@pytest.mark.asyncio
async def test_selection_set_matches_request_canonicalized_name_passes() -> None:
    """#1225 / case 1015: the user's loose phrase ("philadelphia school district")
    resolves to the canonical row name ("School District of Philadelphia"). The
    validator must treat the canonical row as the requested district, not a rogue —
    word order + the connective "of" must not trip a false fail."""
    fn = lookup("selection_set_matches_request")
    assert fn is not None
    ctx = _ctx(
        artifact_snapshot={
            "table": [
                {
                    "district_name": "School District of Philadelphia",
                    "state": "PA",
                    "display_value": "118335",
                },
            ],
            "selection_expected": {
                "scope": "named_districts",
                "states": [],
                "districts": ["philadelphia school district"],
            },
        },
    )
    result = await fn(ctx, {})
    assert result.outcome == "pass", result.reason
    assert result.evidence["scope"] == "named_districts"


@pytest.mark.asyncio
async def test_selection_set_matches_request_rogue_named_district_still_fails() -> None:
    """The canonicalization tolerance must NOT admit a genuinely different district:
    a Los Angeles row against a Philadelphia request is still rogue (guards against
    over-loosening the fuzzy match)."""
    fn = lookup("selection_set_matches_request")
    assert fn is not None
    ctx = _ctx(
        artifact_snapshot={
            "table": [
                {
                    "district_name": "Los Angeles Unified School District",
                    "state": "CA",
                    "display_value": "400000",
                },
            ],
            "selection_expected": {
                "scope": "named_districts",
                "states": [],
                "districts": ["philadelphia school district"],
            },
        },
    )
    result = await fn(ctx, {})
    assert result.outcome == "fail"
    assert "Los Angeles Unified School District" in result.evidence["rogue_districts"]


@pytest.mark.asyncio
async def test_selection_set_matches_request_pass_named_districts_fuzzy_match() -> None:
    """scope=named_districts where the row name is the full canonical form
    while the planner's expected list uses the short form. This is the
    test that justifies the rapidfuzz dependency vs. exact-only matching.

    partial_ratio('Houston Independent School District', 'Houston ISD') == 90,
    which is >= the 88 threshold.
    """
    fn = lookup("selection_set_matches_request")
    assert fn is not None
    ctx = _ctx(
        artifact_snapshot={
            "table": [
                {"district_name": "Houston Independent School District", "state": "TX", "display_value": "200000"},
            ],
            "selection_expected": {
                "scope": "named_districts",
                "states": [],
                "districts": ["Houston ISD"],
            },
        },
    )
    result = await fn(ctx, {})
    assert result.outcome == "pass", f"Expected pass, got {result.outcome}: {result.reason}"


@pytest.mark.asyncio
async def test_selection_set_matches_request_pass_no_scope_constraint() -> None:
    """scope=all_covered_districts or missing selection_expected -> pass with no constraint."""
    fn = lookup("selection_set_matches_request")
    assert fn is not None
    ctx = _ctx(
        artifact_snapshot={
            "table": [
                {"district_name": "LAUSD", "state": "CA", "display_value": "600000"},
            ],
            "selection_expected": {
                "scope": "all_covered_districts",
                "states": [],
                "districts": [],
            },
        },
    )
    result = await fn(ctx, {})
    assert result.outcome == "pass"
    assert "no scope constraint" in result.reason


@pytest.mark.asyncio
async def test_selection_set_matches_request_fail_state_mismatch() -> None:
    """One row from a state outside the requested set -> fail."""
    fn = lookup("selection_set_matches_request")
    assert fn is not None
    ctx = _ctx(
        artifact_snapshot={
            "table": [
                {"district_name": "Los Angeles USD", "state": "CA", "display_value": "600000"},
                {"district_name": "Houston ISD", "state": "TX", "display_value": "200000"},
            ],
            "selection_expected": {
                "scope": "state",
                "states": ["CA"],
                "districts": [],
            },
        },
    )
    result = await fn(ctx, {})
    assert result.outcome == "fail"
    assert result.evidence["rogue_states"] == ["TX"]
    assert result.evidence["expected_states"] == ["CA"]


@pytest.mark.asyncio
async def test_selection_set_matches_request_fail_named_district_mismatch_far() -> None:
    """Row name that fuzzy-scores well below the 88 threshold -> fail.

    partial_ratio('Boston Public Schools', 'Houston ISD') is in the 40s,
    well below 88. Austin/Houston (~84) is also below 88 by design (4-point
    margin between worst near-miss and lowest true-positive).
    """
    fn = lookup("selection_set_matches_request")
    assert fn is not None
    ctx = _ctx(
        artifact_snapshot={
            "table": [
                {"district_name": "Houston ISD", "state": "TX", "display_value": "200000"},
                {"district_name": "Boston Public Schools", "state": "MA", "display_value": "50000"},
            ],
            "selection_expected": {
                "scope": "named_districts",
                "states": [],
                "districts": ["Houston ISD"],
            },
        },
    )
    result = await fn(ctx, {})
    assert result.outcome == "fail"
    assert "Boston Public Schools" in result.evidence["rogue_districts"]
    assert result.evidence["match_threshold"] == 88


@pytest.mark.asyncio
async def test_selection_set_matches_request_pass_when_rows_lack_state() -> None:
    """Rows without a state field can't be disproved by the validator -> pass."""
    fn = lookup("selection_set_matches_request")
    assert fn is not None
    ctx = _ctx(
        artifact_snapshot={
            "table": [
                {"district_name": "Some District", "state": None, "display_value": "100"},
                {"district_name": "Another District", "state": None, "display_value": "200"},
            ],
            "selection_expected": {
                "scope": "state",
                "states": ["CA"],
                "districts": [],
            },
        },
    )
    result = await fn(ctx, {})
    assert result.outcome == "pass"


@pytest.mark.asyncio
async def test_selection_set_matches_request_error_on_missing_table() -> None:
    """Empty artifact_snapshot -> error (missing artifact: table)."""
    fn = lookup("selection_set_matches_request")
    assert fn is not None
    result = await fn(_ctx(artifact_snapshot={}), {})
    assert result.outcome == "error"
    assert "missing artifact: table" in result.reason


# ─── 7. RecordDeterministicEvaluator dispatch (end-to-end through evaluator) ──


@pytest.mark.asyncio
async def test_dispatch_known_validator_produces_real_verdict() -> None:
    """End-to-end: known validator_name produces pass/fail (not error)."""
    ev = RecordDeterministicEvaluator(criterion=_criterion("citation_format", id=42))
    ctx = _ctx(answer_text="Plain text with no citations.")
    verdict = await ev.evaluate(ctx)
    assert verdict.outcome == "pass"
    assert verdict.criterion_id == 42
    assert verdict.judge_source == "deterministic"


@pytest.mark.asyncio
async def test_dispatch_unknown_validator_returns_error_with_known_list() -> None:
    """Unknown validator_name → error, evidence lists known validators."""
    ev = RecordDeterministicEvaluator(
        criterion=_criterion("not_a_real_validator", id=7)
    )
    verdict = await ev.evaluate(_ctx())
    assert verdict.outcome == "error"
    assert "unknown" in verdict.reason
    assert verdict.evidence.model_dump()["validator_name"] == "not_a_real_validator"
    assert "citation_format" in verdict.evidence.model_dump()["known_validators"]
    assert verdict.criterion_id == 7


def test_deterministic_criterion_with_empty_payload_fails_at_construction() -> None:
    """Replaces the old 'empty validator_name → runtime error verdict' test.

    With the typed CriterionPayload union, a deterministic criterion that
    ships with no validator_name fails to load instead of silently producing
    error verdicts on every evaluation. The 'unknown but present' validator_name
    path is still covered by test_dispatch_unknown_validator_name_returns_error.
    """
    c = _criterion("placeholder", id=9)
    with pytest.raises(ValueError):  # pydantic.ValidationError is a ValueError subclass
        CriterionRecord(**{**c.__dict__, "payload": {}})


# ─── 6. currency_format_canonical ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_currency_format_canonical_pass_all_whole_dollars() -> None:
    fn = lookup("currency_format_canonical")
    assert fn is not None
    ctx = _ctx(answer_text="Salaries: $100,000, $50,000, $75,000.")
    result = await fn(ctx, {})
    assert result.outcome == "pass"
    assert result.evidence["currency_count"] == 3
    assert result.evidence["precision"] == "no_cents"


@pytest.mark.asyncio
async def test_currency_format_canonical_pass_all_with_cents() -> None:
    fn = lookup("currency_format_canonical")
    assert fn is not None
    ctx = _ctx(answer_text="Total: $100.50, change $0.25, fee $2.99.")
    result = await fn(ctx, {})
    assert result.outcome == "pass"
    assert result.evidence["currency_count"] == 3
    assert result.evidence["precision"] == "with_cents"


@pytest.mark.asyncio
async def test_currency_format_canonical_pass_no_currency() -> None:
    fn = lookup("currency_format_canonical")
    assert fn is not None
    ctx = _ctx(answer_text="LAUSD enrolled 600,000 students and observed teachers twice yearly.")
    result = await fn(ctx, {})
    assert result.outcome == "pass"
    assert result.evidence["currency_count"] == 0


@pytest.mark.asyncio
async def test_currency_format_canonical_fail_worksheet_canonical_case() -> None:
    """The Worksheet's $136,734.18 / $135,530 / $120,992 example (SSN-205)."""
    fn = lookup("currency_format_canonical")
    assert fn is not None
    ctx = _ctx(
        answer_text=(
            "Three California salaries: $136,734.18, $135,530, $120,992, "
            "mean of $131,085.39."
        )
    )
    result = await fn(ctx, {})
    assert result.outcome == "fail"
    assert "mixed currency precision" in result.reason
    # Two tokens carry cents (136,734.18 and 131,085.39); two don't
    assert result.evidence["with_cents_count"] == 2
    assert result.evidence["no_cents_count"] == 2
    assert result.evidence["first_with_cents"] == "$136,734.18"
    assert result.evidence["first_without_cents"] == "$135,530"


@pytest.mark.asyncio
async def test_currency_format_canonical_fail_two_tokens_mixed() -> None:
    """Smallest fail case: one with cents, one without."""
    fn = lookup("currency_format_canonical")
    assert fn is not None
    ctx = _ctx(answer_text="Pay went from $50,000 to $52,500.75 over three years.")
    result = await fn(ctx, {})
    assert result.outcome == "fail"
    assert result.evidence["with_cents_count"] == 1
    assert result.evidence["no_cents_count"] == 1


@pytest.mark.asyncio
async def test_currency_format_canonical_error_empty_answer() -> None:
    fn = lookup("currency_format_canonical")
    assert fn is not None
    result = await fn(_ctx(answer_text=""), {})
    assert result.outcome == "error"
    assert "missing artifact: answer_text" in result.reason


# ─── 7. Dispatch behaviour ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_catches_validator_exception() -> None:
    """If a validator raises, the evaluator returns error rather than propagating."""

    @registry.register("__test_raises__")
    async def _raiser(ctx, payload):  # type: ignore[no-untyped-def]
        raise ValueError("boom")

    try:
        ev = RecordDeterministicEvaluator(criterion=_criterion("__test_raises__"))
        verdict = await ev.evaluate(_ctx())
        assert verdict.outcome == "error"
        assert "boom" in verdict.reason
        assert verdict.evidence.model_dump()["error_type"] == "ValueError"
    finally:
        # Clean up so other tests are not polluted by the test-only entry
        registry._REGISTRY.pop("__test_raises__", None)
