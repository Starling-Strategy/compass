"""#1513: degenerate ``named_districts`` selection in PendingQueryContext.

Live sessions on case 1159 ("What is the starting teacher salary in Portland
Public Schools?") showed the planner emitting a correct clarify turn whose
``pending_context.selection`` encoded the not-yet-known district as
``{"scope": "named_districts", "districts": []}``. ``SelectionSpec``'s #1069
anchor-vanishing guard rejects that shape — correctly for ``QueryPlan`` (an
executable plan must name its districts), wrongly for ``PendingQueryContext``
(typed PARTIAL query memory while still clarifying). With planner output
retries at 0, the whole correct clarify was discarded for the generic rescue.

These tests pin the fix: PendingQueryContext's before-validator maps exactly
that degenerate raw-dict encoding to ``selection=None`` (the legal "asking"
state), while QueryPlan strictness, direct SelectionSpec construction, and
every other invalid shape keep failing loudly.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from compass_backend.contracts.planning import (
    MetricSpec,
    PendingQueryContext,
    PlannerTurn,
    QueryPlan,
    SelectionSpec,
)


def test_clarify_turn_with_degenerate_selection_validates_to_none() -> None:
    """The staging trace's exact failing payload now validates (#1513).

    Mirrors the BoundPlannerTurn nesting from trace 019eb2fca30eeed67d47cb4c
    58ad6c6c: route="clarify" with the degenerate fragment
    ``{"scope": "named_districts", "districts": []}`` inside
    ``clarification.pending_context``. The district being asked about is
    represented as ``selection=None``; the rest of the pending memory survives.
    """

    payload = json.dumps(
        {
            "route": "clarify",
            "confidence": 0.9,
            "clarification": {
                "question": "Which Portland Public Schools do you mean?",
                "missing_fields": ["district"],
                "candidates": [
                    "Portland Public Schools, OR",
                    "Portland Public Schools, ME",
                ],
                "pending_context": {
                    "operation": "lookup",
                    "question": (
                        "What is the starting teacher salary in "
                        "Portland Public Schools?"
                    ),
                    # EXACT failing fragment from the staging stacktrace.
                    "selection": {"scope": "named_districts", "districts": []},
                    "metrics": [{"name": "starting teacher salary"}],
                    "missing_fields": ["district"],
                },
            },
        }
    )

    turn = PlannerTurn.model_validate_json(payload)

    assert turn.route == "clarify"
    assert turn.clarification is not None
    pending = turn.clarification.pending_context
    assert pending is not None
    assert pending.selection is None
    # The rest of the typed partial memory is intact for promotion later.
    assert pending.operation == "lookup"
    assert pending.metrics == [MetricSpec(name="starting teacher salary")]
    assert pending.missing_fields == ["district"]


def test_query_plan_still_rejects_degenerate_named_districts() -> None:
    """The #1069 anchor-vanishing guard on QueryPlan.selection is untouched."""

    with pytest.raises(
        ValidationError, match="named_districts scope requires districts"
    ):
        QueryPlan.model_validate(
            {
                "operation": "lookup",
                "question": (
                    "What is the starting teacher salary in "
                    "Portland Public Schools?"
                ),
                "selection": {"scope": "named_districts", "districts": []},
                "metrics": [{"name": "starting teacher salary"}],
            }
        )


def test_populated_named_districts_passes_through_unchanged() -> None:
    """A real named_districts selection in pending context is untouched."""

    pending = PendingQueryContext.model_validate(
        {
            "selection": {
                "scope": "named_districts",
                "districts": ["Portland Public Schools"],
                "states": ["OR"],
            }
        }
    )
    assert pending.selection == SelectionSpec(
        scope="named_districts",
        districts=["Portland Public Schools"],
        states=["OR"],
    )

    # SelectionSpec INSTANCES (the from-plan builder path) also pass through.
    spec = SelectionSpec(scope="named_districts", districts=["Portland Public Schools"])
    assert PendingQueryContext(selection=spec).selection == spec


def test_direct_selection_spec_construction_still_raises() -> None:
    """SelectionSpec itself never accepts the degenerate shape."""

    with pytest.raises(
        ValidationError, match="named_districts scope requires districts"
    ):
        SelectionSpec(scope="named_districts", districts=[])


def test_other_invalid_selection_shapes_still_fail_loudly() -> None:
    """The mapping is deliberately narrow — only the exact degenerate encoding.

    Shapes that carry extra information (states, unknown keys) or belong to a
    different scope are NOT silently absorbed into ``None``; they keep failing
    loudly so planner bugs stay visible.
    """

    # named_districts + states but no districts is a different planner error.
    with pytest.raises(ValidationError):
        PendingQueryContext.model_validate(
            {
                "selection": {
                    "scope": "named_districts",
                    "districts": [],
                    "states": ["OR"],
                }
            }
        )
    # Unknown keys still hit SelectionSpec's extra="forbid".
    with pytest.raises(ValidationError):
        PendingQueryContext.model_validate(
            {
                "selection": {
                    "scope": "named_districts",
                    "districts": [],
                    "bogus": True,
                }
            }
        )
    # Other scopes' invalid shapes are untouched by the mapping.
    with pytest.raises(ValidationError, match="state scope requires states"):
        PendingQueryContext.model_validate(
            {"selection": {"scope": "state", "states": []}}
        )
