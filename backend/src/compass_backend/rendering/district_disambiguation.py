"""Deterministic prose for a grounded none-covered district disambiguation.

When a user names an ambiguous district whose name has NO covered match (the
dominant case — ~280 multi-state names have zero covered districts), the
clarification must still name the real candidates by state and say plainly that
none are in the Pathfinder. The candidates come from a grounded NCES search
(`catalog.search_nces_districts` → `NcesDistrictMatch`), never from the
planner's guess.

This module renders those typed matches into the clarification's question +
candidate bullets. Inputs are typed `NcesDistrictMatch` rows; outputs are
typed-template prose — the same "no prose-dispatch below the planner" shape as
`rescue_clarification.py` (the prose template lives in the rendering layer, fed
only typed data). The "is not in the District Policy Pathfinder" framing is the
same one `artifacts/coverage.py` uses for out-of-universe districts.
"""

from __future__ import annotations

from compass_backend.catalog import NcesDistrictMatch
from compass_backend.reference.states import state_full_name

# Keep the bullet list legible — show at most this many states.
_MAX_DISAMBIGUATION_OPTIONS = 5


def _uncovered(matches: list[NcesDistrictMatch]) -> list[NcesDistrictMatch]:
    """Matches with no current Pathfinder coverage, capped for legibility.

    This composer is only used on the none-covered fallback path, so every
    match should already be uncovered; the filter is a defensive guard so a
    stray covered row can never be mislabeled "not in the Pathfinder".
    """

    return [match for match in matches if not match.covered][:_MAX_DISAMBIGUATION_OPTIONS]


def grounded_uncovered_district_labels(matches: list[NcesDistrictMatch]) -> list[str]:
    """Return one grounded "Name — State" label per uncovered match.

    e.g. ``"Cleveland Municipal — Ohio"``. The state is the full name; an
    unknown/blank state degrades to the bare district name.
    """

    labels: list[str] = []
    for match in _uncovered(matches):
        state = state_full_name(match.state)
        labels.append(f"{match.district_name} — {state}" if state else match.district_name)
    return labels


def grounded_uncovered_district_question(matches: list[NcesDistrictMatch]) -> str:
    """Compose the grounded disambiguation question for a none-covered name.

    Names that several districts share the name and that none are in the
    Pathfinder, then invites the user to point at a covered district instead.
    The candidate bullets (rendered separately from
    ``ClarificationRequest.candidates``) carry the per-state grounding.
    """

    uncovered = _uncovered(matches)
    if not uncovered:
        # No grounded rows — caller should not have reached here; return a
        # truthful, candidate-free question rather than an invented one.
        return (
            "I couldn't match that to a district in the District Policy "
            "Pathfinder. Which district did you mean?"
        )
    return (
        "Several districts share that name, and none of them are currently in "
        "the District Policy Pathfinder, so I don't have policy data for them. "
        "The largest by state are below — if you meant a covered district, let "
        "me know which one."
    )
