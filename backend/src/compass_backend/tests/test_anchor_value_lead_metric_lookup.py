"""Renderer regression for B05 / #1556 — equality-anchor lookups must name the
anchor district and its shared reviewed value in prose.

Client bug (B05, case 1175): "Which districts have the same school-year length
as Portland, ME?" routes to an ``operation="lookup"`` narrowed by an
``anchor_value`` equality filter. The renderer (``_render_metric_lookup``) showed
the matching districts and their value column but never stated WHOSE value they
match. With ``exclude_anchor=True`` (the default and the reported shape) the
anchor district is excluded from the table, so the answer floated free of "the
same as *whom*?" — the reader could not see the anchor named anywhere.

The peer-comparison renderer already opens with the anchor's own value
(``_anchor_value_lead``); this brings the equality-anchor lookup to parity. The
shared value is grounded in a real matched row (every matched row equals the
anchor's value by construction), so no execution change is needed.

Run with:
    PYTHONPATH=src uv run pytest \
        src/compass_backend/tests/test_anchor_value_lead_metric_lookup.py --tb=short
"""

from __future__ import annotations

from compass_backend.artifacts import (
    MetricLookupResult,
    MetricValueRow,
    ResultSelection,
    SelectedDistrict,
)
from compass_backend.contracts.planning import (
    AnchorValueSpec,
    FilterSpec,
    MetricSpec,
    OutputSpec,
    QueryPlan,
    SelectionSpec,
)
from compass_backend.rendering.writer import _render_result


_METRIC_NAME = "School-year length"


def _equality_anchor_plan() -> QueryPlan:
    """The B05 equality-anchor lookup shape: 'same <metric> as Portland, ME'."""
    return QueryPlan(
        operation="lookup",
        question="Which districts have the same school-year length as Portland, ME?",
        selection=SelectionSpec(scope="all_covered_districts", districts=[]),
        metrics=[MetricSpec(name=_METRIC_NAME)],
        filters=[
            FilterSpec(
                field=_METRIC_NAME,
                operator="equals",
                kind="metric_value",
                anchor_value=AnchorValueSpec(
                    district="Portland Public Schools",
                    state="ME",
                    metric=_METRIC_NAME,
                    # exclude_anchor defaults True — the reported shape, so the
                    # anchor district is NOT one of the result rows.
                ),
            )
        ],
        output=OutputSpec(row_display="all"),
    )


def _matching_rows_result() -> MetricLookupResult:
    """Two covered districts that share the anchor's value (176 days), with the
    anchor itself excluded from the rows (exclude_anchor=True)."""
    return MetricLookupResult(
        selection=ResultSelection(
            scope="all_covered_districts",
            districts=[
                SelectedDistrict(district_id=11, district_name="Alpha", state="MA"),
                SelectedDistrict(district_id=12, district_name="Beta", state="NH"),
            ],
        ),
        rows=[
            MetricValueRow(
                district_id=11,
                district_name="Alpha",
                state="MA",
                metric_id=10,
                metric_name=_METRIC_NAME,
                value=176,
                display_value="176 days",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="176 days",
                coverage_reason="answer_value",
            ),
            MetricValueRow(
                district_id=12,
                district_name="Beta",
                state="NH",
                metric_id=10,
                metric_name=_METRIC_NAME,
                value=176,
                display_value="176 days",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="176 days",
                coverage_reason="answer_value",
            ),
        ],
        total_considered=2,
        excluded_count=0,
        order_statement="Looked up School-year length for matching districts.",
    )


def test_equality_anchor_lookup_names_anchor_district_in_prose() -> None:
    """The anchor district (excluded from the rows) must still be named in prose.

    This is the B05 RED: on main the answer never names "Portland Public Schools"
    because the anchor is excluded from the table and no prose states it. The
    anchor NAME is the discriminator (its value '176 days' already appears in the
    table cells, so asserting the name proves the lead fired).
    """
    body = _render_result(_equality_anchor_plan(), _matching_rows_result())
    assert "Portland Public Schools" in body, (
        "equality-anchor lookup must name the anchor district in prose so the "
        f"reader sees 'the same as whom?'; got:\n{body}"
    )
    # The shared value is stated alongside the anchor name.
    assert "176 days" in body
    # The anchor's state qualifier is preserved (Portland ME vs OR disambiguation).
    assert "ME" in body


def test_plain_lookup_has_no_anchor_lead() -> None:
    """A lookup with no anchor_value filter is untouched — no anchor sentence."""
    plan = QueryPlan(
        operation="lookup",
        question="What is the school-year length for Alpha?",
        selection=SelectionSpec(scope="named_districts", districts=["Alpha"]),
        metrics=[MetricSpec(name=_METRIC_NAME)],
        output=OutputSpec(row_display="all"),
    )
    body = _render_result(plan, _matching_rows_result())
    assert "share" not in body.lower() or "reviewed value of" not in body.lower(), (
        "a plain lookup must not emit the equality-anchor lead sentence; "
        f"got:\n{body}"
    )
