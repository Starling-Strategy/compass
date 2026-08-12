"""PR1a (#1248): independent intended-geography Gate-3 for resolved districts.

The closed-loop ``_validate_resolved_district_selection`` compares the result's
districts against ``authority.selection`` — but both come from the SAME resolver
output (see ``execution/_helpers.py::_validation_authority``). Answer key == exam.

This is the DC/Houston bug: the planner resolves "Washington DC" to a REAL
"Washington" district in WA. Every membership check passes (the WA district IS
in the resolved selection AND the authority), so the closed loop waves it
through. The independent check here re-derives the user's INTENDED geography
from the prose the user actually typed (``plan.question`` /
``plan.selection.districts``) — an oracle the resolver never touched — and
asserts the resolved district's ``state`` matches it.

Independence proof: in every WRONG case below, the resolved district is REAL and
is fully consistent with ``authority.selection`` (it would pass the closed-loop
Gate-2). The new check still catches it because its oracle is the user prose,
not the resolver's own output.
"""

from __future__ import annotations

from compass_backend.artifacts import (
    MetricRankingResult,
    RankingRow,
    ResultSelection,
    SelectedDistrict,
)
from compass_backend.contracts import MetricSpec, QueryPlan, SelectionSpec
from compass_backend.contracts.validation import (
    ResolvedMetricAuthority,
    ResolvedSelectionAuthority,
    ValidationAuthority,
)
from compass_backend.quality import validate_result

_GEOGRAPHY_CODE = "selection_intended_geography_mismatch"


def _ranking_result(*, district_id: int, district_name: str, state: str) -> MetricRankingResult:
    return MetricRankingResult(
        selection=ResultSelection(
            scope="named_districts",
            districts=[
                SelectedDistrict(
                    district_id=district_id,
                    district_name=district_name,
                    state=state,
                )
            ],
        ),
        rows=[
            RankingRow(
                district_id=district_id,
                district_name=district_name,
                state=state,
                metric_id=1234,
                metric_name="starting salary",
                display_value="$50,000",
                academic_year="2024 - 2025",
                rank=1,
            )
        ],
        total_considered=1,
        excluded_count=0,
        order_statement="Ranking by starting salary.",
    )


def _authority(*, district_id: int) -> ValidationAuthority:
    """Authority built to AGREE with the resolved selection — the closed loop.

    Mirrors ``execution/_helpers.py::_validation_authority``: the authority's
    ``district_ids`` come from the same resolver districts that populate
    ``result.selection``. So the closed-loop check cannot distinguish right from
    wrong; only the independent prose oracle can.
    """

    return ValidationAuthority(
        metrics=[
            ResolvedMetricAuthority(
                metric_id=1234,
                metric_name="starting salary",
                role="primary",
            )
        ],
        selection=ResolvedSelectionAuthority(
            scope="named_districts",
            district_ids=[district_id],
        ),
    )


def _plan(*, question: str, named_phrase: str) -> QueryPlan:
    return QueryPlan(
        question=question,
        selection=SelectionSpec(scope="named_districts", districts=[named_phrase]),
        metrics=[MetricSpec(name="starting salary")],
    )


def test_real_but_wrong_state_district_is_flagged_dc() -> None:
    """User means DC; planner resolved a REAL 'Washington' district in WA."""

    plan = _plan(question="Starting salary in Washington DC", named_phrase="Washington DC")
    # REAL district, but in WA — the wrong geography.
    result = _ranking_result(district_id=2, district_name="Washington", state="WA")
    authority = _authority(district_id=2)

    report = validate_result(plan, result, authority=authority)

    codes = [finding.code for finding in report.findings]
    assert _GEOGRAPHY_CODE in codes, codes
    assert report.valid is False


def test_real_but_wrong_state_district_is_flagged_houston() -> None:
    """User means Houston (TX); planner resolved a real district elsewhere."""

    plan = _plan(question="Teacher pay in Houston", named_phrase="Houston")
    # A real "Houston" school district exists in MN; the user meant Texas.
    result = _ranking_result(district_id=7, district_name="Houston", state="MN")
    authority = _authority(district_id=7)

    report = validate_result(plan, result, authority=authority)

    codes = [finding.code for finding in report.findings]
    assert _GEOGRAPHY_CODE in codes, codes
    assert report.valid is False


def test_correct_geography_district_is_not_flagged() -> None:
    """Contrast PASS: user means DC and the resolved district IS in DC."""

    plan = _plan(question="Starting salary in Washington DC", named_phrase="Washington DC")
    result = _ranking_result(
        district_id=2,
        district_name="District of Columbia Public Schools",
        state="DC",
    )
    authority = _authority(district_id=2)

    report = validate_result(plan, result, authority=authority)

    codes = [finding.code for finding in report.findings]
    assert _GEOGRAPHY_CODE not in codes, codes


def test_ambiguous_intended_geography_does_not_overflag() -> None:
    """Fail-safe: a phrase with no derivable intended state must NOT flag.

    "Riverside" is a common district name with no unambiguous state signal in
    the prose. The validator biases toward flagging only CLEAR mismatches, so a
    correct-looking answer is not penalised when intent can't be re-derived.
    """

    plan = _plan(question="Starting salary in Riverside", named_phrase="Riverside")
    result = _ranking_result(district_id=9, district_name="Riverside", state="CA")
    authority = _authority(district_id=9)

    report = validate_result(plan, result, authority=authority)

    codes = [finding.code for finding in report.findings]
    assert _GEOGRAPHY_CODE not in codes, codes


def test_explicit_state_phrase_matches_resolved_state() -> None:
    """A typed explicit state ('Springfield, Illinois') that matches passes."""

    plan = _plan(
        question="Starting salary in Springfield, Illinois",
        named_phrase="Springfield, Illinois",
    )
    result = _ranking_result(district_id=11, district_name="Springfield", state="IL")
    authority = _authority(district_id=11)

    report = validate_result(plan, result, authority=authority)

    codes = [finding.code for finding in report.findings]
    assert _GEOGRAPHY_CODE not in codes, codes


def test_explicit_state_phrase_mismatch_is_flagged() -> None:
    """'Springfield, Illinois' resolved to a real Springfield in MO is flagged."""

    plan = _plan(
        question="Starting salary in Springfield, Illinois",
        named_phrase="Springfield, Illinois",
    )
    result = _ranking_result(district_id=12, district_name="Springfield", state="MO")
    authority = _authority(district_id=12)

    report = validate_result(plan, result, authority=authority)

    codes = [finding.code for finding in report.findings]
    assert _GEOGRAPHY_CODE in codes, codes
    assert report.valid is False


def _ranking_result_multi(
    districts: list[tuple[int, str, str]],
) -> MetricRankingResult:
    return MetricRankingResult(
        selection=ResultSelection(
            scope="named_districts",
            districts=[
                SelectedDistrict(district_id=i, district_name=n, state=s)
                for (i, n, s) in districts
            ],
        ),
        rows=[
            RankingRow(
                district_id=i,
                district_name=n,
                state=s,
                metric_id=1234,
                metric_name="starting salary",
                display_value="$50,000",
                academic_year="2024 - 2025",
                rank=rank,
            )
            for rank, (i, n, s) in enumerate(districts, start=1)
        ],
        total_considered=len(districts),
        excluded_count=0,
        order_statement="Ranking by starting salary.",
    )


def _authority_multi(district_ids: list[int]) -> ValidationAuthority:
    return ValidationAuthority(
        metrics=[
            ResolvedMetricAuthority(
                metric_id=1234, metric_name="starting salary", role="primary"
            )
        ],
        selection=ResolvedSelectionAuthority(
            scope="named_districts", district_ids=list(district_ids)
        ),
    )


def _plan_multi(*, question: str, named_phrases: list[str]) -> QueryPlan:
    return QueryPlan(
        question=question,
        selection=SelectionSpec(scope="named_districts", districts=list(named_phrases)),
        metrics=[MetricSpec(name="starting salary")],
    )


def test_multi_district_mixed_signal_does_not_overflag() -> None:
    """Coverage semantics: a strong-signal + weak-signal pair must NOT over-flag.

    "Houston and Riverside": intended={TX} only (Riverside is deliberately
    ambiguous → no signal). The earlier per-district rule flagged the correctly
    resolved Riverside (CA ∉ {TX}) — a false refusal of a correct answer. Under
    coverage semantics, TX is covered by Houston ISD, so nothing is flagged.
    """

    plan = _plan_multi(
        question="Compare starting salary in Houston and Riverside",
        named_phrases=["Houston", "Riverside"],
    )
    result = _ranking_result_multi(
        [(1, "Houston Independent School District", "TX"), (2, "Riverside", "CA")]
    )
    authority = _authority_multi([1, 2])

    report = validate_result(plan, result, authority=authority)

    codes = [finding.code for finding in report.findings]
    assert _GEOGRAPHY_CODE not in codes, codes


def test_multi_district_named_geography_uncovered_is_flagged() -> None:
    """Coverage semantics still catches a real-but-wrong district in a pair.

    "Washington DC and Houston": intended={DC, TX}. Resolver returns DCPS (DC)
    plus a real-but-WRONG Houston in MN. TX is named but covered by no resolved
    district → flag.
    """

    plan = _plan_multi(
        question="Compare starting salary in Washington DC and Houston",
        named_phrases=["Washington DC", "Houston"],
    )
    result = _ranking_result_multi(
        [
            (1, "District of Columbia Public Schools", "DC"),
            (7, "Houston", "MN"),  # real, but the wrong Houston
        ]
    )
    authority = _authority_multi([1, 7])

    report = validate_result(plan, result, authority=authority)

    codes = [finding.code for finding in report.findings]
    assert _GEOGRAPHY_CODE in codes, codes
    assert report.valid is False
