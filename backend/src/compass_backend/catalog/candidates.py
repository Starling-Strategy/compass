"""Deterministic candidate ranking helpers for catalog resolution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Protocol, Sequence, TypeVar


class MetricCandidateLike(Protocol):
    """Minimum shape required to rank metric candidates."""

    metric_id: int


CandidateT = TypeVar("CandidateT", bound=MetricCandidateLike)


@dataclass(frozen=True)
class CandidateSearchBatch(Generic[CandidateT]):
    """One ranked candidate list returned by a deterministic catalog search."""

    query: str
    candidates: Sequence[CandidateT]
    source: str = "catalog_search"


@dataclass(frozen=True)
class CandidateSource:
    """Provenance for one candidate occurrence in a source list."""

    query: str
    source: str
    rank: int


@dataclass(frozen=True)
class RankedCandidate(Generic[CandidateT]):
    """One fused candidate plus deterministic ranking evidence."""

    candidate: CandidateT
    score: float
    best_rank: int
    sources: tuple[CandidateSource, ...]

    def metadata(self) -> dict[str, object]:
        """Return JSON-safe ranking metadata for resolver reports."""

        return {
            "candidate_fusion": {
                "score": round(self.score, 8),
                "best_rank": self.best_rank,
                "sources": [
                    {
                        "query": source.query,
                        "source": source.source,
                        "rank": source.rank,
                    }
                    for source in self.sources
                ],
            }
        }


def reciprocal_rank_fuse_metric_candidates(
    batches: Sequence[CandidateSearchBatch[CandidateT]],
    *,
    limit: int,
    rank_constant: int = 60,
) -> list[RankedCandidate[CandidateT]]:
    """Fuse ranked metric candidates by ID without inventing new candidates."""

    by_id: dict[int, tuple[CandidateT, float, int, list[CandidateSource], int]] = {}
    first_seen = 0
    for batch in batches:
        seen_in_batch: set[int] = set()
        for rank, candidate in enumerate(batch.candidates, start=1):
            if candidate.metric_id in seen_in_batch:
                continue
            seen_in_batch.add(candidate.metric_id)
            contribution = 1.0 / (rank_constant + rank)
            source = CandidateSource(
                query=batch.query,
                source=batch.source,
                rank=rank,
            )
            current = by_id.get(candidate.metric_id)
            if current is None:
                by_id[candidate.metric_id] = (
                    candidate,
                    contribution,
                    rank,
                    [source],
                    first_seen,
                )
                first_seen += 1
                continue
            original, score, best_rank, sources, original_seen = current
            sources.append(source)
            by_id[candidate.metric_id] = (
                original,
                score + contribution,
                min(best_rank, rank),
                sources,
                original_seen,
            )

    ranked = [
        RankedCandidate(
            candidate=candidate,
            score=score,
            best_rank=best_rank,
            sources=tuple(sources),
        )
        for candidate, score, best_rank, sources, _first_seen in by_id.values()
    ]
    ranked.sort(
        key=lambda item: (
            -item.score,
            item.best_rank,
            item.candidate.metric_id,
        )
    )
    return ranked[:limit]


__all__ = [
    "CandidateSearchBatch",
    "CandidateSource",
    "RankedCandidate",
    "reciprocal_rank_fuse_metric_candidates",
]
