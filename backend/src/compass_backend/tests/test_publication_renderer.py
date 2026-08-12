"""Tests for the pure-Python publication renderer (#1690).

The ``publication`` route answers "what has NCTQ published about X?" by citing
NCTQ's published writing from ``compass.nctq_publications``. The renderer copies
title/url/summary VERBATIM from the fetched row and grounds every citation in
that row — the LLM authors none of the cited content.
"""

from __future__ import annotations

from compass_backend.answer_layer.nctq_context import NctqPublicationHit
from compass_backend.rendering.publication import (
    PUBLICATION_NO_MATCH_BODY,
    _build_citations,
    render_publication,
    validate_publication_manifest,
)


def _hit(
    *,
    publication_id: str = "42",
    title: str = "The Four-Day School Week: What the Research Says",
    url: str = "https://www.nctq.org/blog/four-day-school-week",
    summary: str = (
        "NCTQ reviewed the evidence on four-day school weeks and found mixed "
        "effects on student outcomes and teacher retention."
    ),
) -> NctqPublicationHit:
    return NctqPublicationHit(
        publication_id=publication_id,
        title=title,
        url=url,
        summary=summary,
    )


def test_render_publication_emits_publication_result_type() -> None:
    manifest = render_publication("four-day school week", [_hit()])

    assert manifest.result_type == "publication"
    assert manifest.status == "rendered"
    assert manifest.validation_valid is True


def test_render_publication_cites_verbatim_title_and_url() -> None:
    hit = _hit()
    manifest = render_publication("four-day school week", [hit])

    citations = manifest.metadata["citations"]
    assert isinstance(citations, list)
    assert len(citations) == 1
    citation = citations[0]
    # Title and URL must be copied verbatim from the fetched row — no
    # rewriting, no truncation.
    assert citation["title"] == hit.title
    assert citation["url"] == hit.url
    assert citation["summary"] == hit.summary
    assert citation["stable_id"] == hit.publication_id
    assert citation["id"] == 1
    assert citation["marker"] == "[1]"


def test_render_publication_names_publication_in_body() -> None:
    hit = _hit()
    manifest = render_publication("four-day school week", [hit])

    # The body names the publication and summarizes it from the row summary.
    assert hit.title in manifest.body
    assert hit.summary in manifest.body
    assert "[1]" in manifest.body


def test_render_publication_has_no_district_table() -> None:
    manifest = render_publication("four-day school week", [_hit()])

    # A publication answer is citation prose, never a district data table.
    assert "|" not in manifest.body


def test_render_publication_dedupes_repeated_url() -> None:
    hit_a = _hit(publication_id="1", title="First Look")
    hit_b = _hit(publication_id="2", title="Second Look")
    # Same URL on both rows: one Sources entry, one shared marker.
    manifest = render_publication("topic", [hit_a, hit_b])

    citations = manifest.metadata["citations"]
    assert len(citations) == 1
    assert citations[0]["marker"] == "[1]"
    # Both titles still surface in the body, sharing the [1] marker.
    assert "First Look" in manifest.body
    assert "Second Look" in manifest.body


def test_render_publication_no_match_is_honest() -> None:
    manifest = render_publication("a topic with no publication", [])

    assert manifest.status == "rendered"
    assert manifest.result_type == "publication"
    assert manifest.body == PUBLICATION_NO_MATCH_BODY
    assert manifest.metadata["citations"] == []


def test_validate_publication_manifest_passes_for_grounded_body() -> None:
    hit = _hit()
    manifest = render_publication("four-day school week", [hit])
    citations = manifest.metadata["citations"]

    validation = validate_publication_manifest(manifest.body, citations, [hit])
    assert validation.valid is True
    assert validation.findings == ()


def test_validate_publication_manifest_flags_citation_not_in_fetched_rows() -> None:
    hit = _hit()
    # A citation whose title/URL is absent from the fetched hits is a grounding
    # failure — the answer asserts a publication no fetched row backs.
    fabricated = [
        {
            "id": 1,
            "marker": "[1]",
            "title": "A Publication That Was Never Fetched",
            "url": "https://www.nctq.org/blog/not-fetched",
        }
    ]
    validation = validate_publication_manifest(
        "Some body that names [1].", fabricated, [hit]
    )
    assert validation.valid is False
    assert any(
        finding.startswith("cited_title_not_in_fetched_rows")
        for finding in validation.findings
    )
    assert any(
        finding.startswith("cited_url_not_in_fetched_rows")
        for finding in validation.findings
    )


def test_validate_publication_manifest_flags_body_marker_without_citation() -> None:
    hit = _hit()
    citations = _build_citations([hit])
    # The body references [2], but only [1] has a Sources entry.
    validation = validate_publication_manifest(
        "Here is [1] and a dangling [2].", citations, [hit]
    )
    assert validation.valid is False
    assert any(
        finding == "body_marker_without_citation:[2]"
        for finding in validation.findings
    )
