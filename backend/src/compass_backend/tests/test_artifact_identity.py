"""Tests for `compute_artifact_id` — the cross-surface identity stamp.

The hash is the foundation of the surface-consistency check: when the
renderer writes `artifact_id` onto `manifest.metadata` and the SSE writer
writes it into the data payload, the validator re-computes it from the
canonical ResultSet and asserts each surface matches. These tests lock
in the invariants the surface_consistency validator depends on:

1. Same content → same hash (stable across processes).
2. Different content → different hash (sensitive to every field that
   affects a rendered surface).
3. Length + character set are deterministic (16 hex chars).
"""

from __future__ import annotations

from compass_backend.artifacts.citations import CitationRef
from compass_backend.artifacts.identity import compute_artifact_id
from compass_backend.artifacts.results import (
    MetricRankingResult,
    RankingRow,
)


def _row(
    *,
    district_id: int = 1,
    district_name: str = "Alpha ISD",
    state: str | None = "TX",
    display_value: str = "80000",
    rank: int = 1,
    citation_markers: list[int] | None = None,
) -> RankingRow:
    return RankingRow(
        district_id=district_id,
        district_name=district_name,
        state=state,
        metric_id=100,
        metric_name="Starting salary",
        value=80000,
        display_value=display_value,
        academic_year="2024 - 2025",
        rank=rank,
        citation_markers=citation_markers or [],
    )


def _result(
    *,
    rows: list[RankingRow] | None = None,
    citations: list[CitationRef] | None = None,
) -> MetricRankingResult:
    return MetricRankingResult(
        rows=rows or [_row()],
        citations=citations or [],
        total_considered=1,
        excluded_count=0,
        order_statement="Sorted by Starting salary descending.",
    )


def test_artifact_id_is_16_hex_chars() -> None:
    """The id width is part of the contract — log lines and span attrs
    assume a small, fixed-width stamp."""
    artifact_id = compute_artifact_id(_result())

    assert len(artifact_id) == 16
    assert all(c in "0123456789abcdef" for c in artifact_id)


def test_artifact_id_stable_across_constructions() -> None:
    """Two identical ResultSets produce the same id.

    Doctrine: identity follows content, not object identity. If we
    construct two separate `MetricRankingResult` instances with the same
    fields, the hash should match — that's what makes the stamp useful
    as a cross-surface comparator.
    """
    a = _result()
    b = _result()

    assert compute_artifact_id(a) == compute_artifact_id(b)


def test_artifact_id_sensitive_to_row_value_change() -> None:
    """Changing `display_value` produces a different id.

    The most common drift scenario: a renderer's escape rule changes,
    the displayed cell mutates, but the stamp on `manifest.metadata`
    was computed from the original ResultSet. Without this sensitivity,
    the validator would silently pass on real drift.
    """
    base = _result()
    drifted = _result(rows=[_row(display_value="80001")])

    assert compute_artifact_id(base) != compute_artifact_id(drifted)


def test_artifact_id_sensitive_to_row_count_change() -> None:
    """Adding a row changes the id."""
    one_row = _result(rows=[_row(rank=1)])
    two_rows = _result(
        rows=[_row(district_id=1, district_name="Alpha", rank=1),
              _row(district_id=2, district_name="Beta", rank=2)]
    )

    assert compute_artifact_id(one_row) != compute_artifact_id(two_rows)


def test_artifact_id_sensitive_to_citation_change() -> None:
    """Changing the citation set changes the id.

    Citations are part of the surface (Sources panel + markdown markers).
    If the citation set changes between the canonical artifact and what
    was emitted, that's a real surface drift the validator should catch.
    """
    base = _result()
    with_citation = _result(
        citations=[
            CitationRef(marker=1, title="Alpha ISD salary schedule", url=None)
        ]
    )

    assert compute_artifact_id(base) != compute_artifact_id(with_citation)


def test_artifact_id_insensitive_to_row_construction_order_kwargs() -> None:
    """The hash is computed from `model_dump(mode='json')` with sort_keys=True,
    so the order in which the user passes keyword args at construction does
    not affect the result. Locks in the cross-process stability claim."""
    row_a = RankingRow(
        district_id=1,
        district_name="Alpha ISD",
        state="TX",
        metric_id=100,
        metric_name="Starting salary",
        display_value="80000",
        academic_year="2024 - 2025",
        rank=1,
        value=80000,
    )
    row_b = RankingRow(
        # Same fields, different kwarg order.
        rank=1,
        value=80000,
        academic_year="2024 - 2025",
        display_value="80000",
        metric_name="Starting salary",
        metric_id=100,
        state="TX",
        district_name="Alpha ISD",
        district_id=1,
    )

    a = _result(rows=[row_a])
    b = _result(rows=[row_b])

    assert compute_artifact_id(a) == compute_artifact_id(b)


def test_artifact_id_sensitive_to_state_change() -> None:
    """State is part of every rendered surface (table column, CSV row);
    different state must produce a different id."""
    in_tx = _result(rows=[_row(state="TX")])
    in_ny = _result(rows=[_row(state="NY")])

    assert compute_artifact_id(in_tx) != compute_artifact_id(in_ny)
