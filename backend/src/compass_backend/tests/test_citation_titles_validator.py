"""W2-M5-02 (#746) — ``citation_titles_resolved`` validator tests.

Catches a regression that reintroduces a generic fallback title on a
user-visible citation. Sheet row 59 (Ashley 2026-05-14, session
``78693257-8590-4ef6-adf2-e0f91eeb3f29``) reported "All citations were
listed as 'Unknown Source' for the title even though they linked to the
correct doc." — the construction-side fallback in
``artifacts/citations.py:_fallback_title`` emits one of three generic
strings when title parsing misses; this validator fails the verdict when
any survives to the rendered result.
"""

from __future__ import annotations

import asyncio

from compass_backend.artifacts import (
    MetricRankingResult,
    RankingRow,
    ResultSelection,
)
from compass_backend.artifacts.citations import CitationRef
from compass_backend.quality._evaluator_context import CompassEvaluatorContext
from compass_backend.tests.conftest import make_evaluator_context
from compass_backend.quality.validators import registry as _registry  # populate


def _result_with_titles(titles: list[str]) -> MetricRankingResult:
    """Build a minimal policy_answer ranking result with the given titles.

    Uses ``source="policy_answer"`` rows so the result has no profile-row
    coverage requirement to satisfy — the validator under test is concerned
    only with the citations list, not the row source. One row per citation
    so every marker is referenced.
    """

    return MetricRankingResult(
        selection=ResultSelection(scope="all_covered_districts"),
        rows=[
            RankingRow(
                district_id=i,
                district_name=f"District {i}",
                state="CA",
                metric_id=200 + i,
                metric_name="salary",
                value=80000.0 + i * 100,
                display_value=f"${80000 + i * 100:,}",
                academic_year="2024 - 2025",
                rank=i,
                source="policy_answer",
                citation_markers=[i],
                coverage_state="covered",
                coverage_display=f"${80000 + i * 100:,}",
                coverage_reason="answer",
            )
            for i in range(1, len(titles) + 1)
        ],
        citations=[
            CitationRef(
                marker=i,
                title=title,
                url=f"https://example.com/doc/{i}",
                academic_year="2024 - 2025",
                document_type="Contract",
            )
            for i, title in enumerate(titles, start=1)
        ],
        total_considered=len(titles),
        excluded_count=0,
        order_statement="Ranked by salary, highest to lowest.",
    )


def _ctx(result) -> CompassEvaluatorContext:
    return make_evaluator_context(
        session_id="sess-746",
        turn_index=0,
        answer_text="placeholder",
        result=result,
    )


def _run(validator, ctx, payload=None):
    return asyncio.run(validator(ctx, payload or {}))


def test_resolved_titles_pass() -> None:
    """Real titles (district + doc-type + year) clear the sentinel set."""

    validator = _registry.lookup("citation_titles_resolved")
    assert validator is not None
    result = _result_with_titles([
        "Capistrano Unified Master Contract, 2024-2025",
        "Anaheim ESD Salary Schedule, 2024-2025",
    ])
    outcome = _run(validator, _ctx(result))
    assert outcome.outcome == "pass"
    assert outcome.evidence["citation_count"] == 2
    assert outcome.evidence["low_quality_title_count"] == 0


def test_source_record_fallback_fails() -> None:
    """Worst-case fallback: no document_type and no URL → ``Source record``.

    Matches the code-level fallback at artifacts/citations.py:208."""

    validator = _registry.lookup("citation_titles_resolved")
    assert validator is not None
    result = _result_with_titles(["Source record"])
    outcome = _run(validator, _ctx(result))
    assert outcome.outcome == "fail"
    assert outcome.evidence["unresolved_count"] == 1
    assert "Source record" in outcome.reason


def test_source_document_fallback_fails() -> None:
    """URL-only fallback at artifacts/citations.py:207 → ``Source document``."""

    validator = _registry.lookup("citation_titles_resolved")
    assert validator is not None
    result = _result_with_titles(["Source document"])
    outcome = _run(validator, _ctx(result))
    assert outcome.outcome == "fail"
    assert outcome.evidence["unresolved_count"] == 1


def test_unknown_source_legacy_fallback_fails() -> None:
    """Sheet row 59's literal — defensive regression coverage even though
    current code does not emit this string. If a renderer path ever
    resurrects it, the validator catches it."""

    validator = _registry.lookup("citation_titles_resolved")
    assert validator is not None
    result = _result_with_titles(["Unknown Source"])
    outcome = _run(validator, _ctx(result))
    assert outcome.outcome == "fail"
    assert "Unknown Source" in outcome.reason


def test_mixed_resolved_and_fallback_fails_with_count() -> None:
    """One bad title among many fails the verdict; evidence carries the
    full count so the verdict reporter can show the blast radius."""

    validator = _registry.lookup("citation_titles_resolved")
    assert validator is not None
    result = _result_with_titles([
        "Capistrano Unified Master Contract, 2024-2025",
        "Source record",
        "Anaheim ESD Salary Schedule, 2024-2025",
        "Unknown Source",
    ])
    outcome = _run(validator, _ctx(result))
    assert outcome.outcome == "fail"
    assert outcome.evidence["unresolved_count"] == 2
    assert outcome.evidence["total_citations"] == 4


def test_doc_type_only_title_fails() -> None:
    """Per #892, the ``"{document_type} source"`` pattern (e.g., "Contract
    source") fails alongside generic fallbacks. It names a doc type but
    lacks district/year/page detail, leaving the citation unverifiable.
    ``low_quality_title_count`` still surfaces in evidence so the subset
    can be counted separately from generic fallbacks."""

    validator = _registry.lookup("citation_titles_resolved")
    assert validator is not None
    result = _result_with_titles(["Contract source", "Board Policy source"])
    outcome = _run(validator, _ctx(result))
    assert outcome.outcome == "fail"
    assert outcome.evidence["unresolved_count"] == 2
    assert outcome.evidence["low_quality_title_count"] == 2


def test_empty_citations_passes() -> None:
    """An answer with no citations on the result has nothing to validate
    — passes (the citation-coverage validators handle the "should have
    had a citation" failure mode separately)."""

    validator = _registry.lookup("citation_titles_resolved")
    assert validator is not None
    result = _result_with_titles([])
    outcome = _run(validator, _ctx(result))
    assert outcome.outcome == "pass"
    assert outcome.evidence["citation_count"] == 0


def test_missing_result_returns_error() -> None:
    """Direct-route or clarification turns have no result — returns error,
    not fail, so the verdict pipeline drops the trial from the denominator."""

    validator = _registry.lookup("citation_titles_resolved")
    assert validator is not None
    outcome = _run(validator, _ctx(None))
    assert outcome.outcome == "error"
    assert "result" in outcome.reason
