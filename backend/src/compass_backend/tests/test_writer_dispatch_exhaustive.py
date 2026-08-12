"""Tests that the writer dispatch hits the right renderer for every variant.

Pre-refactor, `_render_result` did string dispatch on `result.result_type`
with a silent fallback to `_render_metric_ranking`. Adding a new ResultType
without extending the dispatch would route through ranking and produce
markdown that looks plausible but was wrong.

These tests exercise one of each variant through `_render_result` and
assert a marker string unique to that variant's renderer appears in the
output. Together with the `assert_never(result)` exhaustiveness check in
the dispatch, this layer makes "silent fall-through to ranking" impossible.
"""

from __future__ import annotations


from compass_backend.artifacts import (
    CategoricalCountRow,
    MetricCountResult,
    MetricLookupResult,
    MetricRankingResult,
    MetricTrendResult,
    MetricValueRow,
    PeerComparisonResult,
    PeerComparisonRow,
    ProfileLookupResult,
    ProfileValueRow,
    RankingRow,
    ResultSelection,
    SelectedDistrict,
    ThresholdCountRow,
)
from compass_backend.contracts.planning import (
    MetricSpec,
    OutputSpec,
    ProfileFieldSpec,
    QueryPlan,
    SelectionSpec,
)
from compass_backend.rendering.writer import _render_result


def _plan(operation: str = "rank", *, metric_role: str = "primary") -> QueryPlan:
    kwargs: dict[str, object] = {
        "operation": operation,
        "question": "test question",
        "selection": SelectionSpec(
            scope="named_districts" if operation == "profile_lookup" else "all_covered_districts",
            districts=["Alpha"] if operation == "profile_lookup" else [],
        ),
        "output": OutputSpec(row_display="all"),
    }
    if operation == "profile_lookup":
        kwargs["profile_fields"] = [ProfileFieldSpec(name="enrollment")]
    else:
        kwargs["metrics"] = [MetricSpec(name="Test metric", role=metric_role)]
    return QueryPlan(**kwargs)  # type: ignore[arg-type]


def _ranking_selection() -> ResultSelection:
    return ResultSelection(
        scope="all_covered_districts",
        districts=[SelectedDistrict(district_id=1, district_name="Alpha", state="CA")],
    )


def test_dispatch_ranking_hits_ranking_renderer() -> None:
    result = MetricRankingResult(
        selection=_ranking_selection(),
        rows=[
            RankingRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=10,
                metric_name="Test metric",
                value=100.0,
                display_value="$100",
                academic_year="2024 - 2025",
                rank=1,
                coverage_state="covered",
                coverage_display="$100",
                coverage_reason="answer_value",
            )
        ],
        total_considered=1,
        excluded_count=0,
        order_statement="Ranked by Test metric, highest to lowest.",
    )
    body = _render_result(_plan("rank"), result)
    # Ranking renderer produces "| Rank | District | State | ..." header
    assert "| Rank |" in body
    assert "| District |" in body
    assert "| 1 |" in body  # rank value


def test_dispatch_lookup_hits_lookup_renderer() -> None:
    result = MetricLookupResult(
        selection=_ranking_selection(),
        rows=[
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=10,
                metric_name="Test metric",
                value=42,
                display_value="42",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="42",
                coverage_reason="answer_value",
            )
        ],
        total_considered=1,
        excluded_count=0,
        order_statement="Looked up Test metric for selected districts.",
    )
    body = _render_result(_plan("lookup"), result)
    # Lookup renderer header includes "Academic year" — ranking does not
    assert "| Academic year |" in body
    # Lookup does NOT include a "Rank" column
    assert "| Rank |" not in body


def test_dispatch_count_threshold_hits_threshold_renderer() -> None:
    result = MetricCountResult(
        selection=_ranking_selection(),
        rows=[
            ThresholdCountRow(
                metric_id=10,
                metric_name="Test metric",
                value=3,
                display_value="3 of 5 covered districts",
                academic_year="2024 - 2025",
                count=3,
                denominator=5,
                filter_statement="value >= 3",
                coverage_state="covered",
            )
        ],
        total_considered=5,
        excluded_count=0,
        order_statement="Counted qualifying districts for selected metrics.",
    )
    body = _render_result(_plan("count"), result)
    # Threshold count renderer header is "| Metric | Count | Denominator | Filter |"
    assert "| Metric |" in body
    assert "| Count |" in body
    assert "| Denominator |" in body
    assert "| Filter |" in body


def test_dispatch_count_categorical_hits_categorical_renderer() -> None:
    result = MetricCountResult(
        selection=_ranking_selection(),
        rows=[
            CategoricalCountRow(
                metric_id=10,
                metric_name="Test metric",
                value="Yes",
                category="Yes",
                display_value="2 of 5 covered districts",
                academic_year="2024 - 2025",
                count=2,
                denominator=5,
                percent=40.0,
                filter_statement="group by reviewed value",
                coverage_state="covered",
            )
        ],
        total_considered=5,
        excluded_count=0,
        order_statement="Counted categorical value distribution.",
    )
    body = _render_result(_plan("count", metric_role="grouping"), result)
    # Categorical renderer lead is "Counted ... distribution."
    assert "distribution" in body.lower()
    # Categorical table column is "District count" (vs "Count" on threshold)
    assert "District count" in body


def test_dispatch_trend_hits_trend_renderer() -> None:
    result = MetricTrendResult(
        selection=_ranking_selection(),
        rows=[
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=10,
                metric_name="Test metric",
                value=50000.0,
                display_value="$50,000",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="$50,000",
                coverage_reason="answer_value",
            )
        ],
        total_considered=1,
        excluded_count=0,
        order_statement="Trend for Test metric.",
    )
    body = _render_result(_plan("trend"), result)
    # Trend renderer lead is "Built a chronological trend for ..."
    assert "chronological trend" in body
    # Trend table has a "Change" column
    assert "Change" in body


def test_dispatch_profile_hits_profile_renderer() -> None:
    result = ProfileLookupResult(
        selection=_ranking_selection(),
        rows=[
            ProfileValueRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                field_key="enrollment",
                label="Student enrollment",
                metric_name="Student enrollment",
                value=50000,
                display_value="50,000",
                academic_year="2023",
                source_label="NCES directory",
                provenance="NCES directory year: 2023",
            )
        ],
        total_considered=1,
        excluded_count=0,
        order_statement="Looked up profile fields for selected districts.",
    )
    body = _render_result(_plan("profile_lookup"), result)
    # Profile renderer lead mentions "NCES/profile fields"
    assert "NCES/profile fields" in body
    assert "Covered by Compass" in body


def test_dispatch_peer_hits_peer_renderer() -> None:
    result = PeerComparisonResult(
        selection=ResultSelection(
            scope="named_districts",
            districts=[SelectedDistrict(district_id=1, district_name="Alpha", state="CA")],
        ),
        rows=[
            PeerComparisonRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=10,
                metric_name="Test metric",
                value=100.0,
                display_value="$100",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="$100",
                coverage_reason="answer_value",
                peer_role="anchor",
            )
        ],
        total_considered=1,
        excluded_count=0,
        order_statement="Compared peer policy metrics.",
    )
    body = _render_result(_plan("peer_comparison"), result)
    # Peer renderer header has "Peer Rank" and "Peer Rationale"
    assert "Peer Rank" in body
    assert "Peer Rationale" in body


def _similarity_peer_comparison_result() -> PeerComparisonResult:
    """Construct a similarity-shaped PeerComparisonResult with metric_id=0 sentinel rows."""
    return PeerComparisonResult(
        selection=ResultSelection(
            scope="named_districts",
            districts=[
                SelectedDistrict(district_id=1, district_name="Denver", state="CO"),
                SelectedDistrict(district_id=2, district_name="Aurora", state="CO"),
            ],
        ),
        rows=[
            PeerComparisonRow(
                district_id=1,
                district_name="Denver",
                state="CO",
                # Sentinel metric_id=0: similarity-only row, no policy metric.
                metric_id=0,
                metric_name="similarity",
                value=None,
                display_value="anchor",
                academic_year="2024 - 2025",
                source="coverage_state",
                coverage_state="covered",
                coverage_display="Anchor district",
                coverage_reason="Anchor district selected by the user.",
                peer_role="anchor",
                peer_rank=None,
                peer_score=None,
                peer_reason="Anchor district selected by the user.",
            ),
            PeerComparisonRow(
                district_id=2,
                district_name="Aurora",
                state="CO",
                metric_id=0,
                metric_name="similarity",
                value=0.87,
                display_value="0.87",
                academic_year="2024 - 2025",
                source="coverage_state",
                coverage_state="covered",
                coverage_display="Similarity score: 0.87",
                coverage_reason="NCES-similar by geography, enrollment, locale.",
                peer_role="peer",
                peer_rank=1,
                peer_score=0.87,
                peer_reason="NCES-similar by geography, enrollment, locale.",
            ),
        ],
        total_considered=2,
        excluded_count=0,
        order_statement="Found 1 NCES-similar peer district for Denver.",
    )


def test_dispatch_similarity_hits_similarity_renderer() -> None:
    """A PeerComparisonResult with metric_id=0 rows routes to _render_similarity."""
    result = _similarity_peer_comparison_result()
    body = _render_result(_plan("peer_comparison"), result)

    # Similarity renderer uses the order_statement as header — NOT "Compared policy metrics"
    assert "NCES-similar peer district" in body
    assert "Compared policy metrics" not in body


def test_similarity_renderer_does_not_show_metric_column() -> None:
    """Similarity result must NOT expose a Metric=similarity column in the table."""
    result = _similarity_peer_comparison_result()
    body = _render_result(_plan("peer_comparison"), result)

    # The similarity renderer table has Role | Rank | District | State | Similarity Score | Peer Rationale
    # It must NOT have a "Metric" column (which would show metric_name="similarity")
    assert "| Metric |" not in body
    # And must NOT show "similarity" as a Metric cell value
    assert "| similarity |" not in body


def test_similarity_renderer_shows_peer_rationale() -> None:
    """Peer Rationale column is present in the similarity renderer output."""
    result = _similarity_peer_comparison_result()
    body = _render_result(_plan("peer_comparison"), result)

    assert "Peer Rationale" in body
    assert "NCES-similar by geography" in body


def test_similarity_renderer_shows_similarity_score_column() -> None:
    """Similarity Score column is present and anchor row shows em-dash (no score)."""
    result = _similarity_peer_comparison_result()
    body = _render_result(_plan("peer_comparison"), result)

    assert "Similarity Score" in body
    # Peer row has score=0.87
    assert "0.87" in body
    # Anchor row has score=None → rendered as "—"
    assert "—" in body


def test_regular_peer_comparison_result_not_treated_as_similarity() -> None:
    """A PeerComparisonResult with non-zero metric_id must NOT route to _render_similarity."""
    result = PeerComparisonResult(
        selection=ResultSelection(
            scope="named_districts",
            districts=[SelectedDistrict(district_id=1, district_name="Alpha", state="CA")],
        ),
        rows=[
            PeerComparisonRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=42,  # non-zero: real policy metric
                metric_name="Starting salary",
                value=50000.0,
                display_value="$50,000",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="$50,000",
                coverage_reason="answer_value",
                peer_role="anchor",
            )
        ],
        total_considered=1,
        excluded_count=0,
        order_statement="Compared peer policy metrics.",
    )
    assert not result.is_similarity_result
    body = _render_result(_plan("peer_comparison"), result)
    assert "comparison-ready peer districts" in body
    assert "Similarity Score" not in body


def test_is_similarity_result_true_for_sentinel_metric_id() -> None:
    """is_similarity_result is True when all rows use metric_id=0 sentinel."""
    result = _similarity_peer_comparison_result()
    assert result.is_similarity_result is True


def test_is_similarity_result_false_for_empty_rows() -> None:
    """is_similarity_result is False when rows list is empty."""
    result = PeerComparisonResult(
        selection=ResultSelection(scope="all_covered_districts"),
        rows=[],
        total_considered=0,
        excluded_count=0,
        order_statement="No rows.",
    )
    assert result.is_similarity_result is False


def test_comparison_lookup_prior_year_lines_render_below_table() -> None:
    """#1756 / B12: a multi-metric comparison-lookup answer must put every
    per-district coverage sentence — including a stale prior-year
    ("NCTQ last reviewed… the value then was X") line for a district that
    KEEPS a table row — AFTER the markdown table, never before it.

    The fixture is a single MIXED district: Duval County has one answer cell
    (Sick leave days = 10) and one stale prior-year cell (Salary, last
    reviewed in a prior year), so it keeps its table row AND carries a
    prior-year coverage sentence. On pre-fix main that sentence rendered above
    the table header; after the fix it flows through
    _append_coverage_disclosure_section below the table like every other
    lookup path.
    """

    prior_year_sentence = (
        "NCTQ last reviewed Duval County Public Schools for Leave in "
        "2023 - 2024; the value then was 10."
    )
    result = MetricLookupResult(
        selection=ResultSelection(
            scope="named_districts",
            districts=[
                SelectedDistrict(
                    district_id=1,
                    district_name="Duval County Public Schools",
                    state="FL",
                )
            ],
        ),
        rows=[
            MetricValueRow(
                district_id=1,
                district_name="Duval County Public Schools",
                state="FL",
                metric_id=10,
                metric_name="Sick leave days",
                value=10,
                display_value="10",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="10",
                coverage_reason="answer_value",
            ),
            MetricValueRow(
                district_id=1,
                district_name="Duval County Public Schools",
                state="FL",
                metric_id=11,
                metric_name="Salary",
                value=None,
                display_value=prior_year_sentence,
                academic_year="2024 - 2025",
                coverage_state="not_reviewed",
                coverage_display=prior_year_sentence,
                coverage_reason="stale_recent_answer",
            ),
        ],
        total_considered=1,
        excluded_count=0,
        order_statement="Compared selected metrics for selected districts.",
    )
    body = _render_result(_plan("lookup"), result)

    # Two distinct metric names route through the comparison-lookup path,
    # which builds a markdown table with a "| District |" header.
    table_header_index = body.index("| District |")
    coverage_index = body.index(prior_year_sentence)
    # The table header (and every data row under it) must precede the
    # prior-year coverage sentence — no coverage line above the table.
    assert table_header_index < coverage_index, (
        "prior-year coverage sentence rendered before the table:\n" + body
    )
