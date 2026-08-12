"""Tests for compass_backend.planning.delta_detection.

Pure unit tests — no DB, no LLM, no async. Each test constructs a minimal
ConversationMemory and checks whether detect_delta_intent fires.
"""

from __future__ import annotations

from compass_backend.contracts.planning import MetricSpec, QueryPlan, SelectionSpec
from compass_backend.contracts.session import ConversationMemory, QueryContext
from compass_backend.planning.delta_detection import (
    DeltaIntent,
    detect_delta_intent,
)


def _memory_with_prior_query() -> ConversationMemory:
    """Build a ConversationMemory with a prior validated query context."""
    plan = QueryPlan(
        question="prior question",
        operation="rank",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="BA starting salary", role="primary")],
        inherit_from_session=False,
    )
    ctx = QueryContext(query_plan=plan)
    return ConversationMemory(latest_query_context=ctx)


def test_first_turn_returns_none() -> None:
    """No prior query → no delta intent regardless of message content."""
    memory = ConversationMemory()
    assert detect_delta_intent("sort those by salary instead", memory) is None


def test_anaphora_alone_fires() -> None:
    """Anaphoric marker + prior query → DeltaIntent."""
    memory = _memory_with_prior_query()
    result = detect_delta_intent("Show the same five by ending salary", memory)
    assert isinstance(result, DeltaIntent)
    assert "the same" in result.matched_anaphora


def test_verb_with_replacement_fires() -> None:
    """Prior-row referent + replacement marker + prior query → DeltaIntent."""
    memory = _memory_with_prior_query()
    result = detect_delta_intent("Sort those by ending salary instead", memory)
    assert isinstance(result, DeltaIntent)
    assert "sort" in result.matched_verbs


def test_by_phrase_without_prior_result_referent_does_not_fire() -> None:
    """A fresh metric query using "by" should not force prior inheritance."""
    memory = _memory_with_prior_query()
    assert detect_delta_intent("Rank districts by BA salary", memory) is None


def test_fresh_comparison_with_by_phrase_does_not_fire() -> None:
    """Fresh comparisons can contain "by" without referencing prior rows."""
    memory = _memory_with_prior_query()
    assert (
        detect_delta_intent("Compare Chicago and Denver by BA salary", memory)
        is None
    )


def test_above_threshold_does_not_fire_as_anaphora() -> None:
    """The word "above" is a threshold cue, not a prior-result referent."""
    memory = _memory_with_prior_query()
    assert detect_delta_intent("Show districts above $50k", memory) is None


def test_sort_those_by_new_metric_fires() -> None:
    """Prior-row referent plus replacement sort should fire."""
    memory = _memory_with_prior_query()
    result = detect_delta_intent(
        "Now sort those by MA starting salary instead",
        memory,
    )
    assert isinstance(result, DeltaIntent)
    assert "those" in result.matched_anaphora
    assert "sort" in result.matched_verbs


def test_verb_without_replacement_returns_none() -> None:
    """Sort/rank/order verb alone without replacement marker → None."""
    memory = _memory_with_prior_query()
    assert detect_delta_intent("Sort the districts", memory) is None


def test_reverse_verb_fires_alone() -> None:
    """The reverse/flip verbs imply replacement → fire on their own."""
    memory = _memory_with_prior_query()
    result = detect_delta_intent("Reverse that order", memory)
    assert isinstance(result, DeltaIntent)
    assert "reverse" in result.matched_verbs
    assert "that order" in result.matched_anaphora


def test_unrelated_message_returns_none() -> None:
    """Fresh query phrasing → None."""
    memory = _memory_with_prior_query()
    assert detect_delta_intent("Show districts in California", memory) is None


def test_re_sort_hyphen_variant_fires() -> None:
    """The 're-sort' / 'reorder' variants fire with a prior-row referent."""
    memory = _memory_with_prior_query()
    result = detect_delta_intent("Reorder those by minimum salary", memory)
    assert isinstance(result, DeltaIntent)
    assert "reorder" in result.matched_verbs


def test_no_prior_query_blocks_strong_signal() -> None:
    """Even unambiguous delta phrasing requires prior context."""
    memory = ConversationMemory()
    assert detect_delta_intent("Reverse that order", memory) is None


# --------------------------------------------------------------------------
# #1011 / #937: list-those-N referents fire on their own (no verb required)
# when the referent + prior context is sufficient evidence of a
# "list these specific rows" follow-up.
# --------------------------------------------------------------------------


def test_list_those_N_referent_fires_on_failing_reporter_prompt() -> None:
    """Literal failing prompt from #1011 case 994 must fire the detector.

    Prior turn produced a count of 17 districts; turn 2 asks "What are the
    17 districts and how many observations do they require?" Today the LLM
    sometimes refuses ("could not structure that request safely"); firing
    the detector adds the DELTA-INTENT CONSTRAINT block which mandates
    `inherit_selection_from="prior_result_rows"`.
    """
    memory = _memory_with_prior_query()
    result = detect_delta_intent(
        "What are the 17 districts and how many observations do they require?",
        memory,
    )
    assert isinstance(result, DeltaIntent)
    assert "the N districts" in result.matched_anaphora


def test_list_those_N_referent_fires_for_those_N_phrasing() -> None:
    """'Show those 17' fires without needing a verb."""
    memory = _memory_with_prior_query()
    result = detect_delta_intent("Show those 17", memory)
    assert isinstance(result, DeltaIntent)
    assert "those N" in result.matched_anaphora


def test_bare_list_those_n_is_name_list_intent() -> None:
    """C2 (#1414): a bare 'name/list the inherited set' ask is a name/list intent."""
    memory = _memory_with_prior_query()
    result = detect_delta_intent("What are the 18 districts?", memory)
    assert isinstance(result, DeltaIntent)
    assert result.matched_list_referents == ("the N districts",)
    assert result.is_name_list_intent() is True


def test_sort_delta_is_not_name_list_intent() -> None:
    """A sort/reorder delta carries a referent but a verb — not a name/list ask."""
    memory = _memory_with_prior_query()
    result = detect_delta_intent("Reverse those 10 districts", memory)
    assert isinstance(result, DeltaIntent)
    assert result.matched_verbs  # the reverse verb fired
    assert result.is_name_list_intent() is False


def test_aggregate_count_question_is_not_name_list_intent() -> None:
    """'How many of those N ...' wants a number, not the set enumerated (C2).

    The referent fires (so selection still inherits) but the count question
    suppresses the forced list, leaving the count shape to the planner.
    """
    memory = _memory_with_prior_query()
    result = detect_delta_intent(
        "How many of those 12 require background checks?", memory
    )
    assert isinstance(result, DeltaIntent)
    assert result.has_count_question is True
    assert result.is_name_list_intent() is False


def test_list_those_N_referent_fires_for_those_N_districts_phrasing() -> None:
    """'How many observations do those 17 districts require?' fires."""
    memory = _memory_with_prior_query()
    result = detect_delta_intent(
        "How many observations do those 17 districts require?",
        memory,
    )
    assert isinstance(result, DeltaIntent)
    assert "those N districts" in result.matched_anaphora


def test_list_those_N_referent_requires_prior_query() -> None:
    """Strong referent alone is still suppressed without prior context."""
    memory = ConversationMemory()
    assert (
        detect_delta_intent(
            "What are the 17 districts and how many observations do they require?",
            memory,
        )
        is None
    )


def test_list_those_N_with_new_metric_still_fires() -> None:
    """The new metric in the same prompt doesn't suppress the detector."""
    memory = _memory_with_prior_query()
    result = detect_delta_intent(
        "What is the minimum performance pay for those 17 districts?",
        memory,
    )
    assert isinstance(result, DeltaIntent)
    assert "those N districts" in result.matched_anaphora


# --------------------------------------------------------------------------
# #1211: "how many districts is that?" — a count question whose only referent
# is a BARE prior-result pronoun must fire so the planner counts the set it
# just showed, instead of refusing ("I want to make sure I...").
# --------------------------------------------------------------------------


def test_count_question_with_bare_that_fires() -> None:
    """Reporter's literal #1211 prompt fires via a count cue + bare 'that'.

    Prior turn listed the districts with >190 teacher workdays; the follow-up
    "how many districts is that?" carries no list-N referent and no verb, so
    before #1211 the detector returned None and the planner refused. It now
    inherits the prior rows and counts them (has_count_question suppresses any
    forced list).
    """
    memory = _memory_with_prior_query()
    result = detect_delta_intent("how many districts is that?", memory)
    assert isinstance(result, DeltaIntent)
    assert "that" in result.matched_anaphora
    assert result.has_count_question is True
    assert result.is_name_list_intent() is False


def test_count_question_with_bare_them_fires() -> None:
    """'how many of them?' inherits + counts the prior rows."""
    memory = _memory_with_prior_query()
    result = detect_delta_intent("how many of them?", memory)
    assert isinstance(result, DeltaIntent)
    assert "them" in result.matched_anaphora
    assert result.has_count_question is True


def test_bare_that_without_count_question_does_not_fire() -> None:
    """A bare 'that' outside a count question stays inert (relative-clause guard).

    'Show me districts that pay above $50k' uses 'that' as a relative pronoun,
    not a prior-result referent — it must NOT force prior inheritance.
    """
    memory = _memory_with_prior_query()
    assert (
        detect_delta_intent("Show me districts that pay above $50k", memory) is None
    )


def test_fresh_count_question_without_referent_does_not_fire() -> None:
    """A fresh count with no bare pronoun referent does not inherit prior rows."""
    memory = _memory_with_prior_query()
    assert detect_delta_intent("How many districts are in Texas?", memory) is None


def test_count_bare_referent_requires_prior_query() -> None:
    """The bare-referent count path still requires prior context."""
    memory = ConversationMemory()
    assert detect_delta_intent("how many districts is that?", memory) is None
