"""Grounded none-covered district disambiguation — pure unit coverage.

The match strategy (`_distinctive_district_token`) and the prose composer are
the parts testable without a live DB. The full `search_nces_districts_by_name`
SQL is validated against staging `nces_districts` directly (it returns the
prominent Cleveland per state above the enrollment floor).
"""

from __future__ import annotations

import pytest

from compass_backend.catalog import NcesDistrictMatch
from compass_backend.contracts import ProfileFieldSpec, QueryPlan, SelectionSpec
from compass_backend.db.catalog import _distinctive_district_token
from compass_backend.execution import DeterministicQueryExecutor
from compass_backend.rendering.district_disambiguation import (
    grounded_uncovered_district_labels,
    grounded_uncovered_district_question,
)

from compass_backend.tests.test_mvp_execution import (  # type: ignore[attr-defined]
    FakePolicyAnswerRepository,
    _metric_alias,
)


class TestDistinctiveToken:
    def test_drops_generic_district_type_word(self) -> None:
        # "county" is generic — the proper noun must win, so the NCES search
        # keys on "cleveland" and finds Cleveland Municipal (OH), not just the
        # "%cleveland county%" substring.
        assert _distinctive_district_token("Cleveland County") == "cleveland"

    def test_drops_school_stopwords(self) -> None:
        assert _distinctive_district_token("Springfield Public Schools") == "springfield"

    def test_generic_word_never_beats_proper_noun_on_length(self) -> None:
        # "york" (4) is shorter than "county" (6); dropping the generic word is
        # what keeps the proper noun, not raw longest-token.
        assert _distinctive_district_token("York County") == "york"

    def test_bare_name_passes_through(self) -> None:
        assert _distinctive_district_token("Cleveland") == "cleveland"

    def test_empty_returns_none(self) -> None:
        assert _distinctive_district_token("the district") is None


class TestProseComposer:
    def _matches(self) -> list[NcesDistrictMatch]:
        return [
            NcesDistrictMatch(leaid="3904378", district_name="Cleveland Municipal", state="OH", enrollment=33998, covered=False),
            NcesDistrictMatch(leaid="3700900", district_name="Cleveland County Schools", state="NC", enrollment=14406, covered=False),
        ]

    def test_labels_name_districts_by_full_state(self) -> None:
        assert grounded_uncovered_district_labels(self._matches()) == [
            "Cleveland Municipal — Ohio",
            "Cleveland County Schools — North Carolina",
        ]

    def test_question_states_none_in_pathfinder(self) -> None:
        q = grounded_uncovered_district_question(self._matches())
        assert "District Policy Pathfinder" in q
        assert "none" in q.casefold()

    def test_covered_matches_are_filtered_out(self) -> None:
        # Defensive: a covered row must never be labeled "not in the Pathfinder".
        matches = self._matches() + [
            NcesDistrictMatch(leaid="9999999", district_name="Cleveland Covered", state="CA", enrollment=5000, covered=True)
        ]
        labels = grounded_uncovered_district_labels(matches)
        assert all("Covered" not in label for label in labels)

    def test_caps_at_five_options(self) -> None:
        many = [
            NcesDistrictMatch(leaid=str(i), district_name=f"Name {i}", state="OH", enrollment=1000 + i, covered=False)
            for i in range(8)
        ]
        assert len(grounded_uncovered_district_labels(many)) == 5


# ---------------------------------------------------------------------------
# Issue #1758 — the profile_lookup empty-rows branch must split by WHY it is
# empty: an out-of-universe district routes to the honest uncovered-district
# message; a real district with a genuine data gap keeps the "no profile data
# yet" message. These exercise the executor (where the bug fires), not just the
# composer above.
# ---------------------------------------------------------------------------

_OPAQUE_PROFILE_REFUSAL = (
    "I could not resolve sourced NCES/profile values for those districts yet."
)


class _FRLProfileRepository(FakePolicyAnswerRepository):
    """A fake repo wired for an FRL-share profile lookup, with a stubbable NCES
    full-universe search so we can model the reported (zero-match) Bravo case
    and a with-candidates case without a live DB."""

    def __init__(self, *, nces_universe_rows: list[NcesDistrictMatch]) -> None:
        super().__init__(
            aliases=[
                _metric_alias(
                    "frl share",
                    entity_type="nces_field",
                    canonical_id="frpl_pct",
                )
            ]
        )
        # The approved profile field resolves via the alias above to this key.
        from compass_backend.catalog import NCESFieldCandidate

        self.nces_fields = [
            NCESFieldCandidate(
                field_key="frpl_pct",
                label="Free/Reduced-Price Lunch share",
                data_type="float",
                description="Share of students eligible for FRPL.",
            )
        ]
        self._nces_universe_rows = nces_universe_rows
        self.nces_universe_calls: list[str] = []

    async def search_nces_districts_by_name(
        self, name: str
    ) -> list[NcesDistrictMatch]:
        self.nces_universe_calls.append(name)
        return list(self._nces_universe_rows)


def _frl_plan(district: str) -> QueryPlan:
    return QueryPlan(
        operation="profile_lookup",
        question=f"What is the FRL share for {district}?",
        selection=SelectionSpec(scope="named_districts", districts=[district]),
        profile_fields=[ProfileFieldSpec(name="FRL share")],
    )


@pytest.mark.asyncio
async def test_out_of_universe_district_routes_to_uncovered_message() -> None:
    # Reporter-literal prompt (issue #1758): "Bravo School District" is fictional
    # — it is in no universe table, so the full-NCES search returns nothing. The
    # zero-row profile lookup must NOT emit the opaque internal refusal; it routes
    # to the grounded uncovered-district clarification, which degrades to a
    # truthful, candidate-free question when there are no NCES matches.
    repository = _FRLProfileRepository(nces_universe_rows=[])
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(_frl_plan("Bravo School District"))

    assert outcome.clarification is not None
    message = outcome.message
    assert message != _OPAQUE_PROFILE_REFUSAL
    assert "could not resolve sourced NCES/profile values" not in message
    assert "District Policy Pathfinder" in message
    assert outcome.clarification.missing_fields == ["district"]
    # The unknown name fell through unresolved, so we searched the full universe.
    assert repository.nces_universe_calls == ["Bravo School District"]


@pytest.mark.asyncio
async def test_real_district_with_no_frpl_data_keeps_no_profile_data_message() -> None:
    # A real, covered district that resolves cleanly but has no FRPL row is a
    # genuine data gap — NOT an unknown district — so it keeps the original
    # "no profile data yet" message and never claims it isn't in the Pathfinder.
    from compass_backend.execution import DistrictCandidate

    repository = _FRLProfileRepository(nces_universe_rows=[])
    repository.districts = [
        DistrictCandidate(
            district_id=4242,
            district_name="Riverbend Unified",
            state="CA",
        )
    ]
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(_frl_plan("Riverbend Unified"))

    assert outcome.result is None  # refusal carries no result
    assert outcome.message == _OPAQUE_PROFILE_REFUSAL
    # A resolved district never triggers the full-universe uncovered search.
    assert repository.nces_universe_calls == []
