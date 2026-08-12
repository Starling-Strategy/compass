"""Scorecard Pydantic models — re-exported from the shared backend location.

The canonical home is ``compass_backend.quality.scorecard_models``.
This module re-exports everything so that pre-existing imports of the form
``from nctqai.services.compass_quality.models import ...``
continue to work without any change at the call sites.
"""
from compass_backend.quality.scorecard_models import (  # noqa: F401
    CANONICAL_DIM_SLUGS,
    BuildContext,
    ConversationTurn,
    ConversationWithVerdicts,
    DimensionDetail,
    DimensionScore,
    ScenarioCase,
    ScorecardSnapshot,
    Trial,
    Verdict,
)

# Replicated from the backend authority (DO NOT import — runtime import of
# backend internals is forbidden by src/nctqai/AGENTS.md "Import Direction").
# Canonical definition: `_NOT_APPLICABLE_REASON_PREFIX = "not applicable:"` in
# compass_backend/quality/scorecard.py. A validator that fires on every turn of
# a given shape but only meaningfully applies to a subset records
# outcome="error" with this reason prefix for the turns where it has no
# constraint to evaluate. Such verdicts neither passed nor failed — they are
# not-applicable, NOT genuine errors, so the dashboard renders them muted (not
# red) and counts them separately.
_NOT_APPLICABLE_REASON_PREFIX = "not applicable:"


def is_not_applicable(verdict: Verdict) -> bool:
    """Return True if this verdict is a "not applicable" record, not a real error.

    A verdict is not-applicable when ``outcome == "error"`` AND its ``reason``
    starts with ``"not applicable:"`` (the backend convention above). Genuine
    errors — ``outcome == "error"`` with any other reason (e.g. a deterministic
    stub "not wired", or a real "missing artifact:" failure) — return False.
    """
    return (
        verdict.outcome == "error"
        and (verdict.reason or "").startswith(_NOT_APPLICABLE_REASON_PREFIX)
    )


__all__ = [
    "CANONICAL_DIM_SLUGS",
    "BuildContext",
    "ConversationTurn",
    "ConversationWithVerdicts",
    "DimensionDetail",
    "DimensionScore",
    "ScenarioCase",
    "ScorecardSnapshot",
    "Trial",
    "Verdict",
    "is_not_applicable",
]
