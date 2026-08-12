"""Compass quality service — Scorecard / Dimension / Conversation data.

The canonical Pydantic models live in ``compass_backend.quality.scorecard_models``
and are re-exported here for dashboard convenience. Loaders read from real
compass.* tables via the backend pure functions.
"""
from nctqai.services.compass_quality.loaders import (
    load_conversation_with_verdicts,
    load_dimension,
    load_scorecard,
)
from compass_backend.quality.scorecard_models import (
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

__all__ = [
    "BuildContext",
    "CANONICAL_DIM_SLUGS",
    "ConversationTurn",
    "ConversationWithVerdicts",
    "DimensionDetail",
    "DimensionScore",
    "ScenarioCase",
    "ScorecardSnapshot",
    "Trial",
    "Verdict",
    "load_conversation_with_verdicts",
    "load_dimension",
    "load_scorecard",
]
