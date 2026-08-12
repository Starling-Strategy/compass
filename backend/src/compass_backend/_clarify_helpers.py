"""Deterministic clarify-request builders shared by the executor and the
clarify stylist service.

This leaf module lives at the ``compass_backend`` package root (not inside
``execution/``) so that it has no back-deps into ``compass_backend.execution``.
It imports only from ``compass_backend.catalog`` and
``compass_backend.contracts.planning``, which means it can safely be imported
from ``compass_backend.answer_layer.clarify`` without triggering the
``execution.operations → answer_layer.clarify → execution._helpers →
execution.__init__ → execution.executor → execution.operations`` cycle that
previously required a deferred import inside the clarify service.
"""

from __future__ import annotations

from compass_backend.catalog import MetricCandidate
from compass_backend.contracts.planning import (
    ClarificationOption,
    ClarificationRequest,
)


def _metric_clarification_option(candidate: MetricCandidate) -> ClarificationOption:
    """Build a grounded, clickable option from a governed metric candidate.

    Born-from-data (#1348), mirroring
    ``execution.selection._district_clarification_option``: ``value`` is the
    canonical, re-resolvable metric ``name`` (a click sends it back, and
    execution re-grounds it through ``resolve_metric_bundle``'s exact-name
    short-circuit, so a click can never bypass the catalog). ``label`` reuses the
    name; ``detail`` is the catalog ``topic`` when present (``MetricCandidate``
    carries no shorter label or definition text) — never invented.
    """

    return ClarificationOption(
        value=candidate.name,
        label=candidate.name,
        detail=candidate.topic or None,
    )


def _metric_clarification(
    metric_phrase: str,
    *,
    operation: str,
    candidates: list[MetricCandidate],
) -> ClarificationRequest:
    """Build a deterministic clarification for governed metric ambiguity."""

    unique_candidates = _dedupe_metric_candidates(candidates)
    return ClarificationRequest(
        question=(
            f'I found a few Compass metrics that could match "{metric_phrase}". '
            "Do you mean one of these?"
        ),
        missing_fields=["metric"],
        candidates=[candidate.name for candidate in unique_candidates],
        # #1348: the structured, clickable counterparts to the prose
        # ``candidates`` above — the metric analogue of the district-ambiguity
        # branch in ``execution.selection``. Built here, at the deterministic
        # clarify site, from the catalog candidates already in hand, so every
        # execution-origin metric clarify carries options regardless of
        # planner-LLM variance. The clarify stylist preserves them
        # (``answer_layer.clarify`` only rewrites ``question``).
        candidate_options=[
            _metric_clarification_option(candidate) for candidate in unique_candidates
        ],
    )


def _dedupe_metric_candidates(
    candidates: list[MetricCandidate],
) -> list[MetricCandidate]:
    unique_candidates: list[MetricCandidate] = []
    seen_metric_ids: set[int] = set()
    for candidate in candidates:
        if candidate.metric_id in seen_metric_ids:
            continue
        unique_candidates.append(candidate)
        seen_metric_ids.add(candidate.metric_id)
    return unique_candidates


__all__ = [
    "_metric_clarification",
    "_metric_clarification_option",
    "_dedupe_metric_candidates",
]
