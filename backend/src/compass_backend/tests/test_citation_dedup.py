"""Tests for citation dedup-by-URL across an answer (W2-M5-04 / #748).

`citation_markers_for_row` is the single point where per-row sources turn
into the answer-level ``CitationRef`` list. Per #748, two rows that cite
the same URL must share one entry in ``result.citations`` and one inline
marker — even when the rows carry different ``academic_year`` /
``district_id`` / ``page_ref`` / parsed-title values. The previous
identity (title + url + page_ref + district_id + academic_year) was too
fine-grained and produced near-duplicate Sources panel entries that
Ashley flagged across the 5-8 sorting scenario testing sweep (#748).
"""

from __future__ import annotations

from compass_backend.artifacts import (
    CitationRef,
    CitationSource,
    ResultSelection,
    SelectedDistrict,
)
from compass_backend.catalog import MetricCandidate
from compass_backend.contracts import MetricSpec, QueryPlan, SelectionSpec
from compass_backend.db.rows import MetricAnswerRow
from compass_backend.execution.evidence import citation_markers_for_row
from compass_backend.execution.ranking import build_metric_ranking_result


def _row(
    *,
    district_id: int = 1,
    district_name: str = "Detroit",
    state: str = "MI",
    academic_year: str = "2024 - 2025",
    citations: list[CitationSource] | None = None,
) -> MetricAnswerRow:
    return MetricAnswerRow(
        district_id=district_id,
        district_name=district_name,
        state=state,
        metric_id=100,
        metric_name="Starting salary",
        value=80000,
        academic_year=academic_year,
        citations=citations or [],
    )


# ---------------------------------------------------------------------------
# Same URL across rows — the dominant reporter complaint


def test_same_url_different_academic_year_dedups_to_one_marker() -> None:
    """Two rows citing the same Detroit contract URL across two academic
    years must collapse to one CitationRef and one inline marker."""
    citation_refs: list = []
    by_identity: dict = {}

    row_a = _row(
        academic_year="2023 - 2024",
        citations=[
            CitationSource(
                source_name="Detroit Community School District. MI. (2023-2024). Contract.",
                source_url="https://example.org/detroit-contract.pdf",
                academic_year="2023 - 2024",
                district_id=1,
            )
        ],
    )
    row_b = _row(
        academic_year="2024 - 2025",
        citations=[
            CitationSource(
                source_name="Detroit Community School District. MI. (2024-2025). Contract.",
                source_url="https://example.org/detroit-contract.pdf",
                academic_year="2024 - 2025",
                district_id=1,
            )
        ],
    )

    markers_a = citation_markers_for_row(row_a, citation_refs, by_identity)
    markers_b = citation_markers_for_row(row_b, citation_refs, by_identity)

    assert markers_a == [1]
    assert markers_b == [1]
    assert len(citation_refs) == 1
    assert citation_refs[0].url == "https://example.org/detroit-contract.pdf"


def test_same_url_different_page_dedups_to_one_marker() -> None:
    """Same URL with different page anchors still consolidates to a single
    Sources entry. The first-seen page anchor is preserved on the merged
    entry; page-deep-linking precision is the trade-off the issue (#748)
    explicitly accepts in exchange for a clean, scannable Sources block."""
    citation_refs: list = []
    by_identity: dict = {}

    row_a = _row(
        citations=[
            CitationSource(
                source_name="Detroit. MI. (2024-2025). Contract. p. 5.",
                source_url="https://example.org/detroit-contract.pdf",
                academic_year="2024 - 2025",
                district_id=1,
            )
        ],
    )
    row_b = _row(
        citations=[
            CitationSource(
                source_name="Detroit. MI. (2024-2025). Contract. p. 12.",
                source_url="https://example.org/detroit-contract.pdf",
                academic_year="2024 - 2025",
                district_id=1,
            )
        ],
    )

    markers_a = citation_markers_for_row(row_a, citation_refs, by_identity)
    markers_b = citation_markers_for_row(row_b, citation_refs, by_identity)

    assert markers_a == [1]
    assert markers_b == [1]
    assert len(citation_refs) == 1
    # First-seen page anchor wins (deterministic).
    assert citation_refs[0].page_number == 5


def test_same_url_different_district_dedups_to_one_marker() -> None:
    """Same URL across different districts still collapses — URL is the
    single source of truth for citation identity. (Unusual in practice
    but worth locking in: an aggregate report URL referenced by two
    different district rows should appear once.)"""
    citation_refs: list = []
    by_identity: dict = {}

    row_a = _row(
        district_id=1,
        district_name="Detroit",
        citations=[
            CitationSource(
                source_name="Aggregate state report. MI. (2024-2025).",
                source_url="https://example.org/state-report.pdf",
                academic_year="2024 - 2025",
                district_id=1,
            )
        ],
    )
    row_b = _row(
        district_id=2,
        district_name="Ann Arbor",
        citations=[
            CitationSource(
                source_name="Aggregate state report. MI. (2024-2025).",
                source_url="https://example.org/state-report.pdf",
                academic_year="2024 - 2025",
                district_id=2,
            )
        ],
    )

    markers_a = citation_markers_for_row(row_a, citation_refs, by_identity)
    markers_b = citation_markers_for_row(row_b, citation_refs, by_identity)

    assert markers_a == [1]
    assert markers_b == [1]
    assert len(citation_refs) == 1


# ---------------------------------------------------------------------------
# Distinct URLs — these must NOT dedup


def test_different_urls_get_distinct_markers() -> None:
    """Negative case: two different URLs must produce two CitationRefs."""
    citation_refs: list = []
    by_identity: dict = {}

    row_a = _row(
        citations=[
            CitationSource(
                source_name="Detroit. MI. (2024-2025). Contract.",
                source_url="https://example.org/detroit-contract.pdf",
                academic_year="2024 - 2025",
                district_id=1,
            )
        ],
    )
    row_b = _row(
        district_id=2,
        district_name="Ann Arbor",
        citations=[
            CitationSource(
                source_name="Ann Arbor. MI. (2024-2025). Contract.",
                source_url="https://example.org/ann-arbor-contract.pdf",
                academic_year="2024 - 2025",
                district_id=2,
            )
        ],
    )

    markers_a = citation_markers_for_row(row_a, citation_refs, by_identity)
    markers_b = citation_markers_for_row(row_b, citation_refs, by_identity)

    assert markers_a == [1]
    assert markers_b == [2]
    assert len(citation_refs) == 2


# ---------------------------------------------------------------------------
# URL-less sources — fall back to title-based identity


def test_url_less_sources_dedup_by_title() -> None:
    """When citations have no URL (rare — archived/internal records), fall
    back to parsed-title-based identity so document-level dedup still
    applies."""
    citation_refs: list = []
    by_identity: dict = {}

    row_a = _row(
        citations=[
            CitationSource(
                source_name="Internal archive memo X-2024",
                source_url=None,
                academic_year="2024 - 2025",
                district_id=1,
            )
        ],
    )
    row_b = _row(
        academic_year="2023 - 2024",
        citations=[
            CitationSource(
                source_name="Internal archive memo X-2024",
                source_url=None,
                academic_year="2023 - 2024",
                district_id=1,
            )
        ],
    )

    markers_a = citation_markers_for_row(row_a, citation_refs, by_identity)
    markers_b = citation_markers_for_row(row_b, citation_refs, by_identity)

    assert markers_a == [1]
    assert markers_b == [1]
    assert len(citation_refs) == 1


def test_url_less_sources_with_different_titles_do_not_dedup() -> None:
    """Negative for the URL-less fallback: distinct titles → distinct refs."""
    citation_refs: list = []
    by_identity: dict = {}

    row_a = _row(
        citations=[
            CitationSource(
                source_name="Internal memo Alpha",
                source_url=None,
                district_id=1,
            )
        ],
    )
    row_b = _row(
        citations=[
            CitationSource(
                source_name="Internal memo Beta",
                source_url=None,
                district_id=1,
            )
        ],
    )

    markers_a = citation_markers_for_row(row_a, citation_refs, by_identity)
    markers_b = citation_markers_for_row(row_b, citation_refs, by_identity)

    assert markers_a == [1]
    assert markers_b == [2]
    assert len(citation_refs) == 2


# ---------------------------------------------------------------------------
# Composite-ranking cross-child dedup (W2-M5-04 closes the W2-M3-01 gap
# called out at ``rendering/writer.py:302`` — every sibling child's
# ``citations`` field references the same shared deduped list)


def test_build_metric_ranking_result_threads_shared_citation_state() -> None:
    """End-to-end: two sibling ``MetricRankingResult`` children built through
    ``build_metric_ranking_result`` with a shared citation-state pair share
    one ``CitationRef`` per URL and consistent inline markers. This is the
    contract the W2-M3-01 writer.py:302 comment delegated to W2-M5-04."""
    same_url = "https://example.org/detroit-contract.pdf"

    def _build_child(metric_id: int, year: str) -> tuple:
        metric = MetricCandidate(metric_id=metric_id, name=f"Metric-{metric_id}", answer_type="numeric")
        district = SelectedDistrict(district_id=1, district_name="Detroit", state="MI")
        plan = QueryPlan(
            question="rank detroit",
            selection=SelectionSpec(scope="named_districts", districts=["Detroit"]),
            metrics=[MetricSpec(name=f"Metric-{metric_id}")],
        )
        row = MetricAnswerRow(
            district_id=1,
            district_name="Detroit",
            state="MI",
            metric_id=metric_id,
            metric_name=f"Metric-{metric_id}",
            value="80000",
            academic_year=year,
            citations=[
                CitationSource(
                    source_name=f"Detroit. MI. ({year}). Contract.",
                    source_url=same_url,
                    academic_year=year,
                    district_id=1,
                )
            ],
        )
        return plan, metric, row, district

    shared_refs: list[CitationRef] = []
    shared_map: dict[tuple[object, ...], int] = {}

    plan_a, metric_a, row_a, dist = _build_child(metric_id=100, year="2024 - 2025")
    child_a = build_metric_ranking_result(
        plan_a,
        metric_a,
        [row_a],
        recent_rows=[],
        selection=ResultSelection(scope="named_districts", districts=[dist]),
        selected_districts=[dist],
        default_limit=10,
        reviewed_district_ids={1},
        academic_year="2024 - 2025",
        shared_citation_refs=shared_refs,
        shared_citation_marker_by_identity=shared_map,
    )

    plan_b, metric_b, row_b, _ = _build_child(metric_id=200, year="2024 - 2025")
    child_b = build_metric_ranking_result(
        plan_b,
        metric_b,
        [row_b],
        recent_rows=[],
        selection=ResultSelection(scope="named_districts", districts=[dist]),
        selected_districts=[dist],
        default_limit=10,
        reviewed_district_ids={1},
        academic_year="2024 - 2025",
        shared_citation_refs=shared_refs,
        shared_citation_marker_by_identity=shared_map,
    )

    # Only one CitationRef across both children's shared list.
    assert len(shared_refs) == 1
    # Both rows share marker [1] (shared identity map → consistent
    # marker numbering across siblings).
    assert child_a.rows[0].citation_markers == [1]
    assert child_b.rows[0].citation_markers == [1]
    # Pydantic v2 validates (copies) list fields at construction time, so
    # each child's ``citations`` is a snapshot of ``shared_refs`` at the
    # moment that child was built — not a live reference. The answer-level
    # Sources contract is enforced at the envelope: ``_execute_composite_ranking``
    # passes ``citations=list(shared_citation_refs)`` to
    # ``CompositeRankingResult(...)`` after the loop. Child citations are
    # the per-child snapshots; envelope citations are the unified list.
    assert child_a.citations == [shared_refs[0]]
    assert child_b.citations == [shared_refs[0]]


def test_shared_citation_state_dedupes_across_composite_children() -> None:
    """When two sibling composite children both cite the same URL through
    a shared citation state (the production composite path), the URL must
    appear once in the shared list and both children must see the same
    inline marker. Pydantic ``frozen=True`` does not deep-copy mutable
    container fields, so when the second child mutates the shared list
    the first child — already frozen — continues to point at the same
    (now-fuller) list."""
    citation_refs: list = []
    by_identity: dict = {}

    # Simulate two composite siblings each running their per-metric loop
    # over MetricAnswerRow lists that share a Detroit-contract URL.
    same_url = "https://example.org/detroit-contract.pdf"
    sibling_a_row = _row(
        citations=[
            CitationSource(
                source_name="Detroit. MI. (2024-2025). Contract.",
                source_url=same_url,
                academic_year="2024 - 2025",
                district_id=1,
            )
        ],
    )
    sibling_b_row = _row(
        citations=[
            CitationSource(
                source_name="Detroit. MI. (2024-2025). Contract.",
                source_url=same_url,
                academic_year="2024 - 2025",
                district_id=1,
            )
        ],
    )

    markers_a = citation_markers_for_row(sibling_a_row, citation_refs, by_identity)
    markers_b = citation_markers_for_row(sibling_b_row, citation_refs, by_identity)

    assert markers_a == [1]
    assert markers_b == [1]
    # The shared list grew by exactly one entry across both siblings.
    assert len(citation_refs) == 1
    assert citation_refs[0].url == same_url


def test_result_citation_urls_are_unique_across_multi_row_answer() -> None:
    """Renderer-facing invariant: any answer-level CitationRef list built
    by ``citation_markers_for_row`` from multiple rows must have unique
    URLs. This is the property the new ``sources_block_is_deduplicated``
    validator enforces at evaluation time."""
    citation_refs: list = []
    by_identity: dict = {}

    rows = [
        _row(
            district_id=1,
            district_name="Detroit",
            academic_year=year,
            citations=[
                CitationSource(
                    source_name=f"Detroit. MI. ({year}). Contract.",
                    source_url="https://example.org/detroit-contract.pdf",
                    academic_year=year,
                    district_id=1,
                )
            ],
        )
        for year in ("2022 - 2023", "2023 - 2024", "2024 - 2025")
    ] + [
        _row(
            district_id=2,
            district_name="Ann Arbor",
            citations=[
                CitationSource(
                    source_name="Ann Arbor. MI. (2024-2025). Contract.",
                    source_url="https://example.org/ann-arbor-contract.pdf",
                    academic_year="2024 - 2025",
                    district_id=2,
                )
            ],
        )
    ]

    all_markers: list[list[int]] = []
    for row in rows:
        all_markers.append(
            citation_markers_for_row(row, citation_refs, by_identity)
        )

    # Detroit's three years collapse into one entry; Ann Arbor is a second.
    assert len(citation_refs) == 2
    urls = [c.url for c in citation_refs]
    assert sorted(urls) == sorted(
        [
            "https://example.org/ann-arbor-contract.pdf",
            "https://example.org/detroit-contract.pdf",
        ]
    )
    assert all_markers == [[1], [1], [1], [2]]
