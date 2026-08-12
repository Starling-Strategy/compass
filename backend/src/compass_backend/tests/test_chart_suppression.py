"""Tests for chart suppression based on sample size and variance.

The `_should_render_chart` predicate suppresses charts when there are fewer
than 3 numeric points or when all points round to the same value (at ≥6
decimal places precision). This prevents uninformative charts from being
rendered while allowing normal charts through.
"""

from __future__ import annotations


from compass_backend.artifacts.results import (
    CategoricalCountRow,
    MetricCountResult,
    MetricLookupResult,
    MetricRankingResult,
    MetricValueRow,
    RankingRow,
    ResultSelection,
    _should_render_chart,
)


# ─── Unit tests for the predicate ────────────────────────────────────────────


def test_should_render_chart_suppresses_below_3() -> None:
    """Charts with fewer than 3 numeric points should not render."""
    assert _should_render_chart([]) is False
    assert _should_render_chart([1.0]) is False
    assert _should_render_chart([1.0, 2.0]) is False


def test_should_render_chart_suppresses_no_variance() -> None:
    """Charts where all points round to the same value should not render."""
    # All identical
    assert _should_render_chart([1.0, 1.0, 1.0]) is False
    # Same after rounding to 6 decimals
    assert _should_render_chart([1.0, 1.0000001, 1.0000002]) is False
    # Different ranges but same after rounding
    assert _should_render_chart([5.5, 5.5000001, 5.5]) is False


def test_should_render_chart_renders_normal_case() -> None:
    """Charts with ≥3 points and variance should render."""
    assert _should_render_chart([1.0, 2.0, 3.0]) is True
    assert _should_render_chart([1.0, 1.0, 2.0]) is True
    assert _should_render_chart([1.5, 1.5000001, 2.0]) is True
    # Edge case: exactly at the rounding boundary
    assert _should_render_chart([1.0, 1.0000005, 2.0]) is True


# ─── Integration tests for _chart_for_ranking ───────────────────────────────


def _ranking_selection() -> ResultSelection:
    """Helper to construct a minimal ResultSelection."""
    return ResultSelection(
        scope="all_covered_districts",
        districts=[],
        unresolved_districts=[],
        states=[],
    )


def test_chart_for_ranking_suppresses_below_3_points() -> None:
    """_chart_for_ranking returns None when fewer than 3 numeric points."""
    result = MetricRankingResult(
        selection=_ranking_selection(),
        rows=[
            RankingRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=10,
                metric_name="Test metric",
                value=100,
                display_value="100",
                academic_year="2024 - 2025",
                rank=1,
                coverage_state="covered",
                coverage_display="100",
                coverage_reason="answer_value",
            ),
            RankingRow(
                district_id=2,
                district_name="Beta",
                state="CA",
                metric_id=10,
                metric_name="Test metric",
                value=200,
                display_value="200",
                academic_year="2024 - 2025",
                rank=2,
                coverage_state="covered",
                coverage_display="200",
                coverage_reason="answer_value",
            ),
        ],
        total_considered=2,
        excluded_count=0,
        order_statement="Ranked by metric.",
    )
    # Only 2 numeric points → suppressed
    assert result.chart is None


def test_chart_for_ranking_suppresses_no_variance() -> None:
    """_chart_for_ranking returns None when all values round to the same."""
    result = MetricRankingResult(
        selection=_ranking_selection(),
        rows=[
            RankingRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=10,
                metric_name="Test metric",
                value=1.0,
                display_value="1.0",
                academic_year="2024 - 2025",
                rank=1,
                coverage_state="covered",
                coverage_display="1.0",
                coverage_reason="answer_value",
            ),
            RankingRow(
                district_id=2,
                district_name="Beta",
                state="CA",
                metric_id=10,
                metric_name="Test metric",
                value=1.0000001,
                display_value="1.0000001",
                academic_year="2024 - 2025",
                rank=2,
                coverage_state="covered",
                coverage_display="1.0000001",
                coverage_reason="answer_value",
            ),
            RankingRow(
                district_id=3,
                district_name="Gamma",
                state="CA",
                metric_id=10,
                metric_name="Test metric",
                value=1.0000002,
                display_value="1.0000002",
                academic_year="2024 - 2025",
                rank=3,
                coverage_state="covered",
                coverage_display="1.0000002",
                coverage_reason="answer_value",
            ),
        ],
        total_considered=3,
        excluded_count=0,
        order_statement="Ranked by metric.",
    )
    # 3 points but no variance → suppressed
    assert result.chart is None


def test_chart_for_ranking_renders_normal_case() -> None:
    """_chart_for_ranking returns a ChartArtifact for normal data."""
    result = MetricRankingResult(
        selection=_ranking_selection(),
        rows=[
            RankingRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=10,
                metric_name="Test metric",
                value=100,
                display_value="100",
                academic_year="2024 - 2025",
                rank=1,
                coverage_state="covered",
                coverage_display="100",
                coverage_reason="answer_value",
            ),
            RankingRow(
                district_id=2,
                district_name="Beta",
                state="CA",
                metric_id=10,
                metric_name="Test metric",
                value=200,
                display_value="200",
                academic_year="2024 - 2025",
                rank=2,
                coverage_state="covered",
                coverage_display="200",
                coverage_reason="answer_value",
            ),
            RankingRow(
                district_id=3,
                district_name="Gamma",
                state="CA",
                metric_id=10,
                metric_name="Test metric",
                value=300,
                display_value="300",
                academic_year="2024 - 2025",
                rank=3,
                coverage_state="covered",
                coverage_display="300",
                coverage_reason="answer_value",
            ),
        ],
        total_considered=3,
        excluded_count=0,
        order_statement="Ranked by metric.",
    )
    # Normal case: 3 points with variance → rendered
    assert result.chart is not None
    assert result.chart.title == "Test metric by district"
    assert len(result.chart.points) == 3


# ─── Integration tests for _chart_for_metric_lookup ─────────────────────────


def test_chart_for_metric_lookup_suppresses_below_3_points() -> None:
    """_chart_for_metric_lookup returns None when fewer than 3 numeric points."""
    result = MetricLookupResult(
        selection=_ranking_selection(),
        rows=[
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=10,
                metric_name="Test metric",
                value=100,
                display_value="100",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="100",
                coverage_reason="answer_value",
            ),
            MetricValueRow(
                district_id=2,
                district_name="Beta",
                state="CA",
                metric_id=10,
                metric_name="Test metric",
                value=200,
                display_value="200",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="200",
                coverage_reason="answer_value",
            ),
        ],
        total_considered=2,
        excluded_count=0,
        order_statement="Looked up test metric.",
    )
    # Only 2 numeric points → suppressed
    assert result.chart is None


def test_chart_for_metric_lookup_suppresses_no_variance() -> None:
    """_chart_for_metric_lookup returns None when all values round to the same."""
    result = MetricLookupResult(
        selection=_ranking_selection(),
        rows=[
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=10,
                metric_name="Test metric",
                value=50.0,
                display_value="50.0",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="50.0",
                coverage_reason="answer_value",
            ),
            MetricValueRow(
                district_id=2,
                district_name="Beta",
                state="CA",
                metric_id=10,
                metric_name="Test metric",
                value=50.0000001,
                display_value="50.0000001",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="50.0000001",
                coverage_reason="answer_value",
            ),
            MetricValueRow(
                district_id=3,
                district_name="Gamma",
                state="CA",
                metric_id=10,
                metric_name="Test metric",
                value=50.0,
                display_value="50.0",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="50.0",
                coverage_reason="answer_value",
            ),
        ],
        total_considered=3,
        excluded_count=0,
        order_statement="Looked up test metric.",
    )
    # 3 points but no variance → suppressed
    assert result.chart is None


def test_chart_for_metric_lookup_renders_normal_case() -> None:
    """_chart_for_metric_lookup returns a ChartArtifact for normal data."""
    result = MetricLookupResult(
        selection=_ranking_selection(),
        rows=[
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=10,
                metric_name="Test metric",
                value=100,
                display_value="100",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="100",
                coverage_reason="answer_value",
            ),
            MetricValueRow(
                district_id=2,
                district_name="Beta",
                state="CA",
                metric_id=10,
                metric_name="Test metric",
                value=200,
                display_value="200",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="200",
                coverage_reason="answer_value",
            ),
            MetricValueRow(
                district_id=3,
                district_name="Gamma",
                state="CA",
                metric_id=10,
                metric_name="Test metric",
                value=300,
                display_value="300",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="300",
                coverage_reason="answer_value",
            ),
        ],
        total_considered=3,
        excluded_count=0,
        order_statement="Looked up test metric.",
    )
    # Normal case: 3 points with variance → rendered
    assert result.chart is not None
    assert result.chart.title == "Selected metrics by district"
    assert len(result.chart.points) == 3


# ─── Integration tests for _chart_for_metric_count ──────────────────────────


def test_chart_for_metric_count_suppresses_below_3_points() -> None:
    """_chart_for_metric_count returns None when fewer than 3 numeric points."""
    result = MetricCountResult(
        selection=_ranking_selection(),
        rows=[
            CategoricalCountRow(
                metric_id=10,
                metric_name="Test metric",
                value="Yes",
                category="Yes",
                display_value="2 of 5",
                academic_year="2024 - 2025",
                count=2,
                denominator=5,
                filter_statement="All covered districts",
                citation_markers=[],
            ),
            CategoricalCountRow(
                metric_id=10,
                metric_name="Test metric",
                value="No",
                category="No",
                display_value="3 of 5",
                academic_year="2024 - 2025",
                count=3,
                denominator=5,
                filter_statement="All covered districts",
                citation_markers=[],
            ),
        ],
        total_considered=2,
        excluded_count=0,
        order_statement="Counted categories.",
    )
    # Only 2 numeric points → suppressed
    assert result.chart is None


def test_chart_for_metric_count_suppresses_no_variance() -> None:
    """_chart_for_metric_count returns None when all counts round to the same."""
    result = MetricCountResult(
        selection=_ranking_selection(),
        rows=[
            CategoricalCountRow(
                metric_id=10,
                metric_name="Test metric",
                value="A",
                category="A",
                display_value="5 of 5",
                academic_year="2024 - 2025",
                count=5,
                denominator=5,
                filter_statement="All covered districts",
                citation_markers=[],
            ),
            CategoricalCountRow(
                metric_id=10,
                metric_name="Test metric",
                value="B",
                category="B",
                display_value="5 of 5",
                academic_year="2024 - 2025",
                count=5,
                denominator=5,
                filter_statement="All covered districts",
                citation_markers=[],
            ),
            CategoricalCountRow(
                metric_id=10,
                metric_name="Test metric",
                value="C",
                category="C",
                display_value="5 of 5",
                academic_year="2024 - 2025",
                count=5,
                denominator=5,
                filter_statement="All covered districts",
                citation_markers=[],
            ),
        ],
        total_considered=3,
        excluded_count=0,
        order_statement="Counted categories.",
    )
    # 3 points but no variance → suppressed
    assert result.chart is None


def test_chart_for_metric_count_renders_normal_case() -> None:
    """_chart_for_metric_count returns a ChartArtifact for normal data."""
    result = MetricCountResult(
        selection=_ranking_selection(),
        rows=[
            CategoricalCountRow(
                metric_id=10,
                metric_name="Test metric",
                value="Yes",
                category="Yes",
                display_value="2 of 5",
                academic_year="2024 - 2025",
                count=2,
                denominator=5,
                filter_statement="All covered districts",
                citation_markers=[],
            ),
            CategoricalCountRow(
                metric_id=10,
                metric_name="Test metric",
                value="No",
                category="No",
                display_value="3 of 5",
                academic_year="2024 - 2025",
                count=3,
                denominator=5,
                filter_statement="All covered districts",
                citation_markers=[],
            ),
            CategoricalCountRow(
                metric_id=10,
                metric_name="Test metric",
                value="Maybe",
                category="Maybe",
                display_value="5 of 5",
                academic_year="2024 - 2025",
                count=5,
                denominator=5,
                filter_statement="All covered districts",
                citation_markers=[],
            ),
        ],
        total_considered=3,
        excluded_count=0,
        order_statement="Counted categories.",
    )
    # Normal case: 3 points with variance → rendered
    assert result.chart is not None
    assert result.chart.title == "Test metric distribution"
    assert len(result.chart.points) == 3


# ─── Multi-series integration tests for _chart_for_metric_lookup ──────────────


def test_chart_for_metric_lookup_multi_series_happy_path() -> None:
    """_chart_for_metric_lookup with two metrics × 3 districts each produces multi-series chart.

    Verifies:
    - Returns a ChartArtifact (not None)
    - Series count = 2
    - show_legend = True (>1 series)
    - Series are sorted by metric name casefold
    """
    result = MetricLookupResult(
        selection=_ranking_selection(),
        rows=[
            # First metric: "Annual base salary"
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=10,
                metric_name="Annual base salary",
                value=50000,
                display_value="$50,000",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="$50,000",
                coverage_reason="answer_value",
            ),
            MetricValueRow(
                district_id=2,
                district_name="Beta",
                state="CA",
                metric_id=10,
                metric_name="Annual base salary",
                value=60000,
                display_value="$60,000",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="$60,000",
                coverage_reason="answer_value",
            ),
            MetricValueRow(
                district_id=3,
                district_name="Gamma",
                state="CA",
                metric_id=10,
                metric_name="Annual base salary",
                value=55000,
                display_value="$55,000",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="$55,000",
                coverage_reason="answer_value",
            ),
            # Second metric: "Mentor training hours"
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=20,
                metric_name="Mentor training hours",
                value=10,
                display_value="10",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="10",
                coverage_reason="answer_value",
            ),
            MetricValueRow(
                district_id=2,
                district_name="Beta",
                state="CA",
                metric_id=20,
                metric_name="Mentor training hours",
                value=15,
                display_value="15",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="15",
                coverage_reason="answer_value",
            ),
            MetricValueRow(
                district_id=3,
                district_name="Gamma",
                state="CA",
                metric_id=20,
                metric_name="Mentor training hours",
                value=20,
                display_value="20",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="20",
                coverage_reason="answer_value",
            ),
        ],
        total_considered=6,
        excluded_count=0,
        order_statement="Looked up two metrics.",
    )
    # Multi-series happy path: 2 metrics × 3 districts with variance
    assert result.chart is not None
    assert len(result.chart.series) == 2
    assert result.chart.show_legend is True
    # Series should be sorted by metric_name casefold: "Annual" < "Mentor"
    assert result.chart.series[0].label.casefold() < result.chart.series[1].label.casefold()


def test_chart_for_metric_lookup_per_series_predicate_filtering() -> None:
    """_chart_for_metric_lookup filters metrics per-series based on _should_render_chart.

    Verifies:
    - Metric A (3 points with variance) is included
    - Metric B (2 points) is filtered out by predicate
    - Final chart has 1 series (only A)
    - show_legend = False (only 1 series)
    """
    result = MetricLookupResult(
        selection=_ranking_selection(),
        rows=[
            # Metric A: "Annual base salary" - 3 points with variance → passes predicate
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=10,
                metric_name="Annual base salary",
                value=50000,
                display_value="$50,000",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="$50,000",
                coverage_reason="answer_value",
            ),
            MetricValueRow(
                district_id=2,
                district_name="Beta",
                state="CA",
                metric_id=10,
                metric_name="Annual base salary",
                value=60000,
                display_value="$60,000",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="$60,000",
                coverage_reason="answer_value",
            ),
            MetricValueRow(
                district_id=3,
                district_name="Gamma",
                state="CA",
                metric_id=10,
                metric_name="Annual base salary",
                value=55000,
                display_value="$55,000",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="$55,000",
                coverage_reason="answer_value",
            ),
            # Metric B: "Mentor training hours" - only 2 points → filtered by predicate
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=20,
                metric_name="Mentor training hours",
                value=10,
                display_value="10",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="10",
                coverage_reason="answer_value",
            ),
            MetricValueRow(
                district_id=2,
                district_name="Beta",
                state="CA",
                metric_id=20,
                metric_name="Mentor training hours",
                value=15,
                display_value="15",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="15",
                coverage_reason="answer_value",
            ),
        ],
        total_considered=5,
        excluded_count=0,
        order_statement="Looked up two metrics.",
    )
    # Per-series filtering: A passes (3 points), B filtered (2 points)
    assert result.chart is not None
    assert len(result.chart.series) == 1
    assert result.chart.series[0].label == "Annual base salary"
    assert result.chart.show_legend is False
