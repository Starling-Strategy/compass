"""Recall@k evidence contracts for governed catalog candidate discovery."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from compass_backend.catalog import (
    CandidateCard,
    RecallEntityType,
    RecallReport,
    RecallSourceMethod,
)

DEFAULT_CATALOG_RECALL_RUNSET_PATH = Path(__file__).with_name(
    "catalog_recall_runset.yaml"
)

RecallStageStatus = Literal[
    "absent_candidate",
    "present",
    "present_but_not_chosen",
    "execution_failure",
    "rendering_failure",
    "evaluator_failure",
    "passed",
]


class RecallExpectedCandidate(BaseModel):
    """Official thing a recall run should make visible for a case."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_type: RecallEntityType
    entity_ref: str = Field(min_length=1)
    label: str = Field(min_length=1)
    required: bool = True
    notes: str = ""


class CatalogRecallRunsetCase(BaseModel):
    """One scenario-like prompt and the official candidates it should recall."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    user_prompt: str = Field(min_length=1)
    sources: tuple[str, ...] = Field(default_factory=tuple)
    entity_types: tuple[RecallEntityType, ...] = Field(default_factory=tuple)
    expected_candidates: tuple[RecallExpectedCandidate, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _default_entity_types(self) -> "CatalogRecallRunsetCase":
        if self.entity_types:
            return self
        entity_types: list[RecallEntityType] = []
        for expected in self.expected_candidates:
            if expected.entity_type not in entity_types:
                entity_types.append(expected.entity_type)
        self.entity_types = tuple(entity_types)
        return self


class CatalogRecallRunset(BaseModel):
    """Committed M1 recall runset manifest."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1)
    cases: tuple[CatalogRecallRunsetCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_case_ids(self) -> "CatalogRecallRunset":
        case_ids = [case.case_id for case in self.cases]
        duplicates = sorted(
            case_id for case_id, count in Counter(case_ids).items() if count > 1
        )
        if duplicates:
            raise ValueError(f"catalog recall runset has duplicate case_ids: {duplicates}")
        return self


class RecallExpectedCandidateDiagnostic(BaseModel):
    """Recall@k result for one expected official candidate."""

    model_config = ConfigDict(extra="forbid")

    expected: RecallExpectedCandidate
    rank: int | None = Field(default=None, ge=1)
    present_at_5: bool = False
    present_at_10: bool = False
    present_at_25: bool = False
    matched_label: str | None = None
    matched_source_methods: tuple[RecallSourceMethod, ...] = Field(default_factory=tuple)
    matched_debug_ref: str | None = None
    matched_input_phrase: str | None = None
    matched_batch_source: str | None = None
    status: RecallStageStatus


class CatalogRecallCaseEvidence(BaseModel):
    """Case-level recall evidence before resolver/adjudicator execution."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    diagnostics: tuple[RecallExpectedCandidateDiagnostic, ...]
    candidate_refs_top_5: tuple[str, ...] = Field(default_factory=tuple)
    candidate_refs_top_10: tuple[str, ...] = Field(default_factory=tuple)
    candidate_refs_top_25: tuple[str, ...] = Field(default_factory=tuple)
    recall_at_5: bool
    recall_at_10: bool
    recall_at_25: bool
    status_counts: dict[RecallStageStatus, int] = Field(default_factory=dict)

    @classmethod
    def from_report(
        cls,
        *,
        case_id: str,
        report: RecallReport,
        expected_candidates: tuple[RecallExpectedCandidate, ...],
        chosen_entity_refs: set[str] | None = None,
        execution_succeeded: bool | None = None,
        rendering_succeeded: bool | None = None,
        evaluator_succeeded: bool | None = None,
    ) -> "CatalogRecallCaseEvidence":
        diagnostics = diagnose_recall_report(
            report,
            expected_candidates,
            chosen_entity_refs=chosen_entity_refs,
            execution_succeeded=execution_succeeded,
            rendering_succeeded=rendering_succeeded,
            evaluator_succeeded=evaluator_succeeded,
        )
        required_diagnostics = [
            diagnostic
            for diagnostic in diagnostics
            if diagnostic.expected.required
        ]
        return cls(
            case_id=case_id,
            query=report.query,
            diagnostics=tuple(diagnostics),
            candidate_refs_top_5=_candidate_refs_top(report.candidates, 5),
            candidate_refs_top_10=_candidate_refs_top(report.candidates, 10),
            candidate_refs_top_25=_candidate_refs_top(report.candidates, 25),
            recall_at_5=all(
                diagnostic.present_at_5 for diagnostic in required_diagnostics
            ),
            recall_at_10=all(
                diagnostic.present_at_10 for diagnostic in required_diagnostics
            ),
            recall_at_25=all(
                diagnostic.present_at_25 for diagnostic in required_diagnostics
            ),
            status_counts=dict(Counter(diagnostic.status for diagnostic in diagnostics)),
        )


def diagnose_recall_report(
    report: RecallReport,
    expected_candidates: tuple[RecallExpectedCandidate, ...],
    *,
    chosen_entity_refs: set[str] | None = None,
    execution_succeeded: bool | None = None,
    rendering_succeeded: bool | None = None,
    evaluator_succeeded: bool | None = None,
) -> tuple[RecallExpectedCandidateDiagnostic, ...]:
    """Compare expected official refs against a recall report."""

    cards_by_ref = _first_card_by_entity_ref(report.candidates)
    diagnostics: list[RecallExpectedCandidateDiagnostic] = []
    for expected in expected_candidates:
        ranked = cards_by_ref.get((expected.entity_type, expected.entity_ref))
        rank = ranked[0] if ranked is not None else None
        card = ranked[1] if ranked is not None else None
        diagnostics.append(
            RecallExpectedCandidateDiagnostic(
                expected=expected,
                rank=rank,
                present_at_5=rank is not None and rank <= 5,
                present_at_10=rank is not None and rank <= 10,
                present_at_25=rank is not None and rank <= 25,
                matched_label=card.label if card else None,
                matched_source_methods=tuple(card.source_methods) if card else (),
                matched_debug_ref=card.debug_ref if card else None,
                matched_input_phrase=_matched_input_phrase(card),
                matched_batch_source=_matched_batch_source(card),
                status=_diagnostic_status(
                    expected,
                    rank=rank,
                    chosen_entity_refs=chosen_entity_refs,
                    execution_succeeded=execution_succeeded,
                    rendering_succeeded=rendering_succeeded,
                    evaluator_succeeded=evaluator_succeeded,
                ),
            )
        )
    return tuple(diagnostics)


def _matched_input_phrase(card: CandidateCard | None) -> str | None:
    if card is None:
        return None
    matched_query = card.metadata.get("matched_query")
    if isinstance(matched_query, str) and matched_query.strip():
        return matched_query.strip()
    return card.input_phrase


def _matched_batch_source(card: CandidateCard | None) -> str | None:
    if card is None:
        return None
    batch_source = card.metadata.get("batch_source")
    if isinstance(batch_source, str) and batch_source.strip():
        return batch_source.strip()
    return None


def load_catalog_recall_runset(path: Path | None = None) -> CatalogRecallRunset:
    """Load the committed M1 catalog recall runset."""

    runset_path = path or DEFAULT_CATALOG_RECALL_RUNSET_PATH
    payload = yaml.safe_load(runset_path.read_text()) or {}
    return CatalogRecallRunset.model_validate(payload)


def catalog_recall_evidence_to_markdown(
    *,
    version: str,
    evidence: list[CatalogRecallCaseEvidence],
) -> str:
    """Render recall evidence as a compact markdown table."""

    lines = [
        f"# Catalog Recall Audit ({version})",
        "",
        "| Case | @5 | @10 | @25 | Missing expected refs | Status counts |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for case in evidence:
        missing = [
            diagnostic.expected.entity_ref
            for diagnostic in case.diagnostics
            if diagnostic.expected.required and not diagnostic.present_at_25
        ]
        status_counts = ", ".join(
            f"{status}:{count}" for status, count in sorted(case.status_counts.items())
        )
        lines.append(
            "| {case_id} | {at5} | {at10} | {at25} | {missing} | {counts} |".format(
                case_id=case.case_id,
                at5=_yes_no(case.recall_at_5),
                at10=_yes_no(case.recall_at_10),
                at25=_yes_no(case.recall_at_25),
                missing=", ".join(missing) if missing else "-",
                counts=status_counts or "-",
            )
        )
    return "\n".join(lines)


def catalog_recall_evidence_to_json(
    *,
    version: str,
    evidence: list[CatalogRecallCaseEvidence],
) -> str:
    """Render recall evidence as deterministic JSON."""

    payload = {
        "version": version,
        "passed": all(case.recall_at_25 for case in evidence),
        "cases": [case.model_dump(mode="json") for case in evidence],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _first_card_by_entity_ref(
    candidates: list[CandidateCard],
) -> dict[tuple[RecallEntityType, str], tuple[int, CandidateCard]]:
    ranked: dict[tuple[RecallEntityType, str], tuple[int, CandidateCard]] = {}
    for index, card in enumerate(candidates, start=1):
        if not card.entity_ref:
            continue
        key = (card.entity_type, card.entity_ref)
        if key not in ranked:
            ranked[key] = (index, card)
    return ranked


def _candidate_refs_top(candidates: list[CandidateCard], k: int) -> tuple[str, ...]:
    return tuple(
        card.entity_ref
        for card in candidates[:k]
        if card.entity_ref is not None
    )


def _diagnostic_status(
    expected: RecallExpectedCandidate,
    *,
    rank: int | None,
    chosen_entity_refs: set[str] | None,
    execution_succeeded: bool | None,
    rendering_succeeded: bool | None,
    evaluator_succeeded: bool | None,
) -> RecallStageStatus:
    if rank is None or rank > 25:
        return "absent_candidate"
    if chosen_entity_refs is not None and expected.entity_ref not in chosen_entity_refs:
        return "present_but_not_chosen"
    if execution_succeeded is False:
        return "execution_failure"
    if rendering_succeeded is False:
        return "rendering_failure"
    if evaluator_succeeded is False:
        return "evaluator_failure"
    if (
        chosen_entity_refs is not None
        or execution_succeeded is True
        or rendering_succeeded is True
        or evaluator_succeeded is True
    ):
        return "passed"
    return "present"


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


__all__ = [
    "CatalogRecallCaseEvidence",
    "CatalogRecallRunset",
    "CatalogRecallRunsetCase",
    "RecallExpectedCandidate",
    "RecallExpectedCandidateDiagnostic",
    "RecallStageStatus",
    "catalog_recall_evidence_to_json",
    "catalog_recall_evidence_to_markdown",
    "diagnose_recall_report",
    "load_catalog_recall_runset",
]
