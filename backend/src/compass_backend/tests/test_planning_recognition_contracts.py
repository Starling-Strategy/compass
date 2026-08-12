"""Planning recognition contract regressions."""

from __future__ import annotations

from compass_backend.contracts.recognition import (
    PlanningRecognitionCandidate,
    PlanningRecognitionMention,
    PlanningRecognitionReport,
)


def test_planner_context_redacts_execution_ids() -> None:
    report = PlanningRecognitionReport(
        source="planner_draft",
        mentions=[
            PlanningRecognitionMention(
                phrase="starting salary",
                entity_type="metric",
                status="ambiguous",
                recommended_action="clarify",
                candidates=[
                    PlanningRecognitionCandidate(
                        label=(
                            "Annual base salary for a first year teacher with a "
                            "bachelor's degree"
                        ),
                        entity_type="metric",
                        catalog_ref="metric:89",
                        reason="Catalog candidate from approved alias search.",
                    ),
                    PlanningRecognitionCandidate(
                        label=(
                            "Annual base salary for a first year teacher with a "
                            "master's degree"
                        ),
                        entity_type="metric",
                        catalog_ref="metric:96",
                        reason="Catalog candidate from approved alias search.",
                    ),
                ],
            )
        ],
    )

    planner_payload = report.planner_context()

    assert planner_payload["mentions"][0]["phrase"] == "starting salary"
    assert planner_payload["mentions"][0]["status"] == "ambiguous"
    assert planner_payload["mentions"][0]["recommended_action"] == "clarify"
    assert "catalog_ref" not in planner_payload["mentions"][0]["candidates"][0]
    assert "metric:89" not in repr(planner_payload)


def test_report_requires_finalization_for_ambiguity_and_blockers() -> None:
    ambiguous = PlanningRecognitionReport(
        source="planner_draft",
        mentions=[
            PlanningRecognitionMention(
                phrase="benefits",
                entity_type="metric_bundle",
                status="ambiguous",
                recommended_action="clarify",
                candidates=[],
            )
        ],
    )
    unsupported = PlanningRecognitionReport(
        source="planner_draft",
        mentions=[
            PlanningRecognitionMention(
                phrase="union release time",
                entity_type="unsupported_concept",
                status="unsupported",
                recommended_action="refuse",
                message=(
                    "Compass does not yet have a governed metric for union "
                    "release time."
                ),
            )
        ],
    )

    assert ambiguous.requires_planner_finalization is True
    assert unsupported.requires_planner_finalization is True
