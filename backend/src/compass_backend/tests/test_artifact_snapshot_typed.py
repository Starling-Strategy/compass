"""Tests for the typed ArtifactSnapshot contract (Bucket D Item 10).

These tests pin the type-system invariants the typed-snapshot refactor
buys us. Pre-refactor, `CompassEvaluatorContext.artifact_snapshot` was a
`dict[str, Any]` — a typo'd key in a prefilter rule silently returned None
and the rule silently didn't fire. Post-refactor, every access goes
through `BaseModel` attribute lookup; a typo is an `AttributeError` at
runtime AND a static type-check error.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from compass_backend.artifacts.citations import CitationRef
from compass_backend.quality._evaluator_context import (
    ArtifactSnapshot,
    ArtifactTableRow,
    CompassEvaluatorContext,
    SelectionExpected,
)


# ─── 1. Dict coercion via pydantic dataclass ─────────────────────────────────


def test_compass_evaluator_context_coerces_dict_to_artifact_snapshot() -> None:
    """A dict-shaped artifact_snapshot kwarg coerces to ArtifactSnapshot.

    This preserves the adapter coercion path: when pydantic-evals passes
    `ctx.metadata["compass_ctx"]` as a JSON-shaped dict, the
    `_PydanticEvalsAdapter` and `_PydanticRecordEvalsAdapter` in
    `criteria.py` construct CompassEvaluatorContext(**data). The pydantic
    dataclass decorator handles the nested model coercion.
    """

    ctx = CompassEvaluatorContext(
        session_id="s",
        turn_index=0,
        artifact_snapshot={
            "table": [
                {"district_name": "Alpha", "state": "CA", "display_value": "$10"},
            ],
            "selection_expected": {
                "scope": "state",
                "states": ["CA"],
                "districts": [],
            },
        },
    )
    assert isinstance(ctx.artifact_snapshot, ArtifactSnapshot)
    assert len(ctx.artifact_snapshot.table) == 1
    assert ctx.artifact_snapshot.table[0].district_name == "Alpha"
    assert ctx.artifact_snapshot.selection_expected is not None
    assert ctx.artifact_snapshot.selection_expected.scope == "state"


# ─── 2. extra="forbid" — typo'd field is a validation error ──────────────────


def test_artifact_snapshot_rejects_unknown_field() -> None:
    """A misspelled key at the snapshot level is a ValidationError.

    Pre-refactor, `artifact_snapshot["tabel"]` (typo) silently returned None
    when read with `.get("tabel")` and the prefilter rule silently didn't
    fire. Post-refactor, the dict has to validate against the typed model
    on the way in.
    """

    with pytest.raises(ValidationError) as exc:
        ArtifactSnapshot.model_validate({"tabel": []})  # typo
    assert any(err["type"] == "extra_forbidden" for err in exc.value.errors())


def test_artifact_table_row_rejects_unknown_field() -> None:
    """A misspelled column name on a row is a ValidationError."""

    with pytest.raises(ValidationError):
        ArtifactTableRow(
            district_name="Alpha",
            display_value="$10",
            statte="CA",  # typo  # type: ignore[call-arg]
        )


def test_selection_expected_rejects_unknown_scope() -> None:
    """Literal-typed scope catches a typo'd scope name."""

    with pytest.raises(ValidationError) as exc:
        SelectionExpected(scope="all_covered")  # type: ignore[arg-type]
    assert "scope" in str(exc.value).lower()


# ─── 3. model_dump round-trips through model_validate unchanged ──────────────


def test_artifact_snapshot_round_trip() -> None:
    """`ArtifactSnapshot.model_dump()` round-trips through
    `ArtifactSnapshot.model_validate(...)` to the same content.
    """

    original = ArtifactSnapshot(
        table=[
            ArtifactTableRow(district_name="Alpha", state="CA", display_value="$10"),
            ArtifactTableRow(district_name="Bravo", display_value="$20"),
        ],
        selection_expected=SelectionExpected(
            scope="named_districts",
            districts=["Alpha", "Bravo"],
        ),
    )
    parsed = ArtifactSnapshot.model_validate(original.model_dump())
    assert parsed == original


# ─── 4. ArtifactTableRow.state is optional ───────────────────────────────────


def test_artifact_table_row_state_is_optional() -> None:
    """Rows without a state field still construct — state defaults to None."""

    row = ArtifactTableRow(district_name="Alpha", display_value="$10")
    assert row.state is None


# ─── 5. span_attributes stays loose ──────────────────────────────────────────


def test_span_attributes_remains_untyped_dict() -> None:
    """span_attributes is deliberately `dict[str, Any]` — opaque Logfire bag.

    No production code does key-by-key access; only `dict(...)` copy in
    evidence.py. Locking this with a test prevents future drift toward
    typing it (which would force every Logfire attribute to be enumerated
    in the schema).
    """

    ctx = CompassEvaluatorContext(
        session_id="s",
        turn_index=0,
        span_attributes={"any_key": "any_value", "nested": {"deeply": 1}},
    )
    # No coercion — plain dict assignment is fine.
    assert ctx.span_attributes["any_key"] == "any_value"
    assert ctx.span_attributes["nested"] == {"deeply": 1}
    # dict(...) copy still works (the evidence.py contract).
    assert dict(ctx.span_attributes) == ctx.span_attributes


# ─── 6. CitationRef nested coercion ──────────────────────────────────────────


def test_artifact_snapshot_citations_coerce_from_dict() -> None:
    """`citations` accepts CitationRef instances OR dict shapes."""

    raw_citation = {
        "marker": 1,
        "title": "Bravo District Contract, 2024-2025",
        "url": "https://example.org/bravo.pdf",
        "page_number": 2,
        "page_ref": "p. 2",
        "academic_year": "2024 - 2025",
        "document_type": "Contract",
        "district_id": 2,
    }
    snap = ArtifactSnapshot(citations=[raw_citation])  # type: ignore[list-item]
    assert len(snap.citations) == 1
    assert isinstance(snap.citations[0], CitationRef)
    assert snap.citations[0].marker == 1
