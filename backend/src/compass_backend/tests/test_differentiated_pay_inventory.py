"""Tests for the differentiated-pay inventory planner-guidance snippet (#1017).

The snippet teaches the governed *execute-inventory* shape so "differentiated
pay in <region>" commits to a useful answer — an inventory across the four
Differentiated & Performance Pay subtopics — instead of clarifying, and offers
the narrower cuts. Per the planning-strategy memo, the recipe is a governed
*prior* the planner adapts; region expansion + catalog grounding stay in the
deterministic layer (the recipe names the governed region phrase, never a
hardcoded state list — backend guardrail #2).

Run with:
    PYTHONPATH=src uv run pytest \
        src/compass_backend/tests/test_differentiated_pay_inventory.py --tb=short
"""

from __future__ import annotations

from types import SimpleNamespace

from compass_backend.planning.instruction_snippets import (
    DIFFERENTIATED_PAY_INVENTORY,
    select_planner_instruction_snippets,
)


def _names(message: str) -> list[str]:
    deps = SimpleNamespace(message=message, query_context=None, recent_routes=())
    return [s.name for s in select_planner_instruction_snippets(deps)]


# ─── Snippet selection ────────────────────────────────────────────────────────


def test_snippet_fires_for_differentiated_pay_in_the_south() -> None:
    # The #1017 prompt (feedback-sheet row 120).
    assert DIFFERENTIATED_PAY_INVENTORY.name in _names(
        "Do any of the districts in the South offer differentiated pay?"
    )


def test_snippet_does_not_fire_for_data_inventory_request() -> None:
    # "what data do you have on differentiated pay" is DATA_INVENTORY's
    # capability shape (route=direct), not the execute family inventory.
    assert DIFFERENTIATED_PAY_INVENTORY.name not in _names(
        "What data do you have on differentiated pay?"
    )


def test_snippet_does_not_fire_for_count_request() -> None:
    assert DIFFERENTIATED_PAY_INVENTORY.name not in _names(
        "How many districts offer differentiated pay?"
    )


def test_snippet_does_not_fire_for_specific_subtopic() -> None:
    # A specific subtopic phrase does not contain "differentiated pay", so the
    # family inventory must not fire — normal recognition handles it.
    assert DIFFERENTIATED_PAY_INVENTORY.name not in _names(
        "What's the maximum performance pay bonus in Dallas?"
    )


# ─── Recipe body (the governed shape it teaches) ──────────────────────────────


def test_recipe_documents_execute_inventory_shape() -> None:
    body = DIFFERENTIATED_PAY_INVENTORY.body
    assert 'route="execute"' in body
    assert 'operation="lookup"' in body
    # one offer-anchor metric per subtopic, with the exact names catalog
    # resolution depends on (no DB ids — guardrail #2).
    assert (
        "Performance pay based on evaluation ratings, unrelated to salary schedule"
        in body
    )
    assert (
        "District offers additional pay for teaching in schools classified as hard-to-staff"
        in body
    )
    assert "Additional pay for National Board Certification" in body


def test_recipe_passes_region_phrase_to_deterministic_resolver() -> None:
    body = DIFFERENTIATED_PAY_INVENTORY.body
    # Names the governed region phrase and defers expansion to the executor —
    # must NOT enumerate states itself (guardrail #2; ends-vs-means).
    assert '"the South"' in body
    assert "expands it to the governed member states" in body


def test_recipe_commits_instead_of_clarifying_and_offers_narrower_cuts() -> None:
    body = DIFFERENTIATED_PAY_INVENTORY.body
    assert "Do not clarify or refuse" in body
    assert "one way to look at" in body.lower()
    for lens in (
        "Performance pay",
        "Hard-to-staff schools",
        "Hard-to-staff subjects",
        "Other differentiated pay",
    ):
        assert lens in body
