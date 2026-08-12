"""W2-M5-01 (#745) — ``csv_parity_matches_rendered_rows`` validator tests.

The construction-side fix lives in
``src/compass_backend/artifacts/results.py`` (CompositeRankingResult
now populates its own ``csv_export``). This validator is the
evaluation-time check that catches any regression reintroducing either
of the two #745 failure modes: blank CSV (composite or otherwise), or
chat-vs-CSV citation marker drift.
"""

from __future__ import annotations

import asyncio

from compass_backend.artifacts import (
    CompositeRankingResult,
    MetricRankingResult,
    RankingRow,
    ResultSelection,
)
from compass_backend.artifacts.citations import CitationRef
from compass_backend.artifacts.results import CsvExportArtifact
from compass_backend.quality._evaluator_context import CompassEvaluatorContext
from compass_backend.tests.conftest import make_evaluator_context
from compass_backend.quality.validators import registry as _registry  # populate registry


def _ranking(metric_id: int = 100, metric_name: str = "salary") -> MetricRankingResult:
    return MetricRankingResult(
        selection=ResultSelection(scope="all_covered_districts"),
        rows=[
            RankingRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=metric_id,
                metric_name=metric_name,
                value=80000.0,
                display_value="$80,000",
                academic_year="2024 - 2025",
                rank=1,
                source="policy_answer",
                citation_markers=[1],
                coverage_state="covered",
                coverage_display="$80,000",
                coverage_reason="answer",
            ),
        ],
        citations=[
            CitationRef(
                marker=1,
                title="Alpha Contract",
                url="https://example.org/alpha-contract.pdf",
                document_type="Contract",
                academic_year="2024 - 2025",
                district_id=1,
            )
        ],
        total_considered=1,
        excluded_count=0,
        order_statement="Ranked by salary, highest to lowest.",
    )


def _ctx(result) -> CompassEvaluatorContext:
    return make_evaluator_context(
        session_id="sess-csv-parity",
        turn_index=0,
        answer_text="placeholder",
        result=result,
    )


def _run(validator, ctx, payload=None):
    return asyncio.run(validator(ctx, payload or {}))


def test_ranking_csv_parity_passes() -> None:
    validator = _registry.lookup("csv_parity_matches_rendered_rows")
    assert validator is not None
    outcome = _run(validator, _ctx(_ranking()))
    assert outcome.outcome == "pass"
    assert outcome.evidence["chat_row_count"] == 1


def test_composite_csv_parity_passes_after_fix() -> None:
    """The construction-side fix populated ``CompositeRankingResult.csv_export``
    with aggregated child rows. Validator confirms the aggregate."""
    composite = CompositeRankingResult(
        children=[
            _ranking(metric_id=101, metric_name="BA salary"),
            _ranking(metric_id=102, metric_name="MA salary"),
        ],
        total_considered=0,
        excluded_count=0,
        order_statement="Two metrics, ranked highest to lowest by each.",
    )
    validator = _registry.lookup("csv_parity_matches_rendered_rows")
    assert validator is not None
    outcome = _run(validator, _ctx(composite))
    assert outcome.outcome == "pass"
    assert outcome.evidence["chat_row_count"] == 2  # 1 row per child × 2 children


def test_blank_csv_fails() -> None:
    """Simulate the #745 bug: chat has rows but csv_export is empty.

    Note: pydantic v2 re-validates the model on assignment to the
    ``CompassEvaluatorContext`` dataclass field, so ``object.__setattr__``
    -ing ``csv_export`` to ``None`` on the model doesn't persist through
    the ctx assignment. Instead we override with an EMPTY
    ``CsvExportArtifact`` after ctx construction — the validator treats
    ``not csv_export.rows`` and ``csv_export is None`` as equivalent
    failure modes."""
    result = _ranking()
    ctx = _ctx(result)
    # The blank-CSV state the production bug produces is an empty rows list.
    # Pydantic v2 won't re-validate after-the-fact attribute mutations on
    # a dataclass field (it validated once at assignment).
    object.__setattr__(
        ctx.result,
        "csv_export",
        CsvExportArtifact(columns=list(ctx.result.csv_export.columns), rows=[]),
    )
    validator = _registry.lookup("csv_parity_matches_rendered_rows")
    assert validator is not None
    outcome = _run(validator, ctx)
    assert outcome.outcome == "fail"
    assert "blank CSV" in outcome.reason
    assert outcome.evidence["chat_row_count"] == 1


def test_chat_vs_csv_marker_drift_fails() -> None:
    """Simulate marker drift: chat row has [1] but CSV row reports empty markers."""
    result = _ranking()
    drifted_csv = CsvExportArtifact(
        columns=result.csv_export.columns,
        rows=[
            {**row, "citation_markers": ""}  # CSV claims no markers
            for row in result.csv_export.rows
        ],
    )
    drifted = result.model_copy(update={"csv_export": drifted_csv})
    validator = _registry.lookup("csv_parity_matches_rendered_rows")
    assert validator is not None
    outcome = _run(validator, _ctx(drifted))
    assert outcome.outcome == "fail"
    assert "diverging" in outcome.reason


def test_missing_result_returns_error() -> None:
    validator = _registry.lookup("csv_parity_matches_rendered_rows")
    assert validator is not None
    outcome = _run(validator, _ctx(None))
    assert outcome.outcome == "error"
    assert "result" in outcome.reason


def test_zero_row_answer_passes_trivially() -> None:
    """Some answers (covered-universe count with all-zero coverage) emit
    zero rows. No CSV expected; validator passes trivially."""
    empty = MetricRankingResult(
        selection=ResultSelection(scope="all_covered_districts"),
        rows=[],
        citations=[],
        total_considered=0,
        excluded_count=0,
        order_statement="No rows.",
    )
    validator = _registry.lookup("csv_parity_matches_rendered_rows")
    assert validator is not None
    outcome = _run(validator, _ctx(empty))
    assert outcome.outcome == "pass"
    assert outcome.evidence["chat_row_count"] == 0


def _ranking_with_not_reviewed_row(
    metric_id: int = 100,
    metric_name: str = "salary",
) -> MetricRankingResult:
    """A 2-row artifact: one ``covered`` answer row and one ``not_reviewed``
    placeholder row. Under #1514 only the answer row renders in the chat
    table and lands in the auto-built ``csv_export``; the not-reviewed
    district is voiced as a narrative sentence (no coverage-disclosure rows
    involved). ``result.rows`` keeps both — the full record."""
    return MetricRankingResult(
        selection=ResultSelection(scope="all_covered_districts"),
        rows=[
            RankingRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=metric_id,
                metric_name=metric_name,
                value=80000.0,
                display_value="$80,000",
                academic_year="2024 - 2025",
                rank=1,
                source="policy_answer",
                citation_markers=[1],
                coverage_state="covered",
                coverage_display="$80,000",
                coverage_reason="answer",
            ),
            RankingRow(
                district_id=2,
                district_name="Beta",
                state="NY",
                metric_id=metric_id,
                metric_name=metric_name,
                value=None,
                display_value="Not reviewed",
                academic_year="2024 - 2025",
                rank=2,
                source="coverage_state",
                citation_markers=[],
                coverage_state="not_reviewed",
                coverage_display="Not reviewed",
                coverage_reason="not_reviewed",
            ),
        ],
        citations=[
            CitationRef(
                marker=1,
                title="Alpha Contract",
                url="https://example.org/alpha-contract.pdf",
                document_type="Contract",
                academic_year="2024 - 2025",
                district_id=1,
            )
        ],
        total_considered=2,
        excluded_count=0,
        order_statement="Ranked by salary, highest to lowest.",
    )


def test_not_reviewed_row_excluded_from_both_surfaces_passes() -> None:
    """#1514 re-spec of the #1358 case: tables, charts, and CSV data rows hold
    answer rows only; a ``not_reviewed`` row renders on NEITHER surface (it is
    voiced as a narrative sentence instead). A covered+not_reviewed ranking
    therefore renders a 1-row table and a 1-data-row CSV, and parity passes
    with both sides anchored to the same answers-only subset. Per D2 the
    ranking CSV still carries the not-reviewed district exactly once — as a
    coverage-disclosure row appended AFTER the data rows; the validator
    compares only the aligned data-row prefix and tolerates that block.

    Flip history (full spec trail in the registry comment): pre-#1358 the
    validator stripped not_reviewed rows from the CSV side only (phantom
    ``CSV < chat`` fails, the live ``1 < 2`` / ``154 < 156`` reasons); #1358
    made the row a legitimate data row on BOTH surfaces; #1514 removes it
    from both tables while the ranking CSV keeps it as an appended
    disclosure row."""
    result = _ranking_with_not_reviewed_row()
    # Sanity: the auto-built CSV holds the answer data row first, then the
    # not-reviewed district appended once as a disclosure row (D2).
    assert len(result.csv_export.rows) == 2
    assert result.csv_export.rows[0]["coverage_state"] == "covered"
    assert result.csv_export.rows[1]["coverage_state"] == "not_reviewed"
    validator = _registry.lookup("csv_parity_matches_rendered_rows")
    assert validator is not None
    outcome = _run(validator, _ctx(result))
    assert outcome.outcome == "pass"
    assert outcome.evidence["chat_row_count"] == 1


def _composite_with_interleaved_disclosure() -> CompositeRankingResult:
    """The #1514 Fix B (B2) reviewer shape: a 2-child composite whose FIRST
    child carries a promoted not_reviewed row. The envelope CSV concatenates
    each child's FULL csv_export, so child 1's disclosure row sits BETWEEN
    child 1's and child 2's data rows — not appended last."""
    return CompositeRankingResult(
        children=[
            _ranking_with_not_reviewed_row(metric_id=101, metric_name="BA salary"),
            _ranking(metric_id=102, metric_name="MA salary"),
        ],
        total_considered=0,
        excluded_count=0,
        order_statement="Two metrics, ranked highest to lowest by each.",
    )


def test_composite_csv_parity_walks_children_past_interleaved_disclosures() -> None:
    """#1514 Fix B blocker B2 — the old flat ``rows[:chat_row_count]`` slice
    assumed disclosure rows were appended LAST, so it zipped child 1's
    disclosure row (no markers) against child 2's chat row ([1]) and
    false-failed a healthy answer into compass.verdicts. The validator now
    walks the envelope per child, comparing each child's answers-only rows
    against the data-row prefix of that child's slice and skipping each
    child's disclosure tail."""
    composite = _composite_with_interleaved_disclosure()
    # Sanity: the disclosure row is interleaved between the children.
    assert [row["coverage_state"] for row in composite.csv_export.rows] == [
        "covered",
        "not_reviewed",
        "covered",
    ]
    validator = _registry.lookup("csv_parity_matches_rendered_rows")
    assert validator is not None
    outcome = _run(validator, _ctx(composite))
    assert outcome.outcome == "pass"
    assert outcome.evidence["chat_row_count"] == 2
    assert outcome.evidence["csv_row_count"] == 3


def test_composite_csv_parity_still_checks_rows_after_a_disclosure_tail() -> None:
    """The per-child walk must keep validating the LATER child: blanking the
    markers on child 2's data row (envelope index 2, AFTER child 1's
    disclosure tail) still fails — the walk skips disclosure rows, not the
    rows behind them."""
    composite = _composite_with_interleaved_disclosure()
    tampered_rows = [dict(row) for row in composite.csv_export.rows]
    tampered_rows[2] = {**tampered_rows[2], "citation_markers": ""}
    drifted = composite.model_copy(
        update={
            "csv_export": CsvExportArtifact(
                columns=list(composite.csv_export.columns),
                rows=tampered_rows,
            )
        }
    )
    validator = _registry.lookup("csv_parity_matches_rendered_rows")
    assert validator is not None
    outcome = _run(validator, _ctx(drifted))
    assert outcome.outcome == "fail"
    assert "diverging" in outcome.reason
