"""Regressions for typed ``PlannerTurn.recognition_blockers``.

Audit finding #34 (docs/audit/2026-05-25-audit-report.md): the field was
previously ``list[dict[str, object]]``, discarding the already-typed
``PlanningRecognitionMention`` shape. These tests pin the typed contract
at the persistence seam — instances round-trip, dicts coerce, and
malformed payloads raise at field-validation time (in addition to the
existing W1-01 model_validator).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from compass_backend.contracts.planning import (
    ClarificationRequest,
    PlannerTurn,
)
from compass_backend.contracts.recognition import (
    PlanningRecognitionCandidate,
    PlanningRecognitionMention,
)


def _mention() -> PlanningRecognitionMention:
    return PlanningRecognitionMention(
        phrase="starting salary",
        entity_type="metric",
        status="ambiguous",
        recommended_action="clarify",
        candidates=[
            PlanningRecognitionCandidate(
                label="BA starting salary",
                entity_type="metric",
                catalog_ref="metric:89",
            )
        ],
    )


def _clarification() -> ClarificationRequest:
    return ClarificationRequest(
        question="Which starting-salary metric did you mean?",
        missing_fields=["metric"],
        candidates=["BA starting salary"],
    )


def test_typed_mention_instances_round_trip() -> None:
    """Typed ``PlanningRecognitionMention`` instances pass through unchanged."""

    turn = PlannerTurn(
        route="clarify",
        confidence=0.7,
        clarification=_clarification(),
        recognition_blockers=[_mention()],
    )

    assert len(turn.recognition_blockers) == 1
    blocker = turn.recognition_blockers[0]
    assert isinstance(blocker, PlanningRecognitionMention)
    assert blocker.phrase == "starting salary"
    assert blocker.recommended_action == "clarify"


def test_dict_payload_coerces_via_pydantic() -> None:
    """Pydantic v2 coerces a well-formed dict payload into the typed model."""

    turn = PlannerTurn.model_validate(
        {
            "route": "clarify",
            "confidence": 0.7,
            "clarification": _clarification().model_dump(),
            "recognition_blockers": [
                {
                    "phrase": "starting salary",
                    "entity_type": "metric",
                    "status": "ambiguous",
                    "recommended_action": "clarify",
                    "candidates": [],
                }
            ],
        }
    )

    blocker = turn.recognition_blockers[0]
    assert isinstance(blocker, PlanningRecognitionMention)
    assert blocker.phrase == "starting salary"


def test_malformed_blocker_dict_raises_validation_error() -> None:
    """A blocker dict missing required fields fails at field validation."""

    with pytest.raises(ValidationError):
        PlannerTurn(
            route="clarify",
            confidence=0.7,
            clarification=_clarification(),
            recognition_blockers=[{"phrase": "starting salary"}],  # type: ignore[list-item]
        )


def test_invalid_recommended_action_rejected_by_literal() -> None:
    """``recommended_action`` outside the Literal vocabulary is rejected."""

    with pytest.raises(ValidationError):
        PlannerTurn(
            route="clarify",
            confidence=0.7,
            clarification=_clarification(),
            recognition_blockers=[
                {
                    "phrase": "starting salary",
                    "entity_type": "metric",
                    "status": "ambiguous",
                    "recommended_action": "ignore_silently",
                }  # type: ignore[list-item]
            ],
        )
