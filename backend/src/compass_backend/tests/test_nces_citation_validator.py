"""W2-M5-03 (#747) — ``profile_rows_carry_nces_citation`` validator tests.

The construction-side fix lives in ``execution/ranking.py:build_profile_ranking_result``
and ``execution/profile.py:build_profile_lookup_result``. This validator
is the evaluation-time check that catches a regression reintroducing
empty Sources columns on profile-field-backed answers — the exact
failure mode Ashley reported on Golden29 sheet rows 7/8/9.
"""

from __future__ import annotations

import asyncio

from compass_backend.artifacts import (
    MetricRankingResult,
    ProfileLookupResult,
    RankingRow,
    ResultSelection,
    SelectedDistrict,
)
from compass_backend.artifacts.citations import CitationRef
from compass_backend.artifacts.results import ProfileValueRow
from compass_backend.quality._evaluator_context import CompassEvaluatorContext
from compass_backend.tests.conftest import make_evaluator_context
from compass_backend.quality.validators import registry as _registry  # populate


def _profile_ranking_with_citation() -> MetricRankingResult:
    return MetricRankingResult(
        selection=ResultSelection(scope="all_covered_districts"),
        rows=[
            RankingRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=-1001,
                metric_name="Enrollment",
                value=60000.0,
                display_value="60,000",
                academic_year="2024 - 2025",
                rank=1,
                source="profile_field",
                citation_markers=[1],
                coverage_state="covered",
                coverage_display="60,000",
                coverage_reason="profile_field_value",
            ),
        ],
        citations=[
            CitationRef(
                marker=1,
                title="NCES Common Core of Data — ELSI",
                url="https://nces.ed.gov/ccd/elsi/",
                academic_year="2024 - 2025",
                document_type="NCES profile field",
            )
        ],
        total_considered=1,
        excluded_count=0,
        order_statement="Ranked by Enrollment, highest to lowest.",
    )


def _profile_lookup_with_citation() -> ProfileLookupResult:
    return ProfileLookupResult(
        selection=ResultSelection(
            scope="named_districts",
            districts=[SelectedDistrict(district_id=2, district_name="Bravo", state="CA")],
        ),
        rows=[
            ProfileValueRow(
                district_id=2,
                district_name="Bravo",
                state="CA",
                field_key="frpl_pct",
                label="FRPL %",
                value=42.5,
                display_value="42.5%",
                academic_year="2024 - 2025",
                source_label="NCES district profile",
                provenance="NCES directory year: 2024",
                metric_name="FRPL %",
                citation_markers=[1],
                coverage_state="covered",
                coverage_display="42.5%",
                coverage_reason="profile_field_value",
            )
        ],
        citations=[
            CitationRef(
                marker=1,
                title="NCES Common Core of Data — ELSI",
                url="https://nces.ed.gov/ccd/elsi/",
                academic_year="2024 - 2025",
                document_type="NCES profile field",
            )
        ],
        total_considered=1,
        excluded_count=0,
        order_statement="Looked up FRPL % from NCES profile fields.",
    )


def _ctx(result) -> CompassEvaluatorContext:
    return make_evaluator_context(
        session_id="sess-nces",
        turn_index=0,
        answer_text="placeholder",
        result=result,
    )


def _run(validator, ctx, payload=None):
    return asyncio.run(validator(ctx, payload or {}))


def test_profile_ranking_with_citation_passes() -> None:
    validator = _registry.lookup("profile_rows_carry_nces_citation")
    assert validator is not None
    outcome = _run(validator, _ctx(_profile_ranking_with_citation()))
    assert outcome.outcome == "pass"
    assert outcome.evidence["profile_row_count"] == 1


def test_profile_lookup_with_citation_passes() -> None:
    validator = _registry.lookup("profile_rows_carry_nces_citation")
    assert validator is not None
    outcome = _run(validator, _ctx(_profile_lookup_with_citation()))
    assert outcome.outcome == "pass"
    assert outcome.evidence["profile_row_count"] == 1


def test_profile_ranking_with_empty_markers_fails() -> None:
    """Simulate the canonical pre-fix bug state: BOTH
    `row.citation_markers=[]` AND `result.citations=[]` — exactly what
    `build_profile_ranking_result` produced before W2-M5-03 wired the
    NCES allowlist in. The validator must fail-loud rather than treat
    empty citations as a degenerate-pass."""
    result = _profile_ranking_with_citation()
    object.__setattr__(result.rows[0], "citation_markers", [])
    object.__setattr__(result, "citations", [])
    validator = _registry.lookup("profile_rows_carry_nces_citation")
    assert validator is not None
    outcome = _run(validator, _ctx(result))
    assert outcome.outcome == "fail"
    assert "no citation markers" in outcome.reason
    assert outcome.evidence["rows_without_marker_count"] == 1


def test_policy_metric_ranking_is_out_of_scope() -> None:
    """Validator passes trivially for non-profile rows — policy-answer
    ranking carries its own citation path (citation_markers_for_row),
    not the NCES allowlist path."""
    result = MetricRankingResult(
        selection=ResultSelection(scope="all_covered_districts"),
        rows=[
            RankingRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=100,
                metric_name="salary",
                value=80000.0,
                display_value="$80,000",
                academic_year="2024 - 2025",
                rank=1,
                source="policy_answer",
                citation_markers=[],  # no marker, but it's a policy_answer row
                coverage_state="covered",
                coverage_display="$80,000",
                coverage_reason="answer",
            ),
        ],
        citations=[],
        total_considered=1,
        excluded_count=0,
        order_statement="Ranked by salary, highest to lowest.",
    )
    validator = _registry.lookup("profile_rows_carry_nces_citation")
    assert validator is not None
    outcome = _run(validator, _ctx(result))
    assert outcome.outcome == "pass"
    assert outcome.evidence["profile_row_count"] == 0


def test_policy_metric_ranking_with_profile_sort_requires_nces_sort_proof() -> None:
    result = MetricRankingResult(
        selection=ResultSelection(scope="all_covered_districts"),
        rows=[
            RankingRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=100,
                metric_name="salary",
                value=80000.0,
                display_value="$80,000",
                academic_year="2024 - 2025",
                rank=1,
                source="policy_answer",
                citation_markers=[],
                coverage_state="covered",
                coverage_display="$80,000",
                coverage_reason="answer",
                sort_metric_id=-1001,
                sort_metric_name="enrollment",
                sort_display_value="60,000",
                sort_source_document="NCES district profile",
                sort_source_url=None,
            ),
        ],
        citations=[],
        total_considered=1,
        excluded_count=0,
        order_statement="Ranked by enrollment; displayed salary.",
    )
    validator = _registry.lookup("profile_rows_carry_nces_citation")
    assert validator is not None
    outcome = _run(validator, _ctx(result))
    assert outcome.outcome == "fail"
    assert "sort proof" in outcome.reason


def test_missing_result_returns_error() -> None:
    validator = _registry.lookup("profile_rows_carry_nces_citation")
    assert validator is not None
    outcome = _run(validator, _ctx(None))
    assert outcome.outcome == "error"
    assert "result" in outcome.reason
