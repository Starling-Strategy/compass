"""W2-M3-01 phase 4 (#861): renderer formats CompositeRankingResult.

Phase 1 (#852) introduced the ``CompositeRankingResult`` typed artifact.
Phase 2 (#858) added the planner emit signal ``requires_composite_ranking``.
Phase 3 (#860) wired the executor: it now returns
``ExecutionSuccess(result=CompositeRankingResult(children=[...]))`` when the
flag is set.

Phase 4 teaches the renderer to format the composite envelope. Before this
PR, ``writer._render_result`` ends its ``match`` block with
``case _ as unreachable: assert_never(unreachable)``, which means rendering
any ``CompositeRankingResult`` raises an ``AssertionError`` — a hard crash
in production, not a degraded response.

These tests pin the contract: ``render_response`` returns a successful
``ResponseManifest`` whose body contains one markdown rank table per
child, in order, with a composite-level lead line, and the manifest's
``result_type`` discriminator surfaces as ``"composite_ranking"``.
"""

from __future__ import annotations

from compass_backend.artifacts import (
    CitationRef,
    CompositeRankingResult,
    MethodologyRef,
    MetricRankingResult,
    RankingRow,
    ResultSelection,
    SelectedDistrict,
)
from compass_backend.contracts import (
    MetricSpec,
    QueryPlan,
    SelectionSpec,
    ValidationReport,
)
from compass_backend.rendering import render_response


def _valid_report() -> ValidationReport:
    return ValidationReport(
        valid=True,
        dimensions_checked=["selection", "metric"],
        findings=[],
    )


def _composite_plan() -> QueryPlan:
    """Plan that would produce a composite envelope (post phase 2)."""

    return QueryPlan(
        operation="rank",
        question="Rank covered districts on all four benefits metrics, separately.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="BA starting salary"),
            MetricSpec(name="MA starting salary"),
            MetricSpec(name="PhD starting salary"),
            MetricSpec(name="EdD starting salary"),
        ],
        requires_composite_ranking=True,
    )


def _ranking_child(metric_id: int, metric_name: str) -> MetricRankingResult:
    """Build a fully validated MetricRankingResult to use as a composite child.

    Mirrors the canonical fixture in test_mvp_rendering.py:_result() but
    parameterized over metric id/name so each child has distinct content.
    """

    return MetricRankingResult(
        selection=ResultSelection(
            scope="all_covered_districts",
            districts=[
                SelectedDistrict(district_id=1, district_name="Alpha", state="CA"),
                SelectedDistrict(district_id=2, district_name="Bravo", state="CA"),
            ],
        ),
        rows=[
            RankingRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=metric_id,
                metric_name=metric_name,
                value=80000.0 + metric_id,
                display_value=f"${80_000 + metric_id:,}",
                academic_year="2024 - 2025",
                rank=1,
                citation_markers=[1],
            ),
            RankingRow(
                district_id=2,
                district_name="Bravo",
                state="CA",
                metric_id=metric_id,
                metric_name=metric_name,
                value=70000.0 + metric_id,
                display_value=f"${70_000 + metric_id:,}",
                academic_year="2024 - 2025",
                rank=2,
                citation_markers=[1],
            ),
        ],
        citations=[
            CitationRef(
                marker=1,
                title=f"{metric_name} contract",
                url=f"https://example.org/{metric_id}.pdf",
                page_number=2,
                page_ref="p. 2",
                academic_year="2024 - 2025",
                document_type="Contract",
                district_id=1,
            ),
        ],
        total_considered=2,
        excluded_count=0,
        order_statement=f"Ranked by {metric_name}, highest to lowest.",
        methodology_codes=[
            MethodologyRef(code="citation_answer_level_preferred_source_fallback")
        ],
    )


def _composite_result(metric_count: int = 4) -> CompositeRankingResult:
    metrics = [
        (101, "BA starting salary"),
        (102, "MA starting salary"),
        (103, "PhD starting salary"),
        (104, "EdD starting salary"),
    ][:metric_count]
    children = [_ranking_child(mid, name) for mid, name in metrics]
    return CompositeRankingResult(
        selection=ResultSelection(scope="all_covered_districts"),
        children=children,
        total_considered=0,  # validator aggregates from children
        excluded_count=0,
        order_statement=(
            f"{len(children)} metrics, ranked highest to lowest by each."
        ),
    )


def test_render_response_emits_composite_ranking_envelope_without_crash() -> None:
    """The renderer must accept ``CompositeRankingResult`` instead of falling
    through ``writer._render_result``'s ``assert_never`` guard.

    On main (pre-phase-4), this test raises ``AssertionError`` (because
    ``assert_never`` runs when no ``case`` matches the composite variant).
    """

    plan = _composite_plan()
    result = _composite_result(metric_count=4)

    manifest = render_response(plan, result, _valid_report())

    assert manifest.status == "rendered", (
        "renderer must produce a 'rendered' manifest, not crash or return "
        f"'validation_failed' — got status={manifest.status!r}"
    )
    assert manifest.result_type == "composite_ranking"
    assert manifest.validation_valid is True


def test_render_response_emits_one_markdown_table_per_child() -> None:
    """The composite renderer must surface every child as its own rank table.

    Pins the artifact contract: composite envelope is N independently
    rendered ``MetricRankingResult`` tables, not a single merged table.
    """

    plan = _composite_plan()
    result = _composite_result(metric_count=4)

    manifest = render_response(plan, result, _valid_report())

    body = manifest.body
    # Each child's metric name appears as a column header in its own table.
    for _, metric_name in [
        (101, "BA starting salary"),
        (102, "MA starting salary"),
        (103, "PhD starting salary"),
        (104, "EdD starting salary"),
    ]:
        assert metric_name in body, (
            f"missing rendered content for child metric {metric_name!r} — "
            "composite renderer must emit one table per child"
        )

    # Each child has 2 rows; the four tables together should produce 4 rank-1
    # rows and 4 rank-2 rows.
    assert body.count("| 1 | Alpha | CA |") == 4
    assert body.count("| 2 | Bravo | CA |") == 4


def test_render_response_emits_citation_markers_per_child() -> None:
    """Each child's citation markers appear in its own table — composite
    rendering must not strip the Sources column or merge marker spaces."""

    plan = _composite_plan()
    result = _composite_result(metric_count=4)

    manifest = render_response(plan, result, _valid_report())

    body = manifest.body
    # Each child has 2 rows, each row marker [1]; with 4 children we expect
    # 4 children * 2 rows = 8 rendered "[1]" markers in row cells.
    # Sources column header also appears once per table → 4 occurrences.
    assert body.count("| [1] |") == 8, (
        "every child row must carry its citation marker through to the rendered table"
    )
    assert body.count("| Sources |") == 4


def test_render_response_emits_composite_lead_line() -> None:
    """The composite envelope's ``order_statement`` should land at the top
    of the rendered body as a user-visible introduction so the user
    understands they're about to see N separate tables.
    """

    plan = _composite_plan()
    result = _composite_result(metric_count=4)

    manifest = render_response(plan, result, _valid_report())

    # The composite-level order_statement ("4 metrics, ranked highest to
    # lowest by each.") appears in the body, separately from each child's
    # per-table lead.
    assert "4 metrics" in manifest.body
    assert "ranked highest to lowest by each" in manifest.body


def test_render_response_composite_metadata_aggregates_across_children() -> None:
    """``manifest.metadata`` must be honest about the composite envelope:
    the envelope's own ``rows`` is forced empty (phase 1 contract), so
    ``display_metadata_for_result`` needs to sum across children to report
    a non-zero ``row_count`` / ``citation_count`` and a
    ``composite_child_count`` for downstream consumers."""

    plan = _composite_plan()
    result = _composite_result(metric_count=4)

    manifest = render_response(plan, result, _valid_report())

    # 4 children × 2 rows each = 8 rendered rows.
    assert manifest.metadata["row_count"] == 8
    assert manifest.metadata["displayed_row_count"] == 8
    # 4 children × 1 citation each = 4 (no dedup at phase 4; W2-M5-04 owns dedup).
    assert manifest.metadata["citation_count"] == 4
    assert manifest.metadata["composite_child_count"] == 4


def test_render_response_composite_metadata_uses_envelope_citation_count() -> None:
    """When children carry the shared citation list, metadata should not double count."""

    plan = _composite_plan()
    ba_child = _ranking_child(101, "BA starting salary")
    ma_child = _ranking_child(102, "MA starting salary")
    shared_citations = [
        ba_child.citations[0],
        ma_child.citations[0].model_copy(update={"marker": 2}),
    ]
    result = CompositeRankingResult(
        selection=ResultSelection(scope="all_covered_districts"),
        children=[
            ba_child.model_copy(update={"citations": shared_citations}),
            ma_child.model_copy(update={"citations": shared_citations}),
        ],
        citations=shared_citations,
        total_considered=0,
        excluded_count=0,
        order_statement="2 metrics, ranked highest to lowest by each.",
    )

    manifest = render_response(plan, result, _valid_report())

    assert manifest.metadata["citation_count"] == 2


def test_render_response_emits_two_table_composite() -> None:
    """Lower-bound (``min_length=2``) coverage: 2-metric composite renders 2 tables."""

    plan = _composite_plan()
    result = _composite_result(metric_count=2)

    manifest = render_response(plan, result, _valid_report())

    assert manifest.status == "rendered"
    assert manifest.result_type == "composite_ranking"
    assert "BA starting salary" in manifest.body
    assert "MA starting salary" in manifest.body
    assert "PhD starting salary" not in manifest.body
