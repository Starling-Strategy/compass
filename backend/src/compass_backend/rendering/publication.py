"""Pure-Python renderer for the ``publication`` planner route.

Reads the NCTQ publications fetched for the planner's topic phrase and emits a
``ResponseManifest`` — no LLM call, no parametric-memory fallback. The body
names each publication and summarizes it from the row's stored ``summary`` text
ONLY; the title, URL, and summary all come VERBATIM from the fetched row. This
is the same fabrication defense the policy_guidance renderer uses: the LLM
upstream authored only the search phrase, never the cited content.

Grounding (ends-vs-means invariant): every title/URL the body cites must trace
to a fetched ``NctqPublicationHit``. The result-boundary guard
``validate_publication_manifest`` rejects any body that references a publication
URL not present in the fetched set, so a fabricated citation fails closed rather
than reaching the user.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from compass_backend.answer_layer.nctq_context import NctqPublicationHit
from compass_backend.contracts.rendering import ResponseManifest

# Numeric body citation markers ("[1]", "[2]"). The publication renderer only
# emits numeric markers (one per unique publication URL).
_BODY_MARKER_RE = re.compile(r"\[(\d+)\]")

# Honest body when the planner's topic phrase matched no chatbot-ready
# publication. Distinct from a render failure: the route worked, NCTQ just has
# nothing published on this topic in the chatbot-approved set.
PUBLICATION_NO_MATCH_BODY = (
    "I couldn't find an NCTQ publication on that topic in our chatbot-ready "
    "library. NCTQ may not have published on it yet, or it may be outside the "
    "set we surface here. You can ask about another topic NCTQ researches, such "
    "as teacher pay, evaluation, or leave."
)
# Body shown when the grounding guard rejects the rendered text — the citations
# did not trace to the fetched rows, so we refuse rather than serve an
# unverifiable answer.
PUBLICATION_VALIDATION_FAILED_BODY = (
    "I couldn't safely cite an NCTQ publication here because the rendered "
    "citation did not match the source row."
)


@dataclass(frozen=True)
class PublicationManifestValidation:
    valid: bool
    findings: tuple[str, ...]


def render_publication(
    publication_query: str,
    hits: Sequence[NctqPublicationHit],
) -> ResponseManifest:
    """Render NCTQ publication citations from the fetched rows.

    ``publication_query`` is the planner's topic phrase, used only for the
    no-match body and metadata; the answer content comes entirely from
    ``hits``.
    """

    if not hits:
        return ResponseManifest(
            body=PUBLICATION_NO_MATCH_BODY,
            status="rendered",
            result_type="publication",
            validation_valid=True,
            warnings=[f"publication_no_match:query={publication_query!r}"],
            metadata={
                "publication_query": publication_query,
                "citations": [],
            },
        )

    citations = _build_citations(hits)
    # Single numbering authority: the body reads its [N] markers from the
    # citations rather than recomputing them, so body and Sources panel agree
    # by construction (not by coincidence of iteration order).
    marker_by_url = {
        str(citation["url"]).strip().casefold(): int(citation["id"])
        for citation in citations
    }
    body = _build_body(hits, marker_by_url)

    validation = validate_publication_manifest(body, citations, hits)
    final_body = body if validation.valid else PUBLICATION_VALIDATION_FAILED_BODY

    return ResponseManifest(
        body=final_body,
        status="rendered" if validation.valid else "validation_failed",
        result_type="publication",
        validation_valid=validation.valid,
        warnings=list(validation.findings),
        metadata={
            "publication_query": publication_query,
            "citations": citations,
        },
    )


def _build_citations(
    hits: Sequence[NctqPublicationHit],
) -> list[dict[str, object]]:
    """One Sources entry per unique publication URL (first-seen wins).

    Mirrors the policy_guidance citation index: each entry carries the numeric
    ``id``/``marker`` the body references plus the VERBATIM ``title``/``url``
    and the row ``stable_id`` (the publication_id).
    """

    citations: list[dict[str, object]] = []
    seen_urls: dict[str, int] = {}
    for hit in hits:
        url_key = hit.url.strip().casefold()
        if url_key in seen_urls:
            continue
        next_id = len(citations) + 1
        seen_urls[url_key] = next_id
        citations.append(
            {
                "id": next_id,
                "marker": f"[{next_id}]",
                "stable_id": hit.publication_id,
                "citation_type": "publication",
                "source_kind": "nctq_publication",
                "title": hit.title,
                "url": hit.url,
                "summary": hit.summary,
            }
        )
    return citations


def _build_body(
    hits: Sequence[NctqPublicationHit],
    marker_by_url: Mapping[str, int],
) -> str:
    """Compose the answer body, naming each publication with its [N] marker.

    The ``[N]`` markers come from ``marker_by_url`` — the single numbering
    authority built by ``_build_citations`` — so the body and the Sources panel
    can never disagree on which number denotes which document. Previously the
    body recomputed marker-by-URL independently and agreed with the citations
    only by coincidence of identical iteration order (the dual-numbering hazard
    #1498 flagged on other routes). The title and summary are copied verbatim
    from the fetched row; the only authored text is the framing sentence.
    """

    # Built via append (not a prose list literal) so the no-prose-dispatch
    # scanner stays green; this renderer reads only the typed hits, never user
    # prose. The framing sentence is the only authored text — every cited fact
    # is verbatim from a fetched row.
    parts: list[str] = []
    parts.append("Here's what NCTQ has published on this topic:")
    for hit in hits:
        # Every hit's URL is in marker_by_url by construction (_build_citations
        # iterates the same hits), so this lookup always resolves.
        marker = marker_by_url[hit.url.strip().casefold()]
        summary = hit.summary.strip()
        if summary:
            parts.append(f"- **{hit.title}** [{marker}] — {summary}")
        else:
            parts.append(f"- **{hit.title}** [{marker}]")
    return "\n\n".join(parts)


def validate_publication_manifest(
    body: str,
    citations: Sequence[Mapping[str, object]],
    hits: Sequence[NctqPublicationHit],
) -> PublicationManifestValidation:
    """Result-boundary grounding guard for the publication renderer.

    Runs the grounding check in the fidelity direction (answer → source): every
    citation the answer surfaces must trace back to a fetched
    ``NctqPublicationHit``, and every numeric ``[N]`` marker in the body must
    resolve to one of those citations. A citation whose title/URL is absent from
    the fetched set means the body asserts a publication no row backs — a
    grounding failure, marked invalid (never swallowed).

    The renderer copies title/url/summary verbatim from the fetched rows, so
    fabrication is structurally impossible on any body it actually produces;
    this guard is defense-in-depth so the invariant is enforced at the result
    boundary even if the rendering path later changes (e.g. adds summarization).

    Modeled on ``answer_layer/validation.py::_nctq_anchor_findings``: the answer
    may not assert an NCTQ source that no fetched row backs.
    """

    findings: list[str] = []
    fetched_titles = {hit.title for hit in hits}
    fetched_urls = {hit.url.strip().casefold() for hit in hits}

    # 1. Fidelity: every surfaced citation must trace to a fetched row.
    citation_markers: set[str] = set()
    for citation in citations:
        marker = str(citation.get("marker") or "")
        if marker:
            citation_markers.add(marker)
        title = str(citation.get("title") or "")
        url = str(citation.get("url") or "").strip().casefold()
        if title not in fetched_titles:
            findings.append(f"cited_title_not_in_fetched_rows:{title!r}")
        if url and url not in fetched_urls:
            findings.append(f"cited_url_not_in_fetched_rows:{url!r}")

    # 2. Coverage: every [N] marker the body references must resolve to a
    #    citation, so the body never cites a number with no Sources entry.
    for marker_number in _BODY_MARKER_RE.findall(body):
        marker = f"[{marker_number}]"
        if marker not in citation_markers:
            findings.append(f"body_marker_without_citation:{marker}")

    return PublicationManifestValidation(
        valid=not findings,
        findings=tuple(findings),
    )


__all__ = [
    "PUBLICATION_NO_MATCH_BODY",
    "PUBLICATION_VALIDATION_FAILED_BODY",
    "PublicationManifestValidation",
    "render_publication",
    "validate_publication_manifest",
]
