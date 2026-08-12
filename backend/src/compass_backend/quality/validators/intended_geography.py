"""Independent intended-geography Gate-3 for resolved-district selections.

The C1 problem (#1248): the live closed-loop check
``_validate_resolved_district_selection`` compares the result's districts
against ``authority.selection`` — but ``authority.selection`` is built from the
SAME resolver output that produced ``result.selection``
(see ``execution/_helpers.py::_validation_authority``). Answer key == exam, so a
REAL-but-WRONG district (the DC/Houston bug) is waved through.

This validator's oracle is INDEPENDENT of the resolver: it re-derives the user's
INTENDED state from the prose the user actually typed — ``plan.selection.districts``
(the phrase the planner read off the user's request) and ``plan.question`` — and
asserts the resolved district's ``state`` is consistent with it. The resolver
never touched this oracle, so the check can catch a real district landing in the
wrong geography even when every closed-loop membership check passes.

Doctrine note (no-prose-dispatch, backend AGENTS.md #4): this is a VERIFY-only
prose read at the RESULT boundary. It does not route, rewrite, or substitute a
typed field — it adds a check that the resolved ``state`` matches the geography
the user signalled. That is the permitted validator shape. The module lives under
``quality/`` (outside the prose-dispatch scanner's scope by design) and reads
prose only to assert, never to dispatch.

Over-flag tradeoff: an unresolvable / ambiguous intended geography MUST NOT flag
a correct answer. We bias toward flagging ONLY clear, unambiguous mismatches —
we derive an intended state only from a strong, low-false-positive signal
(explicit ``", <state>"`` suffix, the literal token "DC"/"D.C."/"Washington DC",
or a small curated set of unambiguous major cities). When no such signal is
present, or when the prose carries more than one candidate state, we stay silent.
The cost of this bias is some real misses (e.g. an ambiguous city name resolved
to the wrong state slips through), accepted because over-flagging a correct
answer erodes trust and a clarify is preferred over a false refusal.
"""

from __future__ import annotations

import re

from compass_backend.artifacts import ResultSet
from compass_backend.contracts.planning import QueryPlan
from compass_backend.contracts.validation import ValidationFinding
from compass_backend.reference import normalize_state as _normalize_state
from compass_backend.reference.states import STATE_NAME_TO_ABBREVIATION

from .._validation_helpers import _finding

# ─── Intended-geography signals (high-precision, low-recall by design) ────────

# Literal "DC" / "D.C." / "Washington DC" tokens → District of Columbia. The bare
# city name "Washington" is intentionally EXCLUDED here: it collides with the
# state of Washington and several real "Washington" districts, so it is ambiguous
# and must not drive a flag on its own.
_DC_RE = re.compile(
    r"\b(?:washington[,\s]+d\.?\s*c\.?|d\.?\s*c\.?|district\s+of\s+columbia)\b",
    re.IGNORECASE,
)

# Curated unambiguous major cities → state. Kept deliberately small: every entry
# must be a city whose name does NOT collide with a same-named district in
# another state in normal usage. Ambiguous names (Springfield, Columbus,
# Portland, Riverside, Franklin, Washington, …) are EXCLUDED on purpose so the
# oracle stays silent rather than guessing.
_UNAMBIGUOUS_CITY_TO_STATE: dict[str, str] = {
    "houston": "TX",
    "chicago": "IL",
    "los angeles": "CA",
    "philadelphia": "PA",
    "boston": "MA",
    "seattle": "WA",
    "atlanta": "GA",
    "denver": "CO",
    "detroit": "MI",
    "baltimore": "MD",
    "milwaukee": "WI",
    "albuquerque": "NM",
    "nashville": "TN",
    "minneapolis": "MN",
    "san diego": "CA",
    "san francisco": "CA",
    "dallas": "TX",
    "austin": "TX",
}

# Explicit ", <state>" suffix (full name or 2-letter abbrev). Matches the tail of
# a phrase like "Springfield, Illinois" or "Springfield, IL".
_STATE_ABBREVS: frozenset[str] = frozenset(STATE_NAME_TO_ABBREVIATION.values())
_EXPLICIT_STATE_SUFFIX_RE = re.compile(r",\s*([A-Za-z][A-Za-z.\s]+?)\s*$")


def _intended_state_from_text(text: str) -> str | None:
    """Re-derive the user's intended state from a single prose fragment.

    Returns a 2-letter state abbreviation when the fragment carries an
    UNAMBIGUOUS geography signal, else ``None``. Order matters: an explicit
    ``", <state>"`` suffix is the strongest signal, then the DC literal, then a
    curated unambiguous city.
    """

    fragment = (text or "").strip()
    if not fragment:
        return None

    suffix = _EXPLICIT_STATE_SUFFIX_RE.search(fragment)
    if suffix is not None:
        candidate = suffix.group(1).strip()
        normalized = _normalize_state(candidate)
        if normalized in _STATE_ABBREVS:
            return normalized

    if _DC_RE.search(fragment):
        return "DC"

    lowered = " ".join(fragment.casefold().split())
    for city, state in _UNAMBIGUOUS_CITY_TO_STATE.items():
        if re.search(rf"\b{re.escape(city)}\b", lowered):
            return state

    return None


def _intended_states(plan: QueryPlan) -> set[str]:
    """Collect all unambiguous intended states from the user's typed prose.

    Oracle source = the phrases the user actually typed: each named-district
    phrase (``plan.selection.districts``) plus the full ``plan.question``. This
    is independent of the resolver's output (``authority.selection`` /
    ``result.selection``) by construction — nothing here reads a resolved ID.

    If the prose yields MORE THAN ONE distinct intended state we return the set;
    the caller treats a multi-state set as "this district legitimately may match
    any of them" and only flags when the resolved state matches NONE.
    """

    states: set[str] = set()
    for phrase in plan.selection.districts:
        derived = _intended_state_from_text(phrase)
        if derived:
            states.add(derived)
    question_state = _intended_state_from_text(plan.question)
    if question_state:
        states.add(question_state)
    return states


def _validate_intended_geography(
    plan: QueryPlan,
    result: ResultSet,
) -> list[ValidationFinding]:
    """Flag a resolved district whose state contradicts the user's intent.

    Independent Gate-3: re-derives the intended state from user prose (never from
    the resolver) and asserts each resolved district's ``state`` is consistent.
    Fail-safe biased: silent when intent is unresolvable/ambiguous, or when a row
    carries no ``state`` to check.
    """

    if plan.selection.scope != "named_districts":
        return []

    intended = _intended_states(plan)
    if not intended:
        # No unambiguous signal — do not over-flag a possibly-correct answer.
        return []

    selection = result.selection
    resolved = list(selection.districts) if selection is not None else []
    resolved_states = {
        normalized
        for district in resolved
        if (normalized := _normalize_state(district.state))
    }
    if not resolved_states:
        # No resolved row carries a state — cannot assert coverage; stay silent.
        return []

    # Coverage semantics (not per-district membership): flag only when a state
    # the user UNAMBIGUOUSLY named is covered by NO resolved district — i.e. "you
    # asked for <state>, nothing came back in <state>." This catches the
    # DC/Houston real-but-wrong bug (intended DC, resolver returned a WA district
    # → DC uncovered) WITHOUT over-flagging a legitimately multi-state selection:
    # a resolved district in some *other* state is not penalised as long as every
    # named geography is itself honoured. The earlier "resolved_state not in
    # intended" rule false-flagged a correct weak-signal district (e.g. "Houston
    # and Riverside" → Riverside/CA flagged because the pooled intended set was
    # only {TX}).
    uncovered = sorted(intended - resolved_states)
    if not uncovered:
        return []

    return [
        _finding(
            code="selection_intended_geography_mismatch",
            dimension="selection",
            message=(
                "The user named a geography that no resolved district matches — "
                "a real-but-wrong district may have been selected."
            ),
            # severity stays the default "error" so the live path BLOCKS the
            # wrong-district answer (ValidationReport.valid=False → render_response
            # short-circuits to status="validation_failed" rather than rendering a
            # real-but-wrong district). A graceful "which <name> did you mean?"
            # clarify pivot is a follow-up; refusing the wrong answer is the floor.
            metadata={
                "intended_states": sorted(intended),
                "uncovered_states": uncovered,
                "resolved_states": sorted(resolved_states),
                "resolved_districts": [
                    {
                        "district_id": district.district_id,
                        "district_name": district.district_name,
                        "resolved_state": _normalize_state(district.state),
                    }
                    for district in resolved
                ],
            },
        )
    ]
