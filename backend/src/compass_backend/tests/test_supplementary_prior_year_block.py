"""Renderer tests: stale prior-year disclosures render as D7 narrative (#1514).

A stale district (reason="stale_recent_answer") never gets a table row, but it
re-enters the rendered narrative as the canonical prior-year sentence —
"NCTQ last reviewed {district} for {subject} in {prior_year}; the value then
was {value}." — inside the "Not included in ranking" availability block
(#1514 D8, reversing the #1435 whole-body suppression). The #1435 decisions
that survive: no supplementary "Most recent available" table, and the CSV
still drops stale disclosure rows.
"""

from compass_backend.artifacts import (
    CitationRef,
    CoverageBreakdown,
    CoverageDisclosure,
    CoverageFrame,
    MethodologyRef,
    MetricRankingResult,
    RankingRow,
    ResultSelection,
    ResultSet,
    SelectedDistrict,
)
from compass_backend.contracts import (
    MetricSpec,
    QueryPlan,
    SelectionSpec,
    TemporalSpec,
    ValidationReport,
)
from compass_backend.rendering import render_response
from compass_backend.rendering.composer import _format_coverage_disclosure


_METRIC_NAME = "Annual base salary for a first year teacher with a bachelor's degree"


def test_format_coverage_disclosure_named_reason_voices_sentence_not_label() -> None:
    """#1702 (criterion 26, case 14): the ranking path no longer routes
    not_reviewed/unavailable through ``_format_coverage_disclosure`` — those gaps
    are COUNTED upstream. The formatter is now the NAMED-reason formatter: a
    stale (prior-year) district is voiced as its canonical sentence (already on
    ``disclosure.display``), never a "{district}: Not reviewed" short-label
    pairing the judge flags as a per-district placeholder.
    """
    disclosure = CoverageDisclosure(
        district_id=1,
        district_name="Houston Independent School District",
        state="TX",
        metric_id=5,
        metric_name="Minimum number of formal observations",
        academic_year="2024 - 2025",
        coverage_state="not_reviewed",
        display=(
            "NCTQ last reviewed Houston Independent School District for "
            "Minimum number of formal observations in 2023 - 2024; the value "
            "then was 1."
        ),
        reason="stale_recent_answer",
        prior_academic_year="2023 - 2024",
        prior_display_value="1",
    )

    line = _format_coverage_disclosure(disclosure)

    assert line == disclosure.display
    assert ": Not reviewed" not in line


def _plan(intent: str = "current") -> QueryPlan:
    return QueryPlan(
        question=(
            "Of the 10 largest districts by enrollment, which pay teachers "
            "the highest starting salary?"
        ),
        selection=SelectionSpec(scope="largest_districts"),
        metrics=[MetricSpec(name=_METRIC_NAME)],
        temporal=TemporalSpec(intent=intent),
    )


def _valid_report() -> ValidationReport:
    return ValidationReport(
        valid=True,
        dimensions_checked=[
            "selection",
            "metric",
            "sort_order",
            "coverage_state",
            "citation_coverage",
            "numeric_token_provenance",
        ],
        findings=[],
    )


def _current_row(
    *,
    district_id: int,
    district_name: str,
    state: str,
    value: float,
    display_value: str,
    rank: int,
    citation_marker: int,
) -> RankingRow:
    return RankingRow(
        district_id=district_id,
        district_name=district_name,
        state=state,
        metric_id=89,
        metric_name=_METRIC_NAME,
        value=value,
        display_value=display_value,
        academic_year="2024 - 2025",
        rank=rank,
        citation_markers=[citation_marker],
        coverage_state="covered",
        coverage_display=display_value,
        coverage_reason="answer_value",
    )


def _stale_display(district_name: str, prior_value: str, prior_year: str) -> str:
    """The canonical D7 sentence the label authority emits (#1514)."""

    return (
        f"NCTQ last reviewed {district_name} for {_METRIC_NAME} in "
        f"{prior_year}; the value then was {prior_value}."
    )


def _stale_disclosure(
    *,
    district_id: int,
    district_name: str,
    state: str,
    prior_value: str,
    prior_year: str = "2022 - 2023",
) -> CoverageDisclosure:
    return CoverageDisclosure(
        district_id=district_id,
        district_name=district_name,
        state=state,
        metric_id=89,
        metric_name=_METRIC_NAME,
        academic_year="2024 - 2025",
        coverage_state="not_reviewed",
        display=_stale_display(district_name, prior_value, prior_year),
        reason="stale_recent_answer",
        prior_academic_year=prior_year,
        prior_display_value=prior_value,
    )


def _result_two_current_three_stale() -> ResultSet:
    rows = [
        _current_row(
            district_id=57,
            district_name="Gwinnett County Public Schools",
            state="GA",
            value=59146.0,
            display_value="$59,146",
            rank=1,
            citation_marker=1,
        ),
        _current_row(
            district_id=37,
            district_name="Broward County Public Schools",
            state="FL",
            value=51402.0,
            display_value="$51,402",
            rank=2,
            citation_marker=2,
        ),
    ]
    disclosures = [
        _stale_disclosure(
            district_id=60,
            district_name="Chicago Public Schools",
            state="IL",
            prior_value="$66,330",
            prior_year="2023 - 2024",
        ),
        _stale_disclosure(
            district_id=157,
            district_name="Houston Independent School District",
            state="TX",
            prior_value="$61,500",
        ),
        _stale_disclosure(
            district_id=15,
            district_name="Los Angeles Unified School District",
            state="CA",
            prior_value="$56,107",
        ),
    ]
    selection = ResultSelection(
        scope="largest_districts",
        districts=[
            SelectedDistrict(
                district_id=row.district_id,
                district_name=row.district_name,
                state=row.state,
            )
            for row in rows
        ]
        + [
            SelectedDistrict(
                district_id=d.district_id,
                district_name=d.district_name,
                state=d.state,
            )
            for d in disclosures
        ],
    )
    citations = [
        CitationRef(
            marker=1,
            title="Gwinnett salary scale 2024-2025",
            url="https://example.org/gwinnett.pdf",
            academic_year="2024 - 2025",
            district_id=57,
        ),
        CitationRef(
            marker=2,
            title="Broward salary scale 2024-2025",
            url="https://example.org/broward.pdf",
            academic_year="2024 - 2025",
            district_id=37,
        ),
    ]
    return MetricRankingResult(
        selection=selection,
        rows=rows,
        citations=citations,
        coverage_frame=CoverageFrame(
            universe_count=5,
            in_scope_count=5,
            addressed_count=5,
            real_data_count=2,
            not_reviewed_count=3,
            out_of_universe_count=0,
            coverage_ratio=2 / 5,
            breakdown=CoverageBreakdown(
                answer_value_count=2,
                stale_recent_answer_count=3,
            ),
        ),
        coverage_disclosures=disclosures,
        total_considered=5,
        excluded_count=3,
        order_statement=(
            "Ranked by Annual base salary for a first year teacher with a "
            "bachelor's degree, highest to lowest."
        ),
        methodology_codes=[
            MethodologyRef(code="citation_answer_level_preferred_source_fallback")
        ],
    )


def _result_all_stale() -> ResultSet:
    """A ranking where EVERY district is stale (zero answer rows) — exercises the
    empty-display_rows branch the #1228 regression guard below depends on."""

    disclosures = [
        _stale_disclosure(
            district_id=60,
            district_name="Chicago Public Schools",
            state="IL",
            prior_value="$66,330",
            prior_year="2023 - 2024",
        ),
        _stale_disclosure(
            district_id=157,
            district_name="Houston Independent School District",
            state="TX",
            prior_value="$61,500",
        ),
        _stale_disclosure(
            district_id=15,
            district_name="Los Angeles Unified School District",
            state="CA",
            prior_value="$56,107",
        ),
    ]
    selection = ResultSelection(
        scope="largest_districts",
        districts=[
            SelectedDistrict(
                district_id=d.district_id,
                district_name=d.district_name,
                state=d.state,
            )
            for d in disclosures
        ],
    )
    return MetricRankingResult(
        selection=selection,
        rows=[],
        citations=[],
        coverage_frame=CoverageFrame(
            universe_count=3,
            in_scope_count=3,
            addressed_count=3,
            real_data_count=0,
            not_reviewed_count=3,
            out_of_universe_count=0,
            coverage_ratio=0.0,
            breakdown=CoverageBreakdown(
                answer_value_count=0,
                stale_recent_answer_count=3,
            ),
        ),
        coverage_disclosures=disclosures,
        total_considered=3,
        excluded_count=3,
        order_statement=(
            "Ranked by Annual base salary for a first year teacher with a "
            "bachelor's degree, highest to lowest."
        ),
        methodology_codes=[
            MethodologyRef(code="citation_answer_level_preferred_source_fallback")
        ],
    )


def test_all_stale_ranking_keeps_disclosure_visible_not_folded() -> None:
    """#1228 regression: when EVERY district is stale (no answer rows), the
    per-district 'NCTQ last reviewed …' sentences ARE the whole answer — they
    must stay visible, never under a '#### ' heading the frontend would fold out
    of sight (the empty-table guard mirrors _render_metric_lookup)."""

    manifest = render_response(_plan(), _result_all_stale(), _valid_report())
    body = manifest.body

    assert manifest.status == "rendered"
    # The disclosure sentences are the answer, and they are present.
    assert "NCTQ last reviewed Chicago Public Schools" in body
    # There is no answer table to lead with.
    assert "| Rank | District |" not in body
    # CRITICAL: the availability section is NOT a folded #### heading — that
    # would hide the only content. It renders as the plain, visible label.
    assert "#### Not included in ranking" not in body
    assert "Not included in ranking" in body


def test_secondary_sections_render_as_h4_headings() -> None:
    """#1228 renderer->frontend contract on the RANKING path: every secondary
    section is emitted as an H4 (``#### ``) heading, and the lead + answer table
    are never under a heading (answer-first).

    The chat UI folds *every* top-level ``<h4>`` section into a collapsible
    (``collapsifyAgentSections``), so this convention is what makes that fold
    correct by construction on this path: H4 means "foldable secondary section."
    The lookup path is pinned by the companion test below. (Some sibling paths —
    comparison-lookup, categorical-count — remain coverage-first in Phase 1; see
    the plan doc §8, so this is deliberately scoped to ranking, not a
    renderer-wide invariant.)
    """

    manifest = render_response(
        _plan(), _result_two_current_three_stale(), _valid_report()
    )
    _assert_h4_section_contract(
        manifest.body, expected=("Not included in ranking", "Methodology")
    )


def _assert_h4_section_contract(body: str, *, expected: tuple[str, ...]) -> None:
    """Shared H4 section-contract assertions for a rendered answer body."""

    for label in expected:
        assert f"#### {label}" in body, f"missing #### {label}"
    # Every heading in the body is H4 — no stray ##/### section dividers, and no
    # secondary-section label leaks as a bare paragraph line.
    for line in body.splitlines():
        if line.lstrip().startswith("#"):
            assert line.startswith("#### "), f"non-H4 heading leaked: {line!r}"
    for label in expected:
        assert f"\n{label}\n" not in f"\n{body}\n", f"{label} leaked as bare line"
    # Answer-first: the body opens with the lead prose, not a heading or table.
    assert not body.lstrip().startswith("#")
    assert not body.lstrip().startswith("|")


def test_stale_districts_render_prior_year_narrative() -> None:
    """#1514 D8: every stale district gets the canonical D7 sentence —
    name, prior year, AND prior value — in the availability block."""

    manifest = render_response(_plan(), _result_two_current_three_stale(), _valid_report())

    assert manifest.status == "rendered"

    # No supplementary table; the narrative carries the prior-year facts.
    assert "Most recent available" not in manifest.body

    assert "Not included in ranking" in manifest.body
    assert (
        "- NCTQ last reviewed Chicago Public Schools for "
        f"{_METRIC_NAME} in 2023 - 2024; the value then was $66,330."
    ) in manifest.body
    assert (
        "- NCTQ last reviewed Houston Independent School District for "
        f"{_METRIC_NAME} in 2022 - 2023; the value then was $61,500."
    ) in manifest.body
    assert (
        "- NCTQ last reviewed Los Angeles Unified School District for "
        f"{_METRIC_NAME} in 2022 - 2023; the value then was $56,107."
    ) in manifest.body

    # Never as table rows, never via the retired short label.
    assert "| Chicago Public Schools" not in manifest.body
    assert "| Houston Independent School District" not in manifest.body
    assert "Older year only" not in manifest.body

    # #1228 / VOICE-R1: the answer table leads; the prior-year narrative now
    # FOLLOWS it (under a collapsible #### heading), reversing the prior
    # coverage-first ordering.
    assert manifest.body.index("NCTQ last reviewed Chicago Public Schools") > (
        manifest.body.index("| Rank | District |")
    )

    # Ranked current rows ARE present.
    assert "Gwinnett County Public Schools" in manifest.body
    assert "Broward County Public Schools" in manifest.body

    # The CSV still drops stale disclosure rows (#1435 survives in the export).
    result = _result_two_current_three_stale()
    assert result.csv_export is not None
    csv_names = {row["district_name"] for row in result.csv_export.rows}
    assert "Chicago Public Schools" not in csv_names


def test_full_current_coverage_no_stale_output() -> None:
    """No stale disclosures → no prior-year narrative anywhere (unchanged)."""

    rows = [
        _current_row(
            district_id=57,
            district_name="Gwinnett County Public Schools",
            state="GA",
            value=59146.0,
            display_value="$59,146",
            rank=1,
            citation_marker=1,
        ),
        _current_row(
            district_id=37,
            district_name="Broward County Public Schools",
            state="FL",
            value=51402.0,
            display_value="$51,402",
            rank=2,
            citation_marker=2,
        ),
    ]
    result = MetricRankingResult(
        rows=rows,
        citations=[
            CitationRef(
                marker=1,
                title="Gwinnett salary scale 2024-2025",
                url="https://example.org/gwinnett.pdf",
                academic_year="2024 - 2025",
                district_id=57,
            ),
            CitationRef(
                marker=2,
                title="Broward salary scale 2024-2025",
                url="https://example.org/broward.pdf",
                academic_year="2024 - 2025",
                district_id=37,
            ),
        ],
        total_considered=2,
        excluded_count=0,
        order_statement=(
            "Ranked by Annual base salary for a first year teacher with a "
            "bachelor's degree, highest to lowest."
        ),
        methodology_codes=[
            MethodologyRef(code="citation_answer_level_preferred_source_fallback")
        ],
    )
    manifest = render_response(_plan(), result, _valid_report())

    assert manifest.status == "rendered"
    assert "Most recent available" not in manifest.body
    assert "NCTQ last reviewed" not in manifest.body


def test_mixed_reasons_stale_and_not_reviewed_both_voiced() -> None:
    """Stale rows render the D7 sentence; never-reviewed districts keep their
    short-label bullets — both inside one availability block."""

    rows = [
        _current_row(
            district_id=57,
            district_name="Gwinnett County Public Schools",
            state="GA",
            value=59146.0,
            display_value="$59,146",
            rank=1,
            citation_marker=1,
        ),
    ]
    stale = [
        _stale_disclosure(
            district_id=60,
            district_name="Chicago Public Schools",
            state="IL",
            prior_value="$66,330",
            prior_year="2023 - 2024",
        ),
        _stale_disclosure(
            district_id=157,
            district_name="Houston Independent School District",
            state="TX",
            prior_value="$61,500",
        ),
    ]
    not_reviewed = [
        CoverageDisclosure(
            district_id=400 + i,
            district_name=f"Never-Reviewed District {i}",
            state="CA",
            metric_id=89,
            metric_name=_METRIC_NAME,
            academic_year="2024 - 2025",
            coverage_state="not_reviewed",
            display=f"NCTQ hasn't reviewed Never-Reviewed District {i} for 2024 - 2025 yet.",
            reason="district_not_reviewed",
        )
        for i in (1, 2)
    ]
    disclosures = stale + not_reviewed
    selection = ResultSelection(
        scope="largest_districts",
        districts=[
            SelectedDistrict(district_id=57, district_name="Gwinnett County Public Schools", state="GA"),
            *(
                SelectedDistrict(
                    district_id=d.district_id,
                    district_name=d.district_name,
                    state=d.state,
                )
                for d in disclosures
            ),
        ],
    )
    result = MetricRankingResult(
        selection=selection,
        rows=rows,
        citations=[
            CitationRef(
                marker=1,
                title="Gwinnett salary scale 2024-2025",
                url="https://example.org/gwinnett.pdf",
                academic_year="2024 - 2025",
                district_id=57,
            ),
        ],
        coverage_frame=CoverageFrame(
            universe_count=5,
            in_scope_count=5,
            addressed_count=3,
            real_data_count=1,
            not_reviewed_count=4,
            out_of_universe_count=0,
            coverage_ratio=1 / 5,
            sparse=True,
            breakdown=CoverageBreakdown(
                answer_value_count=1,
                stale_recent_answer_count=2,
                district_not_reviewed_count=2,
            ),
        ),
        coverage_disclosures=disclosures,
        total_considered=5,
        excluded_count=4,
        order_statement=(
            "Ranked by Annual base salary for a first year teacher with a "
            "bachelor's degree, highest to lowest."
        ),
        methodology_codes=[
            MethodologyRef(code="citation_answer_level_preferred_source_fallback")
        ],
    )

    manifest = render_response(_plan(), result, _valid_report())

    assert manifest.status == "rendered"

    assert "Not included in ranking" in manifest.body

    # Stale districts appear with the D7 sentence (name + prior year + value).
    assert (
        "- NCTQ last reviewed Chicago Public Schools for "
        f"{_METRIC_NAME} in 2023 - 2024; the value then was $66,330."
    ) in manifest.body
    assert (
        "- NCTQ last reviewed Houston Independent School District for "
        f"{_METRIC_NAME} in 2022 - 2023; the value then was $61,500."
    ) in manifest.body

    # #1702: never-reviewed districts are COUNTED (criterion 26), not named
    # per-district. The 2 district_not_reviewed gaps collapse to one counted
    # clause whose "2" is backed by breakdown.district_not_reviewed_count.
    assert (
        "- NCTQ hasn't reviewed 2 districts for the requested year yet."
    ) in manifest.body
    assert "NCTQ hasn't reviewed Never-Reviewed District" not in manifest.body
    assert "Never-Reviewed District 1: Not reviewed" not in manifest.body

    # No supplementary block heading, no retired vocabulary.
    assert "Most recent available" not in manifest.body
    assert "Older year only" not in manifest.body


def test_specific_year_intent_stale_renders_same_narrative() -> None:
    """temporal.intent='specific_year' → same D8 narrative path."""

    manifest = render_response(
        _plan(intent="specific_year"),
        _result_two_current_three_stale(),
        _valid_report(),
    )

    assert manifest.status == "rendered"
    assert "Most recent available" not in manifest.body
    assert (
        "NCTQ last reviewed Chicago Public Schools for "
        f"{_METRIC_NAME} in 2023 - 2024; the value then was $66,330."
    ) in manifest.body
