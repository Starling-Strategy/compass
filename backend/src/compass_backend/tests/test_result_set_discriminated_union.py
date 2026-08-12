"""Tests for the ResultSet discriminated-union contract.

These tests pin the type-system invariants the discriminated-union refactor
buys us. They are the "things that would have been silent before but are
loud now" regression layer.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from compass_backend import artifacts as artifacts_package
from compass_backend.artifacts import (
    RESULT_SET_TYPES,
    CompositeRankingResult,
    MetricCountResult,
    MetricLookupResult,
    MetricRankingResult,
    MetricTrendResult,
    MetricValueRow,
    PeerComparisonResult,
    ProfileLookupResult,
    RankingRow,
    ResultSet,
)


def _ranking() -> MetricRankingResult:
    return MetricRankingResult(
        rows=[
            RankingRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=10,
                metric_name="Average starting salary",
                value=50000.0,
                display_value="$50,000",
                academic_year="2024 - 2025",
                rank=1,
                coverage_state="covered",
                coverage_display="$50,000",
                coverage_reason="answer_value",
            )
        ],
        total_considered=1,
        excluded_count=0,
        order_statement="Ranked by salary, highest to lowest.",
    )


def _lookup() -> MetricLookupResult:
    return MetricLookupResult(
        rows=[
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=10,
                metric_name="Test metric",
                value=42,
                display_value="42",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="42",
                coverage_reason="answer_value",
            )
        ],
        total_considered=1,
        excluded_count=0,
        order_statement="Looked up test metric for selected districts.",
    )


# ─── Round-trip parsing ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "factory,expected_cls",
    [
        (_ranking, MetricRankingResult),
        (_lookup, MetricLookupResult),
    ],
)
def test_result_set_round_trip_preserves_variant(factory, expected_cls) -> None:
    """JSON dump → TypeAdapter parse picks the correct variant by discriminator."""

    adapter = TypeAdapter(ResultSet)
    original = factory()
    dumped = original.model_dump()
    parsed = adapter.validate_python(dumped)
    assert isinstance(parsed, expected_cls)
    assert parsed.result_type == original.result_type
    assert len(parsed.rows) == len(original.rows)


def test_result_set_types_tuple_contains_every_variant() -> None:
    """RESULT_SET_TYPES is the runtime tuple used for isinstance dispatch."""

    assert set(RESULT_SET_TYPES) == {
        MetricRankingResult,
        MetricLookupResult,
        MetricCountResult,
        MetricTrendResult,
        ProfileLookupResult,
        PeerComparisonResult,
        # W2-M3-01 (#806): composite envelope joins the discriminated union.
        CompositeRankingResult,
    }


# ─── Negative cases: the type system should reject these ─────────────────────


def test_result_set_rejects_unknown_discriminator_value() -> None:
    """An unknown `result_type` tag fails parsing with a tagged-union error."""

    adapter = TypeAdapter(ResultSet)
    payload = {
        "result_type": "metric_made_up_kind",
        "rows": [],
        "total_considered": 0,
        "excluded_count": 0,
        "order_statement": "n/a",
    }
    with pytest.raises(ValidationError) as exc:
        adapter.validate_python(payload)
    # Pydantic v2 produces a tagged-union error mentioning the discriminator.
    assert "result_type" in str(exc.value)


def test_result_set_rejects_missing_discriminator() -> None:
    """A payload with no `result_type` fails — there is no default variant."""

    adapter = TypeAdapter(ResultSet)
    payload = {
        "rows": [],
        "total_considered": 0,
        "excluded_count": 0,
        "order_statement": "n/a",
    }
    with pytest.raises(ValidationError) as exc:
        adapter.validate_python(payload)
    assert "result_type" in str(exc.value)


def test_variant_rejects_mismatched_result_type_tag() -> None:
    """Constructing a variant with the wrong tag raises at construction."""

    with pytest.raises(ValidationError):
        # mypy / pyright would also flag this — runtime sanity check
        MetricLookupResult(
            result_type="metric_ranking",  # type: ignore[arg-type]
            rows=[],
            total_considered=0,
            excluded_count=0,
            order_statement="n/a",
        )


def test_variant_extra_fields_forbidden() -> None:
    """ConfigDict(extra='forbid') means unknown fields raise at construction."""

    with pytest.raises(ValidationError) as exc:
        MetricLookupResult(
            rows=[],
            total_considered=0,
            excluded_count=0,
            order_statement="n/a",
            ghost_field="should_fail",  # type: ignore[call-arg]
        )
    assert any(err["type"] == "extra_forbidden" for err in exc.value.errors())


def test_every_artifacts_model_forbids_extra_fields() -> None:
    """Every BaseModel defined in the artifacts package must set extra='forbid'.

    The pa-eval contract canary (`ChatResponse` extra=forbid) only catches
    drift on models that forbid extras; a single permissive leaf (#1503 B13:
    `ListCoverageSummary` shipped with frozen=True but no extra='forbid')
    lets contract drift through silently. The walk covers every module in
    the package — not just `results` but also `coverage` and `citations`,
    whose models nest inside `_ResultSetBase` — so this omission class is
    impossible anywhere on the artifact surface, present or future.
    """

    modules = [
        importlib.import_module(f"{artifacts_package.__name__}.{info.name}")
        for info in pkgutil.iter_modules(artifacts_package.__path__)
    ]
    offenders = sorted(
        f"{module.__name__.rsplit('.', 1)[-1]}.{name}"
        for module in modules
        for name, member in inspect.getmembers(module, inspect.isclass)
        if issubclass(member, BaseModel)
        and member.__module__ == module.__name__
        and member.model_config.get("extra") != "forbid"
    )
    assert offenders == [], (
        f"models in compass_backend.artifacts missing extra='forbid': {offenders}"
    )
