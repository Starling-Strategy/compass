"""Catalog regression for B05 / #1556 — a referent's lone fuzzy candidate must
not be auto-accepted when its state contradicts the user's explicit qualifier.

Client bug (B05): "same school-year length as Portland, ME" silently resolved the
anchor to Portland Public Schools, OR (id 127) instead of Portland, ME (id 72).

The literal comma/space suffix forms already resolve correctly on main (the
covered-index intersection in CatalogResolver.resolve_districts narrows
"Portland" + states={ME} to the ME row). This test pins the residual GROUNDING
HOLE that survives that path: the live fuzzy / recall search does NOT strictly
honor the state filter (see this module's docstring), so it can drop a lone
WRONG-state candidate into the ``ambiguous`` bucket even for a ME-filtered query.
``resolve_referent_district`` then auto-accepted that lone candidate purely
because *some* state was present — substituting OR for the ME the user named.

The fix gates the lone-candidate auto-accept on STATE CONSISTENCY: when the user
supplied an explicit state qualifier (a trailing "ME" or a typed
AnchorValueSpec.state), the lone candidate's state must be one of those states,
else we clarify rather than silently answer about the wrong district.

NOTE (slider-down / eval-gated): this is a defensive grounding guard. The literal
B05 path is already green on main (the prod OR sighting is deploy-lag); this
hardens against a wrong-state recall hit reaching the auto-accept. If recall
really returns cross-state hits, the deeper owning boundary is the recall service.

Run with:
    PYTHONPATH=src uv run pytest \
        src/compass_backend/tests/test_referent_state_consistency.py --tb=short
"""

from __future__ import annotations

import pytest

from compass_backend.catalog import DistrictCandidate, DistrictResolution
from compass_backend.contracts.planning import (
    MetricSpec,
    QueryPlan,
    SelectionSpec,
)
from compass_backend.execution.referent_resolution import resolve_referent_district
from compass_backend.execution.types import ExecutionClarification


_PORTLAND_OR = DistrictCandidate(
    district_id=127, district_name="Portland Public Schools", state="OR"
)
_PORTLAND_ME = DistrictCandidate(
    district_id=72, district_name="Portland Public Schools", state="ME"
)


class _FuzzyIgnoresStateCatalog:
    """A catalog whose resolution mirrors the live fuzzy/recall path that does
    NOT strictly honor the state filter: it returns a lone wrong-state candidate
    in the ``ambiguous`` bucket regardless of the requested states.

    This is the seam the module docstring (#1192 / #739) calls out — the fuzzy
    path "puts a lone fuzzy hit there." On main, ``resolve_referent_district``
    auto-accepted it whenever any state was present.
    """

    def __init__(self, lone: DistrictCandidate) -> None:
        self._lone = lone

    async def resolve_districts(self, names, *, states=None):
        return DistrictResolution(
            resolved=[],
            unresolved=[],
            ambiguous={names[0]: [self._lone]},
        )


class _FuzzyHonorsStateCatalog:
    """Control: the fuzzy path returns the CORRECT-state lone candidate."""

    def __init__(self, lone: DistrictCandidate) -> None:
        self._lone = lone

    async def resolve_districts(self, names, *, states=None):
        return DistrictResolution(
            resolved=[],
            unresolved=[],
            ambiguous={names[0]: [self._lone]},
        )


def _plan() -> QueryPlan:
    return QueryPlan(
        operation="lookup",
        question="Which districts have the same school-year length as Portland, ME?",
        selection=SelectionSpec(scope="all_covered_districts", districts=[]),
        metrics=[MetricSpec(name="school-year length")],
    )


@pytest.mark.asyncio
async def test_wrong_state_lone_candidate_clarifies_not_substitutes() -> None:
    """User said "Portland, ME"; fuzzy returns lone Portland OR → must clarify.

    B05 RED on main: the OR candidate was auto-accepted (returned as the anchor)
    because a state qualifier was present, silently answering about the wrong
    district. The fix requires the lone candidate's state to match the user's
    explicit qualifier, so a contradiction (ME asked, OR returned) clarifies.
    """
    catalog = _FuzzyIgnoresStateCatalog(_PORTLAND_OR)

    result = await resolve_referent_district(catalog, "Portland, ME", _plan())

    assert isinstance(result, ExecutionClarification), (
        "a lone candidate whose state (OR) contradicts the user's explicit "
        "qualifier (ME) must clarify, not silently resolve to the wrong district; "
        f"got {type(result).__name__}: "
        f"{getattr(result, 'district_id', None)} {getattr(result, 'state', '')}"
    )


@pytest.mark.asyncio
async def test_correct_state_lone_candidate_still_resolves() -> None:
    """User said "Portland, ME"; fuzzy returns lone Portland ME → still resolves.

    The state-consistency gate must not regress the legitimate disambiguation:
    when the lone candidate's state matches the explicit qualifier, it resolves.
    """
    catalog = _FuzzyHonorsStateCatalog(_PORTLAND_ME)

    result = await resolve_referent_district(catalog, "Portland, ME", _plan())

    assert isinstance(result, DistrictCandidate), (
        "a lone candidate whose state matches the explicit qualifier must "
        f"resolve; got {type(result).__name__}"
    )
    assert result.district_id == 72


@pytest.mark.asyncio
async def test_extra_state_qualifier_also_gates() -> None:
    """A typed AnchorValueSpec.state (extra_states) gates the same way as a suffix.

    When the anchor state is passed as ``extra_states={'ME'}`` (the preferred
    typed path), a lone OR candidate still contradicts it and must clarify.
    """
    catalog = _FuzzyIgnoresStateCatalog(_PORTLAND_OR)

    result = await resolve_referent_district(
        catalog, "Portland", _plan(), extra_states={"ME"}
    )

    assert isinstance(result, ExecutionClarification), (
        "an explicit extra_states qualifier (ME) contradicting a lone OR "
        f"candidate must clarify; got {type(result).__name__}"
    )
