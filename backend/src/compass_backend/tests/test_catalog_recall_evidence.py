from __future__ import annotations

from pathlib import Path

import yaml

from compass_backend.catalog import CandidateCard, RecallBatch, RecallReport
from compass_backend.quality.catalog_recall import (
    CatalogRecallCaseEvidence,
    CatalogRecallRunset,
    RecallExpectedCandidate,
    catalog_recall_evidence_to_json,
    catalog_recall_evidence_to_markdown,
    diagnose_recall_report,
    load_catalog_recall_runset,
)


def _candidate(index: int, entity_ref: str) -> CandidateCard:
    return CandidateCard(
        input_phrase="query",
        entity_type="metric",
        label=f"Candidate {index}",
        entity_ref=entity_ref,
        source_methods=["metric_search"],
        debug_ref=f"metric:{index}",
    )


def _report_with_ranked_metric_refs(*entity_refs: str) -> RecallReport:
    return RecallReport(
        query="query",
        batches=[
            RecallBatch(
                query="query",
                entity_types=["metric"],
                candidates=[
                    _candidate(index, entity_ref)
                    for index, entity_ref in enumerate(entity_refs, start=1)
                ],
                limit=25,
            )
        ],
    )


def test_catalog_recall_runset_loads_m1_cases() -> None:
    runset = load_catalog_recall_runset()

    case_ids = {case.case_id for case in runset.cases}
    assert "golden-22-philadelphia-peer-benefits" in case_ids
    assert "golden-24-starting-salary-workdays" in case_ids
    assert "m1-719-strike-legality" in case_ids
    assert "m1-782-unsupported-union-release-time" in case_ids

    golden_24 = next(
        case for case in runset.cases if case.case_id == "golden-24-starting-salary-workdays"
    )
    assert golden_24.entity_types == ("metric",)
    assert {expected.entity_ref for expected in golden_24.expected_candidates} == {
        "metric:89",
        "metric:69",
    }


def test_catalog_recall_runset_rejects_duplicate_case_ids(tmp_path: Path) -> None:
    payload = {
        "version": "test",
        "cases": [
            {
                "case_id": "duplicate",
                "user_prompt": "one",
                "expected_candidates": [
                    {
                        "entity_type": "metric",
                        "entity_ref": "metric:1",
                        "label": "One",
                    }
                ],
            },
            {
                "case_id": "duplicate",
                "user_prompt": "two",
                "expected_candidates": [
                    {
                        "entity_type": "metric",
                        "entity_ref": "metric:2",
                        "label": "Two",
                    }
                ],
            },
        ],
    }
    path = tmp_path / "runset.yaml"
    path.write_text(yaml.safe_dump(payload))

    try:
        load_catalog_recall_runset(path)
    except ValueError as exc:
        assert "duplicate case_ids" in str(exc)
    else:  # pragma: no cover - defensive assertion clarity
        raise AssertionError("duplicate case id should fail validation")


def test_recall_diagnostics_mark_present_at_5_10_25_and_absent() -> None:
    refs = [f"metric:{index}" for index in range(1, 26)]
    report = _report_with_ranked_metric_refs(*refs)
    expected = (
        RecallExpectedCandidate(entity_type="metric", entity_ref="metric:5", label="5"),
        RecallExpectedCandidate(entity_type="metric", entity_ref="metric:10", label="10"),
        RecallExpectedCandidate(entity_type="metric", entity_ref="metric:25", label="25"),
        RecallExpectedCandidate(entity_type="metric", entity_ref="metric:26", label="26"),
    )

    diagnostics = diagnose_recall_report(report, expected)

    by_ref = {diagnostic.expected.entity_ref: diagnostic for diagnostic in diagnostics}
    assert by_ref["metric:5"].rank == 5
    assert by_ref["metric:5"].present_at_5 is True
    assert by_ref["metric:10"].present_at_5 is False
    assert by_ref["metric:10"].present_at_10 is True
    assert by_ref["metric:25"].present_at_10 is False
    assert by_ref["metric:25"].present_at_25 is True
    assert by_ref["metric:26"].rank is None
    assert by_ref["metric:26"].status == "absent_candidate"

    evidence = CatalogRecallCaseEvidence.from_report(
        case_id="case",
        report=report,
        expected_candidates=expected,
    )
    assert evidence.candidate_refs_top_5 == tuple(refs[:5])
    assert evidence.candidate_refs_top_10 == tuple(refs[:10])
    assert evidence.candidate_refs_top_25 == tuple(refs[:25])


def test_recall_diagnostics_preserve_matched_phrase_and_batch_source() -> None:
    report = RecallReport(
        query="full prompt",
        batches=[
            RecallBatch(
                query="full prompt",
                source="catalog_alias_overlap",
                entity_types=["metric"],
                candidates=[
                    CandidateCard(
                        input_phrase="starting salary",
                        entity_type="metric",
                        label="Starting salary",
                        entity_ref="metric:89",
                        source_methods=["alias"],
                        debug_ref="alias_metric:starting salary:89",
                        metadata={
                            "origin_query": "full prompt",
                            "matched_query": "starting salary",
                            "batch_source": "catalog_alias_overlap",
                        },
                    )
                ],
                limit=25,
            )
        ],
    )
    expected = (
        RecallExpectedCandidate(
            entity_type="metric",
            entity_ref="metric:89",
            label="Starting salary",
        ),
    )

    diagnostics = diagnose_recall_report(report, expected)

    assert diagnostics[0].matched_input_phrase == "starting salary"
    assert diagnostics[0].matched_batch_source == "catalog_alias_overlap"


def test_recall_case_evidence_classifies_present_not_chosen() -> None:
    report = _report_with_ranked_metric_refs("metric:1", "metric:2")
    expected = (
        RecallExpectedCandidate(entity_type="metric", entity_ref="metric:1", label="One"),
        RecallExpectedCandidate(entity_type="metric", entity_ref="metric:2", label="Two"),
    )

    evidence = CatalogRecallCaseEvidence.from_report(
        case_id="case",
        report=report,
        expected_candidates=expected,
        chosen_entity_refs={"metric:1"},
    )

    assert evidence.recall_at_5 is True
    assert evidence.status_counts == {"passed": 1, "present_but_not_chosen": 1}


def test_recall_case_evidence_classifies_later_stage_failures() -> None:
    report = _report_with_ranked_metric_refs("metric:1")
    expected = (
        RecallExpectedCandidate(entity_type="metric", entity_ref="metric:1", label="One"),
    )

    execution = CatalogRecallCaseEvidence.from_report(
        case_id="execution",
        report=report,
        expected_candidates=expected,
        execution_succeeded=False,
    )
    rendering = CatalogRecallCaseEvidence.from_report(
        case_id="rendering",
        report=report,
        expected_candidates=expected,
        rendering_succeeded=False,
    )
    evaluator = CatalogRecallCaseEvidence.from_report(
        case_id="evaluator",
        report=report,
        expected_candidates=expected,
        evaluator_succeeded=False,
    )

    assert execution.status_counts == {"execution_failure": 1}
    assert rendering.status_counts == {"rendering_failure": 1}
    assert evaluator.status_counts == {"evaluator_failure": 1}


def test_recall_evidence_renders_markdown_and_json() -> None:
    report = _report_with_ranked_metric_refs("metric:1")
    evidence = CatalogRecallCaseEvidence.from_report(
        case_id="case",
        report=report,
        expected_candidates=(
            RecallExpectedCandidate(
                entity_type="metric",
                entity_ref="metric:1",
                label="One",
            ),
        ),
    )

    markdown = catalog_recall_evidence_to_markdown(
        version="test",
        evidence=[evidence],
    )
    json_output = catalog_recall_evidence_to_json(
        version="test",
        evidence=[evidence],
    )

    assert "| case | yes | yes | yes | - | present:1 |" in markdown
    assert '"passed": true' in json_output


def test_catalog_recall_runset_model_defaults_entity_types() -> None:
    runset = CatalogRecallRunset.model_validate(
        {
            "version": "test",
            "cases": [
                {
                    "case_id": "case",
                    "user_prompt": "prompt",
                    "expected_candidates": [
                        {
                            "entity_type": "district",
                            "entity_ref": "district:133",
                            "label": "Philadelphia",
                        },
                        {
                            "entity_type": "metric_bundle",
                            "entity_ref": "metric_bundle:232,233",
                            "label": "Benefits",
                        },
                    ],
                }
            ],
        }
    )

    assert runset.cases[0].entity_types == ("district", "metric_bundle")
