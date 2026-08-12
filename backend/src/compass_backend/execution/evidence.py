"""Evidence attachment helpers for deterministic execution artifacts."""

from __future__ import annotations

from compass_backend.artifacts import CitationRef, citation_ref_from_source

from .types import MetricAnswerRow


def citation_markers_for_row(
    row: MetricAnswerRow,
    citation_refs: list[CitationRef],
    citation_marker_by_identity: dict[tuple[object, ...], int],
) -> list[int]:
    """Attach stable citation markers for one retained policy answer row."""

    citation_markers: list[int] = []
    for source in row.citations:
        source_with_row_context = source.model_copy(
            update={
                "academic_year": source.academic_year or row.academic_year,
                "district_id": source.district_id or row.district_id,
            }
        )
        candidate_citation = citation_ref_from_source(
            source_with_row_context,
            marker=len(citation_refs) + 1,
        )
        identity = _citation_identity(candidate_citation)
        marker = citation_marker_by_identity.get(identity)
        if marker is None:
            marker = candidate_citation.marker
            citation_marker_by_identity[identity] = marker
            citation_refs.append(candidate_citation)
        if marker not in citation_markers:
            citation_markers.append(marker)
    return citation_markers


def _citation_identity(citation: CitationRef) -> tuple[object, ...]:
    """Dedup key for citations within a single answer.

    Per W2-M5-04 (#748): two rows that cite the same document must share
    one entry in the answer-level Sources block and one inline marker —
    even when the rows carry different ``academic_year`` / ``district_id``
    / page-anchor / parsed-title values. URL is the single source of truth
    when present; when absent (rare — archived/internal records), fall
    back to parsed-title-based identity so document-level dedup still
    applies.

    Trade-off (deliberate, per #748): page-deep-link precision is
    sacrificed in favour of one Sources entry per URL. The first-seen
    page anchor is preserved on the merged entry because subsequent
    occurrences re-use the first marker (see ``citation_markers_for_row``)
    and never re-append a new CitationRef — so if row A cites
    ``contract.pdf#page=5`` and row B cites ``contract.pdf#page=12``, the
    Sources entry deep-links to page 5 only. Reporter (#748) explicitly
    accepted this in exchange for a clean, scannable Sources block.
    """
    url = (citation.url or "").strip()
    if url:
        return ("url", url.casefold())
    return ("title", citation.title.strip().casefold())
