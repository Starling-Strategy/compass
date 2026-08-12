"""Service boundary for stylist-composed clarify questions."""

from __future__ import annotations

import logging
from typing import Protocol

from compass_backend.answer_layer.clarify_agent import build_clarify_stylist_agent
from compass_backend.catalog.resolution import MetricCandidate
from compass_backend.contracts.answer_layer import ClarifyBrief, ClarifyDraft
from compass_backend.contracts.planning import ClarificationRequest
from compass_backend._clarify_helpers import (
    _dedupe_metric_candidates,
    _metric_clarification,
)

_logger = logging.getLogger(__name__)


class ClarifyStylistAgent(Protocol):
    async def run(self, prompt: str): ...


async def compose_clarify_question_async(
    metric_phrase: str,
    *,
    operation: str,
    candidates: list[MetricCandidate],
    adjudicator_hint: str | None = None,
    stylist_agent: ClarifyStylistAgent | None = None,
) -> ClarificationRequest:
    """Compose a clarify question in stylist voice; fall back to the
    deterministic f-string on any stylist failure."""
    fallback = _metric_clarification(
        metric_phrase, operation=operation, candidates=candidates
    )
    deduped = _dedupe_metric_candidates(candidates)
    try:
        brief = ClarifyBrief(
            metric_phrase=metric_phrase,
            operation=operation,
            candidate_labels=tuple(candidate.name for candidate in deduped),
            adjudicator_hint=adjudicator_hint,
        )
        agent = stylist_agent or build_clarify_stylist_agent()
        result = await agent.run(brief.model_dump_json(indent=2))
        draft = (
            result.output
            if isinstance(result.output, ClarifyDraft)
            else ClarifyDraft.model_validate(result.output)
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "clarify stylist fallback to f-string: %s", type(exc).__name__
        )
        return fallback
    return fallback.model_copy(update={"question": draft.question})


__all__ = ["ClarifyStylistAgent", "compose_clarify_question_async"]
