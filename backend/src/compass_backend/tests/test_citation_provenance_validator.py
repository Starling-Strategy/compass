"""#1717 — ``citation_provenance_matches_row`` validator tests.

Eval was previously blind to citation *correctness*: a rendered row could cite
a source document that belongs to an entirely different district and every
deterministic citation check (format, dedup, titles, NCES presence) would still
pass. The anchor failure shape comes from case 365 ("Research Integration and
Exemplary Policies"): California rows whose Sources point at Hawaii / Colorado
documents — a provenance mismatch no existing criterion caught.

This validator compares each rendered row's ``district_id`` to the
``district_id`` of every citation the row references (via ``citation_markers``).
A mismatch is a FAIL; rows without a citation, citations with no source district,
or rows with no district are pass-through (precision-first — see the validator
docstring). Link-liveness / 404 checking is deliberately OUT OF SCOPE for an
offline verdict (a verdict cannot fetch URLs).

Run with:
    PYTHONPATH=src uv run pytest \
        src/compass_backend/tests/test_citation_provenance_validator.py --tb=short
"""

from __future__ import annotations

import asyncio

from compass_backend.artifacts import (
    CitationRef,
    MetricRankingResult,
    RankingRow,
    ResultSelection,
)
from compass_backend.quality._evaluator_context import CompassEvaluatorContext
from compass_backend.quality.validators import registry as _registry  # populate
from compass_backend.tests.conftest import make_evaluator_context


def _ca_ranking(
    *,
    row_district_ids: tuple[int, int] = (10, 11),
    citation_district_ids: tuple[int | None, int | None] = (10, 11),
) -> MetricRankingResult:
    """Two California rows, each citing one source document.

    ``row_district_ids`` are the rendered rows' own districts; the matching
    ``citation_district_ids`` are the SOURCE-document districts the citations
    point at. When they line up the answer is honest; when row 0's citation
    points at a different district (e.g. Hawaii) the answer is citing the wrong
    state's document — the case-365 mismatch shape.
    """

    return MetricRankingResult(
        selection=ResultSelection(scope="state", states=["CA"]),
        rows=[
            RankingRow(
                district_id=row_district_ids[0],
                district_name="Los Angeles Unified",
                state="CA",
                metric_id=42,
                metric_name="Strike legality",
                value="Prohibited",
                display_value="Prohibited",
                academic_year="2024 - 2025",
                rank=1,
                citation_markers=[1],
                coverage_state="covered",
            ),
            RankingRow(
                district_id=row_district_ids[1],
                district_name="San Diego Unified",
                state="CA",
                metric_id=42,
                metric_name="Strike legality",
                value="Prohibited",
                display_value="Prohibited",
                academic_year="2024 - 2025",
                rank=2,
                citation_markers=[2],
                coverage_state="covered",
            ),
        ],
        citations=[
            CitationRef(
                marker=1,
                title="Los Angeles Unified. CA. Board Policy",
                url="https://example.org/la.pdf",
                district_id=citation_district_ids[0],
            ),
            CitationRef(
                marker=2,
                title="San Diego Unified. CA. Board Policy",
                url="https://example.org/sd.pdf",
                district_id=citation_district_ids[1],
            ),
        ],
        total_considered=2,
        excluded_count=0,
        order_statement="Strike legality for California districts.",
    )


def _ctx(result) -> CompassEvaluatorContext:
    return make_evaluator_context(
        session_id="sess-prov",
        turn_index=0,
        answer_text="placeholder",
        route="execute",
        result=result,
    )


def _run(validator, ctx, payload=None):
    return asyncio.run(validator(ctx, payload or {}))


def test_validator_is_registered() -> None:
    assert _registry.lookup("citation_provenance_matches_row") is not None
    assert "citation_provenance_matches_row" in _registry.known_validators()


# ── KNOWN-GOOD: each row cites its own district → PASS ────────────────────────


def test_matching_provenance_passes() -> None:
    """The honest case: each CA row's citation points at that same CA district."""
    validator = _registry.lookup("citation_provenance_matches_row")
    assert validator is not None
    outcome = _run(validator, _ctx(_ca_ranking()))
    assert outcome.outcome == "pass", outcome.reason
    assert outcome.evidence["rows_checked"] == 2
    assert outcome.evidence["mismatch_count"] == 0


# ── KNOWN-BAD: a CA row cites a Hawaii document → FAIL ────────────────────────


def test_mismatched_provenance_fails() -> None:
    """Anchor case-365 shape: row 0 is a California district but its citation's
    source document belongs to district 99 (Hawaii). A genuinely wrong input
    MUST fail — this is the provenance gap the criterion closes."""
    validator = _registry.lookup("citation_provenance_matches_row")
    assert validator is not None
    # Row 0 (LA Unified, district_id=10) cites a marker whose source doc is
    # district 99 — a Hawaii document attached to a California row.
    result = _ca_ranking(citation_district_ids=(99, 11))
    outcome = _run(validator, _ctx(result))
    assert outcome.outcome == "fail", outcome.reason
    assert outcome.evidence["mismatch_count"] == 1
    # The offending row + marker + both district ids surface in evidence.
    first = outcome.evidence["mismatches"][0]
    assert first["row_district_id"] == 10
    assert first["citation_district_id"] == 99
    assert first["marker"] == 1
    assert first["district_name"] == "Los Angeles Unified"


def test_mismatch_on_second_of_two_markers_fails() -> None:
    """A row may carry several markers; a mismatch on ANY of them must fail.
    Row 0 (district 10) cites markers [1, 3]: marker 1 matches its own district
    but marker 3's source document is a THIRD district (99) that no other row
    references — so it is not a deduped shared marker, it is a genuine
    wrong-district citation."""
    validator = _registry.lookup("citation_provenance_matches_row")
    assert validator is not None
    result = _ca_ranking()
    # Add a third citation whose source document belongs to district 99, and
    # have row 0 (district 10) reference it alongside its own correct marker.
    object.__setattr__(
        result,
        "citations",
        list(result.citations)
        + [
            CitationRef(
                marker=3,
                title="Hawaii Department of Education memo",
                url="https://hawaii.gov/memo.pdf",
                district_id=99,
            )
        ],
    )
    object.__setattr__(result.rows[0], "citation_markers", [1, 3])
    outcome = _run(validator, _ctx(result))
    assert outcome.outcome == "fail", outcome.reason
    # marker 3 -> district 99, but the row is district 10.
    markers = {m["marker"] for m in outcome.evidence["mismatches"]}
    assert 3 in markers


# ── PASS-THROUGH cases (must NOT false-fire) ──────────────────────────────────


def test_rows_without_citations_pass() -> None:
    """Rows lacking citation markers have no provenance to verify — pass."""
    validator = _registry.lookup("citation_provenance_matches_row")
    assert validator is not None
    result = _ca_ranking()
    object.__setattr__(result.rows[0], "citation_markers", [])
    object.__setattr__(result.rows[1], "citation_markers", [])
    outcome = _run(validator, _ctx(result))
    assert outcome.outcome == "pass", outcome.reason
    assert outcome.evidence["rows_checked"] == 0


def test_citation_without_source_district_passes() -> None:
    """When a citation carries no source ``district_id`` (None), provenance
    cannot be determined — the validator must pass through rather than
    false-fail. This is the single highest-risk false-fire path: most
    citations across the corpus carry a None source district."""
    validator = _registry.lookup("citation_provenance_matches_row")
    assert validator is not None
    result = _ca_ranking(citation_district_ids=(None, None))
    outcome = _run(validator, _ctx(result))
    assert outcome.outcome == "pass", outcome.reason
    # Both rows carry a citation, but neither citation names a source district,
    # so provenance is undeterminable for both — zero checkable rows, zero
    # mismatches. (A row only counts as "checked" once a marker resolves to a
    # citation with a known source district.)
    assert outcome.evidence["rows_checked"] == 0
    assert outcome.evidence["mismatch_count"] == 0


def test_row_without_district_id_passes() -> None:
    """A row with no ``district_id`` (e.g. an aggregate/count row) cannot be
    provenance-checked — pass through."""
    validator = _registry.lookup("citation_provenance_matches_row")
    assert validator is not None
    result = _ca_ranking()
    # RankingRow.district_id is required-int, so simulate a None-district row by
    # swapping in a marker that points to a different district while the row
    # carries no resolvable district — emulated by clearing the row id.
    object.__setattr__(result.rows[0], "district_id", None)
    outcome = _run(validator, _ctx(result))
    assert outcome.outcome == "pass", outcome.reason
    # Only the second row (id=11, citation 11) was checkable; it matched.
    assert outcome.evidence["mismatch_count"] == 0


def test_marker_not_in_citations_passes() -> None:
    """A row marker that does not resolve to any citation cannot be checked for
    provenance (that is the ``citation_format`` validator's job) — pass
    through here rather than double-failing the same defect."""
    validator = _registry.lookup("citation_provenance_matches_row")
    assert validator is not None
    result = _ca_ranking()
    object.__setattr__(result.rows[0], "citation_markers", [777])
    outcome = _run(validator, _ctx(result))
    assert outcome.outcome == "pass", outcome.reason
    assert outcome.evidence["mismatch_count"] == 0


def test_shared_deduped_citation_across_districts_passes() -> None:
    """Compass dedups citations by URL: when two districts cite the SAME
    document the answer keeps one CitationRef with one shared marker, and that
    surviving ref carries only the first-seen row's district_id (see
    execution/evidence.py). A naive per-row comparison would false-fire on the
    second district. The shared-marker guard must skip it -> PASS.

    This is the real-world false-fire that synthetic 1:1 row:citation tests
    hide; it would otherwise pollute every sweep with shared state-law /
    county-wide documents."""
    validator = _registry.lookup("citation_provenance_matches_row")
    assert validator is not None
    result = MetricRankingResult(
        selection=ResultSelection(scope="state", states=["CA"]),
        rows=[
            RankingRow(
                district_id=10,
                district_name="Los Angeles Unified",
                state="CA",
                metric_id=42,
                metric_name="Strike legality",
                value="Prohibited",
                display_value="Prohibited",
                academic_year="2024 - 2025",
                rank=1,
                citation_markers=[1],
                coverage_state="covered",
            ),
            RankingRow(
                district_id=11,
                district_name="San Diego Unified",
                state="CA",
                metric_id=42,
                metric_name="Strike legality",
                value="Prohibited",
                display_value="Prohibited",
                academic_year="2024 - 2025",
                rank=2,
                citation_markers=[1],  # same shared document
                coverage_state="covered",
            ),
        ],
        # One deduped CitationRef; carries the FIRST row's district_id (10).
        citations=[
            CitationRef(
                marker=1,
                title="California State Law on Teacher Strikes",
                url="https://ca.gov/strike-law.pdf",
                district_id=10,
            )
        ],
        total_considered=2,
        excluded_count=0,
        order_statement="Strike legality for California districts.",
    )
    outcome = _run(validator, _ctx(result))
    assert outcome.outcome == "pass", outcome.reason
    # The shared marker was excluded, so no row was provenance-checkable.
    assert outcome.evidence["mismatch_count"] == 0


def test_shared_WRONG_doc_currently_passes_deferred_to_followup() -> None:
    """KNOWN LIMITATION (recall hole), pinned deliberately.

    The shared-marker guard excludes EVERY marker referenced by >1 district to
    kill the dedup false-fire. The trade-off: if a single GENUINELY-WRONG
    document is fanned out across several districts' rows (one bad Hawaii doc
    attached to a batch of CA rows), provenance is "ambiguous" by the guard's
    rule and the validator returns PASS, not FAIL — even though it is a real
    wrong-attachment bug. Distinguishing shared-legit from shared-wrong is the
    deferred #1717 MULTI-TABLE ATTRIBUTION follow-up (explicitly out of scope
    here). This test pins the current behaviour so the limitation is not
    "fixed" by accident; flip it to FAIL when that follow-up lands."""
    validator = _registry.lookup("citation_provenance_matches_row")
    assert validator is not None
    # Two CA districts (10, 11) both cite ONE shared document whose source is
    # district 99 (Hawaii) — wrong for BOTH rows.
    result = _ca_ranking()
    object.__setattr__(result.rows[0], "citation_markers", [1])
    object.__setattr__(result.rows[1], "citation_markers", [1])
    object.__setattr__(
        result,
        "citations",
        [
            CitationRef(
                marker=1,
                title="Hawaii Department of Education memo",
                url="https://hawaii.gov/memo.pdf",
                district_id=99,
            )
        ],
    )
    outcome = _run(validator, _ctx(result))
    # Current (precision-first) behaviour: marker 1 is shared across districts
    # 10 and 11, so it is excluded and the wrong-doc fan-out is NOT caught.
    assert outcome.outcome == "pass", outcome.reason
    assert outcome.evidence["mismatch_count"] == 0


def test_no_result_is_not_applicable() -> None:
    """No ResultSet and a non-execute route → not applicable, never a hard
    failure (mirrors the other result-bearing validators)."""
    validator = _registry.lookup("citation_provenance_matches_row")
    assert validator is not None
    ctx = make_evaluator_context(
        session_id="sess-prov",
        turn_index=0,
        answer_text="placeholder",
        route="clarify",
        result=None,
    )
    outcome = _run(validator, ctx)
    assert outcome.outcome == "error"
    assert "not applicable" in outcome.reason
