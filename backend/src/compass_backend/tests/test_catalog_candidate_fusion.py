"""Tests for deterministic catalog candidate fusion."""

from __future__ import annotations

from compass_backend.catalog.candidates import (
    CandidateSearchBatch,
    reciprocal_rank_fuse_metric_candidates,
)
from compass_backend.catalog import MetricCandidate


def _metric(metric_id: int, name: str) -> MetricCandidate:
    return MetricCandidate(metric_id=metric_id, name=name, answer_type="numeric")


def test_candidate_fusion_dedupes_and_accumulates_rank_evidence() -> None:
    first = _metric(1, "Planning time")
    duplicate = _metric(1, "Planning time")
    second = _metric(2, "Teacher planning requirements")

    ranked = reciprocal_rank_fuse_metric_candidates(
        (
            CandidateSearchBatch(query="planning time", candidates=(second, first)),
            CandidateSearchBatch(query="planning", candidates=(second, duplicate)),
        ),
        limit=5,
    )

    assert [item.candidate.metric_id for item in ranked] == [2, 1]
    assert ranked[0].score > ranked[1].score
    assert [source.query for source in ranked[0].sources] == [
        "planning time",
        "planning",
    ]
    assert ranked[0].metadata()["candidate_fusion"]["best_rank"] == 1


def test_candidate_fusion_uses_stable_metric_id_tie_break() -> None:
    ranked = reciprocal_rank_fuse_metric_candidates(
        (
            CandidateSearchBatch(
                query="salary",
                candidates=(
                    _metric(20, "Maximum salary"),
                    _metric(10, "Starting salary"),
                ),
            ),
        ),
        limit=2,
    )

    assert [item.candidate.metric_id for item in ranked] == [20, 10]


def test_candidate_fusion_never_emits_candidates_outside_search_results() -> None:
    ranked = reciprocal_rank_fuse_metric_candidates(
        (
            CandidateSearchBatch(query="health", candidates=(_metric(232, "Medical"),)),
            CandidateSearchBatch(query="benefits", candidates=(_metric(233, "Dental"),)),
        ),
        limit=10,
    )

    assert {item.candidate.metric_id for item in ranked} == {232, 233}
