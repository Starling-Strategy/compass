"""Brief-building helpers for the guarded answer layer."""

from __future__ import annotations

from compass_backend.answer_layer.briefs import (
    build_answer_brief,
    canonical_caveat_fragments,
    grounded_result_facts,
    immutable_markdown_blocks,
    numeric_tokens,
    source_markers,
)
from compass_backend.answer_layer.validation import validate_answer_draft
from compass_backend.artifacts import (
    CoverageFrame,
    MetricLookupResult,
    MetricValueRow,
    ResultSelection,
    SelectedDistrict,
)
from compass_backend.contracts.answer_layer import AdjacentMetric, AnswerDraft
from compass_backend.contracts.rendering import ResponseManifest


BODY = """Denver's starting salary is $55,000 for 2024-2025. [1]

This uses currently reviewed data and may not include every schedule lane.

| District | State | Starting salary | Sources |
| --- | --- | ---: | --- |
| Denver Public Schools | CO | $55,000 | [1] |

Sources

- [1] Denver collective bargaining agreement
"""


def test_immutable_markdown_blocks_extracts_tables_and_sources_blocks() -> None:
    blocks = immutable_markdown_blocks(BODY)

    assert [block.kind for block in blocks] == ["table", "sources"]
    assert blocks[0].text.startswith("| District | State |")
    assert "| Denver Public Schools | CO | $55,000 | [1] |" in blocks[0].text
    assert blocks[1].text == "Sources\n\n- [1] Denver collective bargaining agreement"


def test_immutable_markdown_blocks_seals_h4_section_headings() -> None:
    """#1228: each '#### ' secondary-section heading is sealed as an immutable
    block (kind='section_heading') so a stylist that drops it is rejected and
    the answer-first deterministic body ships instead. The bullet CONTENT under
    a heading is NOT sealed — only the heading line."""

    body = """Of the 133 districts you asked about, 77 have current reviewed data.

| District | Value |
| --- | --- |
| Aldine ISD | $64,000 |

#### Districts without a current reviewed value
NCTQ last reviewed Albuquerque Public Schools in 2022 - 2023; the value then was $50,000.

#### Methodology
- Bachelor's-lane starting salary.
"""
    blocks = immutable_markdown_blocks(body)
    kinds = [block.kind for block in blocks]
    assert kinds == ["table", "section_heading", "section_heading"]
    headings = [block.text for block in blocks if block.kind == "section_heading"]
    assert headings == [
        "#### Districts without a current reviewed value",
        "#### Methodology",
    ]
    # The per-district sentence (bullet content) is NOT sealed — only the heading.
    assert all("Albuquerque" not in block.text for block in blocks)


def test_numeric_tokens_ignores_source_marker_numbers() -> None:
    assert numeric_tokens(BODY) == ("$55,000", "2024", "2025")


def test_source_markers_extracts_numeric_and_policy_guidance_markers() -> None:
    body = "See this policy stance [stance:teacher-pay] and source [12]."

    assert source_markers(body) == ("[stance:teacher-pay]", "[12]")


def test_canonical_caveat_fragments_extracts_coverage_lines() -> None:
    body = """Compass found partial data.

Data availability: Of the 5 districts you asked about, 2 have current reviewed data; 3 haven't been reviewed for 2024 - 2025 yet.

| District | Value |
| --- | --- |
| Alpha | Not applicable |

Sources

- [1] Not applicable source title should not become a caveat
"""

    assert canonical_caveat_fragments(body) == (
        "Data availability: Of the 5 districts you asked about, 2 have current reviewed data; 3 haven't been reviewed for 2024 - 2025 yet.",
    )


def test_canonical_caveat_fragments_seals_1514_coverage_sentences() -> None:
    """#1514: the canonical coverage narrative — the D9 district-counting
    lead, the D7 stale sentence, the rule-2 not-reviewed sentence, and the D6
    out-of-Pathfinder sentence — must all be sealed as caveat fragments so
    the voice pass cannot drop them."""

    body = """Of the 3 districts you asked about, 1 has current reviewed data; 1 hasn't been reviewed for 2024 - 2025 yet.

NCTQ last reviewed Bravo for starting salary in 2023 - 2024; the value then was $60,000.

NCTQ hasn't reviewed Charlie for starting salary in 2024 - 2025 yet.

Ghost District, CA is not in the District Policy Pathfinder.

| District | Value |
| --- | --- |
| Alpha | $52,000 |
"""

    assert canonical_caveat_fragments(body) == (
        "Of the 3 districts you asked about, 1 has current reviewed data; 1 hasn't been reviewed for 2024 - 2025 yet.",
        "NCTQ last reviewed Bravo for starting salary in 2023 - 2024; the value then was $60,000.",
        "NCTQ hasn't reviewed Charlie for starting salary in 2024 - 2025 yet.",
        "Ghost District, CA is not in the District Policy Pathfinder.",
    )


def test_canonical_caveat_fragments_seals_match_denominator_prevalence_lead() -> None:
    """#1337 / FILT-88: the match-denominator prevalence lead must survive the
    voice pass — on the filtered-list path AND the count path (whose lead shares
    the 'match your criteria' phrasing and was previously unsealed, cf. the
    threshold-transparency cases 1030/1142)."""

    body = """Of 100 districts with a current value for Total contracted workdays per academic year, 42 (42.0%) match your criteria.

NCTQ hasn't reviewed 26 districts in this selection for 2024 - 2025 yet.

| District | State | Value |
| --- | --- | --- |
| Alpha | MD | 191 |
"""

    fragments = canonical_caveat_fragments(body)
    assert (
        "Of 100 districts with a current value for Total contracted workdays "
        "per academic year, 42 (42.0%) match your criteria." in fragments
    )
    assert (
        "NCTQ hasn't reviewed 26 districts in this selection for "
        "2024 - 2025 yet." in fragments
    )


def test_canonical_caveat_fragments_seals_count_path_match_lead() -> None:
    """The count path's match-denominator sentence is now sealed by the same
    marker — the sibling threshold-transparency fix rides along (#1337)."""

    body = """Of 100 covered districts with data, 42 (42.0%) match your criteria.

| Metric | Count |
| --- | --- |
| Workdays | 42 |
"""

    assert (
        "Of 100 covered districts with data, 42 (42.0%) match your criteria."
        in canonical_caveat_fragments(body)
    )


def test_build_answer_brief_from_manifest_seals_rendered_body_metadata() -> None:
    manifest = ResponseManifest(
        body=BODY,
        status="rendered",
        result_type="metric_lookup",
        validation_valid=True,
        warnings=["partial_coverage"],
        metadata={
            "question": "What is Denver's salary schedule?",
            "artifact_id": "abc123",
            "catalog_resolution": {"internal": "do not send"},
            "catalog_recall": {"internal": "do not send"},
        },
    )

    brief = build_answer_brief(
        manifest,
        caveat_fragments=("currently reviewed data",),
        allowed_nctq_context=("NCTQ has curated compensation research.",),
    )

    assert brief.user_question == "What is Denver's salary schedule?"
    assert brief.result_type == "metric_lookup"
    assert brief.deterministic_body == BODY
    assert brief.manifest_metadata["artifact_id"] == "abc123"
    assert "catalog_resolution" not in brief.manifest_metadata
    assert "catalog_recall" not in brief.manifest_metadata
    assert brief.immutable_blocks[0].kind == "table"
    assert brief.numeric_tokens == ("$55,000", "2024", "2025")
    assert brief.source_markers == ("[1]",)
    assert brief.caveat_fragments == ("currently reviewed data",)


def test_build_answer_brief_auto_seals_coverage_caveats() -> None:
    body = """Compass found partial data.

Data availability: Of the 5 districts you asked about, 2 have current reviewed data; 3 haven't been reviewed for 2024 - 2025 yet.

| District | Value |
| --- | --- |
| Alpha | Yes |
"""
    manifest = ResponseManifest(
        body=body,
        status="rendered",
        result_type="metric_lookup",
        validation_valid=True,
    )

    brief = build_answer_brief(manifest)

    assert brief.caveat_fragments == (
        "Data availability: Of the 5 districts you asked about, 2 have current reviewed data; 3 haven't been reviewed for 2024 - 2025 yet.",
    )


def test_answer_brief_propagates_adjacent_metrics_from_manifest_metadata():
    manifest = ResponseManifest(
        body="Vermont's first-year teacher base salary with a bachelor's degree is $42,500.",
        status="rendered",
        result_type="metric_lookup",
        validation_valid=True,
        metadata={
            "question": "teacher pay in vermont",
            "adjacent_metrics": [
                {"metric_id": 11, "label": "first-year teacher base salary, master's"},
                {"metric_id": 12, "label": "max teacher base salary, new-hire schedule"},
            ],
        },
    )
    brief = build_answer_brief(manifest)
    assert brief.adjacent_metrics == (
        AdjacentMetric(label="first-year teacher base salary, master's"),
        AdjacentMetric(label="max teacher base salary, new-hire schedule"),
    )


def test_answer_brief_omits_adjacent_metrics_when_none_in_metadata():
    manifest = ResponseManifest(
        body="A clean answer with no adjacents.",
        status="rendered",
        result_type="metric_lookup",
        validation_valid=True,
        metadata={"question": "Q"},
    )
    brief = build_answer_brief(manifest)
    assert brief.adjacent_metrics == ()


def test_answer_brief_surfaces_attached_chart_and_csv_from_manifest_metadata():
    # WS-2 (#1242): the chart/CSV ship beside the text, so the markdown body
    # the stylist sees never contains them. The brief must tell the stylist they
    # ARE attached, so it never claims "Compass can't generate a chart".
    manifest = ResponseManifest(
        body="Ranked covered districts by starting salary.",
        status="rendered",
        result_type="metric_ranking",
        validation_valid=True,
        metadata={"question": "rank districts", "has_chart": True, "has_csv_export": True},
    )
    brief = build_answer_brief(manifest)
    assert brief.attached_artifacts == ("chart", "csv_export")


def test_answer_brief_surfaces_only_csv_when_no_chart():
    manifest = ResponseManifest(
        body="Looked up starting salary for one district.",
        status="rendered",
        result_type="metric_lookup",
        validation_valid=True,
        metadata={"question": "Q", "has_chart": False, "has_csv_export": True},
    )
    brief = build_answer_brief(manifest)
    assert brief.attached_artifacts == ("csv_export",)


def test_answer_brief_omits_attached_artifacts_when_metadata_absent():
    manifest = ResponseManifest(
        body="A clean answer with no artifacts.",
        status="rendered",
        result_type="metric_lookup",
        validation_valid=True,
        metadata={"question": "Q"},
    )
    brief = build_answer_brief(manifest)
    assert brief.attached_artifacts == ()


def test_answer_brief_has_no_adjacent_metrics_when_metadata_key_is_empty_list():
    """Negative case: explicit empty adjacent_metrics list (from select_one
    flowing through the same manifest channel) must produce no footer."""
    manifest = ResponseManifest(
        body="The answer.",
        status="rendered",
        result_type="metric_lookup",
        validation_valid=True,
        metadata={"question": "single-metric prompt", "adjacent_metrics": []},
    )
    brief = build_answer_brief(manifest)
    assert brief.adjacent_metrics == ()


def test_answer_brief_skips_malformed_adjacent_metric_entries():
    """Defensive: entries missing label, with empty label, or non-dict shape
    are dropped silently — they shouldn't crash the brief builder or surface
    junk to the stylist."""
    manifest = ResponseManifest(
        body="Answer.",
        status="rendered",
        result_type="metric_lookup",
        validation_valid=True,
        metadata={
            "question": "Q",
            "adjacent_metrics": [
                {"metric_id": 11, "label": "valid label"},
                {"metric_id": 12},  # missing label
                {"metric_id": 13, "label": ""},  # empty label
                "not a dict",  # malformed shape
                {"metric_id": 14, "label": 99},  # non-string label
            ],
        },
    )
    brief = build_answer_brief(manifest)
    assert brief.adjacent_metrics == (AdjacentMetric(label="valid label"),)


# ─── #1759: grounded ResultSet values widen the seal allowlist ──────────────


# B05-shape result: the anchor district (Portland Public Schools, OR) carries
# its own contracted-days value (176) on a real row, but the rendered narrative
# body shows only the *matching* districts in its table and never states the
# anchor's own figure. So "176" is grounded in the ResultSet yet absent from
# manifest.body — exactly the gap that made the seal reject the richer draft.
_ANCHOR_GAP_BODY = """These districts have a contracted school year close to Portland Public Schools (OR).

Data availability: Of the 97 districts you asked about, 4 match your criteria.

| District | State | Contracted days | Sources |
| --- | --- | ---: | --- |
| Alpha School District | CA | 180 | [1] |
| Bravo Unified | TX | 182 | [1] |

Sources

- [1] District calendar
"""


def _anchor_gap_result() -> MetricLookupResult:
    """A lookup whose anchor row's value (176) is NOT in the rendered body.

    The two matching districts (Alpha/Bravo) appear in the body table; the
    anchor (Portland Public Schools, OR) is a real selected row carrying the
    grounded 176 value, but the narrative never prints the anchor's own figure
    — the B05 'never states the anchor district answer' gap.
    """

    return MetricLookupResult(
        selection=ResultSelection(
            scope="named_districts",
            districts=[
                SelectedDistrict(
                    district_id=1, district_name="Portland Public Schools", state="OR"
                ),
            ],
        ),
        rows=[
            MetricValueRow(
                district_id=1,
                district_name="Portland Public Schools",
                state="OR",
                metric_id=42,
                metric_name="Total contracted workdays per academic year",
                value=176.0,
                display_value="176 days",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="176 days",
                citation_markers=[1],
            ),
        ],
        total_considered=1,
        excluded_count=0,
        order_statement="Looked up contracted days for the anchor district.",
        coverage_frame=CoverageFrame(
            universe_count=1,
            in_scope_count=1,
            addressed_count=1,
            real_data_count=1,
            not_reviewed_count=0,
            out_of_universe_count=0,
            coverage_ratio=1.0,
        ),
    )


def _anchor_gap_manifest() -> ResponseManifest:
    return ResponseManifest(
        body=_ANCHOR_GAP_BODY,
        status="rendered",
        result_type="metric_lookup",
        validation_valid=True,
        metadata={"question": "What other districts have the same length school year?"},
    )


def test_grounded_result_facts_extracts_anchor_value_tokens() -> None:
    """The anchor row's display value becomes a sealed fact whose numeric token
    matches what the seal extracts from a draft, with the citation marker."""

    facts = grounded_result_facts(_anchor_gap_result())

    assert any(
        fact.label == "Portland Public Schools"
        and fact.value == "176"
        and fact.source_markers == ("[1]",)
        for fact in facts
    )


def test_grounded_result_facts_none_when_no_result_set() -> None:
    assert grounded_result_facts(None) == ()


def test_seal_rejects_grounded_anchor_value_without_facts() -> None:
    """RED on main: with no ResultSet threaded in, the brief's numeric allowlist
    is derived from the body alone — which never prints 176 — so a draft that
    states the grounded anchor value is rejected as a 'new numeric token' and the
    barer deterministic body ships instead (the B05 regression)."""

    manifest = _anchor_gap_manifest()
    # Built the way main builds it: no result_set, so facts stay empty.
    brief = build_answer_brief(manifest)
    assert "176" not in numeric_tokens(manifest.body)  # the gap is real

    draft = AnswerDraft(
        body=_ANCHOR_GAP_BODY
        + "\nPortland Public Schools itself has 176 days. [1]\n"
    )
    report = validate_answer_draft(brief, draft, mode="gated")

    assert not report.accepted
    assert any(f.code == "new_numeric_token" for f in report.findings)


def test_seal_accepts_grounded_anchor_value_with_result_facts() -> None:
    """GREEN on branch: threading the ResultSet seals 176 as a grounded fact, so
    the same richer draft survives the seal (accepted) instead of falling back to
    the barer body. The guard stays closed — an invented number is still
    rejected."""

    manifest = _anchor_gap_manifest()
    brief = build_answer_brief(manifest, result_set=_anchor_gap_result())

    accepted_draft = AnswerDraft(
        body=_ANCHOR_GAP_BODY
        + "\nPortland Public Schools itself has 176 days. [1]\n"
    )
    accepted = validate_answer_draft(brief, accepted_draft, mode="gated")
    assert accepted.accepted, accepted.findings

    invented_draft = AnswerDraft(
        body=_ANCHOR_GAP_BODY
        + "\nPortland Public Schools itself has 999 days. [1]\n"
    )
    invented = validate_answer_draft(brief, invented_draft, mode="gated")
    assert not invented.accepted
    assert any(f.code == "new_numeric_token" for f in invented.findings)
