"""#1665 — a digit-free "name them" after a count deterministically lists the
prior result rows, state-qualified (no Portland disambiguation).

Trace session e0c5a385: a covered-universe / topic-coverage salary count answers
"77 districts"; the user says "name them"; the answer was a catalog
disambiguation block ("I found more than one covered district matching 'Portland
Public Schools' — ME or OR?") instead of listing the 77.

Root cause (b) — ROUTING, not missing state. The prior result rows already carry
state. But ``detect_delta_intent`` returned None for the digit-free "name them"
(the words name/list are in none of the verb tuples, and every list-those-N
referent requires a digit), so no inherit_selection_from was forced; the LLM
free-emitted a bare named_districts plan that collided at resolve_districts.

These pure unit tests pin the fix deterministically (no LLM):
  1. detect_delta_intent("name them", <prior result rows>) fires and forces
     inheritance from the prior RESULT ROWS (present_target == "matches").
  2. The resulting SelectionSpec (through normalize → merge) is state-qualified
     ("Portland Public Schools, ME" and ", OR" both present — no bare-name
     collision).
  3. A first-turn "name 5 districts with sick leave data" does NOT fire the
     follow-up inherit.
"""

from __future__ import annotations

from compass_backend.contracts.planning import (
    MetricSpec,
    PlannerTurn,
    QueryPlan,
    SelectionSpec,
)
from compass_backend.contracts.session import (
    ConversationMemory,
    QueryContext,
    QueryContextDistrictRef,
)
from compass_backend.planning.delta_detection import (
    DeltaIntent,
    detect_delta_intent,
)
from compass_backend.planning.planner import (
    merge_query_plan_with_context,
    normalize_delta_intent_turn,
)


def _covered_universe_count_context() -> QueryContext:
    """A covered-universe / topic-coverage salary count whose MATCHES snapshot
    (``result_districts``) is the full covered set WITH state.

    Mirrors the e0c5a385 count shape: denominator_district_ids is empty, so the
    population path is empty (result_population_districts=[], count_denominator
    absent) and the follow-up must inherit the RESULT ROWS, not the population.
    Two members share the name "Portland Public Schools" (the real ME/OR
    duplicate) so the test exercises the state-qualified handle.
    """
    plan = QueryPlan(
        question="How many districts have data on starting salary?",
        operation="count",
        count_kind="topic_coverage_count",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="BA starting salary", role="primary")],
    )
    return QueryContext(
        query_plan=plan,
        result_districts=[
            QueryContextDistrictRef(
                district_id=1, district_name="Portland Public Schools", state="ME"
            ),
            QueryContextDistrictRef(
                district_id=2, district_name="Portland Public Schools", state="OR"
            ),
            QueryContextDistrictRef(
                district_id=3,
                district_name="Austin Independent School District",
                state="TX",
            ),
        ],
        # Covered-universe count: no qualifying-vs-denominator split → no
        # population members and no denominator integer.
        result_population_districts=[],
        count_denominator=None,
    )


def _concrete_plan(prompt: str, context: QueryContext) -> QueryPlan:
    """Chain detector → normalize → merge for `prompt` against `context`,
    returning the concrete plan execution would run. Mirrors the #393
    test's `_run_population_followup` helper."""
    memory = ConversationMemory(latest_query_context=context)
    delta = detect_delta_intent(prompt, memory)
    assert delta is not None
    # The LLM emits a minimal/garbled inherit plan; the detector forces the rest.
    llm_plan = QueryPlan(
        question=prompt,
        operation="lookup",
        selection=SelectionSpec(scope="unspecified"),
        inherit_from_session=True,
    )
    turn = PlannerTurn(route="execute", confidence=0.9, query_plan=llm_plan)
    normalized, _ = normalize_delta_intent_turn(turn, delta)
    return merge_query_plan_with_context(normalized.query_plan, context)


# ── Detector fires + routes to the prior RESULT ROWS (matches) ────────────


def test_name_them_fires_as_result_row_followup() -> None:
    """"name them" after a count fires and inherits the prior RESULT ROWS.

    present_target stays "matches" (covered-universe count → no population), so
    normalize forces inherit_selection_from="prior_result_rows".
    """
    context = _covered_universe_count_context()
    memory = ConversationMemory(latest_query_context=context)
    delta = detect_delta_intent("name them", memory)
    assert isinstance(delta, DeltaIntent)
    assert delta.fired_name_list_referent is True
    assert delta.present_target == "matches"
    assert delta.is_name_list_intent() is True

    turn = PlannerTurn(
        route="execute",
        confidence=0.9,
        query_plan=QueryPlan(
            question="name them",
            operation="lookup",
            selection=SelectionSpec(scope="unspecified"),
            inherit_from_session=True,
        ),
    )
    normalized, changed = normalize_delta_intent_turn(turn, delta)
    assert changed is True
    assert normalized.query_plan.inherit_selection_from == "prior_result_rows"


def test_name_them_lists_state_qualified_districts_no_collision() -> None:
    """The concrete selection enumerates the prior rows as state-qualified
    handles — both Portland twins present, no bare-name collision."""
    context = _covered_universe_count_context()
    concrete = _concrete_plan("name them", context)

    assert concrete.selection.scope == "named_districts"
    assert "Portland Public Schools, ME" in concrete.selection.districts
    assert "Portland Public Schools, OR" in concrete.selection.districts
    assert "Austin Independent School District, TX" in concrete.selection.districts
    # The bare ambiguous name must NOT appear (it would re-resolve to >1 covered
    # match → the disambiguation block the bug produced).
    assert "Portland Public Schools" not in concrete.selection.districts
    # All three covered rows are enumerated.
    assert len(concrete.selection.districts) == 3
    # A lookup over the named set, not a re-count.
    assert concrete.operation == "lookup"


def test_list_them_paraphrase_fires() -> None:
    """"list them" is the same enumerate-the-prior-rows intent."""
    context = _covered_universe_count_context()
    memory = ConversationMemory(latest_query_context=context)
    delta = detect_delta_intent("list them", memory)
    assert isinstance(delta, DeltaIntent)
    assert delta.fired_name_list_referent is True
    assert delta.is_name_list_intent() is True


def test_name_the_districts_paraphrase_fires() -> None:
    """"name the districts" — the literal "the districts" anaphora pairs with the
    name verb (neither word is in the digit-requiring referent tuples)."""
    context = _covered_universe_count_context()
    memory = ConversationMemory(latest_query_context=context)
    delta = detect_delta_intent("name the districts", memory)
    assert isinstance(delta, DeltaIntent)
    assert delta.fired_name_list_referent is True
    assert delta.is_name_list_intent() is True


def test_those_anaphora_does_not_fire_name_list_referent() -> None:
    """"those" is a demonstrative determiner, NOT a pure pronoun, so it is
    deliberately excluded from the name/list anaphora. A bare "name those" /
    "list those" must not trip the name-list inherit (it falls through to the
    soft LLM follow-up snippet) — the conservative #1665 posture. The
    pre-existing weak-anaphora path may still return a DeltaIntent, but
    ``fired_name_list_referent`` must stay False."""
    context = _covered_universe_count_context()
    memory = ConversationMemory(latest_query_context=context)
    for prompt in ("name those", "list those"):
        delta = detect_delta_intent(prompt, memory)
        assert delta is None or delta.fired_name_list_referent is False


def test_fresh_those_selection_does_not_fire_name_list_inherit() -> None:
    """"those" + a fresh qualifier is a NEW selection that must NOT be forced to
    inherit the prior rows ("asked for the Texas ones, got all the prior rows").
    This is the same over-fire class the #1665 review blocked for "the
    districts"; "those" is dropped from the name-list anaphora entirely (rather
    than guarded) because the trailing-qualifier guard cannot tell anaphoric
    "those districts" from fresh "those Texas districts".

    The invariant is at the name-list-referent level: "those" must never set
    ``fired_name_list_referent`` (which is what forces
    ``inherit_selection_from='prior_result_rows'`` and discards the fresh
    filter). The pre-existing weak-anaphora ``_REFERENT_PATTERNS`` path (which
    predates #1665 and is out of scope here) may still return a DeltaIntent, but
    it must not trip the name-list inherit."""
    context = _covered_universe_count_context()
    memory = ConversationMemory(latest_query_context=context)
    for prompt in (
        "list those districts in Texas",
        "name those top 10 districts by enrollment",
    ):
        delta = detect_delta_intent(prompt, memory)
        fired = delta is not None and delta.fired_name_list_referent
        assert not fired, f"{prompt!r} must not fire the name-list inherit"
        if delta is not None:
            assert delta.is_name_list_intent() is False


def test_name_all_of_them_paraphrase_fires() -> None:
    """"name all of them" fires via the verb + "all of them" anaphora."""
    context = _covered_universe_count_context()
    memory = ConversationMemory(latest_query_context=context)
    delta = detect_delta_intent("name all of them", memory)
    assert isinstance(delta, DeltaIntent)
    assert delta.fired_name_list_referent is True
    assert delta.is_name_list_intent() is True


# ── Negative guards ───────────────────────────────────────────────────────


def test_first_turn_name_request_does_not_fire_follow_up_inherit() -> None:
    """A first-turn "name 5 districts with sick leave data" carries the name VERB
    but no prior-result ANAPHORA — it must NOT force prior-row inheritance.

    Even WITH prior context present, the absence of them/the districts/all of
    them keeps the name-list referent inert, and no list-those-N referent
    matches ("5 districts" is a fresh selection, not "the 5 districts")."""
    context = _covered_universe_count_context()
    memory = ConversationMemory(latest_query_context=context)
    assert detect_delta_intent("name 5 districts with sick leave data", memory) is None


def test_name_them_requires_prior_query() -> None:
    """The name-them referent still requires prior context (first-turn safety)."""
    assert detect_delta_intent("name them", ConversationMemory()) is None


def test_name_verb_alone_without_anaphora_does_not_fire() -> None:
    """"name the largest districts" — verb but no prior-result anaphora → inert."""
    context = _covered_universe_count_context()
    memory = ConversationMemory(latest_query_context=context)
    assert detect_delta_intent("name the largest districts in Texas", memory) is None


def test_fresh_the_districts_query_with_qualifier_does_not_fire() -> None:
    """"the districts" + a trailing fresh qualifier is a NEW selection, not a
    prior-result referent — it must NOT force inheritance (the #1665-review
    over-fire regression: "asked for California, got the prior Texas rows").

    Even with prior context present, a name/list verb plus "the districts IN
    Texas" / "WITH paid family leave" / "THAT pay the most" stays inert because
    the negative lookahead on the "the districts" anaphora rejects a trailing
    qualifier word. Only the standalone "name the districts" (no qualifier)
    inherits — covered by test_name_the_districts_paraphrase_fires."""
    context = _covered_universe_count_context()
    memory = ConversationMemory(latest_query_context=context)
    assert detect_delta_intent("list the districts in Texas", memory) is None
    assert (
        detect_delta_intent("name the districts with paid family leave", memory)
        is None
    )
    assert detect_delta_intent("list the districts that pay the most", memory) is None
