"""Document detail page -- /docs/{src_id}

Hero header + detail grid + inline full text. No modals, no repetition.
"""

from fasthtml.common import A, Div, H2, H3, P, Pre, Span

from starlette.requests import Request

from nctqai.components import (
    ConfidenceScoreBadge,
    DocumentTypeBadge,
    ExtractionStatusBadge,
    QualityFlagBadge,
    ReadabilityBadge,
)
from nctqai.layout import Layout
from nctqai.routes._auth import require_section
from nctqai.services.docs import get_document_detail
from nctqai.utils import format_ay_ids


def register_routes(rt):

    @rt("/docs/{src_id}")
    def get_document_detail_page(request: Request, src_id: int):
        """Document detail with hero header, detail grid, and inline text."""
        user, deny = require_section(request, "docs")
        if deny:
            return deny
        doc = get_document_detail(src_id)
        if not doc:
            return Layout(
                "Not Found",
                "",
                P("Document not found."),
                section="docs",
                sub_nav="/docs",
                user=user,
            )

        effective_type = doc.ai_document_type or doc.src_type

        # ── Back link ──────────────────────────────────────────
        back = Div(
            A("\u2190 Back to Documents", href="/docs", cls="uk-link-text text-sm"),
            cls="uk-margin-bottom",
        )

        # ── 1. Hero header ─────────────────────────────────────
        badges = Div(
            DocumentTypeBadge(effective_type) if effective_type else None,
            ExtractionStatusBadge(doc.extraction_status),
            ReadabilityBadge(doc.ai_readability) if doc.ai_readability else None,
            cls="flex flex-wrap gap-2 uk-margin-small-bottom",
        )

        meta_line = Div(
            Span(
                f"{doc.district_name} (ID: {doc.district_id})",
                cls="uk-text-muted text-sm",
            ),
            Span(" · ", cls="uk-text-muted text-sm") if doc.effective_ay_ids else None,
            Span(
                format_ay_ids(doc.effective_ay_ids),
                cls="text-sm font-semibold",
            )
            if doc.effective_ay_ids
            else None,
            cls="flex items-center gap-1",
        )

        actions = Div(cls="doc-hero-actions")
        if doc.src_link:
            actions = Div(
                A(
                    "\u2b07 Download Original",
                    href=doc.src_link,
                    target="_blank",
                    cls="doc-action-primary",
                ),
                A(
                    "\u2193 View Full Text",
                    href="#full-text",
                    cls="doc-action-secondary",
                )
                if doc.full_text
                else None,
                cls="doc-hero-actions",
            )
        elif doc.full_text:
            actions = Div(
                A(
                    "\u2193 View Full Text",
                    href="#full-text",
                    cls="doc-action-secondary",
                ),
                cls="doc-hero-actions",
            )

        hero = Div(
            H2(doc.display_title, cls="uk-margin-remove-bottom"),
            badges,
            meta_line,
            actions,
            cls="review-card uk-padding-small",
        )

        # ── 2. Detail grid ─────────────────────────────────────
        detail_grid = Div(
            _detail_item("Document Type", DocumentTypeBadge(effective_type) if effective_type else "\u2014"),
            _detail_item("Temporal Class", DocumentTypeBadge(doc.ai_temporal_class) if doc.ai_temporal_class else "\u2014"),
            _detail_item("Academic Years", format_ay_ids(doc.effective_ay_ids) if doc.effective_ay_ids else "\u2014"),
            _detail_item("Valid From", doc.valid_from or "\u2014"),
            _detail_item("Valid To", doc.valid_to or "\u2014"),
            _detail_item("AI Confidence", ConfidenceScoreBadge(doc.ai_confidence) if doc.ai_confidence is not None else "\u2014"),
            _detail_item("Text Length", f"{doc.text_length:,} chars" if doc.text_length else "\u2014"),
            _detail_item("Tables Found", str(doc.table_count)),
            cls="detail-grid",
        )

        detail_card = Div(
            Div("Details", cls="review-card-label"),
            detail_grid,
            cls="review-card",
        )

        # ── 3. AI Summary (conditional) ────────────────────────
        summary_card = None
        if doc.ai_summary:
            summary_card = Div(
                Div("AI Summary", cls="review-card-label"),
                P(doc.ai_summary, cls="text-sm reasoning-text"),
                cls="review-card",
            )

        # ── 4. Quality alerts (conditional) ────────────────────
        quality_section = None
        flags = doc.quality_flags
        has_issues = bool(flags) or bool(doc.extraction_error)
        if has_issues:
            alert_items = []
            if flags:
                alert_items.extend(QualityFlagBadge(f) for f in flags)
            if doc.extraction_error:
                alert_items.append(
                    Span(doc.extraction_error, cls="badge badge-doc-danger")
                )
            quality_section = Div(
                Div("Quality Alerts", cls="review-card-label"),
                Div(*alert_items, cls="flex flex-wrap gap-2"),
                cls="review-card quality-flag-card",
            )

        # ── 5. Full text (inline, no modal) ────────────────────
        text_section = None
        if doc.full_text:
            stats_line = []
            if doc.text_length:
                stats_line.append(f"{doc.text_length:,} chars")
            if doc.table_count:
                stats_line.append(f"{doc.table_count} tables")

            text_section = Div(
                Div(
                    H3("Extracted Text", cls="uk-margin-remove-bottom text-lg"),
                    Span(
                        " · ".join(stats_line),
                        cls="uk-text-muted text-sm",
                    )
                    if stats_line
                    else None,
                    cls="flex items-center gap-2 uk-margin-small-bottom",
                ),
                Pre(
                    doc.full_text,
                    cls="doc-text-pre",
                ),
                cls="review-card",
                id="full-text",
            )

        content = Div(
            back,
            hero,
            detail_card,
            summary_card,
            quality_section,
            text_section,
        )

        return Layout(
            doc.display_title[:60],
            f"src_id={doc.src_id}",
            content,
            section="docs",
            sub_nav="/docs",
            breadcrumb=[("Documents", "/docs"), (doc.display_title[:60], None)],
            user=user,
        )


def _detail_item(label, value):
    """Single item in the detail grid."""
    return Div(
        Span(label, cls="label"),
        Span(value, cls="value") if isinstance(value, str) else Div(value, cls="value"),
        cls="detail-item",
    )
