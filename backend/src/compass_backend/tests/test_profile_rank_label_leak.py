"""Regression: profile-field rankings must not leak the machine field_key (#1495).

A "rank districts by [profile field]" query stored the machine snake_case key
(``frpl_pct``) in ``RankingRow.sort_metric_name`` so the sort-proof validator
could match the authority's ``field_key``. The renderer then read
``sort_metric_name`` for BOTH the answer lead and the user-visible table header,
so the lead said "I ranked … by frpl_pct" and the table carried both a
``frpl_pct`` column and an ``FRPL %`` column with identical values.

The fix carries the human label on a separate ``sort_metric_label`` field; the
renderer prefers it, and the validator path keeps reading ``sort_metric_name``
(the field_key). These fixtures set the REAL field_key on ``sort_metric_name``
(not the hand-set human label that hid the bug from the old fixtures) so the
regression can't reappear invisibly.
"""

from __future__ import annotations

from compass_backend.artifacts import (
    MethodologyRef,
    MetricRankingResult,
    RankingRow,
)
from compass_backend.contracts import MetricSpec, QueryPlan, SelectionSpec
from compass_backend.rendering import render_response
from compass_backend.rendering.shared import sort_metric_name
from compass_backend.tests.test_mvp_rendering import _valid_report


def _profile_only_ranking_result() -> MetricRankingResult:
    """A "rank by FRPL %" result, as build_profile_ranking_result produces it:

    the displayed metric IS the sort field, ``metric_name`` is the human label,
    and ``sort_metric_name`` carries the machine field_key for the validator.
    """

    return MetricRankingResult(
        rows=[
            RankingRow(
                district_id=index,
                district_name=f"District {index}",
                state="CA",
                metric_id=-1002,
                metric_name="FRPL %",
                value=90.0 - index,
                display_value=f"{90 - index}%",
                academic_year="2024 - 2025",
                rank=index,
                source="profile_field",
                coverage_state="covered",
                coverage_display=f"{90 - index}%",
                coverage_reason="profile_field_value",
                sort_metric_id=-1002,
                sort_metric_name="frpl_pct",  # real machine field_key
                sort_metric_label="FRPL %",  # human label for the renderer
                sort_value=90.0 - index,
                sort_display_value=f"{90 - index}%",
                sort_academic_year="2024 - 2025",
            )
            for index in (1, 2)
        ],
        total_considered=2,
        excluded_count=0,
        order_statement="Ranked by FRPL %, highest to lowest.",
        methodology_codes=[MethodologyRef(code="profile_rank_uses_profile_field")],
    )


def _plan() -> QueryPlan:
    return QueryPlan(
        question="Rank covered districts by free and reduced-price lunch share.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="FRPL %")],
    )


def test_shared_helper_returns_human_label_not_field_key() -> None:
    result = _profile_only_ranking_result()
    assert sort_metric_name(result) == "FRPL %"


def test_profile_rank_render_shows_label_no_field_key_no_duplicate_column() -> None:
    manifest = render_response(_plan(), _profile_only_ranking_result(), _valid_report())

    assert manifest.status == "rendered"
    body = manifest.body
    # The machine key never reaches the user.
    assert "frpl_pct" not in body
    # The human label appears in the lead and the (single) table header.
    assert "FRPL %" in body
    # No duplicate column: the header row must carry "FRPL %" exactly once.
    header_line = next(line for line in body.splitlines() if line.startswith("| Rank"))
    assert header_line.count("FRPL %") == 1, header_line
