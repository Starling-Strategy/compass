"""Tests for the ``sources_block_is_deduplicated`` validator (W2-M5-04 / #748).

The validator asserts that ``ctx.result.citations`` contains no duplicate
URLs — i.e., the answer-level Sources block is properly consolidated.
Paired with the dedup-by-URL change in ``execution/evidence.py``; if a
future change re-introduces multi-marker entries for the same document,
this validator catches it at evaluation time.
"""

from __future__ import annotations

import asyncio

from compass_backend.artifacts.citations import CitationRef
from compass_backend.artifacts.results import (
    MetricRankingResult,
    RankingRow,
)
from compass_backend.contracts.planning import PlannerRoute
from compass_backend.quality._evaluator_context import (
    ArtifactSnapshot,
    CompassEvaluatorContext,
)
from compass_backend.tests.conftest import make_evaluator_context
from compass_backend.quality.validators import registry as _registry  # noqa: F401 — populate registry


def _result(*, citations: list[CitationRef] | None = None) -> MetricRankingResult:
    return MetricRankingResult(
        rows=[
            RankingRow(
                district_id=1,
                district_name="Detroit",
                state="MI",
                metric_id=100,
                metric_name="Starting salary",
                value=80000,
                display_value="80000",
                academic_year="2024 - 2025",
                rank=1,
                citation_markers=[1] if citations else [],
            )
        ],
        citations=citations or [],
        total_considered=1,
        excluded_count=0,
        order_statement="Sorted by Starting salary descending.",
    )


def _ctx(
    *,
    result: MetricRankingResult | None = None,
    route: PlannerRoute | None = None,
    snapshot_citations: list[CitationRef] | None = None,
) -> CompassEvaluatorContext:
    return make_evaluator_context(
        session_id="sess-sources-dedup",
        turn_index=0,
        answer_text="placeholder",
        result=result,
        route=route,
        artifact_snapshot=ArtifactSnapshot(citations=snapshot_citations or []),
    )


def _run(validator, ctx: CompassEvaluatorContext, payload: dict | None = None):
    return asyncio.run(validator(ctx, payload or {}))


def test_unique_urls_pass() -> None:
    """Two CitationRefs with distinct URLs → outcome=pass."""
    validator = _registry.lookup("sources_block_is_deduplicated")
    assert validator is not None
    result = _result(
        citations=[
            CitationRef(marker=1, title="Detroit Contract", url="https://a.example/x.pdf"),
            CitationRef(marker=2, title="Ann Arbor Contract", url="https://b.example/y.pdf"),
        ]
    )
    outcome = _run(validator, _ctx(result=result))
    assert outcome.outcome == "pass"
    assert outcome.evidence["total_citations"] == 2
    assert outcome.evidence["unique_urls"] == 2


def test_duplicate_urls_fail() -> None:
    """Two CitationRefs with the same URL → outcome=fail."""
    validator = _registry.lookup("sources_block_is_deduplicated")
    assert validator is not None
    same = "https://example.org/detroit-contract.pdf"
    result = _result(
        citations=[
            CitationRef(marker=1, title="Detroit Contract 2023-2024", url=same),
            CitationRef(marker=2, title="Detroit Contract 2024-2025", url=same),
        ]
    )
    outcome = _run(validator, _ctx(result=result))
    assert outcome.outcome == "fail"
    assert outcome.evidence["duplicate_count"] == 1
    assert outcome.evidence["unique_urls"] == 1
    assert outcome.evidence["total_citations"] == 2


def test_case_insensitive_url_match() -> None:
    """Trivial case-only divergence in URLs is still a duplicate."""
    validator = _registry.lookup("sources_block_is_deduplicated")
    assert validator is not None
    result = _result(
        citations=[
            CitationRef(marker=1, title="A", url="https://Example.Org/Doc.pdf"),
            CitationRef(marker=2, title="B", url="https://example.org/doc.pdf"),
        ]
    )
    outcome = _run(validator, _ctx(result=result))
    assert outcome.outcome == "fail"


def test_missing_url_does_not_dedup() -> None:
    """URL-less citations are not counted toward the unique-URL invariant.
    Two title-only entries with no URL still pass — title-based dedup is
    enforced at construction (see test_citation_dedup), not at the
    Sources-block validator level."""
    validator = _registry.lookup("sources_block_is_deduplicated")
    assert validator is not None
    result = _result(
        citations=[
            CitationRef(marker=1, title="Memo Alpha", url=None),
            CitationRef(marker=2, title="Memo Beta", url=None),
        ]
    )
    outcome = _run(validator, _ctx(result=result))
    assert outcome.outcome == "pass"
    assert outcome.evidence["unique_urls"] == 0


def test_missing_result_returns_error_on_non_policy_guidance_routes() -> None:
    """No ResultSet AND not a policy_guidance turn → error (can't evaluate).

    Migration 081 keeps `applicable_routes` to `[execute, policy_guidance]`,
    so the pipeline shouldn't dispatch this validator on clarify/direct in
    practice — but if it ever did, error is the correct outcome since
    those routes carry no Sources block to validate."""
    validator = _registry.lookup("sources_block_is_deduplicated")
    assert validator is not None
    outcome = _run(validator, _ctx(result=None, route="clarify"))
    assert outcome.outcome == "error"
    assert "result" in outcome.reason


def test_empty_citations_pass() -> None:
    """Zero citations is trivially unique — outcome=pass."""
    validator = _registry.lookup("sources_block_is_deduplicated")
    assert validator is not None
    outcome = _run(validator, _ctx(result=_result(citations=[])))
    assert outcome.outcome == "pass"
    assert outcome.evidence["total_citations"] == 0


# ---------------------------------------------------------------------------
# Refs #867 — policy_guidance route reads citations from artifact_snapshot


def test_policy_guidance_unique_urls_pass() -> None:
    """policy_guidance turn with distinct URLs in artifact_snapshot.citations
    (mirrored from manifest.metadata by
    `verdict_pipeline._policy_guidance_citations_from_manifest`) → pass."""
    validator = _registry.lookup("sources_block_is_deduplicated")
    assert validator is not None
    outcome = _run(
        validator,
        _ctx(
            result=None,
            route="policy_guidance",
            snapshot_citations=[
                CitationRef(
                    marker=1,
                    title="Detroit — Annual salary",
                    url="https://teacherquality.nctq.org/contract-database/district/detroit",
                ),
                CitationRef(
                    marker=2,
                    title="Boston — Annual salary",
                    url="https://teacherquality.nctq.org/contract-database/district/boston",
                ),
            ],
        ),
    )
    assert outcome.outcome == "pass"
    assert outcome.evidence["total_citations"] == 2
    assert outcome.evidence["unique_urls"] == 2


def test_policy_guidance_duplicate_urls_fail() -> None:
    """policy_guidance turn whose Sources block reintroduces a duplicate URL
    (e.g., a renderer regression) → fail."""
    validator = _registry.lookup("sources_block_is_deduplicated")
    assert validator is not None
    same = "https://teacherquality.nctq.org/contract-database/district/detroit"
    outcome = _run(
        validator,
        _ctx(
            result=None,
            route="policy_guidance",
            snapshot_citations=[
                CitationRef(marker=1, title="Detroit (stance)", url=same),
                CitationRef(marker=2, title="Detroit (exemplar)", url=same),
            ],
        ),
    )
    assert outcome.outcome == "fail"
    assert outcome.evidence["duplicate_count"] == 1
    assert outcome.evidence["unique_urls"] == 1


def test_policy_guidance_empty_snapshot_passes() -> None:
    """A policy_guidance turn with no citations is trivially unique."""
    validator = _registry.lookup("sources_block_is_deduplicated")
    assert validator is not None
    outcome = _run(
        validator,
        _ctx(result=None, route="policy_guidance", snapshot_citations=[]),
    )
    assert outcome.outcome == "pass"
    assert outcome.evidence["total_citations"] == 0
