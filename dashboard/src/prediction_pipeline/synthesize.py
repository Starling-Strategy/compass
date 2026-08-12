"""Combine k-diversity prediction runs into 1 SuggestedAnswer.

All functions are pure: take data in, return data out. No AI calls, no DB calls.
Default config: 15 runs (k=2..16), INA threshold=9.

Nathan's PiedPiper equivalent: PredictionPipeline._aggregate_predictions()
Same core logic: modal vote with INA consensus override. We add entropy
tracking and citation quality aggregation.
"""
import logging
import math
from collections import Counter
from contextlib import nullcontext as _nullcontext

from prediction_pipeline.config import Config
from prediction_pipeline.models import PredictionRun, SuggestedAnswer

logger = logging.getLogger(__name__)

try:
    import logfire
except ImportError:
    logfire = None


def modal_vote(runs: list[PredictionRun]) -> str:
    """Most common predicted_answer."""
    counts = Counter(r.predicted_answer for r in runs)
    return counts.most_common(1)[0][0]


def vote_distribution(runs: list[PredictionRun]) -> dict[str, int]:
    """Full vote counts."""
    return dict(Counter(r.predicted_answer for r in runs))


def is_ina_consensus(runs: list[PredictionRun], threshold: int = 6) -> bool:
    """True if >= threshold runs predicted INA."""
    ina_count = sum(1 for r in runs if r.predicted_answer.upper() == "INA")
    return ina_count >= threshold


def calculate_entropy(runs: list[PredictionRun]) -> float:
    """Shannon entropy. 0.0 = unanimous."""
    counts = Counter(r.predicted_answer for r in runs)
    total = len(runs)
    entropy = 0.0
    for count in counts.values():
        p = count / total
        if p > 0:
            entropy -= p * math.log2(p)
    return round(entropy, 4)


def calculate_agreement(runs: list[PredictionRun]) -> tuple[float, int]:
    """(agreement_pct, n_unique_answers)."""
    counts = Counter(r.predicted_answer for r in runs)
    winner_count = counts.most_common(1)[0][1]
    return round(winner_count / len(runs) * 100, 1), len(counts)


def pick_citations(runs, winning_answer) -> list[dict]:
    """Best citations from winning runs, deduplicated by (doc_id, quote).

    Picks the best citations before validation — all citations are included
    regardless of verified status. Validation happens post-synthesis on the
    final suggested answer's citations only.

    Fallback: if winning runs have no citations, use citations from any
    non-INA run. The evidence is still about the same topic and useful
    for analyst review even if that run reached a different conclusion.

    Cap at 5 citations.
    """
    winners = [r for r in runs if r.predicted_answer == winning_answer]
    winners.sort(key=lambda r: r.confidence or 0, reverse=True)
    seen: set[tuple[str, str]] = set()
    citations = []
    for run in winners:
        for cite in (run.key_citations_json or []):
            doc_id = cite.get("doc_id", "")
            quote = cite.get("quote", "")
            key = (doc_id, quote)
            if key in seen:
                continue
            seen.add(key)
            citations.append(cite)

    # Fallback: winning runs had no citations — try any non-INA run
    if not citations:
        others = [r for r in runs if r.predicted_answer.upper() != "INA"]
        others.sort(key=lambda r: r.confidence or 0, reverse=True)
        for run in others:
            for cite in (run.key_citations_json or []):
                doc_id = cite.get("doc_id", "")
                quote = cite.get("quote", "")
                key = (doc_id, quote)
                if key in seen:
                    continue
                seen.add(key)
                citations.append(cite)
                if len(citations) >= 5:
                    break
            if len(citations) >= 5:
                break
        if citations:
            logger.info("  Citation fallback: Q%s — %s citations from non-winning runs",
                        runs[0].q_id, len(citations))

    return citations[:5]


def synthesize(generation_id, runs, config, ina_threshold_override: int | None = None) -> SuggestedAnswer:
    """Orchestrate: vote -> INA check -> entropy -> citations -> SuggestedAnswer."""
    with logfire.span(
        "synthesize",
        q_id=runs[0].q_id,
        n_runs=len(runs),
    ) if logfire else _nullcontext():
        ina_thresh = ina_threshold_override if ina_threshold_override is not None else config.ina_threshold
        winner = modal_vote(runs)
        if is_ina_consensus(runs, ina_thresh):
            if winner.upper() != "INA":
                ina_count = sum(1 for r in runs if r.predicted_answer.upper() == "INA")
                logger.warning(
                    "INA consensus override: Q%s modal_vote='%s' overridden to INA (%s/%s >= %s)",
                    runs[0].q_id, winner, ina_count, len(runs), ina_thresh,
                )
            winner = "INA"
        entropy = calculate_entropy(runs)
        agreement_pct, n_unique = calculate_agreement(runs)
        citations = pick_citations(runs, winner)
        if winner.upper() == "INA":
            citations = []  # INA = no evidence found, no citations
        winning_runs = [r for r in runs if r.predicted_answer == winner]
        winning_runs.sort(key=lambda r: r.confidence or 0, reverse=True)
        best_reasoning = winning_runs[0].reasoning if winning_runs else None

        if logfire:
            logfire.info(
                "synthesis_result",
                winner=winner,
                entropy=entropy,
                agreement_pct=agreement_pct,
                n_unique=n_unique,
            )

        return SuggestedAnswer(
            district_id=runs[0].district_id, ay_id=runs[0].ay_id, q_id=runs[0].q_id,
            suggested_answer=winner, confidence=agreement_pct / 100,
            reasoning=best_reasoning, citations_json=citations,
            is_ina=winner.upper() == "INA",
            generation_id=generation_id, entropy=entropy,
            agreement_pct=agreement_pct, n_unique_answers=n_unique,
            n_predictions=len(runs), vote_distribution=vote_distribution(runs),
            model_version=config.model_version, source=config.source,
        )
