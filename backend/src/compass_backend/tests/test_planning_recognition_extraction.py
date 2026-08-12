"""Planning recognition mention extraction regressions."""

from __future__ import annotations

from compass_backend.contracts.planning import (
    FilterSpec,
    MetricSpec,
    OutputSpec,
    PlannerTurn,
    ProfileFieldSpec,
    QueryPlan,
    SelectionSpec,
    SortSpec,
)
from compass_backend.recognition.extraction import extract_planning_mentions


def test_extracts_metric_district_filter_sort_and_profile_mentions() -> None:
    turn = PlannerTurn(
        route="execute",
        confidence=0.82,
        query_plan=QueryPlan(
            operation="rank",
            question="Rank Chicago and Dallas by starting salary and FRPL.",
            selection=SelectionSpec(
                scope="named_districts",
                districts=["Chicago", "Dallas"],
            ),
            metrics=[MetricSpec(name="starting salary")],
            profile_fields=[ProfileFieldSpec(name="free and reduced lunch share")],
            filters=[
                FilterSpec(
                    field="teacher workdays",
                    operator="greater_than",
                    value=190,
                )
            ],
            sort=SortSpec(field="starting salary", direction="desc"),
            output=OutputSpec(format="table"),
        ),
    )

    mentions = extract_planning_mentions(turn)
    keys = {
        (mention.entity_type, mention.phrase, mention.source_field)
        for mention in mentions
    }

    assert ("district", "Chicago", "selection.districts") in keys
    assert ("district", "Dallas", "selection.districts") in keys
    assert ("metric", "starting salary", "metrics.name") in keys
    assert (
        "profile_field",
        "free and reduced lunch share",
        "profile_fields.name",
    ) in keys
    assert ("metric", "teacher workdays", "filters.field") in keys
    assert ("metric", "starting salary", "sort.field") in keys


def test_sort_field_enrollment_is_profile_field_mention() -> None:
    turn = PlannerTurn(
        route="execute",
        confidence=0.78,
        query_plan=QueryPlan(
            operation="rank",
            question="Rank districts by enrollment, largest first.",
            selection=SelectionSpec(scope="all_covered_districts"),
            profile_fields=[ProfileFieldSpec(name="enrollment")],
            sort=SortSpec(field="enrollment", direction="desc"),
            output=OutputSpec(format="table"),
        ),
    )

    mentions = extract_planning_mentions(turn)
    keys = {
        (mention.entity_type, mention.phrase, mention.source_field)
        for mention in mentions
    }

    assert ("profile_field", "enrollment", "sort.field") in keys
    assert ("metric", "enrollment", "sort.field") not in keys


def test_sort_field_frpl_pct_is_profile_field_mention() -> None:
    turn = PlannerTurn(
        route="execute",
        confidence=0.78,
        query_plan=QueryPlan(
            operation="rank",
            question="Rank districts by FRPL percentage.",
            selection=SelectionSpec(scope="all_covered_districts"),
            profile_fields=[ProfileFieldSpec(name="frpl_pct")],
            sort=SortSpec(field="frpl_pct", direction="asc"),
            output=OutputSpec(format="table"),
        ),
    )

    mentions = extract_planning_mentions(turn)
    keys = {
        (mention.entity_type, mention.phrase, mention.source_field)
        for mention in mentions
    }

    assert ("profile_field", "frpl_pct", "sort.field") in keys


def test_sort_field_unknown_phrase_still_routes_to_metric() -> None:
    turn = PlannerTurn(
        route="execute",
        confidence=0.78,
        query_plan=QueryPlan(
            operation="rank",
            question="Rank districts by teacher morale.",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="teacher morale")],
            sort=SortSpec(field="teacher morale", direction="desc"),
            output=OutputSpec(format="table"),
        ),
    )

    mentions = extract_planning_mentions(turn)
    keys = {
        (mention.entity_type, mention.phrase, mention.source_field)
        for mention in mentions
    }

    assert ("metric", "teacher morale", "sort.field") in keys


def test_non_execute_turn_has_no_mentions() -> None:
    turn = PlannerTurn(
        route="direct",
        confidence=0.8,
        direct_response={
            "message": "Compass contains district policy and NCES profile data.",
            "reason": "Capability explanation.",
        },
    )

    assert extract_planning_mentions(turn) == []
