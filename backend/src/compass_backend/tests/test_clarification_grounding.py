"""#1348 Option B — deterministically ground a planner-emitted district clarify.

When the planner recognizes a same-name district ambiguity at planning time it
emits route=clarify and may author candidate_options itself (non-grounded prose
values). This module re-resolves the district phrase through the catalog and
overrides candidate_options with real, grounded choices (value=district_id,
state-qualified label) so the options are born-from-data and a click resolves.
"""

from __future__ import annotations

import asyncio

from compass_backend.catalog import (
    DistrictCandidate,
    DistrictResolution,
    MetricBundleResolution,
    MetricCandidate,
    NcesDistrictMatch,
)
from compass_backend.contracts import ClarificationOption, ClarificationRequest
from compass_backend.contracts.planning import PlannerTurn
from compass_backend.orchestration.clarification_grounding import (
    apply_district_clarification_grounding,
    apply_metric_clarification_grounding,
    district_clarification_seed_phrases,
    grounded_district_options,
    grounded_metric_options,
    metric_clarification_seed_phrases,
)
from compass_backend.db.catalog import _distinctive_district_token


class _FakeCatalog:
    def __init__(
        self,
        ambiguous: dict[str, list[DistrictCandidate]],
        nces: list[NcesDistrictMatch] | None = None,
    ):
        self._ambiguous = ambiguous
        # NCES matches are keyed by the distinctive token of the seed phrase so
        # the fallback search mirrors the real repository (token + per-state).
        self._nces = nces or []

    async def resolve_districts(self, names, *, states=None):  # noqa: ANN001
        out: dict[str, list[DistrictCandidate]] = {}
        for name in names:
            if name in self._ambiguous:
                out[name] = self._ambiguous[name]
        return DistrictResolution(ambiguous=out)

    async def search_nces_districts(self, name):  # noqa: ANN001
        token = _distinctive_district_token(name)
        if not token:
            return []
        return [m for m in self._nces if token in m.district_name.casefold()]


_CLEVELANDS = [
    NcesDistrictMatch(leaid="3904378", district_name="Cleveland Municipal", state="OH", enrollment=33998, covered=False),
    NcesDistrictMatch(leaid="3700900", district_name="Cleveland County Schools", state="NC", enrollment=14406, covered=False),
    NcesDistrictMatch(leaid="4814370", district_name="Cleveland ISD", state="TX", enrollment=11567, covered=False),
]


_PORTLANDS = {
    "Portland Public Schools": [
        DistrictCandidate(district_id=701, district_name="Portland Public Schools", state="OR", city="Portland", enrollment=44000),
        DistrictCandidate(district_id=702, district_name="Portland Public Schools", state="ME", city="Portland", enrollment=6500),
    ]
}


class TestSeedPhrases:
    def test_dedupes_and_strips_state_suffix(self) -> None:
        clar = ClarificationRequest(
            question="Which one?",
            missing_fields=["district"],
            candidate_options=[
                ClarificationOption(value="x", label="Portland Public Schools"),
                ClarificationOption(value="y", label="Portland Public Schools, OR"),
            ],
        )
        assert district_clarification_seed_phrases(clar) == ["Portland Public Schools"]

    def test_falls_back_to_prose_candidates(self) -> None:
        clar = ClarificationRequest(
            question="Which one?",
            missing_fields=["district"],
            candidates=["Lincoln, NE", "Lincoln County, OR"],
        )
        # state suffix stripped; both collapse to distinct base names
        assert district_clarification_seed_phrases(clar) == ["Lincoln", "Lincoln County"]


class TestGroundedOptions:
    def test_grounds_to_real_district_ids_and_state_labels(self) -> None:
        clar = ClarificationRequest(
            question="There are two Portland Public Schools — which one?",
            missing_fields=["district"],
            candidate_options=[
                ClarificationOption(value="Portland Public Schools, Portland, OR", label="Portland Public Schools"),
            ],
        )
        grounded = asyncio.run(grounded_district_options(clar, _FakeCatalog(_PORTLANDS)))
        assert [o.value for o in grounded] == ["701", "702"]
        assert [o.label for o in grounded] == [
            "Portland Public Schools, OR",
            "Portland Public Schools, ME",
        ]
        assert grounded[0].detail == "44,000 students · Portland"


class TestApplyGrounding:
    def test_overrides_model_authored_options(self) -> None:
        turn = PlannerTurn(
            route="clarify",
            confidence=0.6,
            clarification=ClarificationRequest(
                question="Which Portland?",
                missing_fields=["district"],
                candidate_options=[
                    ClarificationOption(value="Portland Public Schools, Portland, OR", label="Portland Public Schools"),
                    ClarificationOption(value="Portland Public Schools, Portland, ME", label="Portland Public Schools"),
                ],
            ),
        )
        out = asyncio.run(apply_district_clarification_grounding(turn, _FakeCatalog(_PORTLANDS)))
        opts = out.clarification.candidate_options
        assert [o.value for o in opts] == ["701", "702"]  # real district_ids
        assert all(o.value.isdigit() for o in opts)  # custody: no prose values survive
        # prose candidates are synced to the grounded labels (prose + chips agree)
        assert out.clarification.candidates == [
            "Portland Public Schools, OR",
            "Portland Public Schools, ME",
        ]

    def test_non_district_clarify_untouched(self) -> None:
        turn = PlannerTurn(
            route="clarify",
            confidence=0.6,
            clarification=ClarificationRequest(
                question="Which metric?",
                missing_fields=["metric"],
                candidates=["starting salary", "max salary"],
            ),
        )
        out = asyncio.run(apply_district_clarification_grounding(turn, _FakeCatalog(_PORTLANDS)))
        assert out is turn  # unchanged

    def test_clears_options_when_grounding_finds_nothing(self) -> None:
        turn = PlannerTurn(
            route="clarify",
            confidence=0.6,
            clarification=ClarificationRequest(
                question="Which Nowhere?",
                missing_fields=["district"],
                candidate_options=[
                    ClarificationOption(value="made up", label="Nowhere District"),
                ],
            ),
        )
        # Covered grounding empty AND NCES empty (default) -> drop-ungrounded.
        out = asyncio.run(apply_district_clarification_grounding(turn, _FakeCatalog(_PORTLANDS)))
        # No grounded match -> never ship ungrounded options.
        assert out.clarification.candidate_options == []

    def test_grounds_none_covered_name_via_nces(self) -> None:
        """The dominant case: a same-name ambiguity with NO covered match.

        Covered resolution returns nothing, so the seam falls back to the
        grounded NCES search and rebuilds the clarification with real
        candidates by state + a 'none in the Pathfinder' question. Regression
        for cases 340 / 379 (Cleveland).
        """
        turn = PlannerTurn(
            route="clarify",
            confidence=0.6,
            clarification=ClarificationRequest(
                question="Which Cleveland County district — North Carolina, or a different state?",
                missing_fields=["district"],
                candidates=["Cleveland County"],  # planner's typed seed
            ),
        )
        catalog = _FakeCatalog(ambiguous={}, nces=_CLEVELANDS)  # 0 covered
        out = asyncio.run(apply_district_clarification_grounding(turn, catalog))

        clar = out.clarification
        # Grounded, real candidates by state — the largest first.
        assert clar.candidates == [
            "Cleveland Municipal — Ohio",
            "Cleveland County Schools — North Carolina",
            "Cleveland ISD — Texas",
        ]
        # Uncovered districts are prose only — never clickable chips.
        assert clar.candidate_options == []
        # The question states plainly that none are in the Pathfinder, replacing
        # the planner's NC-only guess.
        assert "District Policy Pathfinder" in clar.question
        assert "North Carolina" not in clar.question  # no single-state guess

    def test_none_covered_terminal_when_nces_also_empty(self) -> None:
        """A genuinely unknown name: NCES empty too -> keep drop-ungrounded."""
        turn = PlannerTurn(
            route="clarify",
            confidence=0.6,
            clarification=ClarificationRequest(
                question="Which Nowhere?",
                missing_fields=["district"],
                candidates=["Nowhere"],
            ),
        )
        catalog = _FakeCatalog(ambiguous={}, nces=_CLEVELANDS)  # no "nowhere" match
        out = asyncio.run(apply_district_clarification_grounding(turn, catalog))
        # No grounded NCES rows -> candidates untouched, no invented options.
        assert out.clarification.candidate_options == []


# --- Metric clarify grounding (#1348) -----------------------------------------
#
# Planner-direct metric clarifies carry no typed candidate set (the level names
# live only in the question prose). The planner echoes the single ambiguous
# phrase into `candidates`; this stage expands it through the catalog the same
# way `resolve_metric_bundle` does at execution time, into grounded, clickable
# options whose value is the canonical, re-resolvable metric name.

_PLANNING_TIME_BUNDLE = {
    "planning time": [
        MetricCandidate(
            metric_id=57,
            name="Minimum amount of elementary teacher planning time per week (in minutes)",
            topic="Planning time",
        ),
        MetricCandidate(
            metric_id=58,
            name="Minimum amount of middle school teacher planning time per week (in minutes)",
            topic="Planning time",
        ),
        MetricCandidate(
            metric_id=59,
            name="Minimum amount of high school teacher planning time per week (in minutes)",
            topic="Planning time",
        ),
    ]
}


class _FakeMetricCatalog:
    """Mirror of CatalogResolver.resolve_metric_bundle for the grounding seam."""

    def __init__(self, bundles: dict[str, list[MetricCandidate]]):
        self._bundles = bundles

    async def resolve_metric_bundle(  # noqa: ANN001
        self, query, *, numeric_only=False, limit=5, degree_lane=None
    ):
        candidates = self._bundles.get(query.strip().casefold(), [])
        return MetricBundleResolution(
            query=query, resolved=[], candidates=list(candidates)
        )


class TestMetricSeedPhrases:
    def test_reads_echoed_phrase_from_candidates(self) -> None:
        clar = ClarificationRequest(
            question="Which planning-time metric?",
            missing_fields=["metric"],
            candidates=["planning time"],
        )
        assert metric_clarification_seed_phrases(clar) == ["planning time"]

    def test_dedupes_option_label_and_prose(self) -> None:
        clar = ClarificationRequest(
            question="Which one?",
            missing_fields=["metric"],
            candidate_options=[
                ClarificationOption(value="planning time", label="planning time")
            ],
            candidates=["planning time"],
        )
        assert metric_clarification_seed_phrases(clar) == ["planning time"]


class TestGroundedMetricOptions:
    def test_expands_one_phrase_into_grounded_candidate_set(self) -> None:
        clar = ClarificationRequest(
            question="Which planning-time metric?",
            missing_fields=["metric"],
            candidates=["planning time"],  # the single echoed phrase
        )
        grounded = asyncio.run(
            grounded_metric_options(clar, _FakeMetricCatalog(_PLANNING_TIME_BUNDLE))
        )
        # The one phrase expands deterministically into all three real metrics;
        # value is the canonical name (re-resolvable by execution), not invented.
        assert [o.value for o in grounded] == [
            c.name for c in _PLANNING_TIME_BUNDLE["planning time"]
        ]
        assert grounded[0].detail == "Planning time"  # topic, not definition text


class TestApplyMetricGrounding:
    def test_expands_echoed_phrase_into_options(self) -> None:
        turn = PlannerTurn(
            route="clarify",
            confidence=0.6,
            clarification=ClarificationRequest(
                question="Planning time breaks down by school level — which to rank by?",
                missing_fields=["metric"],
                candidates=["planning time"],
            ),
        )
        out = asyncio.run(
            apply_metric_clarification_grounding(
                turn, _FakeMetricCatalog(_PLANNING_TIME_BUNDLE)
            )
        )
        opts = out.clarification.candidate_options
        assert len(opts) == 3
        assert opts[0].value == (
            "Minimum amount of elementary teacher planning time per week (in minutes)"
        )
        # prose candidates are synced to the grounded labels (prose + chips agree)
        assert out.clarification.candidates == [o.label for o in opts]

    def test_non_metric_clarify_untouched(self) -> None:
        turn = PlannerTurn(
            route="clarify",
            confidence=0.6,
            clarification=ClarificationRequest(
                question="Which district?",
                missing_fields=["district"],
                candidates=["Cleveland County"],
            ),
        )
        out = asyncio.run(
            apply_metric_clarification_grounding(
                turn, _FakeMetricCatalog(_PLANNING_TIME_BUNDLE)
            )
        )
        assert out is turn  # symmetric with district grounding ignoring metric

    def test_rescue_origin_clarify_untouched(self) -> None:
        # #1613: a rescue-fallback clarify is a planner FAILURE — never decorate
        # it with fabricated choices.
        turn = PlannerTurn(
            route="clarify",
            confidence=0.6,
            clarification=ClarificationRequest(
                question="I need to clarify two things first.",
                missing_fields=["metric"],
                candidates=["planning time"],
                rescue_origin=True,
            ),
        )
        out = asyncio.run(
            apply_metric_clarification_grounding(
                turn, _FakeMetricCatalog(_PLANNING_TIME_BUNDLE)
            )
        )
        assert out is turn

    def test_keeps_prose_when_phrase_unresolvable(self) -> None:
        turn = PlannerTurn(
            route="clarify",
            confidence=0.6,
            clarification=ClarificationRequest(
                question="Which metric?",
                missing_fields=["metric"],
                candidates=["a phrase the catalog has never heard of"],
            ),
        )
        out = asyncio.run(
            apply_metric_clarification_grounding(
                turn, _FakeMetricCatalog(_PLANNING_TIME_BUNDLE)
            )
        )
        # Nothing grounded -> keep the planner's prose clarify, ship no chips.
        assert out is turn
        assert out.clarification.candidate_options == []

    def test_scrubs_ungrounded_planner_options_when_unresolvable(self) -> None:
        # m2: if the planner authored its OWN (ungrounded) options and the
        # phrase doesn't resolve, never ship the invented chips — drop them and
        # keep prose, mirroring the district grounder's drop-ungrounded guard.
        turn = PlannerTurn(
            route="clarify",
            confidence=0.6,
            clarification=ClarificationRequest(
                question="Which metric?",
                missing_fields=["metric"],
                candidates=["a phrase the catalog has never heard of"],
                candidate_options=[
                    ClarificationOption(value="made up", label="A made-up metric"),
                ],
            ),
        )
        out = asyncio.run(
            apply_metric_clarification_grounding(
                turn, _FakeMetricCatalog(_PLANNING_TIME_BUNDLE)
            )
        )
        # Invented chips dropped; prose candidates preserved as the fallback.
        assert out.clarification.candidate_options == []
        assert out.clarification.candidates == [
            "a phrase the catalog has never heard of"
        ]


class TestGroundedMetricOptionsDedupe:
    def test_dedupes_metric_ids_across_overlapping_phrases(self) -> None:
        # T2: two seed phrases whose candidate sets overlap on a metric_id must
        # yield each metric once (the grounder's own seen_ids loop, distinct
        # from the execution-origin builder's _dedupe_metric_candidates).
        levels = _PLANNING_TIME_BUNDLE["planning time"]
        catalog = _FakeMetricCatalog(
            {
                "planning time": levels,
                "elementary planning": [levels[0]],  # metric_id 57 overlaps
            }
        )
        clar = ClarificationRequest(
            question="Which one?",
            missing_fields=["metric"],
            candidates=["planning time", "elementary planning"],
        )
        grounded = asyncio.run(grounded_metric_options(clar, catalog))
        values = [o.value for o in grounded]
        assert values == [c.name for c in levels]  # 57 appears once, not twice
        assert len(values) == len(set(values))
