"""Tests for the anchor-value equality-filter planner-guidance snippet (#1485).

Filter QA case 1026: "Which districts have the same school-year length as
Portland, ME?" routed to ``operation="peer_comparison"`` and dead-ended on the
single-anchor shape check ("peer_comparison takes exactly one anchor district
(received 6)"). A "same VALUE as <anchor>" ask is an equality filter on the
anchor's reviewed metric value (``FilterSpec.anchor_value``, ``kind="metric_value"``),
NOT a peer/similarity request. This snippet teaches that routing and must fire
on the reporter's literal prompt while staying off genuine peer/similarity asks.

Run with:
    PYTHONPATH=src uv run pytest \
        src/compass_backend/tests/test_anchor_value_filter_snippet.py --tb=short
"""

from __future__ import annotations

from types import SimpleNamespace

from compass_backend.planning.instruction_snippets import (
    ANCHOR_VALUE_FILTER,
    select_planner_instruction_snippets,
)


def _names(message: str) -> list[str]:
    deps = SimpleNamespace(message=message, query_context=None, recent_routes=())
    return [s.name for s in select_planner_instruction_snippets(deps)]


# ─── Snippet selection: fires on "same value as <anchor>" ─────────────────────


def test_snippet_fires_on_reporter_literal_prompt() -> None:
    # The #1485 / case-1026 literal prompt.
    assert ANCHOR_VALUE_FILTER.name in _names(
        "Which districts have the same school-year length as Portland, ME?"
    )


def test_snippet_fires_on_named_anchor_salary_equality() -> None:
    assert ANCHOR_VALUE_FILTER.name in _names(
        "Which districts have the same starting salary as Dallas ISD?"
    )


def test_snippet_fires_on_self_referent_count() -> None:
    assert ANCHOR_VALUE_FILTER.name in _names(
        "How many districts give teachers the same number of sick days as us in Denver?"
    )


# ─── Snippet selection: stays OFF peer/similarity asks ────────────────────────


def test_snippet_does_not_fire_on_peer_policy_comparison() -> None:
    # "peer districts ... how do sick leave policies compare" is a
    # characteristics-based peer ask (similarity / peer_comparison), not an
    # equality filter on a single metric VALUE.
    assert ANCHOR_VALUE_FILTER.name not in _names(
        "Who are Denver's peer districts and how do sick leave policies compare?"
    )


def test_snippet_does_not_fire_on_similarity_discovery() -> None:
    assert ANCHOR_VALUE_FILTER.name not in _names(
        "Find peers to Denver and show their salaries."
    )


def test_snippet_requires_a_same_or_match_word() -> None:
    # An "as" connector alone (no "same"/"match" equality word) must not trip
    # the snippet — the first required group gates it.
    assert ANCHOR_VALUE_FILTER.name not in _names(
        "How much do teachers make in Texas as of this year?"
    )


# ─── Recipe body (the governed shape it teaches) ──────────────────────────────


def test_recipe_routes_to_anchor_value_filter_not_peer_comparison() -> None:
    body = ANCHOR_VALUE_FILTER.body.lower()
    assert "anchor_value" in body
    # It must explicitly steer AWAY from the wrong operation.
    assert "peer_comparison" in body
    assert "not " in body  # "...not operation='peer_comparison'..."


def test_recipe_keeps_anchor_out_of_selection_districts() -> None:
    body = ANCHOR_VALUE_FILTER.body.lower()
    assert "selection.districts" in body
    assert "all_covered_districts" in body
