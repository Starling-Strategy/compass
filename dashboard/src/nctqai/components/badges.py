"""Badge components for the NCTQ.ai dashboard.

Colored pill badges for status, confidence, INA, priority, question IDs,
and document-specific badges (readability, extraction status, type, alignment).
"""

from fasthtml.common import Span


def HoldBadge():
    """Blue 'On Hold' pill for answers frozen from analyst review."""
    return Span("On Hold", cls="badge badge-on-hold", role="status")


def StatusBadge(status: str | None):
    """Map review status to a colored badge.

    Accepted statuses: "approved", "accepted" -> green.
    Rejected statuses: "incorrect", "rejected" -> red.
    On hold: "on_hold" -> blue.
    Default: "unreviewed" -> slate.
    """
    if not status:
        return Span("Unreviewed", cls="badge badge-unreviewed", role="status")

    normalized = status.strip().lower()

    if normalized in ("approved", "accepted"):
        return Span("Accepted", cls="badge badge-accepted", role="status")
    if normalized in ("incorrect", "rejected"):
        return Span("Rejected", cls="badge badge-rejected", role="status")
    if normalized == "on_hold":
        return HoldBadge()
    return Span("Unreviewed", cls="badge badge-unreviewed", role="status")


def INABadge():
    """Amber 'INA' pill for Issue Not Addressed indicators."""
    return Span("INA", cls="badge badge-ina")


def q_label(q_id: int, q_num: str | None = None) -> str:
    """Display label for a question: q_num if available, else 'Q{q_id}'."""
    return q_num if q_num else f"Q{q_id}"


def QIdBadge(q_id: int, q_num: str | None = None):
    """Pill showing question identifier (e.g., 'HB1' or fallback 'Q3')."""
    return Span(q_label(q_id, q_num), cls="badge badge-qid")


# ---------------------------------------------------------------------------
# Document-specific badges (ported from document_pipeline.dashboard)
# ---------------------------------------------------------------------------

# Badge CSS class constants for document badges (light mode)
_DOC_SUCCESS = "badge badge-doc-success"
_DOC_WARNING = "badge badge-doc-warning"
_DOC_DANGER = "badge badge-doc-danger"
_DOC_NEUTRAL = "badge badge-doc-neutral"


def ReadabilityBadge(readability: str | None):
    """Color-coded readability badge: good (green), fair (amber), poor (red)."""
    if not readability:
        return Span("\u2014", cls="uk-text-muted")
    config = {
        "good": (_DOC_SUCCESS, "Good"),
        "fair": (_DOC_WARNING, "Fair"),
        "poor": (_DOC_DANGER, "Poor"),
    }
    cls, label = config.get(readability.lower(), (_DOC_NEUTRAL, readability))
    return Span(label, cls=cls)


def ExtractionStatusBadge(status: str | None):
    """Status badge: success (green), failed (red), pending (gray)."""
    config = {
        "success": (_DOC_SUCCESS, "Success"),
        "failed": (_DOC_DANGER, "Failed"),
        "pending": (_DOC_NEUTRAL, "Pending"),
    }
    cls, label = config.get(
        (status or "").lower(), (_DOC_NEUTRAL, status or "Unknown")
    )
    return Span(label, cls=cls)


def DocumentTypeBadge(doc_type: str | None):
    """Neutral badge showing document classification."""
    if not doc_type:
        return Span("\u2014", cls="uk-text-muted")
    label = doc_type.replace("_", " ").title()
    return Span(label, cls=_DOC_NEUTRAL)


def AYAlignmentBadge(human_ay_ids, ai_ay_ids):
    """Badge showing human vs AI academic year agreement.

    Exact Match (green), Partial overlap (amber), Disagree (red).
    """
    if not ai_ay_ids or not human_ay_ids:
        return Span("\u2014", cls="uk-text-muted")
    human_set = set(human_ay_ids) if human_ay_ids else set()
    ai_set = set(ai_ay_ids) if ai_ay_ids else set()
    if human_set == ai_set:
        return Span("Exact Match", cls=_DOC_SUCCESS)
    if human_set & ai_set:
        return Span("Partial", cls=_DOC_WARNING)
    return Span("Disagree", cls=_DOC_DANGER)


def TypeAlignmentBadge(human_type: str | None, ai_type: str | None):
    """Badge showing human vs AI document type agreement.

    Agree (green) or shows both types side by side (amber).
    """
    if not ai_type:
        return Span("\u2014", cls="uk-text-muted")
    if human_type == ai_type:
        return Span("Agree", cls=_DOC_SUCCESS)
    human_label = (human_type or "other").replace("_", " ").title()
    ai_label = ai_type.replace("_", " ").title()
    return Span(f"{human_label} / {ai_label}", cls=_DOC_WARNING)


def ConfidenceScoreBadge(score: float | None):
    """Color-coded confidence score: green >= 0.8, amber >= 0.5, red < 0.5."""
    if score is None:
        return Span("\u2014", cls="uk-text-muted")
    label = f"{score:.2f}"
    if score >= 0.8:
        cls = _DOC_SUCCESS
    elif score >= 0.5:
        cls = _DOC_WARNING
    else:
        cls = _DOC_DANGER
    return Span(label, cls=cls)


def QualityFlagBadge(flag: str):
    """Small warning badge for a quality flag."""
    return Span(flag.replace("_", " "), cls=_DOC_DANGER)
