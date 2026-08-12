"""Case 393 — denominator-membership follow-ups present the POPULATION.

Turn 1 is a threshold count: "Of 90 covered districts with data, 19 (21.1%)
require formal observations >= 3 times/year." 90 is the selected POPULATION
(covered districts with a current value — the denominator's members); 19 is
the MATCHES (the qualifying subset).

These tests pin the selection-vs-presentation split deterministically (no LLM):
the count result carries BOTH sets into session memory, a population-referent
follow-up ("Who are the 90?" / "who has data?") presents the POPULATION, and a
matches-referent follow-up ("the 19" / "these") still presents the MATCHES.

Increment 2 covers the memory leg; increments 3-4 the detector and planner legs.
"""

from __future__ import annotations

import pytest
from pydantic_ai import ModelRetry

from compass_backend.artifacts import (
    MetricCountResult,
    ResultSelection,
    SelectedDistrict,
    ThresholdCountRow,
    coverage_frame_from_state_counts,
)
from compass_backend.contracts import (
    ConversationMemory,
    MetricSpec,
    PlannerTurn,
    QueryContext,
    QueryContextDistrictRef,
    QueryPlan,
    SelectionSpec,
)
from compass_backend.orchestration.result_diagnostics import (
    _query_context_for_result,
    _result_population_district_refs,
)
from compass_backend.planning import (
    PlannerDeps,
    validate_planner_turn_quality,
)
from compass_backend.tests.test_mvp_count import (
    FakeCountRepository as _FakeCountRepository,
)


def _count_plan() -> QueryPlan:
    return QueryPlan(
        question=(
            "How many districts require formal observations at least 3 times "
            "per year for non-tenured teachers?"
        ),
        operation="count",
        count_kind="threshold_count",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="formal observations", role="primary")],
    )


def _count_result() -> MetricCountResult:
    """A threshold count whose population (90) is larger than its matches (19).

    Modeled small: 3 covered-with-data districts (the population / denominator
    members), of which 2 match the threshold.
    """
    selection = ResultSelection(
        scope="all_covered_districts",
        districts=[
            SelectedDistrict(district_id=10, district_name="Alpha", state="CA"),
            SelectedDistrict(district_id=20, district_name="Bravo", state="CA"),
            SelectedDistrict(district_id=30, district_name="Charlie", state="CA"),
        ],
    )
    row = ThresholdCountRow(
        metric_id=39,
        metric_name="Minimum formal observations per cycle (non-tenured)",
        value=2,
        display_value="2 of 3 covered districts",
        academic_year="2024 - 2025",
        count=2,
        denominator=3,
        percent=66.7,
        filter_statement="value >= 3",
        qualifying_district_ids=[10, 20],
        denominator_district_ids=[10, 20, 30],
        coverage_state="covered",
        coverage_display="2 of 3 covered districts",
        coverage_reason="count_summary",
    )
    return MetricCountResult(
        selection=selection,
        rows=[row],
        coverage_frame=coverage_frame_from_state_counts(
            {"covered": 3},
            universe_count=3,
        ),
        total_considered=3,
        excluded_count=0,
        order_statement="Counted qualifying districts for selected metrics.",
    )


def _memory_after_count(
    *,
    matched: int,
    population: int,
    persist_members: bool = True,
    count_denominator: int | None = None,
) -> ConversationMemory:
    """Build memory whose prior turn was a threshold count with `population`
    denominator members and `matched` qualifying districts.

    ``persist_members=False`` simulates the #1658 serialization flake — the
    population MEMBER list is dropped while the denominator INTEGER survives;
    ``count_denominator`` defaults to ``population``."""
    matched_refs = [
        QueryContextDistrictRef(district_id=i, district_name=f"M{i}", state="CA")
        for i in range(1, matched + 1)
    ]
    population_refs = (
        [
            QueryContextDistrictRef(
                district_id=100 + i, district_name=f"P{i}", state="CA"
            )
            for i in range(1, population + 1)
        ]
        if persist_members
        else []
    )
    ctx = QueryContext(
        query_plan=_count_plan(),
        result_districts=matched_refs,
        result_population_districts=population_refs,
        count_denominator=population if count_denominator is None else count_denominator,
    )
    return ConversationMemory(latest_query_context=ctx)


# ── Increment 3: the population-vs-matches discriminator ──────────────────


def test_population_referent_the_N_fires_as_population() -> None:
    """Variant C literal prompt 'Who are the 90?' — bare 'the N' whose N equals
    the prior denominator presents the POPULATION."""
    from compass_backend.planning.delta_detection import detect_delta_intent

    memory = _memory_after_count(matched=19, population=90)
    result = detect_delta_intent("Who are the 90?", memory)
    assert result is not None
    assert result.present_target == "population"
    assert result.is_name_list_intent() is True


def test_population_referent_the_N_districts_fires_as_population() -> None:
    """Variant B literal prompt 'Who are the 90 districts that have data on this?'
    presents the POPULATION (N equals the denominator)."""
    from compass_backend.planning.delta_detection import detect_delta_intent

    memory = _memory_after_count(matched=19, population=90)
    result = detect_delta_intent(
        "Who are the 90 districts that have data on this?", memory
    )
    assert result is not None
    assert result.present_target == "population"
    assert result.is_name_list_intent() is True


def test_population_referent_those_N_fires_as_population() -> None:
    """Paraphrase 'Who are those 90?' — the 'those N' surface that _LIST_THOSE_N
    already captures must STILL route to population when N == the denominator
    (otherwise it would list the 19 mislabeled as the 90 — the original bug)."""
    from compass_backend.planning.delta_detection import detect_delta_intent

    memory = _memory_after_count(matched=19, population=90)
    result = detect_delta_intent("Who are those 90?", memory)
    assert result is not None
    assert result.present_target == "population"
    assert result.is_name_list_intent() is True


def test_population_referent_those_N_districts_fires_as_population() -> None:
    """'Show me those 90 districts' — 'those N districts' surface routes to
    population when N == the denominator."""
    from compass_backend.planning.delta_detection import detect_delta_intent

    memory = _memory_after_count(matched=19, population=90)
    result = detect_delta_intent("Show me those 90 districts", memory)
    assert result is not None
    assert result.present_target == "population"


def test_those_N_matching_matched_count_stays_matches() -> None:
    """'Who are those 19?' — N == matched count, so it presents the matches."""
    from compass_backend.planning.delta_detection import detect_delta_intent

    memory = _memory_after_count(matched=19, population=90)
    result = detect_delta_intent("Who are those 19?", memory)
    assert result is not None
    assert result.present_target == "matches"


def test_matches_referent_the_N_districts_stays_matches() -> None:
    """'What are the 19 districts?' — N equals the MATCHED count, so it presents
    the matches (the existing list-those-N path, unregressed)."""
    from compass_backend.planning.delta_detection import detect_delta_intent

    memory = _memory_after_count(matched=19, population=90)
    result = detect_delta_intent("What are the 19 districts?", memory)
    assert result is not None
    assert result.present_target == "matches"
    assert result.is_name_list_intent() is True


def test_these_referent_stays_matches() -> None:
    """Variant A 'Who are these?' presents the MATCHES (approved mapping)."""
    from compass_backend.planning.delta_detection import detect_delta_intent

    memory = _memory_after_count(matched=19, population=90)
    result = detect_delta_intent("Who are these?", memory)
    # "these" is a weak anaphor with no verb/replacement marker; it may or may
    # not fire, but if it fires it must NOT route to population.
    if result is not None:
        assert result.present_target == "matches"


def test_bare_the_N_not_matching_any_prior_count_does_not_fire() -> None:
    """'Show me the 5 districts' where 5 is neither denominator nor matched must
    NOT fire the bare 'the N' population path (no real number to anchor to)."""
    from compass_backend.planning.delta_detection import detect_delta_intent

    memory = _memory_after_count(matched=19, population=90)
    assert detect_delta_intent("Show me the 5", memory) is None


def test_population_referent_requires_prior_count() -> None:
    """A bare 'the 90' after a RANK prior (no population carried) does not fire
    the population path — empty result_population_districts."""
    from compass_backend.planning.delta_detection import detect_delta_intent

    rank_plan = QueryPlan(
        question="Rank districts by formal observations",
        operation="rank",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="formal observations", role="primary")],
    )
    ctx = QueryContext(
        query_plan=rank_plan,
        result_districts=[],
        result_population_districts=[],
    )
    memory = ConversationMemory(latest_query_context=ctx)
    assert detect_delta_intent("Who are the 90?", memory) is None


def test_denominator_keyword_routes_to_population() -> None:
    """The unambiguous 'denominator' keyword (no numeric N) routes to population
    when a prior count carried one — the only retained phrasing fallback."""
    from compass_backend.planning.delta_detection import detect_delta_intent

    memory = _memory_after_count(matched=19, population=90)
    result = detect_delta_intent("Who is in the denominator?", memory)
    assert result is not None
    assert result.present_target == "population"


def test_generic_have_data_phrasing_does_not_hijack_fresh_query() -> None:
    """A fresh 'which districts have data on teacher salary?' after a count must
    NOT be force-scoped to the prior count's population. The generic 'have data'
    phrase is not a population referent on its own — only a numeric N == the
    denominator (or the explicit 'denominator' keyword) routes to population."""
    from compass_backend.planning.delta_detection import detect_delta_intent

    memory = _memory_after_count(matched=19, population=90)
    assert (
        detect_delta_intent("which districts have data on teacher salary?", memory)
        is None
    )


# ── #1658 fix-forward: anaphoric phrase survives data drift ────────────────
# A TCD sync moved metric-39 from "90 covered / 19 match" to "96 / 20" AFTER the
# case fixtures were written. A follow-up that cites the STALE number ("the 90")
# must still route to the population via the anaphoric "have data on this" phrase
# — the cited number matches NEITHER the (drifted) 96 population NOR the 20
# matches, so it must not veto the phrase. This is the deployed-staging failure
# (sweep df9fdadb, 8/8 fail) reproduced deterministically.


def test_stale_cited_number_with_anaphoric_phrase_routes_to_population() -> None:
    """Deployed-failure repro: population drifted to 96, matches to 20, but the
    user cites the stale '90'. 'Who are the 90 districts that have data on this?'
    routes to POPULATION on the anaphoric phrase (90 matches neither 96 nor 20,
    so it does not veto). On HEAD the numeric branch returned 'matches'."""
    from compass_backend.planning.delta_detection import _classify_present_target

    memory = _memory_after_count(matched=20, population=96)
    target, fired = _classify_present_target(
        "Who are the 90 districts that have data on this?", memory
    )
    assert target == "population"
    assert fired is True


def test_which_districts_have_data_on_this_routes_to_population() -> None:
    """C03's drift-proof rewording: 'Which districts have data on this?' (no
    number) fires the detector and routes to population via the anaphoric
    phrase, regardless of the live count."""
    from compass_backend.planning.delta_detection import detect_delta_intent

    memory = _memory_after_count(matched=20, population=96)
    result = detect_delta_intent("Which districts have data on this?", memory)
    assert result is not None
    assert result.present_target == "population"
    assert result.is_name_list_intent() is True


def test_anaphoric_phrase_routes_to_population_even_when_members_dropped() -> None:
    """The anaphoric phrase + the #1658 persistence flake combined: the member
    list is dropped (only the count_denominator integer survives) AND the user
    cites a stale number. Still routes to population."""
    from compass_backend.planning.delta_detection import _classify_present_target

    memory = _memory_after_count(matched=20, population=96, persist_members=False)
    assert memory.latest_query_context.result_population_districts == []
    assert memory.latest_query_context.count_denominator == 96
    target, fired = _classify_present_target(
        "who are the 90 districts that have data on this?", memory
    )
    assert target == "population"
    assert fired is True


def test_stale_bare_number_without_phrase_stays_matches_documents_limit() -> None:
    """The limit that forced the C01/C03 rewording: a BARE stale number ('Who
    are the 90?') with NO phrase has no signal once the count drifts off it, so
    it stays 'matches'. Bare-numeric asks are inherently drift-fragile — the live
    B-spine cases use phrases; the bare-numeric discriminator (cited N == the
    current population) is covered by the exact-match unit tests above."""
    from compass_backend.planning.delta_detection import _classify_present_target

    memory = _memory_after_count(matched=20, population=96)
    target, _ = _classify_present_target("Who are the 90?", memory)
    assert target == "matches"


# ── Increment 4: planner wires population through to the selection ────────


def _count_context_with_threshold_filter(
    *, matched: int, population: int
) -> QueryContext:
    """A prior threshold count whose plan carried the value>=3 filter and which
    resolved a population larger than its matched subset."""
    from compass_backend.contracts.planning import FilterSpec

    prior_plan = QueryPlan(
        question=(
            "How many districts require formal observations at least 3 times "
            "per year for non-tenured teachers?"
        ),
        operation="count",
        count_kind="threshold_count",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="formal observations", role="primary")],
        filters=[
            FilterSpec(field="formal observations", operator="greater_than_or_equal", value=3)
        ],
    )
    matched_refs = [
        QueryContextDistrictRef(district_id=i, district_name=f"M{i}", state="CA")
        for i in range(1, matched + 1)
    ]
    population_refs = [
        QueryContextDistrictRef(district_id=100 + i, district_name=f"P{i}", state="CA")
        for i in range(1, population + 1)
    ]
    return QueryContext(
        query_plan=prior_plan,
        result_districts=matched_refs,
        result_population_districts=population_refs,
    )


def _run_population_followup(prompt: str, context: QueryContext) -> QueryPlan:
    """Chain detector → normalize → merge for a follow-up against `context`,
    returning the concrete plan execution would run."""
    from compass_backend.contracts.planning import PlannerTurn
    from compass_backend.planning.delta_detection import detect_delta_intent
    from compass_backend.planning.planner import (
        merge_query_plan_with_context,
        normalize_delta_intent_turn,
    )

    memory = ConversationMemory(latest_query_context=context)
    delta = detect_delta_intent(prompt, memory)
    assert delta is not None
    # The LLM emits a minimal inherit plan; the detector forces the rest.
    llm_plan = QueryPlan(
        question=prompt,
        operation="lookup",
        selection=SelectionSpec(scope="unspecified"),
        inherit_from_session=True,
        inherit_selection_from="prior_result_rows",
    )
    turn = PlannerTurn(route="execute", confidence=0.9, query_plan=llm_plan)
    normalized, _ = normalize_delta_intent_turn(turn, delta)
    return merge_query_plan_with_context(normalized.query_plan, context)


def test_population_followup_selects_population_and_drops_value_filter() -> None:
    """Variant B/C: the population follow-up lists the 90 (population members),
    NOT the 19, and drops the value>=3 threshold so it does not re-narrow."""
    from compass_backend.execution.filters import filter_kind

    context = _count_context_with_threshold_filter(matched=19, population=90)
    concrete = _run_population_followup("Who are the 90?", context)

    # Selection is the POPULATION (P-prefixed ids), not the matches (M-prefixed).
    assert concrete.selection.scope == "named_districts"
    assert len(concrete.selection.districts) == 90
    assert all(name.startswith("P") for name in concrete.selection.districts)
    # The metric-value threshold must be gone (parity: listing the 90 must not
    # re-narrow to the 19).
    assert not any(
        filter_kind(f) == "metric_value" for f in concrete.filters
    )
    # A lookup over the population, not a re-count.
    assert concrete.operation == "lookup"


def test_matches_followup_still_selects_matches() -> None:
    """The existing 'what are the 19 districts' path is unregressed: it lists the
    19 matches and keeps inheriting the matched set."""
    context = _count_context_with_threshold_filter(matched=19, population=90)
    concrete = _run_population_followup("What are the 19 districts?", context)

    assert concrete.selection.scope == "named_districts"
    assert len(concrete.selection.districts) == 19
    assert all(name.startswith("M") for name in concrete.selection.districts)


# ── Increment 4 parity: the executor leg (lookup over the population) ─────


@pytest.mark.asyncio
async def test_executor_population_followup_lists_all_covered_population() -> None:
    """End-to-end deterministic parity (no LLM): turn-1 count, build context,
    turn-2 population follow-up THROUGH THE EXECUTOR. The lookup over the
    population returns exactly the denominator members (all covered, no stale
    prior-year rows), never re-narrowed to the matches.

    Reuses test_mvp_count's FakeCountRepository / DeterministicQueryExecutor.
    """
    from compass_backend.catalog import DistrictCandidate, MetricCandidate
    from compass_backend.contracts.planning import FilterSpec, PlannerTurn
    from compass_backend.execution import DeterministicQueryExecutor
    from compass_backend.planning.delta_detection import detect_delta_intent
    from compass_backend.planning.planner import (
        merge_query_plan_with_context,
        normalize_delta_intent_turn,
    )
    from compass_backend.catalog import DistrictResolution
    from compass_backend.tests.test_mvp_count import (
        FakeCountRepository,
        _answer_row,
        _approved_metric_alias,
    )

    class _NameHonoringRepository(FakeCountRepository):
        """resolve_districts that honors the requested names AND states (the
        shared fake returns ALL districts, which would let a non-population
        district leak into the turn-2 lookup and mask the parity check).

        Mirrors the real ``db/catalog.py`` shape closely enough to exercise the
        #393 ID-keyed handle: a "Name, ST" handle misses the bare-name index, so
        the real ``CatalogResolver`` peels the state suffix and re-resolves the
        bare name under that state — which this fake must honor."""

        async def resolve_districts(self, names, *, states=None):
            state_values = {state.upper() for state in (states or set())}
            resolved = []
            unresolved = []
            for name in names:
                matches = [
                    d
                    for d in self.districts
                    if d.district_name.casefold() == name.casefold()
                    and (not state_values or (d.state or "").upper() in state_values)
                ]
                if matches:
                    resolved.extend(matches)
                else:
                    # Populate unresolved so the real CatalogResolver runs its
                    # split_state_suffix retry on a "Name, ST" handle (#393).
                    unresolved.append(name)
            return DistrictResolution(resolved=resolved, unresolved=unresolved)

    metric = MetricCandidate(
        metric_id=39,
        name=(
            "Minimum number of formal observations per evaluation cycle "
            "for non-tenured teachers"
        ),
        answer_type="numeric",
    )
    # Population = Alpha(3) + Bravo(2), both covered numeric. Match (>=3) = Alpha.
    # Charlie is INA (covered-but-not-numeric → out of the denominator).
    repository = _NameHonoringRepository(
        metrics=[metric],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
            DistrictCandidate(district_id=3, district_name="Charlie", state="CA"),
        ],
        rows=[
            _answer_row(1, "Alpha", "3", metric_id=39, metric_name=metric.name),
            _answer_row(2, "Bravo", "2", metric_id=39, metric_name=metric.name),
            _answer_row(3, "Charlie", "INA", metric_id=39, metric_name=metric.name),
        ],
        aliases=[_approved_metric_alias(metric.name, metric_ids=[39])],
    )
    executor = DeterministicQueryExecutor(repository)

    # Turn 1: the threshold count.
    turn1_plan = QueryPlan(
        operation="count",
        count_kind="threshold_count",
        question=(
            "How many districts require formal observations at least 3 times "
            "per year for non-tenured teachers?"
        ),
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name=metric.name)],
        filters=[FilterSpec(field="value", operator="greater_than_or_equal", value=3)],
    )
    count_outcome = await executor.execute(turn1_plan)
    assert count_outcome.result is not None
    count_row = count_outcome.result.rows[0]
    assert count_row.qualifying_district_ids == [1]  # matches
    assert count_row.denominator_district_ids == [1, 2]  # population

    # Build the session context the orchestrator would persist.
    context = _query_context_for_result(
        query_plan=turn1_plan,
        authority=None,
        result=count_outcome.result,
    )
    assert [r.district_id for r in context.result_population_districts] == [1, 2]

    # Turn 2: "Who are the 2?" (numeric N == population). Chain the detector +
    # planner exactly as orchestration does.
    memory = ConversationMemory(latest_query_context=context)
    delta = detect_delta_intent("Who are the 2?", memory)
    assert delta is not None and delta.present_target == "population"
    llm_plan = QueryPlan(
        question="Who are the 2?",
        operation="lookup",
        selection=SelectionSpec(scope="unspecified"),
        inherit_from_session=True,
        inherit_selection_from="prior_result_rows",
    )
    normalized, _ = normalize_delta_intent_turn(
        PlannerTurn(route="execute", confidence=0.9, query_plan=llm_plan), delta
    )
    concrete = merge_query_plan_with_context(normalized.query_plan, context)
    # #393 MAJOR 1: the helper now emits ID-disambiguating "Name, ST" handles
    # (not bare names) so a duplicate-named population can't collide on re-resolve.
    assert sorted(concrete.selection.districts) == ["Alpha, CA", "Bravo, CA"]

    lookup_outcome = await executor.execute(concrete)
    assert lookup_outcome.result is not None
    # The lookup lists exactly the POPULATION (Alpha + Bravo), all covered — not
    # just the match (Alpha), and no stale prior-year fallback rows.
    listed_ids = sorted({row.district_id for row in lookup_outcome.result.rows})
    assert listed_ids == [1, 2]
    assert all(
        row.coverage_state == "covered" for row in lookup_outcome.result.rows
    )
    assert all(
        row.academic_year == "2024 - 2025" for row in lookup_outcome.result.rows
    )


# ── MAJOR 2: the LLM-direct (non-detector) population path drops the filter ──


def test_llm_direct_population_inherit_drops_value_filter_without_detector() -> None:
    """An LLM-emitted plan with inherit_selection_from='prior_result_population'
    on a phrasing the deterministic detector does NOT recognize must STILL list
    the population with the value threshold dropped.

    The detector recognizes only the "denominator" keyword and the anaphoric
    "have data on this/it/that" (see _POPULATION_PHRASE_PATTERNS). A bare "which
    districts have a recorded value?" carries no anaphor, so the detector does
    NOT fire on it — yet the LLM may still emit prior_result_population for it.
    So the population invariants (materialize the population + DROP the inherited
    value filter) must live in the ALWAYS-RUN merge layer keyed on
    inherit_selection_from, not only inside normalize_delta_intent_turn (which
    runs only when the detector fires).
    """
    from compass_backend.execution.filters import filter_kind
    from compass_backend.planning.delta_detection import detect_delta_intent
    from compass_backend.planning.planner import merge_query_plan_with_context

    context = _count_context_with_threshold_filter(matched=19, population=90)

    # Precondition: the detector does NOT fire on this phrasing — so the
    # filter-drop cannot come from normalize_delta_intent_turn.
    memory = ConversationMemory(latest_query_context=context)
    assert (
        detect_delta_intent("which districts have a recorded value?", memory)
        is None
    )

    # The LLM emits the population inherit flag directly (no detector normalization).
    llm_plan = QueryPlan(
        question="which districts have a recorded value?",
        operation="lookup",
        selection=SelectionSpec(scope="unspecified"),
        inherit_from_session=True,
        inherit_selection_from="prior_result_population",
    )
    concrete = merge_query_plan_with_context(llm_plan, context)

    # Lists the POPULATION (the 90 P-prefixed), not the 19 matches.
    assert concrete.selection.scope == "named_districts"
    assert len(concrete.selection.districts) == 90
    assert all(name.startswith("P") for name in concrete.selection.districts)
    # Parity: the value threshold MUST be dropped so the population is not
    # re-narrowed to the matches.
    assert not any(filter_kind(f) == "metric_value" for f in concrete.filters)


def test_population_inherit_precedes_sentinel_referent_in_session_merge() -> None:
    """A population follow-up that ALSO carries a sentinel referent (scope=
    named_districts + "those districts") must still list the POPULATION, not the
    matches. The session-path merge's sentinel-referent branch must yield to the
    prior_result_population flag — mirroring the non-session path's precedence —
    otherwise the sentinel branch short-circuits and returns the 19 matches.
    """
    from compass_backend.planning.planner import merge_query_plan_with_context

    context = _count_context_with_threshold_filter(matched=19, population=90)
    plan = QueryPlan(
        question="who are those 90 districts?",
        operation="lookup",
        selection=SelectionSpec(
            scope="named_districts", districts=["those districts"]
        ),
        inherit_from_session=True,
        inherit_selection_from="prior_result_population",
    )
    concrete = merge_query_plan_with_context(plan, context)
    assert concrete.selection.scope == "named_districts"
    assert len(concrete.selection.districts) == 90
    assert all(name.startswith("P") for name in concrete.selection.districts)


def test_normalize_plan_selection_population_inherit_without_session_flag() -> None:
    """The non-delta direct path (_normalize_plan_selection_with_context) must
    also honor inherit_selection_from='prior_result_population': an LLM plan that
    sets the population flag but omits inherit_from_session=True (so it skips
    merge_query_plan_with_context entirely) still materializes the POPULATION and
    drops the value threshold.

    On HEAD this path's is_inherit_flag only recognized 'prior_result_rows', so a
    population plan no-opped through it and could surface the matches.
    """
    from compass_backend.contracts.planning import FilterSpec
    from compass_backend.execution.filters import filter_kind
    from compass_backend.planning.planner import (
        _normalize_plan_selection_with_context,
    )

    context = _count_context_with_threshold_filter(matched=19, population=90)

    plan = QueryPlan(
        question="which districts have data on this?",
        inherit_from_session=False,
        inherit_selection_from="prior_result_population",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="formal observations")],
        filters=[
            FilterSpec(
                field="formal observations",
                operator="greater_than_or_equal",
                value=3,
            )
        ],
    )
    result = _normalize_plan_selection_with_context(plan, context)

    assert result.selection.scope == "named_districts"
    assert len(result.selection.districts) == 90
    assert all(name.startswith("P") for name in result.selection.districts)
    # Parity: the value threshold is dropped on this path too.
    assert not any(filter_kind(f) == "metric_value" for f in result.filters)


# ── MAJOR 3: the eval criterion must be gradeable across the multi-turn flow ──


def _migration_138_sql() -> str:
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    path = (
        root
        / "src"
        / "compass_data_sync"
        / "migrations"
        / "138_seed_denominator_membership_regression_393.sql"
    )
    return path.read_text()


def test_migration_138_asserting_criterion_is_scenario_fit_not_judge_prompt() -> None:
    """The 'denominator_followup_lists_population' criterion must grade with
    check_type='scenario_fit', not 'judge_prompt'.

    A plain judge_prompt judge receives only the single-turn answer_text and a
    few scalars — it cannot see the turn-1 -> turn-2 transcript, so "lists the
    population not the matches across two turns" is structurally ungradeable, and
    the planner change is NOT actually eval-gated (AGENTS.md closure rule). Only
    scenario_fit renders the full case_transcript (quality/scenario_fit.py
    _render_case_transcript), so the discrimination can be judged.
    """
    sql = _migration_138_sql()
    assert "'denominator_followup_lists_population'" in sql
    # The criterion block (everything after the criteria INSERT) must declare
    # scenario_fit and must NOT seed the inert judge_prompt 'judge_prompt' key.
    criteria_block = sql[sql.index("INSERT INTO compass.criteria"):]
    assert "'scenario_fit'" in criteria_block
    assert "'judge_prompt'" not in criteria_block
    # scenario_fit reads payload.judge_prompt_template (NOT judge_prompt).
    assert "judge_prompt_template" in criteria_block
    assert '"judge_prompt"' not in criteria_block


def test_migration_138_drops_inert_expected_output_flags() -> None:
    """The per-case expected_output flags (must_list_population_not_matches,
    must_inherit_prior_result_population, must_drop_value_threshold,
    must_list_matches_not_population) are read by NO code — grep proves it. They
    falsely imply the cases gate on them. Remove them; the scenario_fit judge
    grades the expected_behaviour prose + full transcript instead.
    """
    sql = _migration_138_sql()
    for inert_flag in (
        "must_list_population_not_matches",
        "must_inherit_prior_result_population",
        "must_drop_value_threshold",
        "must_list_matches_not_population",
    ):
        assert inert_flag not in sql, f"inert flag still present: {inert_flag}"


def _migration_140_sql() -> str:
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    path = (
        root
        / "src"
        / "compass_data_sync"
        / "migrations"
        / "140_reword_denominator_membership_cases_drift_proof.sql"
    )
    return path.read_text()


def test_migration_140_rewords_bare_numeric_population_prompts_drift_proof() -> None:
    """The two bare-numeric population follow-ups are reworded to phrase-based
    (drift-proof) prompts, and no bare 'Who are the 90?' population prompt is
    re-introduced. A regression prompt that hardcodes a live count broke when the
    TCD sync moved the denominator 90 -> 96 (sweep df9fdadb); these must lean on
    phrasing, not a frozen number."""
    sql = _migration_140_sql()
    # C01 -> the unambiguous "denominator" keyword.
    assert "C01-BARE-THE-N" in sql
    assert "Who is in the denominator?" in sql
    # C03 -> the anaphoric "have data on this" phrase.
    assert "C03-THOSE-N" in sql
    assert "Which districts have data on this?" in sql
    # C02 matches guard -> the current matched count (coherent n==matched return).
    assert "What are the 20 districts?" in sql
    # The bare-numeric population prompts must not survive as SET values.
    assert "'\"Who are the 90?\"'" not in sql
    assert "'\"Who are those 90?\"'" not in sql


def _migration_143_sql() -> str:
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    path = (
        root
        / "src"
        / "compass_data_sync"
        / "migrations"
        / "143_reword_c01_denominator_list_intent.sql"
    )
    return path.read_text()


def test_migration_143_rewords_c01_to_which_districts() -> None:
    """C01 is reworded from 'Who is in the denominator?' (ambiguous — could be a
    definition request) to 'Which districts are in the denominator?' (unambiguously
    a list of members). The 'denominator' keyword still fires the detector; the
    'which districts' prefix makes the list intent explicit for the LLM planner."""
    sql = _migration_143_sql()
    # WHERE clause targets the mig-140 case_code being replaced.
    assert "C01-DENOMINATOR" in sql
    # New prompt is unambiguously a list request.
    assert "Which districts are in the denominator?" in sql
    # The old ambiguous prompt must not survive as a jsonb SET value
    # (comments and notes may reference it; only the literal '".."'::jsonb form matters).
    assert "'\"Who is in the denominator?\"'" not in sql


def test_population_extractor_reads_denominator_district_ids() -> None:
    """_result_population_district_refs returns the denominator members (the 90),
    NOT the matching subset (the 19)."""
    refs = _result_population_district_refs(_count_result())
    assert [ref.district_id for ref in refs] == [10, 20, 30]
    assert [ref.district_name for ref in refs] == ["Alpha", "Bravo", "Charlie"]


def test_query_context_carries_population_and_matches_separately() -> None:
    """The built QueryContext keeps the matches in result_districts (the 19) and
    the population in result_population_districts (the 90)."""
    context = _query_context_for_result(
        query_plan=_count_plan(),
        authority=None,
        result=_count_result(),
    )
    # Matches (unchanged behavior): the qualifying subset.
    assert [ref.district_id for ref in context.result_districts] == [10, 20]
    # Population (new): the denominator members.
    assert [ref.district_id for ref in context.result_population_districts] == [
        10,
        20,
        30,
    ]


# ── MAJOR 1: parity survives a duplicate district name in the population ──────


class _RealisticResolveRepository(_FakeCountRepository):
    """A count repository whose ``resolve_districts`` mirrors the REAL resolver
    semantics in ``db/catalog.py`` (a name-keyed index where a name with >1
    covered match buckets to ``ambiguous`` and is dropped, and a ``states``
    filter narrows the candidate set).

    The shared ``FakeCountRepository`` returns ALL districts as ``resolved``
    regardless of the requested names, so it cannot reproduce the duplicate-name
    parity break. This faithful fake, wrapped in the real ``CatalogResolver``,
    exercises the actual ``split_state_suffix`` retry path the fix relies on.
    """

    async def resolve_districts(self, names, *, states=None):
        from compass_backend.catalog import DistrictResolution
        from compass_backend.db.catalog import (
            normalize_district_name_for_resolution as _norm,
        )

        state_values = {state.upper() for state in (states or set())}
        index: dict[str, list] = {}
        for district in self.districts:
            if state_values and (district.state or "").upper() not in state_values:
                continue
            normalized = _norm(district.district_name)
            if normalized:
                index.setdefault(normalized, []).append(district)

        resolved: list = []
        unresolved: list[str] = []
        ambiguous: dict[str, list] = {}
        for name in names:
            matches = index.get(_norm(name), [])
            if len(matches) == 1:
                resolved.append(matches[0])
            elif len(matches) > 1:
                ambiguous[name] = matches
            else:
                unresolved.append(name)
        return DistrictResolution(
            resolved=resolved, unresolved=unresolved, ambiguous=ambiguous
        )


@pytest.mark.asyncio
async def test_population_followup_lists_all_when_population_has_duplicate_name() -> None:
    """Parity on a duplicate district name (the real universe has exactly one:
    'Portland Public Schools' in BOTH ME and OR).

    The population follow-up must list ALL of the population (count == the stated
    denominator) even when the population contains two districts that share a
    name. On HEAD the shared helper emits BARE names, so the duplicate
    re-resolves to >1 covered match → 'ambiguous' → the whole turn falls into a
    clarification (no list / a short list), breaking parity. The ID-keyed fix
    (carry each district's state into the handle) lets re-resolution disambiguate
    the shared name, so all members list.
    """
    from compass_backend.catalog import DistrictCandidate, MetricCandidate
    from compass_backend.contracts.planning import FilterSpec, PlannerTurn
    from compass_backend.execution import DeterministicQueryExecutor
    from compass_backend.planning.delta_detection import detect_delta_intent
    from compass_backend.planning.planner import (
        merge_query_plan_with_context,
        normalize_delta_intent_turn,
    )
    from compass_backend.tests.test_mvp_count import (
        _answer_row,
        _approved_metric_alias,
    )

    metric = MetricCandidate(
        metric_id=39,
        name=(
            "Minimum number of formal observations per evaluation cycle "
            "for non-tenured teachers"
        ),
        answer_type="numeric",
    )
    # Population = Portland ME(3) + Portland OR(2) + Austin TX(4). All covered
    # numeric → all in the denominator. Two share the name "Portland Public
    # Schools" (the real duplicate).
    repository = _RealisticResolveRepository(
        metrics=[metric],
        districts=[
            DistrictCandidate(
                district_id=1, district_name="Portland Public Schools", state="ME"
            ),
            DistrictCandidate(
                district_id=2, district_name="Portland Public Schools", state="OR"
            ),
            DistrictCandidate(
                district_id=3, district_name="Austin Independent School District", state="TX"
            ),
        ],
        rows=[
            _answer_row(1, "Portland Public Schools", "3", metric_id=39, metric_name=metric.name, state="ME"),
            _answer_row(2, "Portland Public Schools", "2", metric_id=39, metric_name=metric.name, state="OR"),
            _answer_row(3, "Austin Independent School District", "4", metric_id=39, metric_name=metric.name, state="TX"),
        ],
        aliases=[_approved_metric_alias(metric.name, metric_ids=[39])],
    )
    executor = DeterministicQueryExecutor(repository)

    turn1_plan = QueryPlan(
        operation="count",
        count_kind="threshold_count",
        question=(
            "How many districts require formal observations at least 3 times "
            "per year for non-tenured teachers?"
        ),
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name=metric.name)],
        filters=[FilterSpec(field="value", operator="greater_than_or_equal", value=3)],
    )
    count_outcome = await executor.execute(turn1_plan)
    assert count_outcome.result is not None
    count_row = count_outcome.result.rows[0]
    # Population (denominator) = all 3 covered; matches (>=3) = Portland ME + Austin.
    assert count_row.denominator_district_ids == [1, 2, 3]

    context = _query_context_for_result(
        query_plan=turn1_plan,
        authority=None,
        result=count_outcome.result,
    )
    assert [r.district_id for r in context.result_population_districts] == [1, 2, 3]

    memory = ConversationMemory(latest_query_context=context)
    delta = detect_delta_intent("Who are the 3?", memory)
    assert delta is not None and delta.present_target == "population"
    llm_plan = QueryPlan(
        question="Who are the 3?",
        operation="lookup",
        selection=SelectionSpec(scope="unspecified"),
        inherit_from_session=True,
        inherit_selection_from="prior_result_rows",
    )
    normalized, _ = normalize_delta_intent_turn(
        PlannerTurn(route="execute", confidence=0.9, query_plan=llm_plan), delta
    )
    concrete = merge_query_plan_with_context(normalized.query_plan, context)

    lookup_outcome = await executor.execute(concrete)
    # The whole population must list, not collapse to a clarification or drop the
    # duplicate-named members.
    assert lookup_outcome.result is not None
    listed_ids = sorted({row.district_id for row in lookup_outcome.result.rows})
    assert listed_ids == [1, 2, 3]
    # Parity: listed == the stated denominator.
    assert len(listed_ids) == len(count_row.denominator_district_ids)


@pytest.mark.asyncio
async def test_matches_followup_handles_resolve_through_catalog_resolver() -> None:
    """No-regress proof for the MATCHES path under the #393 "Name, ST" handle.

    The shared helper now emits "Name, ST" handles for BOTH the population and the
    matches inheritance. This pins that the matches-path selection RESOLVES through
    the real CatalogResolver's split_state_suffix retry — i.e. the suffix change
    did not break name resolution on the existing list-those-N matches path.

    We assert at the resolution boundary (``resolve_selection`` over the merged
    matches plan), not full ``execute``: a matches follow-up KEEPS the prior
    count's value threshold (only the population path drops it), and a lookup with
    a metric-value filter is rejected by an unrelated shape guard. Isolating the
    resolution step proves exactly the suffix round-trip we changed.
    """
    from compass_backend.artifacts import ResultSelection
    from compass_backend.catalog import (
        DistrictCandidate,
        DistrictResolution,
        MetricCandidate,
    )
    from compass_backend.catalog.resolution import CatalogResolver
    from compass_backend.contracts.planning import FilterSpec, PlannerTurn
    from compass_backend.execution.selection import resolve_selection
    from compass_backend.planning.delta_detection import detect_delta_intent
    from compass_backend.planning.planner import (
        merge_query_plan_with_context,
        normalize_delta_intent_turn,
    )

    class _FaithfulRepository(_FakeCountRepository):
        """resolve_districts mirroring db/catalog.py: name-keyed, states-honoring,
        and populating ``unresolved`` so the real split_state_suffix retry runs on
        a "Name, ST" handle."""

        async def resolve_districts(self, names, *, states=None):
            state_values = {state.upper() for state in (states or set())}
            resolved: list = []
            unresolved: list[str] = []
            for name in names:
                matches = [
                    d
                    for d in self.districts
                    if d.district_name.casefold() == name.casefold()
                    and (not state_values or (d.state or "").upper() in state_values)
                ]
                if matches:
                    resolved.extend(matches)
                else:
                    unresolved.append(name)
            return DistrictResolution(resolved=resolved, unresolved=unresolved)

    repository = _FaithfulRepository(
        metrics=[MetricCandidate(metric_id=39, name="formal observations", answer_type="numeric")],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="TX"),
        ],
        rows=[],
    )
    catalog = CatalogResolver(repository)

    # A prior count carried two matched districts (the 19, modeled as Alpha+Bravo).
    prior_plan = QueryPlan(
        operation="count",
        count_kind="threshold_count",
        question="How many districts require formal observations >= 3?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="formal observations")],
        filters=[FilterSpec(field="value", operator="greater_than_or_equal", value=3)],
    )
    context = QueryContext(
        query_plan=prior_plan,
        result_districts=[
            QueryContextDistrictRef(district_id=1, district_name="Alpha", state="CA"),
            QueryContextDistrictRef(district_id=2, district_name="Bravo", state="TX"),
        ],
        result_population_districts=[],
    )

    memory = ConversationMemory(latest_query_context=context)
    delta = detect_delta_intent("What are the 2 districts?", memory)
    assert delta is not None and delta.present_target == "matches"
    llm_plan = QueryPlan(
        question="What are the 2 districts?",
        operation="lookup",
        selection=SelectionSpec(scope="unspecified"),
        inherit_from_session=True,
        inherit_selection_from="prior_result_rows",
    )
    normalized, _ = normalize_delta_intent_turn(
        PlannerTurn(route="execute", confidence=0.9, query_plan=llm_plan), delta
    )
    concrete = merge_query_plan_with_context(normalized.query_plan, context)
    # Matches selection carries the "Name, ST" handles for the 2 qualifying.
    assert sorted(concrete.selection.districts) == ["Alpha, CA", "Bravo, TX"]

    # The handles resolve through the real CatalogResolver split_state_suffix
    # retry — no ambiguity, no drop, no clarification.
    selection = await resolve_selection(concrete, catalog, allow_unresolved=False)
    assert isinstance(selection, ResultSelection)
    assert sorted(d.district_id for d in selection.districts) == [1, 2]


# ── #1658: robust to the intermittent persistence drop of the member list ──
# The count's denominator INTEGER survives serialization reliably; the population
# MEMBER list (result_population_districts) is intermittently dropped (~12%). The
# follow-up must still (a) classify as population — grounding on the integer, not
# the member-list length — and (b) materialize the population by RE-DERIVING it
# (an all-covered lookup of the inherited metric) rather than an empty named set.


def test_classifier_grounds_on_denominator_integer_when_members_dropped() -> None:
    """#1658: even when result_population_districts is dropped, "the 90" classifies
    as POPULATION because it equals the persisted denominator integer (not the
    matched count). Without this it falls back to the matches — the wrong answer."""
    from compass_backend.planning.delta_detection import _classify_present_target

    memory = _memory_after_count(matched=19, population=90, persist_members=False)
    assert memory.latest_query_context.result_population_districts == []
    assert memory.latest_query_context.count_denominator == 90

    target, fired = _classify_present_target(
        "who are the 90 districts that have data on this?", memory
    )
    assert target == "population"
    assert fired is True
    # And the matches referent still classifies as matches.
    assert _classify_present_target("what are the 19 districts?", memory) == (
        "matches",
        False,
    )


def test_population_materialization_rederives_when_members_dropped() -> None:
    """#1658: with the member list dropped, the population follow-up re-derives via
    an all-covered lookup of the inherited metric (immune to the persistence flake),
    instead of building an empty named_districts selection."""
    from compass_backend.planning.planner import (
        _selection_from_context_population_rows,
    )

    context = _memory_after_count(
        matched=19, population=90, persist_members=False
    ).latest_query_context
    selection = _selection_from_context_population_rows(context)
    assert selection.scope == "all_covered_districts"

    # When the members ARE present, it still enumerates them exactly (unchanged).
    present = _memory_after_count(matched=19, population=3).latest_query_context
    sel_present = _selection_from_context_population_rows(present)
    assert sel_present.scope == "named_districts"
    assert len(sel_present.districts) == 3


# ── #1673: the population OUTPUT-VALIDATOR guard defers to int recovery ──────
# Companion to the merge-stage recovery test above: those prove the recovery
# materializes the population; these prove validate_planner_turn_quality's
# #393/#1673 guard now LETS THE TURN THROUGH to that recovery instead of
# rejecting first (which, under retries={"output":0}, became a rescue
# clarification the user saw instead of the answer).


def _population_inherit_turn() -> PlannerTurn:
    """A 'who are the N?' follow-up: execute + inherit the prior count's
    population. The guard at planner.py:826 runs on exactly this shape."""
    return PlannerTurn(
        route="execute",
        confidence=0.8,
        query_plan=QueryPlan(
            inherit_from_session=True,
            inherit_selection_from="prior_result_population",
            question="Who are the 90?",
            selection=SelectionSpec(scope="unspecified"),
            metrics=[],
        ),
    )


def test_population_guard_recovers_when_members_dropped_with_denominator() -> None:
    """The member list flaked empty in serialization (#1658) but the
    count_denominator integer survived (#1660). The guard must NOT reject — it
    lets the turn through so the merge stage re-derives the population. Before
    #1673 this raised ModelRetry, surfacing a rescue clarification to the user."""
    turn = _population_inherit_turn()
    memory = _memory_after_count(matched=19, population=90, persist_members=False)
    assert memory.latest_query_context.result_population_districts == []
    assert memory.latest_query_context.count_denominator == 90

    validated = validate_planner_turn_quality(
        turn, PlannerDeps(message="Who are the 90?", memory=memory)
    )
    assert validated is turn


def test_population_guard_rejects_when_no_prior_count() -> None:
    """With NO retained count to recover from (no members AND no
    count_denominator), the guard still rejects — recovery is impossible, so a
    clarification is the correct outcome."""
    turn = _population_inherit_turn()
    ctx = QueryContext(
        query_plan=_count_plan(),
        result_districts=[],
        result_population_districts=[],
        count_denominator=None,
    )
    memory = ConversationMemory(latest_query_context=ctx)

    with pytest.raises(ModelRetry, match="requires a previous count"):
        validate_planner_turn_quality(
            turn, PlannerDeps(message="Who are the 90?", memory=memory)
        )
