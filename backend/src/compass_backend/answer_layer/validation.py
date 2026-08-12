"""Validate AI-improved answer drafts against sealed answer briefs."""

from __future__ import annotations

import re
from collections.abc import Iterable

from compass_backend.answer_layer.briefs import numeric_tokens, source_markers
from compass_backend.contracts.answer_layer import (
    AnswerBrief,
    AnswerDraft,
    AnswerLayerFinding,
    AnswerLayerMode,
    AnswerLayerReport,
)

# #1070: the stylist sometimes asserts "Compass doesn't have reviewed data for
# X" about an entity that is simply missing from THIS turn's result rows — even
# though the value was reviewed and shown a turn earlier (still in
# memory.prior_results). The deterministic body is the source of truth for what
# Compass lacks: the renderer states genuine absence in canonical coverage prose
# (briefs._CANONICAL_CAVEAT_LINE_MARKERS, and the "cannot answer cleanly" shape
# in the answer style guide). So a NEW absence claim the stylist introduced —
# one the deterministic body does not itself make — is fabricated anti-grounding
# and is rejected here. This mirrors _nctq_anchor_findings: it fires only on
# phrasing the stylist ADDED, never on absence prose the renderer already wrote.
_ABSENCE_CLAIM_RE = re.compile(
    r"(?:does(?:n['’]?t| not)|do(?:n['’]?t| not)|lacks?)\s+"
    r"(?:currently\s+)?(?:have\s+)?[^.]*?"
    r"(?:reviewed\s+data|reviewed\s+numeric|data\s+(?:for|on))"
    r"|no\s+(?:currently\s+)?reviewed\s+(?:data|numeric)",
)

_BANNED_INTERNAL_TERMS = (
    "resultset",
    "artifact_id",
    "artifact id",
    "coverage frame",
    "planner route",
    "validator",
    "trace id",
)
_NCTQ_CONTEXT_TERMS = (
    "nctq recommends",
    "nctq recommendation",
    "nctq's recommendation",
    "national model",
    "policy stance",
    "policy recommendation",
)
_MIN_BODY_LENGTH = 40


def validate_answer_draft(
    brief: AnswerBrief,
    draft: AnswerDraft,
    *,
    mode: AnswerLayerMode,
) -> AnswerLayerReport:
    """Return the accept/reject decision for one answer-layer draft."""

    findings = tuple(_findings(brief, draft))
    accepted = not findings
    return AnswerLayerReport(
        mode=mode,
        attempted=True,
        accepted=accepted,
        status="accepted" if accepted else "rejected",
        reason=None if accepted else findings[0].code,
        findings=findings,
        draft_body=draft.body,
    )


def _finding(code: str, message: str) -> AnswerLayerFinding:
    return AnswerLayerFinding(code=code, message=message)


def _findings(brief: AnswerBrief, draft: AnswerDraft) -> Iterable[AnswerLayerFinding]:
    yield from _body_length_findings(draft)
    yield from _immutable_block_findings(brief, draft)
    yield from _numeric_findings(brief, draft)
    yield from _source_marker_findings(brief, draft)
    yield from _caveat_findings(brief, draft)
    yield from _fabricated_absence_findings(brief, draft)
    yield from _banned_term_findings(draft)
    yield from _nctq_context_findings(brief, draft)
    yield from _nctq_anchor_findings(brief, draft)


def _body_length_findings(draft: AnswerDraft) -> Iterable[AnswerLayerFinding]:
    if len(draft.body.strip()) < _MIN_BODY_LENGTH:
        yield _finding("body_too_short", f"Minimum body length is {_MIN_BODY_LENGTH}.")


def _immutable_block_findings(
    brief: AnswerBrief,
    draft: AnswerDraft,
) -> Iterable[AnswerLayerFinding]:
    for block in brief.immutable_blocks:
        if block.text not in draft.body:
            label = block.block_id or block.kind
            yield _finding(
                "missing_immutable_block",
                f"Draft removed immutable block {label}.",
            )


def _numeric_findings(
    brief: AnswerBrief,
    draft: AnswerDraft,
) -> Iterable[AnswerLayerFinding]:
    allowed = set(brief.numeric_tokens) | {
        fact.value for fact in brief.facts if fact.value
    }
    for token in numeric_tokens(draft.body):
        if token not in allowed:
            yield _finding("new_numeric_token", f"Draft introduced {token}.")


def _source_marker_findings(
    brief: AnswerBrief,
    draft: AnswerDraft,
) -> Iterable[AnswerLayerFinding]:
    allowed = set(brief.source_markers)
    for fact in brief.facts:
        allowed.update(fact.source_markers)
    for marker in source_markers(draft.body):
        if marker not in allowed:
            yield _finding("new_source_marker", f"Draft introduced {marker}.")


def _caveat_findings(
    brief: AnswerBrief,
    draft: AnswerDraft,
) -> Iterable[AnswerLayerFinding]:
    lowered = draft.body.casefold()
    for fragment in brief.caveat_fragments:
        if fragment.casefold() not in lowered:
            yield _finding(
                "missing_caveat_fragment",
                f"Draft omitted required caveat fragment {fragment}.",
            )


def _fabricated_absence_findings(
    brief: AnswerBrief,
    draft: AnswerDraft,
) -> Iterable[AnswerLayerFinding]:
    """Reject "no reviewed data for X" prose the stylist invented (#1070).

    The deterministic body is the sole authority for what Compass lacks. When
    the renderer states genuine absence it writes the canonical coverage prose
    that the brief seals (``briefs.canonical_caveat_fragments``); the stylist
    must preserve it verbatim, which the caveat check already enforces. What it
    must NOT do is *introduce* an absence claim about an entity that is merely
    missing from this turn's result rows — the #1070 Dallas case, where the
    reviewed value was shown a prior turn and is still in ``memory.prior_results``.

    So we fire only on absence-claim phrasing present in the draft but absent
    from the deterministic body — the same added-prose discipline as
    ``_nctq_anchor_findings``. A claim the renderer already made is preserved
    prose, not a fabrication.
    """

    deterministic_claims = {
        match.group(0) for match in _ABSENCE_CLAIM_RE.finditer(brief.deterministic_body.casefold())
    }
    for match in _ABSENCE_CLAIM_RE.finditer(draft.body.casefold()):
        if match.group(0) not in deterministic_claims:
            yield _finding(
                "fabricated_absence_claim",
                "Draft asserts Compass lacks reviewed data that the deterministic "
                "answer does not itself state as absent.",
            )
            return


def _banned_term_findings(draft: AnswerDraft) -> Iterable[AnswerLayerFinding]:
    lowered = draft.body.casefold()
    for term in _BANNED_INTERNAL_TERMS:
        if term in lowered:
            yield _finding("banned_internal_term", f"Draft used {term}.")


def _nctq_context_findings(
    brief: AnswerBrief,
    draft: AnswerDraft,
) -> Iterable[AnswerLayerFinding]:
    if brief.allowed_nctq_context:
        return
    lowered = draft.body.casefold()
    for term in _NCTQ_CONTEXT_TERMS:
        if term in lowered:
            yield _finding(
                "unsupported_nctq_context",
                f"Draft added unsupported NCTQ context: {term}.",
            )


def _nctq_anchor_findings(
    brief: AnswerBrief,
    draft: AnswerDraft,
) -> Iterable[AnswerLayerFinding]:
    """When NCTQ context is admitted, require any NEW NCTQ claim to link a URL.

    Defense in depth: opening ``allowed_nctq_context`` turns off the strict
    ``_nctq_context_findings`` check above. Without this anchor, the model
    could synthesize "NCTQ recommends..." prose unsupported by any snippet.

    Only fires for NCTQ phrasing the stylist *added* — terms that already
    appear in the deterministic body are preserved prose, not new claims,
    and the renderer is the source of truth for them.
    """

    if not brief.allowed_nctq_context:
        return
    draft_lower = draft.body.casefold()
    deterministic_lower = brief.deterministic_body.casefold()
    new_terms = [
        term
        for term in _NCTQ_CONTEXT_TERMS
        if term in draft_lower and term not in deterministic_lower
    ]
    if not new_terms:
        return
    snippet_urls = [
        url
        for url in (
            _extract_snippet_url(snippet) for snippet in brief.allowed_nctq_context
        )
        if url
    ]
    if any(_body_contains_url(draft.body, url) for url in snippet_urls):
        return
    yield _finding(
        "nctq_url_anchor_missing",
        "Draft used NCTQ policy phrasing but did not link a provided snippet URL.",
    )


def _extract_snippet_url(snippet: str) -> str | None:
    """Parse the ``URL: <url>`` line from a formatted NctqSnippet string."""

    for line in snippet.splitlines():
        stripped = line.strip()
        if stripped.startswith("URL:"):
            return stripped[len("URL:"):].strip() or None
    return None


def _body_contains_url(body: str, url: str) -> bool:
    """Check whether ``url`` appears in ``body``, tolerating trailing slashes.

    The stylist often wraps URLs in markdown links; some authors trim trailing
    slashes, others add them. The anchor check should pass either way as
    long as the URL is recognisable.
    """

    canonical = url.rstrip("/").strip()
    if not canonical:
        return False
    if canonical in body:
        return True
    return f"{canonical}/" in body
