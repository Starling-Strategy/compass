"""W2-M5-03 (#747) — NCES profile rows emit citations from the governed allowlist.

Ashley's Golden29 sheet rows 7/8/9 (2026-05-08) all show the same complaint
across three back-to-back enrollment-rank queries: "Sources column was
empty (this was based on NCES data); so no way to verify." The methodology
prose already names NCES as the source ("NCES directory year: 2024"), so
Compass *knows* the source — it just doesn't emit a citation.

After W2-M5-00a (PR #864) landed the governed `compass.nces_allowlist`
with `canonical_url` per field, this PR wires that URL into the
deterministic profile-rank and profile-lookup builders so every row
carries a citation marker and the result emits one CitationRef per
distinct NCES URL (W2-M5-04's URL-dedup pattern applies — multiple rows
of the same field share one entry).

Closes the unfixed half of #745 acceptance criterion #1 ("CSV includes
the same citations the user sees") for NCES-backed rows, which #745's
PR explicitly punted to this issue.
"""

from __future__ import annotations

from compass_backend.artifacts import ResultSelection
from compass_backend.catalog import MetricCandidate, NCESFieldCandidate, ProfileFieldRankRow
from compass_backend.db.rows import MetricAnswerRow
from compass_backend.catalog.resolution import ProfileFieldValueRow
from compass_backend.contracts.planning import (
    MetricSpec,
    ProfileFieldSpec,
    QueryPlan,
    SelectionSpec,
)
from compass_backend.execution.profile import build_profile_lookup_result
from compass_backend.execution.ranking import (
    build_profile_ordered_metric_ranking_result,
    build_profile_ranking_result,
)


# ─── Fixtures ───────────────────────────────────────────────────────────────


def _rank_row(district_id: int, district_name: str, value: float, field_key: str = "enrollment") -> ProfileFieldRankRow:
    return ProfileFieldRankRow(
        district_id=district_id,
        district_name=district_name,
        state="CA",
        field_key=field_key,
        label="Enrollment" if field_key == "enrollment" else field_key.upper(),
        value=value,
        display_value=f"{int(value):,}",
        academic_year="2024 - 2025",
        vintage="NCES directory year: 2024",
    )


def _rank_plan() -> QueryPlan:
    return QueryPlan(
        operation="rank",
        question="Rank districts by enrollment, largest first.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="enrollment")],
        profile_fields=[ProfileFieldSpec(name="enrollment")],
    )


def _selection() -> ResultSelection:
    return ResultSelection(scope="all_covered_districts")


def _lookup_plan() -> QueryPlan:
    return QueryPlan(
        operation="profile_lookup",
        question="What is the FRL share for Bravo?",
        selection=SelectionSpec(scope="named_districts", districts=["Bravo"]),
        profile_fields=[ProfileFieldSpec(name="frpl_pct")],
    )


def _frpl_field() -> NCESFieldCandidate:
    return NCESFieldCandidate(
        field_key="frpl_pct",
        label="FRPL %",
        data_type="numeric",
    )


def _lookup_row(field_key: str = "frpl_pct") -> ProfileFieldValueRow:
    return ProfileFieldValueRow(
        requested_name="FRL share",
        district_id=2,
        district_name="Bravo",
        state="CA",
        field_key=field_key,
        label="FRPL %" if field_key == "frpl_pct" else field_key,
        value=42.5,
        display_value="42.5%",
        academic_year="2024 - 2025",
        source="profile_field",
        source_label="NCES district profile",
        provenance="NCES directory year: 2024",
        covered_by_compass=True,
    )


# ─── Profile ranking: enrollment-rank (Ashley sheet rows 7/8/9) ─────────────


def test_profile_ranking_emits_one_nces_citation_for_enrollment() -> None:
    """Bug from #747: 'Rank districts by enrollment' had empty Sources.

    Fix: build_profile_ranking_result reads canonical_url from the
    governed NCES allowlist (migration 060 / W2-M5-00a) and emits one
    CitationRef shared across all rows (W2-M5-04 URL-dedup pattern)."""
    rows = [
        _rank_row(1, "Alpha", 60000),
        _rank_row(2, "Bravo", 45000),
        _rank_row(3, "Charlie", 30000),
    ]
    result = build_profile_ranking_result(
        _rank_plan(),
        rows,
        selection=_selection(),
        direction="desc",
    )

    # Exactly one CitationRef across the answer (URL dedup): all three
    # enrollment rows share the same NCES ELSI page.
    assert len(result.citations) == 1
    citation = result.citations[0]
    assert citation.marker == 1
    assert citation.url == "https://nces.ed.gov/ccd/elsi/"
    assert "NCES Common Core of Data" in citation.title

    # Every row references the shared marker.
    for row in result.rows:
        assert row.citation_markers == [1], (
            f"row {row.district_name} missing NCES citation marker — #747 regression"
        )


def test_profile_ranking_populates_sort_source_url_from_allowlist() -> None:
    """Pre-W2-M5-03 the sort_source_url was always None on profile rows.
    Now it carries the canonical NCES URL so the sort-proof contract
    surfaces the source the way policy-metric rankings already do."""
    rows = [_rank_row(1, "Alpha", 60000)]
    result = build_profile_ranking_result(
        _rank_plan(), rows, selection=_selection(), direction="desc"
    )
    row = result.rows[0]
    assert row.sort_source_url == "https://nces.ed.gov/ccd/elsi/"
    assert row.sort_source_document == "NCES Common Core of Data — ELSI"


def test_profile_ordered_metric_ranking_populates_sort_source_from_allowlist() -> None:
    """Policy-answer rows ordered by NCES profile fields need sort-proof URLs."""

    result = build_profile_ordered_metric_ranking_result(
        QueryPlan(
            operation="rank",
            question="Show starting salaries ordered by enrollment.",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="BA starting salary")],
            profile_fields=[ProfileFieldSpec(name="enrollment")],
        ),
        MetricCandidate(metric_id=89, name="BA starting salary", answer_type="numeric"),
        rows=[
            MetricAnswerRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=89,
                metric_name="BA starting salary",
                value="$50,000",
                academic_year="2024 - 2025",
            )
        ],
        recent_rows=[],
        profile_rows=[_rank_row(1, "Alpha", 60000, field_key="enrollment")],
        selection=_selection(),
        direction="desc",
        reviewed_district_ids={1},
        academic_year="2024 - 2025",
    )

    row = result.rows[0]
    assert row.sort_source_url == "https://nces.ed.gov/ccd/elsi/"
    assert row.sort_source_document == "NCES Common Core of Data — ELSI"
    assert result.csv_export.rows[0]["sort_source_url"] == "https://nces.ed.gov/ccd/elsi/"


def test_profile_ranking_same_field_dedupes_to_one_citation() -> None:
    """The W2-M5-04 URL-dedup pattern applies here: two enrollment rows
    point at the same NCES ELSI page, so the answer-level Sources block
    has exactly one entry. Each row's citation_markers references that
    same marker. (The ranking builder currently takes a single
    field_key per call, so cross-field collision is structurally
    unreachable; finding (14) in the W2-M5-03 adversarial review
    covers the hypothetical scenario where two field_keys map to the
    same URL.)"""
    result = build_profile_ranking_result(
        _rank_plan(),
        [_rank_row(1, "Alpha", 60000), _rank_row(2, "Bravo", 45000)],
        selection=_selection(),
        direction="desc",
    )
    assert len(result.citations) == 1
    assert result.rows[0].citation_markers == [1]
    assert result.rows[1].citation_markers == [1]


# ─── Profile lookup: FRL share (issue's second proposed scenario) ───────────


def test_profile_lookup_emits_nces_citation_for_frpl() -> None:
    """The 'What is the FRL share for Bravo?' shape goes through
    build_profile_lookup_result. Same gap as ranking: no citation emitted
    pre-fix. Fix uses the same allowlist lookup pattern."""
    result = build_profile_lookup_result(
        _lookup_plan(),
        _frpl_field(),
        [_lookup_row(field_key="frpl_pct")],
        academic_year="2024 - 2025",
    )

    assert len(result.citations) == 1
    citation = result.citations[0]
    assert citation.url == "https://nces.ed.gov/ccd/elsi/"
    assert citation.marker == 1
    assert result.rows[0].citation_markers == [1]


def test_profile_lookup_dedupes_shared_url_across_fields() -> None:
    """#1567: a multi-field lookup must not repeat one NCES URL in Sources.

    Several NCES fields (enrollment, teachers_fte, pupil_teacher_ratio,
    number_of_schools) all resolve to the same ELSI page. The builder used
    to key citation dedup on ``field_key``, so a Philadelphia multi-field
    lookup emitted one CitationRef per field — three duplicate ELSI URLs,
    failing ``sources_block_is_deduplicated``. Dedup is now keyed on the
    citation identity (URL), so the shared page appears once and every
    sharing row references that one marker; a distinct-URL field (locale →
    EDGE) still gets its own marker.
    """
    fields = [
        NCESFieldCandidate(field_key="enrollment", label="Enrollment", data_type="numeric"),
        NCESFieldCandidate(field_key="teachers_fte", label="Teachers (FTE)", data_type="numeric"),
        NCESFieldCandidate(field_key="pupil_teacher_ratio", label="Pupil/teacher ratio", data_type="numeric"),
        NCESFieldCandidate(field_key="locale_text", label="Locale", data_type="text"),
    ]
    plan = QueryPlan(
        operation="profile_lookup",
        question="Show Philadelphia's enrollment, teachers, ratio, and locale.",
        selection=SelectionSpec(scope="named_districts", districts=["Philadelphia"]),
        profile_fields=[
            ProfileFieldSpec(name="enrollment"),
            ProfileFieldSpec(name="teachers"),
            ProfileFieldSpec(name="pupil teacher ratio"),
            ProfileFieldSpec(name="locale"),
        ],
    )

    def _row(field_key: str, value, display: str) -> ProfileFieldValueRow:
        return ProfileFieldValueRow(
            requested_name=field_key,
            district_id=42,
            district_name="Philadelphia City SD",
            state="PA",
            field_key=field_key,
            label=field_key,
            value=value,
            display_value=display,
            academic_year="2024 - 2025",
            source="profile_field",
            source_label="NCES district profile",
            provenance="NCES directory year: 2024",
            covered_by_compass=True,
        )

    rows = [
        _row("enrollment", 197000.0, "197,000"),
        _row("teachers_fte", 9000.0, "9,000"),
        _row("pupil_teacher_ratio", 21.9, "21.9"),
        _row("locale_text", "City: Large", "City: Large"),
    ]
    result = build_profile_lookup_result(
        plan, fields, rows, academic_year="2024 - 2025"
    )

    # The three ELSI fields collapse to one Sources entry; locale (EDGE) is
    # its own — two unique URLs total, no duplicates.
    urls = [c.url for c in result.citations]
    assert urls.count("https://nces.ed.gov/ccd/elsi/") == 1, urls
    assert len(urls) == len(set(urls)) == 2, urls

    marker_by_field = {
        row.field_key: row.citation_markers for row in result.rows
    }
    elsi_marker = next(
        c.marker for c in result.citations if c.url == "https://nces.ed.gov/ccd/elsi/"
    )
    for field_key in ("enrollment", "teachers_fte", "pupil_teacher_ratio"):
        assert marker_by_field[field_key] == [elsi_marker], field_key
    # locale points at the distinct EDGE marker, not the shared ELSI one.
    assert marker_by_field["locale_text"] != [elsi_marker]


def test_profile_lookup_emits_edge_url_for_locale_text() -> None:
    """Issue #747 acceptance criterion lists 'urbanicity' explicitly. The
    `locale_text` field is the NCES urbanicity surface and lives at a
    different canonical URL (EDGE) than the ELSI fields. Confirms the
    allowlist is consulted per-field, not hardcoded to a single URL."""
    field = NCESFieldCandidate(
        field_key="locale_text",
        label="Locale",
        data_type="text",
    )
    plan = QueryPlan(
        operation="profile_lookup",
        question="What is the urbanicity locale for Bravo?",
        selection=SelectionSpec(scope="named_districts", districts=["Bravo"]),
        profile_fields=[ProfileFieldSpec(name="locale")],
    )
    row = ProfileFieldValueRow(
        requested_name="locale",
        district_id=2,
        district_name="Bravo",
        state="CA",
        field_key="locale_text",
        label="Locale",
        value="Suburb: Large",
        display_value="Suburb: Large",
        academic_year="2024 - 2025",
        source="profile_field",
        source_label="NCES district profile",
        provenance="NCES directory year: 2024",
        covered_by_compass=True,
    )
    result = build_profile_lookup_result(
        plan, field, [row], academic_year="2024 - 2025"
    )
    assert len(result.citations) == 1
    assert (
        result.citations[0].url
        == "https://nces.ed.gov/programs/edge/Geographic/LocaleBoundaries"
    )
    assert "EDGE" in result.citations[0].title
    assert result.rows[0].citation_markers == [1]


def test_profile_lookup_emits_finance_url_for_total_rev_pp() -> None:
    """Different NCES fields point at different canonical URLs. Total
    revenue per pupil cites the F-33 finance survey, not ELSI. Asserts
    the allowlist is actually consulted per-field (not hardcoded to ELSI)."""
    field = NCESFieldCandidate(
        field_key="total_rev_pp",
        label="Total revenue per pupil",
        data_type="numeric",
    )
    plan = QueryPlan(
        operation="profile_lookup",
        question="What is Bravo's total revenue per pupil?",
        selection=SelectionSpec(scope="named_districts", districts=["Bravo"]),
        profile_fields=[ProfileFieldSpec(name="total revenue per pupil")],
    )
    row = ProfileFieldValueRow(
        requested_name="total revenue per pupil",
        district_id=2,
        district_name="Bravo",
        state="CA",
        field_key="total_rev_pp",
        label="Total revenue per pupil",
        value=15000.0,
        display_value="$15,000",
        academic_year="2024 - 2025",
        source="profile_field",
        source_label="NCES district profile",
        provenance="NCES directory year: 2024",
        covered_by_compass=True,
    )
    result = build_profile_lookup_result(
        plan, field, [row], academic_year="2024 - 2025"
    )
    assert len(result.citations) == 1
    assert result.citations[0].url == "https://nces.ed.gov/ccd/f33agency.asp"
    assert "F-33" in result.citations[0].title


# ─── Whole-answer invariant ──────────────────────────────────────────────────


def test_no_profile_row_lacks_citation_marker() -> None:
    """Parameterised invariant: every profile-backed row (ranking or
    lookup) must carry at least one citation marker, and that marker must
    reference an entry in result.citations. This is the property the new
    profile_rows_carry_nces_citation validator enforces at eval time."""
    rank_result = build_profile_ranking_result(
        _rank_plan(),
        [_rank_row(1, "Alpha", 60000)],
        selection=_selection(),
        direction="desc",
    )
    lookup_result = build_profile_lookup_result(
        _lookup_plan(),
        _frpl_field(),
        [_lookup_row()],
        academic_year="2024 - 2025",
    )

    for result, label in [(rank_result, "ranking"), (lookup_result, "lookup")]:
        valid_markers = {c.marker for c in result.citations}
        for row in result.rows:
            assert row.citation_markers, (
                f"{label} row {row.district_name} has empty citation_markers"
            )
            for marker in row.citation_markers:
                assert marker in valid_markers, (
                    f"{label} row marker {marker} not in result.citations"
                )
